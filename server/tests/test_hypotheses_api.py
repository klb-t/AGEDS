"""HTTP API, the /hypotheses page (disclaimers in every section, escaping) and the CLI as subprocess."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app.analysis import eventstudy as es
from server.app.analysis.hypotheses import DISCLAIMER
from server.tests.corpus import hypotheses as corpus

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from server.app import db as dbmod, evidence, main
    from server.app.config import Settings
    s = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', s)
    monkeypatch.setattr(evidence, 'settings', s)
    dbmod.init_db()
    with TestClient(main.app) as c:
        yield c


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def test_api_flow_and_error_mapping(client):
    scenario = corpus.SCENARIOS['planted_effect']()
    ids = corpus.populate(scenario)
    definition = corpus.definition_for(scenario)
    preview = ok(client.post('/api/hypotheses/preview', json={'definition': definition}))
    registered = ok(client.post('/api/hypotheses', json={'definition': definition, 'actor': 'analyst'}))
    assert registered['definition_sha256'] == preview['definition_sha256'] and registered['created']
    assert registered['disclaimer'] == DISCLAIMER
    ok(client.post('/api/hypotheses', json={'definition': {**definition, 'windows': []}}), 422)
    ok(client.post('/api/hypotheses', json={'definition': definition, 'unexpected': 1}), 422)
    ok(client.get('/api/hypotheses/999'), 404)
    ok(client.get('/api/hypotheses/versions/0'), 422)
    hid, vid = registered['hypothesis_id'], registered['version_id']
    unchanged = ok(client.post(f'/api/hypotheses/{hid}/versions', json={'definition': definition}))
    assert unchanged['created'] is False
    run = ok(client.post(f'/api/hypotheses/versions/{vid}/event-study', json={}))
    again = ok(client.post(f'/api/hypotheses/versions/{vid}/event-study', json={'actor': 'analyst'}))
    assert again['result_sha256'] == run['result_sha256'] and again['identical_result_runs'] == [run['run_id']]
    ok(client.post(f'/api/hypotheses/versions/{vid}/event-study', json={'overrides': {'windows': []}}), 422)
    ok(client.post('/api/hypotheses/versions/999/event-study', json={}), 404)
    assert ok(client.get(f'/api/hypotheses/runs/{run["run_id"]}'))['result']['disclaimer'] == DISCLAIMER
    candidates = ok(client.post(f'/api/hypotheses/{hid}/candidates', json={'limit': 10}))
    assert candidates['added_item_ids']
    ok(client.post(f'/api/hypotheses/{hid}/candidates', json={'provider': 'remote'}), 422)
    ok(client.post(f'/api/hypotheses/{hid}/candidates', json={'direction': 'disconfirming'}), 422)  # none preregistered
    ok(client.post(f'/api/hypotheses/{hid}/candidates', json={'direction': 'sideways'}), 422)
    ok(client.post(f'/api/hypotheses/{hid}/candidates', json={'opt_in': 'yes'}), 422)
    item = ok(client.post(f'/api/hypotheses/{hid}/dossier', json={'target_kind': 'event', 'target_id': ids['stimulus'][0],
                                                                  'classification': 'context'}))
    ok(client.post(f'/api/hypotheses/{hid}/dossier', json={'target_kind': 'event', 'target_id': 10**9}), 404)
    classified = ok(client.post(f'/api/hypotheses/dossier/{candidates["added_item_ids"][0]}/classify',
                                json={'classification': 'supporting', 'rationale': 'checked'}))
    assert classified['classification'] == 'supporting'
    ok(client.post(f'/api/hypotheses/dossier/{item["item_id"]}/classify', json={'classification': 'true'}), 422)
    thesis = ok(client.post(f'/api/hypotheses/{hid}/theses', json={'text': 'no pointer', 'status_type': 'normative'}))
    assert thesis['unsupported'] and thesis['flags'] == ['unsupported']
    ok(client.post(f'/api/hypotheses/{hid}/theses', json={'text': 'x', 'status_type': 'certain'}), 422)
    disclosure = ok(client.post(f'/api/hypotheses/{hid}/disclosure', json={'quadrant': 'unknown_unknowns', 'text': 'gaps'}))
    assert disclosure['quadrants'][3]['human_text'] == 'gaps'
    detail = ok(client.get(f'/api/hypotheses/{hid}'))
    assert {k: detail[k]['disclaimer'] for k in ('dossier', 'theses', 'disclosure')} == {k: DISCLAIMER for k in ('dossier', 'theses', 'disclosure')}
    assert [r['kind'] for r in detail['runs']] == ['event_study', 'event_study', 'candidate_search']
    listing = ok(client.get('/api/hypotheses'))
    assert listing['hypotheses'][0]['first_analysed_at'] == run['created_at']


def test_page_sections_carry_the_disclaimer_and_escape_source_text(client):
    empty = client.get('/hypotheses')
    assert empty.status_code == 200 and 'href="/hypotheses"' in empty.text and 'Hipotezy' in empty.text
    from server.app import evidence
    source = evidence.ensure_source('synthetic', 'escaping fixture')
    hostile = '<script>window.__x=1</script></script><img src=x onerror=1>'
    scenario = corpus.SCENARIOS['planted_effect']()
    corpus.populate(scenario)
    evidence.add_event(source_id=source, artifact_id=None, event_type='health', body='wizyta ' + hostile,
                       ts_start='2025-02-01T10:00:00+00:00')
    definition = corpus.definition_for(scenario) | {'title': 'T ' + hostile}
    registered = ok(client.post('/api/hypotheses', json={'definition': definition}))
    hid = registered['hypothesis_id']
    run = ok(client.post(f'/api/hypotheses/versions/{registered["version_id"]}/event-study', json={}))
    ok(client.post(f'/api/hypotheses/{hid}/candidates', json={'queries': ['wizyta'], 'limit': 200}))
    page = client.get(f'/hypotheses?id={hid}')
    assert page.status_code == 200
    html = page.text
    sections = re.findall(r'<section class="panel" data-section="([a-z-]+)">(.*?)</section>', html, re.S)
    assert [name for name, _ in sections] == ['register', 'versions', 'event-study', 'dossier', 'theses', 'disclosure']
    for name, body in sections:
        assert f'data-disclaimer-sha256="{DISCLAIMER["sha256_pl"]}"' in body, name
        assert DISCLAIMER['pl'] in body and DISCLAIMER['en'].replace("'", '&#39;') in body, name
    for wid in es.ALWAYS_PRESENT_WARNINGS:
        assert f'data-warning-id="{wid}"' in html
    assert 'data-registered-at' in html and 'data-first-analysed-at' in html
    assert f'data-result-sha256="{run["result_sha256"]}"' in html
    assert '<script>window.__x' not in html and '<img src=x' not in html
    assert '&lt;script&gt;window.__x=1&lt;/script&gt;' in html
    data = re.search(r'<script type="application/json" id="hyp-run-data">(.*?)</script>', html, re.S).group(1)
    assert json.loads(data)['input']['input_sha256'] == run['result']['input']['input_sha256']
    assert client.get(f'/hypotheses?id={hid}&run=999').status_code == 404
    other = ok(client.post('/api/hypotheses', json={'definition': corpus.definition_for(scenario) | {'title': 'other'}}))
    assert client.get(f'/hypotheses?id={other["hypothesis_id"]}&run={run["run_id"]}').status_code == 404
    assert client.get('/static/hypotheses.mjs').status_code == 200
    assert client.get('/static/hypotheses-chart.mjs').status_code == 200


def cli(env, *args, expect=0):
    result = subprocess.run([sys.executable, '-m', 'server.app.cli', *map(str, args)], cwd=REPO, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == expect, (args, result.stdout, result.stderr)
    return json.loads(result.stdout) if expect == 0 else json.loads(result.stderr.strip().splitlines()[-1])


def test_cli_commands_as_subprocesses(tmp_path, monkeypatch):
    from server.app import db as dbmod, evidence
    from server.app.config import Settings
    s = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', s)
    monkeypatch.setattr(evidence, 'settings', s)
    dbmod.init_db()
    scenario = corpus.SCENARIOS['unknown_zone']()
    ids = corpus.populate(scenario)
    env = {**os.environ, 'EW_DATA_DIR': str(tmp_path), 'EW_DB_PATH': str(tmp_path / 'e.db'),
           'EW_STORE_DIR': str(tmp_path / 'store'), 'PYTHONPATH': str(REPO)}
    definition = tmp_path / 'definition.json'
    definition.write_text(json.dumps(corpus.definition_for(scenario)), encoding='utf-8')
    preview = cli(env, 'hypothesis-preview', definition)
    registered = cli(env, 'hypothesis-register', definition, '--actor', 'cli-analyst')
    assert registered['definition_sha256'] == preview['definition_sha256'] and registered['created']
    changed = tmp_path / 'definition-v2.json'
    changed.write_text(json.dumps(corpus.definition_for(scenario, permutations=49)), encoding='utf-8')
    v2 = cli(env, 'hypothesis-version', registered['hypothesis_id'], changed, '--change-note', 'fewer permutations')
    assert v2['version_number'] == 2 and v2['previous_version_id'] == registered['version_id']
    listing = cli(env, 'hypotheses-list')
    assert listing['hypotheses'][0]['latest_version_number'] == 2 and listing['disclaimer'] == DISCLAIMER
    summary = cli(env, 'eventstudy-run', v2['version_id'], '--summary')
    assert summary['status'] == 'ok' and summary['input']['untimed_excluded'] == 7 and summary['disclaimer'] == DISCLAIMER
    rerun = cli(env, 'eventstudy-run', v2['version_id'], '--permutations', '19', '--seed', '3')
    assert rerun['parameters']['permutations'] == 19 and rerun['result']['deviations_from_registration']
    shown = cli(env, 'eventstudy-show', summary['run_id'])
    assert shown['result_sha256'] == summary['result_sha256'] and shown['integrity'] == 'hash_matches'
    out = tmp_path / 'out' / 'run.json'
    written = cli(env, 'eventstudy-show', summary['run_id'], '--output', out)
    assert json.loads(out.read_text())['result_sha256'] == written['result_sha256']
    cli(env, 'eventstudy-show', summary['run_id'], '--output', out, expect=2)  # never overwrites
    item = cli(env, 'dossier-add', registered['hypothesis_id'], 'event', ids['stimulus'][0], '--classification', 'context')
    decided = cli(env, 'dossier-classify', item['item_id'], 'supporting', '--rationale', 'letter matches')
    assert [d['classification'] for d in decided['decisions']] == ['context', 'supporting']
    proposals = cli(env, 'dossier-candidates', registered['hypothesis_id'], '--query', 'wizyta', '--limit', '5')
    assert proposals['provider']['name'] == 'none'
    cli(env, 'dossier-candidates', registered['hypothesis_id'], '--provider', 'fake', expect=2)
    thesis = cli(env, 'thesis-add', registered['hypothesis_id'], '--type', 'inference_chain', '--text', 'timing only',
                 '--evidence', str(item['item_id']))
    assert thesis['flags'] == []
    cli(env, 'disclosure-add', registered['hypothesis_id'], 'known_unknowns', 'phone clock zone')
    shown = cli(env, 'hypothesis-show', registered['hypothesis_id'])
    assert len(shown['versions']) == 2 and len(shown['theses']['theses']) == 1
    missing = cli(env, 'hypothesis-show', 999, expect=2)
    assert missing['error'] == 'NotFound'
    duplicate = tmp_path / 'duplicate.json'
    duplicate.write_text('{"title": "a", "title": "b"}', encoding='utf-8')
    assert 'repeats JSON key' in cli(env, 'hypothesis-register', duplicate, expect=2)['message']
    nan = tmp_path / 'nan.json'
    nan.write_text(json.dumps(corpus.definition_for(scenario)).replace('"seed": 12345', '"seed": NaN'), encoding='utf-8')
    assert 'nonfinite' in cli(env, 'hypothesis-register', nan, expect=2)['message']

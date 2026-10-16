"""HTTP and CLI contracts of the time budget (synthetic temporary databases only)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import db, evidence, main
from server.tests.corpus import activity

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = replace(db.settings, data_dir=tmp_path, db_path=tmp_path / 'db.sqlite', store_dir=tmp_path / 'store')
    patches = [patch.object(module, 'settings', settings) for module in (db, evidence, main)]
    for p in patches:
        p.start()
    monkeypatch.delenv('AGEDS_ACTIVITY_CATEGORIES', raising=False)
    try:
        with TestClient(main.app) as test_client:
            test_client.manifest = activity.build(tmp_path / 'activity')
            test_client.ids = activity.populate(test_client.manifest)
            yield test_client
    finally:
        for p in reversed(patches):
            p.stop()


def _body(client, **over):
    m = client.manifest
    return {'from': m['window']['from'], 'to': m['window']['to'], 'zone': activity.ZONE,
            'config': activity.CATEGORIES, **over}


def test_page_nav_and_static(client):
    html = client.get('/analysis/budget').text
    assert 'data-budget' in html and '/static/analysis_budget.mjs' in html
    assert '<a href="/analysis/budget">Budżet czasu</a>' in html
    assert 'nie bezczynność' in html
    assert client.get('/static/analysis_budget.mjs').status_code == 200
    assert client.get('/static/analysis_budget.css').status_code == 200


def test_run_show_candle_events_and_correction(client, monkeypatch):
    assert client.get('/api/analysis/config').json() == {'configured': False, 'env_var': 'AGEDS_ACTIVITY_CATEGORIES'}
    missing = client.post('/api/analysis/budget/runs', json=_body(client, config=None))
    assert missing.status_code == 422 and 'paths are not accepted' in missing.json()['detail']
    created = client.post('/api/analysis/budget/runs', json=_body(client))
    assert created.status_code == 200, created.text
    summary = created.json()
    run_id = summary['run']['run_id']
    assert summary['artifact_class'] == 'interpretive' and summary['config']['version'] == 'corpus-activity-1'
    assert summary['exclusions']['untimed']['count'] == 2
    again = client.post('/api/analysis/budget/runs', json=_body(client)).json()
    assert again['run'] == {**summary['run'], 'reused': True}
    listed = client.get('/api/analysis/runs', params={'kind': 'time_budget'}).json()['runs']
    assert [r['run_id'] for r in listed] == [run_id]
    run = client.get(f'/api/analysis/budget/runs/{run_id}').json()
    assert run['result']['config']['origin'] == 'inline_http'
    day = client.get(f'/api/analysis/budget/runs/{run_id}/candle',
                     params={'granularity': 'day', 'bucket': '2025-03-28', 'category': 'sprawy_urzedowe'}).json()
    ids = {e['event_id'] for e in day['events']}
    assert client.ids['call-official'] in ids and client.ids['mail-official'] in ids
    call = next(e for e in day['events'] if e['event_id'] == client.ids['call-official'])
    assert call['label_in_run']['path'] == 'rule' and call['label_in_run']['rules'] == 'urzedy-numery@1'
    assert call['links']['event'] == f"/api/analysis/events/{client.ids['call-official']}"
    gap = client.get(f'/api/analysis/budget/runs/{run_id}/candle',
                     params={'granularity': 'day', 'bucket': '2025-04-01', 'category': 'nieobserwowane'}).json()
    assert gap['events'] == [] and 'not idleness' in gap['note'] and gap['values']['probable'] == 86400
    for params, code in (({'granularity': 'day', 'bucket': '1999-01-01', 'category': 'inne'}, 404),
                         ({'granularity': 'year', 'bucket': '2025', 'category': 'inne'}, 422),
                         ({'granularity': 'day', 'bucket': '2025-04-01', 'category': 'zdrowie'}, 404)):
        assert client.get(f'/api/analysis/budget/runs/{run_id}/candle', params=params).status_code == code
    assert client.get('/api/analysis/budget/runs/999').status_code == 404
    # human correction via HTTP: append-only, visible path, new run differs, old run unchanged
    corrected = client.post('/api/analysis/labels/corrections',
                            json={'event_id': client.ids['sms-override'], 'categories': {'odpoczynek': 1}, 'note': 'test'})
    assert corrected.status_code == 200 and corrected.json()['path'] == 'annotation'
    assert client.post('/api/analysis/labels/corrections',
                       json={'event_id': client.ids['sms-override'], 'categories': {'nieobserwowane': 1}}).status_code == 422
    assert client.post('/api/analysis/labels/corrections', json={'event_id': 999999, 'categories': {}}).status_code == 404
    monkeypatch.setenv('AGEDS_ACTIVITY_CATEGORIES', client.manifest['config_path'])
    detail = client.get(f"/api/analysis/events/{client.ids['sms-override']}").json()
    assert detail['current_label']['path'] == 'annotation' and detail['current_label']['shadowed'][0]['path'] == 'rule'
    assert detail['stored_labels'][0]['path'] == 'rule'  # the earlier run's stored label stays
    second = client.post('/api/analysis/budget/runs', json=_body(client, config=None)).json()
    assert second['run']['run_id'] != run_id and second['labels']['by_path']['annotation'] == 1
    assert client.get(f'/api/analysis/budget/runs/{run_id}').json()['result_sha256'] == run['result_sha256']
    info = client.get('/api/analysis/config').json()
    assert info['configured'] and info['origin'] == 'file' and info['version'] == 'corpus-activity-1'


def test_http_refuses_paths_bad_params_and_unopted_models(client):
    bad = client.post('/api/analysis/budget/runs', json=_body(client, config_path='/etc/passwd'))
    assert bad.status_code == 422
    assert client.post('/api/analysis/budget/runs', json=_body(client, params={'nope': 1})).status_code == 422
    assert client.post('/api/analysis/budget/runs', json=_body(client, zone='Nowhere/City')).status_code == 422
    assert client.post('/api/analysis/budget/runs', json=_body(client, assume_zone='Europe/Warsaw')).status_code == 422
    refused = client.post('/api/analysis/model-label-runs', json=_body(client, provider='fake'))
    assert refused.status_code == 422 and 'opt_in' in refused.json()['detail']
    assert client.post('/api/analysis/model-label-runs', json=_body(client, provider='remote', opt_in=True)).status_code == 422
    none = client.post('/api/analysis/model-label-runs', json=_body(client)).json()
    assert none['status'] == 'not_run' and none['provider'] == 'none'
    feats = client.post('/api/analysis/features/runs', json=_body(client, params={'budget_params': {'granularities': ['week']}}))
    assert feats.status_code == 200, feats.text
    fid = feats.json()['run']['run_id']
    stored = client.get(f'/api/analysis/features/runs/{fid}').json()
    assert {s['id'] for s in stored['result']['series']} >= {'message_count', 'tone', 'category_time_share'}
    assert client.get(f'/api/analysis/budget/runs/{fid}').status_code == 404  # kind is checked


def _cli(env, *args, cwd):
    return subprocess.run([sys.executable, '-m', 'server.app.cli', *map(str, args)], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)


def test_cli_budget_run_show_list_features_and_corrections(tmp_path):
    data = tmp_path / 'data'
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONDONTWRITEBYTECODE='1', EW_DATA_DIR=str(data),
               EW_DB_PATH=str(data / 'db.sqlite'), EW_STORE_DIR=str(data / 'store'))
    env.pop('AGEDS_ACTIVITY_CATEGORIES', None)
    work = tmp_path / 'work'
    work.mkdir()
    assert 'database not found' in _cli(env, 'budget-list', cwd=work).stderr
    setup = ('import json, sys\nfrom pathlib import Path\nfrom server.app import db\nfrom server.tests.corpus import activity\n'
             'db.init_db()\nm = activity.build(Path(sys.argv[1]))\nids = activity.populate(m)\n'
             'print(json.dumps({"config": m["config_path"], "ids": ids}))\n')
    seeded = json.loads(subprocess.run([sys.executable, '-c', setup, str(tmp_path / 'private')], cwd=work, env=env,
                                       check=True, timeout=120, capture_output=True, text=True).stdout)
    no_config = _cli(env, 'budget-run', '--from', '2025-03-27', '--to', '2025-04-04', '--zone', activity.ZONE, cwd=work)
    assert no_config.returncode == 2 and 'AGEDS_ACTIVITY_CATEGORIES' in no_config.stderr
    output = tmp_path / 'out' / 'budget.json'
    run = _cli(env, 'budget-run', '--config', seeded['config'], '--from', '2025-03-27', '--to', '2025-04-04',
               '--zone', activity.ZONE, '--params', '{"session_gap_s": 600}', output, cwd=work)
    assert run.returncode == 0, run.stderr
    status = json.loads(run.stdout)
    assert status['run']['reused'] is False and status['source_bytes_written'] is False
    assert status['exclusions']['untimed']['by_reason'] == {'unknown_zone': 2}
    stored = json.loads(output.read_text())
    assert stored['parameters']['session_gap_s'] == 600 and stored['schema'] == 'ageds.time_budget_result.v1'
    assert _cli(env, 'budget-show', status['run']['run_id'], output, cwd=work).returncode == 2  # never overwrites
    show = json.loads(_cli(env, 'budget-show', status['run']['run_id'], cwd=work).stdout)
    assert show['run']['result_sha256'] == status['run']['result_sha256']
    env_config = dict(env, AGEDS_ACTIVITY_CATEGORIES=seeded['config'])
    zoned = _cli(env_config, 'budget-run', '--from', '2025-03-27', '--to', '2025-04-04', '--zone', activity.ZONE,
                 '--assume-zone', activity.ZONE, '--assumption-rule', 'cli-test', cwd=work)
    assert zoned.returncode == 0, zoned.stderr
    assert json.loads(zoned.stdout)['window']['assumption_rule'] == 'cli-test'
    correct = _cli(env, 'label-correct', seeded['ids']['sms-override'], '--categories', '{"odpoczynek": 1}', cwd=work)
    assert json.loads(correct.stdout)['path'] == 'annotation'
    labels = json.loads(_cli(env_config, 'labels-show', seeded['ids']['sms-override'], cwd=work).stdout)
    assert labels['current_label']['path'] == 'annotation' and len(labels['annotations']) == 1
    feats = _cli(env_config, 'features-run', '--from', '2025-03-27', '--to', '2025-04-04', '--zone', activity.ZONE, cwd=work)
    assert feats.returncode == 0, feats.stderr
    fid = json.loads(feats.stdout)['run']['run_id']
    assert 'message_count' in json.loads(_cli(env, 'features-show', fid, cwd=work).stdout)['series']
    model = _cli(env_config, 'model-label-run', '--from', '2025-03-27', '--to', '2025-04-04', '--zone', activity.ZONE,
                 '--provider', 'fake', cwd=work)
    assert model.returncode == 2 and 'opt_in' in model.stderr
    listing = json.loads(_cli(env, 'budget-list', cwd=work).stdout)
    assert {r['kind'] for r in listing['runs']} == {'time_budget', 'feature_series'} and len(listing['runs']) == 3
    assert _cli(env, 'budget-run', '--config', seeded['config'], '--from', '2025-03-27', '--to', '2025-04-04',
                '--zone', activity.ZONE, '--params', '[1]', cwd=work).returncode == 2

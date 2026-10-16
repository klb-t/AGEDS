"""HTTP and CLI surfaces of the communication/Takeout/media importers."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import db as dbmod, evidence, main
from server.app.api import imports_comm as api_module
from server.tests.corpus import communications

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    corpus = tmp_path / 'corpus'
    manifest = communications.build(corpus)
    test_settings = replace(dbmod.settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store', scan_roots=(corpus,))
    test_settings.store_dir.mkdir(parents=True)
    for module in (dbmod, evidence, main, api_module):
        monkeypatch.setattr(module, 'settings', test_settings)
    with TestClient(main.app) as test_client:
        yield test_client, manifest, corpus


def upload(client, kind, path, **data):
    with open(path, 'rb') as handle:
        return client.post(f'/api/imports/comm/{kind}', files={'file': (Path(path).name, handle.read())}, data=data)


def test_upload_imports_and_read_endpoints(client):
    http, m, _ = client
    response = upload(http, 'ics', m['calendar']['path'])
    assert response.status_code == 200, response.text
    assert response.json()['events'] == 8
    with dbmod.session() as db:
        weekly = db.execute("SELECT id FROM events WHERE external_id='uid:weekly-dst@example.org'").fetchone()['id']
        source = db.execute('SELECT source_locator FROM artifacts WHERE id=?', (response.json()['artifact_id'],)).fetchone()
    assert source['source_locator'] == 'upload:ics:calendar.ics'
    projection = http.get(f'/api/imports/comm/calendar/{weekly}/occurrences', params={'max_instances': 2}).json()
    assert projection['stored'] is False and len(projection['occurrences']) == 2
    assert http.get('/api/imports/comm/calendar/999999/occurrences').status_code == 404
    times = http.get(f'/api/imports/comm/events/{weekly}/times').json()
    assert {t['kind'] for t in times} >= {'start', 'end'}
    mail = upload(http, 'email', m['mbox']['path'])
    assert mail.status_code == 200 and mail.json()['messages'] == 6
    with dbmod.session() as db:
        first = db.execute("SELECT id FROM events WHERE external_id='<root-1@example.org>'").fetchone()['id']
    refs = http.get(f'/api/imports/comm/events/{first}/media-references').json()
    assert {r['sha256'] for r in refs if r['reference_name'] == 'report.pdf'} == {m['pdf']['sha256']}
    assert upload(http, 'vcf', m['contacts']['path']).status_code == 200
    contacts = http.get('/api/imports/comm/contacts', params={'limit': 2}).json()
    assert len(contacts['items']) == 2 and contacts['has_more'] is True
    tels = [v for v in contacts['items'][0]['values'] if v['value_kind'] == 'tel']
    assert tels[0]['normalized_candidate'] == communications.NUMBER_B
    names = http.post('/api/imports/comm/recording-names', json={'names': list(m['recordings']['names'].values())})
    assert names.status_code == 200 and names.json()['events'] == 3
    listing = http.get('/api/imports/comm/recording-names').json()
    assert listing['items'][0]['hypotheses']['number']['status'] == 'correlation_hypothesis_from_file_name'
    assert http.post('/api/imports/comm/unknown-kind', files={'file': ('x', b'x')}).status_code == 404


def test_opt_in_imports_are_refused_without_flag(client):
    http, m, _ = client
    refused = upload(http, 'location-history', m['location']['timeline'])
    assert refused.status_code == 403 and 'opt in' in refused.json()['detail']
    accepted = upload(http, 'location-history', m['location']['timeline'], enable_location_history='true')
    assert accepted.status_code == 200 and accepted.json()['options']['enable_location'] is True
    assert upload(http, 'browser-history', m['browser']['chrome']).status_code == 403
    assert upload(http, 'browser-history', m['browser']['chrome'], enable_browser_history='true').status_code == 200
    activity = upload(http, 'takeout-activity', m['takeout']['search_json'])
    assert activity.json()['skipped_browser_history_not_enabled'] == 1


def test_path_imports_are_confined_to_configured_roots(client):
    http, m, corpus = client
    folder = Path(m['whatsapp']['folder']).relative_to(corpus).as_posix()
    result = http.post('/api/imports/comm/whatsapp-export/path', json={'rootId': 0, 'relativePath': folder})
    assert result.status_code == 200, result.text
    assert result.json()['present'] == 1 and result.json()['mode'] == 'folder'
    names = http.post('/api/imports/comm/recording-names/path', json={'rootId': 0, 'relativePath': 'recordings'})
    assert names.status_code == 200 and names.json()['observed'] == 5
    assert http.post('/api/imports/comm/ics/path', json={'rootId': 0, 'relativePath': '../x'}).status_code == 422
    assert http.post('/api/imports/comm/ics/path', json={'rootId': 3, 'relativePath': 'x'}).status_code == 404
    assert http.post('/api/imports/comm/ics/path', json={'rootId': 0, 'relativePath': 'calendar'}).status_code == 422
    billing = http.post('/api/imports/comm/billing/path', json={'rootId': 0, 'relativePath': 'billing/bill_daymonth.csv'})
    assert billing.status_code == 200 and billing.json()['events'] == 4
    entity = http.post('/api/imports/comm/calls-xml/path', json={'rootId': 0, 'relativePath': 'calls/calls_with_entity.xml'})
    assert entity.status_code == 422


def _cli(args, env):
    return subprocess.run([sys.executable, '-m', 'server.app.cli', *args], cwd=REPO, env=env, capture_output=True,
                          text=True, timeout=120)


def test_cli_imports_emit_json_and_respect_opt_in(tmp_path):
    corpus = tmp_path / 'corpus'
    manifest = communications.build(corpus)
    data = tmp_path / 'data'
    env = {**os.environ, 'EW_DATA_DIR': str(data), 'EW_DB_PATH': str(data / 'e.db'), 'EW_STORE_DIR': str(data / 'store')}
    done = _cli(['import-email', manifest['mbox']['path']], env)
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload['command'] == 'import-email' and payload['format'] == 'mbox' and payload['messages'] == 6
    assert payload['source_bytes_written'] is False
    refused = _cli(['import-location-history', manifest['location']['records']], env)
    assert refused.returncode == 2 and 'LocationOptInRequired' in refused.stderr
    allowed = _cli(['import-location-history', manifest['location']['records'], '--enable-location-history'], env)
    assert allowed.returncode == 0 and json.loads(allowed.stdout)['events'] == 2
    names = _cli(['observe-recording-names', manifest['recordings']['dir']], env)
    assert json.loads(names.stdout)['paths']['observed'] == 5
    limited = _cli(['import-ics', manifest['calendar']['path'], '--max-records', '2'], env)
    assert json.loads(limited.stdout)['coverage'] == {'complete': False}
    missing = _cli(['import-vcf', str(tmp_path / 'nope.vcf')], env)
    assert missing.returncode == 2 and 'FileNotFoundError' in missing.stderr
    after = {str(p.relative_to(corpus)): __import__('hashlib').sha256(p.read_bytes()).hexdigest()
             for p in sorted(corpus.rglob('*')) if p.is_file()}
    assert after == manifest['source_sha256']

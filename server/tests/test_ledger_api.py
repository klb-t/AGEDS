"""Ledger HTTP routes, page and CLI (uploads as temporary copies, opt-in refusals, masking)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from server.app.main import app
from server.tests.ledger_support import counts, env, rows  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2]


def _upload(client, kind, path, **form):
    with open(path, 'rb') as handle:
        return client.post(f'/api/ledger/import/{kind}', files={'file': (Path(path).name, handle)}, data=form)


def test_uploads_queries_and_page(env):
    m = env['m']
    client = TestClient(app)
    response = _upload(client, 'tabular', m['bank_pl']['files']['cp1250'], profile='example_bank_pl_style_v1')
    assert response.status_code == 200, response.text
    assert response.json()['events'] == 8
    observation = rows('SELECT metadata_json FROM source_observations ORDER BY id DESC LIMIT 1')[0]
    assert 'temporary' in observation['metadata_json']  # the upload copy's mtime is not a source time
    assert _upload(client, 'camt053', m['camt']['two_statements']).status_code == 200
    assert _upload(client, 'mt940', m['mt940']['path']).status_code == 200
    refused = _upload(client, 'health', m['health']['apple']['export'])
    assert refused.status_code == 403 and counts()['health_samples'] == 0
    accepted = _upload(client, 'health', m['health']['apple']['export'], enable_health='true')
    assert accepted.status_code == 200 and accepted.json()['format'] == 'apple'
    assert _upload(client, 'tabular', m['bank_dc']['ambiguous'], profile='nope').status_code == 422
    assert client.post('/api/ledger/import/unknown', files={'file': ('x.csv', b'a')}).status_code == 404
    masked = client.get('/api/ledger/entries', params={'mask_accounts': 'true'}).json()
    first = masked['items'][0]
    assert first['account_masking'] == 'display_only' and '*' in first['counterparty_account']
    raw = client.get('/api/ledger/entries').json()['items'][0]
    assert raw['counterparty_account'] == m['ibans']['pl_b']
    totals = client.get('/api/ledger/totals').json()['rows']
    pln_debit = next(t for t in totals if t['currency'] == 'PLN' and t['flow'] == 'debit')
    assert pln_debit['sum_exact'] == '-2260.56' and pln_debit['sum_minor'] == -226056
    statements = client.get('/api/ledger/statements').json()['items']
    assert {s['balance_check'] for s in statements} == {'match', 'mismatch', 'not_checkable'}
    sessions = client.get('/api/ledger/health/sessions').json()['items']
    assert {s['kind'] for s in sessions} == {'sleep', 'workout'}
    assert client.get('/api/ledger/profiles').json()['profiles'][0]['id'] == 'example_bank_pl_style_v1'
    page = client.get('/ledger')
    assert page.status_code == 200 and 'Rejestry' in page.text and 'mismatch' in page.text
    assert m['ibans']['pl_b'] not in page.text  # masked for display by default
    assert m['ibans']['pl_b'] in client.get('/ledger', params={'mask': 0}).text
    assert '<script>alert' not in page.text


def test_path_imports_are_confined_to_scan_roots(env, monkeypatch):
    from dataclasses import replace
    from server.app import config
    from server.app.api import imports_comm
    settings = replace(config.settings, scan_roots=(env['root'],))
    monkeypatch.setattr(imports_comm, 'settings', settings)
    client = TestClient(app)
    ok = client.post('/api/ledger/import/camt053/path', json={'rootId': 0, 'relativePath': 'statements/camt053_v8.xml'})
    assert ok.status_code == 200, ok.text
    assert client.post('/api/ledger/import/camt053/path',
                       json={'rootId': 0, 'relativePath': '../outside.xml'}).status_code == 422
    assert client.post('/api/ledger/import/health/path',
                       json={'rootId': 0, 'relativePath': 'health/apple/export.xml'}).status_code == 403


def _cli(env, *args):
    data = env['tmp'] / 'data'
    environment = {'EW_DATA_DIR': str(data), 'EW_DB_PATH': str(data / 'e.db'), 'EW_STORE_DIR': str(data / 'store'),
                   'PATH': '/usr/bin:/bin'}
    return subprocess.run([sys.executable, '-m', 'server.app.cli', *args], cwd=ROOT, capture_output=True, text=True,
                          env=environment, timeout=120)


def test_cli_commands(env):
    m = env['m']
    done = _cli(env, 'ledger-import', m['transit']['taps'], '--profile', 'example_transit_taps_v1')
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload['command'] == 'ledger-import' and payload['segments'] == 3 and payload['source_bytes_written'] is False
    refused = _cli(env, 'health-import', m['health']['fitbit']['sleep'])
    assert refused.returncode == 2 and 'enable_health' in refused.stderr
    health = _cli(env, 'health-import', m['health']['fitbit']['sleep'], '--enable-health')
    assert health.returncode == 0 and json.loads(health.stdout)['format'] == 'fitbit'
    mt = _cli(env, 'mt940-import', m['mt940']['path'], '--two-digit-year-base', '2000')
    assert mt.returncode == 0 and json.loads(mt.stdout)['statement_count'] == 3
    camt = _cli(env, 'camt-import', m['camt']['v8'])
    assert camt.returncode == 0
    profiles = json.loads(_cli(env, 'ledger-profiles').stdout)
    assert 'EXAMPLE' in profiles['notice']
    statements = json.loads(_cli(env, 'ledger-statements').stdout)
    assert len(statements['items']) == 4
    listing = json.loads(_cli(env, 'ledger-entries', '--mask-accounts', '--limit', '5').stdout)
    assert len(listing['items']) == 5
    sessions = json.loads(_cli(env, 'health-sessions').stdout)
    assert {s['kind'] for s in sessions['items']} == {'sleep'}

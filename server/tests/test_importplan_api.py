"""HTTP API, /import page and CLI of the import planner (AGEDS-TASK-20261016-I)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import db as dbmod
from server.app import main
from server.tests.test_catalog_scan import tree_state
from server.tests.test_importplan_detect import LABELS, make_world

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def api(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    monkeypatch.setattr(main, 'settings', world.settings)
    with TestClient(main.app) as client:
        world.client = client
        yield world


def _create(api) -> dict:
    response = api.client.post('/api/import-plans', json={'collectionIds': list(api.collections.values()), 'actor': 'web-test'})
    assert response.status_code == 200, response.text
    return response.json()


def test_http_plan_review_run_and_cross_exports(api):
    created = _create(api)
    plan_id = created['plan']['id']
    assert created['summary']['items'] == 23 and created['summary']['ambiguous'] == 1
    listing = api.client.get('/api/import-plans').json()
    assert listing['plans'][0]['id'] == plan_id
    assert {i['key'] for i in listing['importers']} >= {'chatgpt_export', 'location_history', 'billing'}
    ambiguous = api.client.get(f'/api/import-plans/{plan_id}', params={'ambiguous': 'true'}).json()
    assert ambiguous['matching'] == 1 and ambiguous['items'][0]['locator'] == 'exports/ambiguous'
    calendars = api.client.get(f'/api/import-plans/{plan_id}', params={'importer': 'ics', 'q': 'calendar'}).json()
    assert [i['locator'].rsplit('/', 1)[-1] for i in calendars['items']] == ['Example.ics']
    paged = api.client.get(f'/api/import-plans/{plan_id}', params={'limit': 5}).json()
    assert paged['has_more'] and paged['next_after_ordinal'] == 5
    second = api.client.get(f'/api/import-plans/{plan_id}', params={'limit': 5, 'afterOrdinal': 5}).json()
    assert [i['ordinal'] for i in second['items']] == [6, 7, 8, 9, 10]
    item_id = calendars['items'][0]['id']
    detail = api.client.get(f'/api/import-plans/{plan_id}/items/{item_id}').json()
    assert detail['members'][0]['sniffed_type'] == 'ics'
    # approve: a stale base version is a conflict, an invalid decision is 422
    stale = api.client.post(f'/api/import-plans/{plan_id}/decisions', json={'baseVersion': 2, 'actor': 'x', 'bulk': {'action': 'approve'}})
    assert stale.status_code == 409
    bad = api.client.post(f'/api/import-plans/{plan_id}/decisions', json={
        'baseVersion': 1, 'actor': 'x', 'decisions': [{'itemId': ambiguous['items'][0]['id'], 'status': 'approved'}]})
    assert bad.status_code == 422 and 'importer_not_chosen' in bad.json()['detail']
    extra = api.client.post(f'/api/import-plans/{plan_id}/decisions', json={'baseVersion': 1, 'actor': 'x', 'unknown': 1})
    assert extra.status_code == 422
    decided = api.client.post(f'/api/import-plans/{plan_id}/decisions', json={
        'baseVersion': 1, 'actor': 'web-test', 'note': 'chat only',
        'bulk': {'action': 'approve', 'filter': {'importer': 'chatgpt_export'}}}).json()
    assert decided['version'] == 2 and decided['changed'] == 3
    summary = api.client.get(f'/api/import-plans/{plan_id}/summary').json()
    assert summary['to_run']['items'] == 3 and summary['by_status']['approved'] == 3
    run = api.client.post(f'/api/import-plans/{plan_id}/runs', json={'actor': 'web-test'})
    assert run.status_code == 200, run.text
    body = run.json()
    assert body['status'] == 'completed' and body['selected_items'] == 3
    detail = api.client.get(f'/api/import-plan-runs/{body["run_id"]}').json()
    assert {a['status'] for a in detail['attempts']} == {'done'} and 'lease_token' not in detail['attempts'][0]
    assert api.client.get(f'/api/import-plans/{plan_id}/runs').json()['runs'][0]['id'] == body['run_id']
    crossing = api.client.get(f'/api/import-plans/{plan_id}/cross-exports').json()
    assert crossing['before_import']['shared_conversations_total'] == 3
    assert crossing['after_import']['conversations_total'] == 3
    assert api.client.get('/api/import-plans/999').status_code == 404
    assert api.client.post('/api/import-plans', json={'collectionIds': [999]}).status_code == 404
    assert api.client.post('/api/import-plans', json={'collectionIds': [1], 'limits': {'nope': 1}}).status_code == 422
    assert tree_state(api.corpus) == api.before


def test_import_page_renders_review_table_and_escapes_source_text(api):
    landing = api.client.get('/import')
    assert landing.status_code == 200
    assert '<a href="/import">Import</a>' in landing.text and 'Generuj plan' in landing.text
    for label in LABELS.values():
        assert label in landing.text
    plan_id = _create(api)['plan']['id']
    page = api.client.get('/import', params={'plan': plan_id}).text
    assert 'Plan #%d' % plan_id in page and 'niejednoznaczne — wybierz' in page
    assert 'takeout-20241001.zip!/Takeout/takeout-inner.zip!/Mail/All mail Including Spam and Trash.mbox' in page
    assert 'opt_in_disabled:enable_location' in page and 'parameter_missing:profile_id' in page
    assert page.count('<script') == 1 and 'onerror=alert' not in page.replace('&lt;img src=x onerror=alert(1)&gt;', '')
    filtered = api.client.get('/import', params={'plan': plan_id, 'status': 'proposed', 'ambiguous': 'true'}).text
    assert filtered.count('class="ip-row ') == 1
    blocked = api.client.get('/import', params={'plan': plan_id, 'blocked': 'true'}).text
    assert blocked.count('class="ip-row ') == 5
    old = api.client.get('/import', params={'plan': plan_id, 'version': 1}).text
    assert 'data-ip-action="decide"' in old  # version 1 is the latest so far


def test_abandon_endpoint(api):
    with dbmod.session() as db:
        db.execute('''INSERT INTO catalog_scans(case_id,collection_id,root_path,root_kind,status,started_at,catalog_version,
                      signature_table_version,scanner_version,limits_json,policy_json) VALUES(1,?,?,?,'running','2020-01-01T00:00:00+00:00','x','x','x','{}','{}')''',
                   (api.collections['drive_c'], str(api.corpus / 'drive_c'), 'directory'))
        scan_id = db.execute('SELECT MAX(id) FROM catalog_scans').fetchone()[0]
    missing = api.client.post(f'/api/catalog/scans/{scan_id}/abandon', json={'actor': 'web'})
    assert missing.status_code == 422
    done = api.client.post(f'/api/catalog/scans/{scan_id}/abandon', json={'actor': 'web', 'reason': 'scanner killed'})
    assert done.status_code == 200 and done.json()['abandoned'] is True
    again = api.client.post(f'/api/catalog/scans/{scan_id}/abandon', json={'actor': 'web', 'reason': 'again'})
    assert again.status_code == 422
    assert api.client.post('/api/catalog/scans/99999/abandon', json={'actor': 'web', 'reason': 'x'}).status_code == 404


def test_cli_end_to_end(tmp_path):
    world_root = tmp_path
    from server.tests.corpus import import_layout
    expected = import_layout.build(world_root / 'corpus')
    before = tree_state(world_root / 'corpus')
    data = world_root / 'data'
    env = {**os.environ, 'EW_DATA_DIR': str(data), 'EW_DB_PATH': str(data / 'evidence.db'), 'EW_STORE_DIR': str(data / 'store'),
           'EW_SCAN_ROOTS': ''}

    def run(*args, expect: int = 0) -> dict:
        process = subprocess.run([sys.executable, '-m', 'server.app.cli', *map(str, args)], cwd=REPO, env=env,
                                 capture_output=True, text=True, timeout=300)
        assert process.returncode == expect, (args, process.stdout, process.stderr)
        stream = process.stdout if expect == 0 else process.stderr
        return json.loads(stream.strip().splitlines()[-1])
    collection = run('catalog-collection-add', 'Telefon A (synthetic)')['id']
    run('catalog-scan', expected['roots']['phone_a'], '--collection', collection, '--temp-dir', world_root / 'spool')
    created = run('import-plan-create', '--collection', collection, '--billing-profile', 'example_semicolon_daymonth_v1',
                  '--temp-dir', world_root / 'work')
    plan_id = created['plan']['id']
    assert created['summary']['items'] == 10 and created['summary']['blocked'] == {}
    shown = run('import-plan-show', plan_id, '--importer', 'calls_xml')
    calls = shown['items'][0]
    run('import-plan-decide', plan_id, '--base-version', 1, '--actor', 'cli', '--approve', 999, expect=2)
    stale = run('import-plan-decide', plan_id, '--base-version', 3, '--actor', 'cli', '--approve-all-ready', expect=2)
    assert stale['error'] == 'Conflict'
    decided = run('import-plan-decide', plan_id, '--base-version', 1, '--actor', 'cli', '--approve-all-ready',
                  '--importer', f'{calls["id"]}=sms_backup', '--note', 'all ready')
    assert decided['version'] == 2 and decided['changed'] == 10
    summary = run('import-plan-summary', plan_id)
    assert summary['to_run']['items'] == 10
    result = run('import-plan-run', plan_id, '--temp-dir', world_root / 'work')
    assert result['status'] == 'completed' and result['selected_items'] == 10
    assert run('import-plan-run', plan_id, '--temp-dir', world_root / 'work')['selected_items'] == 0
    runs = run('import-plan-runs', plan_id)
    assert [r['status'] for r in runs['runs']] == ['completed', 'completed']
    one = run('import-plan-runs', plan_id, '--run', result['run_id'])
    assert len(one['attempts']) == 10
    item = run('import-plan-show', plan_id, '--item', calls['id'])
    assert item['status'] == 'done' and item['state']['importer'] == 'sms_backup'
    assert run('import-plan-cross', plan_id)['after_import']['conversations_total'] == 0
    assert run('import-plan-work')['jobs'] == 0
    assert run('import-plan-list')['plans'][0]['id'] == plan_id
    run('catalog-scan-abandon', 1, '--actor', 'cli', '--reason', 'not running', expect=2)
    run('import-plan-create', '--collection', collection, '--temp-dir', world_root / 'corpus' / 'phone_a', expect=2)
    assert tree_state(world_root / 'corpus') == before
    assert list((world_root / 'work').iterdir()) == []

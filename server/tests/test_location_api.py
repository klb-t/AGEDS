"""HTTP routes, CLI commands and import-plan integration of the location area (task L1)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import db as dbmod, evidence, main, packages
from server.app.catalog import store
from server.app.catalog.scan import run_catalog_scan
from server.app.importplan import execute, plan
from server.app.importplan.adapters import IMPORTERS
from server.tests.corpus import location as corpus

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    manifest = corpus.build(tmp_path / 'corpus')
    test_settings = replace(dbmod.settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store', scan_roots=(tmp_path / 'corpus',))
    test_settings.store_dir.mkdir(parents=True)
    for module in (dbmod, evidence, main):
        monkeypatch.setattr(module, 'settings', test_settings)
    with TestClient(main.app) as http:
        yield http, manifest, tmp_path


def upload(http, path, **data):
    with open(path, 'rb') as handle:
        return http.post('/api/location/import', files={'file': (Path(path).name, handle.read())}, data=data)


def test_http_import_presence_window_lanes_and_correlations(client):
    http, m, _ = client
    f = m['files']
    refused = upload(http, f['Takeout/Location History/Records.json'])
    assert refused.status_code == 403
    with dbmod.session() as db:
        assert db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0] == 0
    ok = upload(http, f['Takeout/Location History/Records.json'], enable_location='true', stream='phone')
    assert ok.status_code == 200 and ok.json()['format'] == 'records_json' and ok.json()['detection']['format'] == 'google'
    with dbmod.session() as db:
        scope = db.execute("SELECT json_extract(metadata_json,'$.filesystem_stat_scope') FROM source_observations "
                           "ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert scope == 'temporary_copy_not_original_source'
    assert upload(http, f['tracks/walk.gpx'], enable_location='true', format='gpx').status_code == 200
    assert upload(http, f['tracks/ride.fit'], enable_location='true').status_code == 501
    assert upload(http, f['calendar/meetings.ics'], enable_location='true').status_code == 422
    assert upload(http, f['tracks/points.geojson'], enable_location='true', geojson_options='{bad').status_code == 422
    geo = upload(http, f['tracks/points.geojson'], enable_location='true', geojson_options='{"time_properties": ["ts"]}')
    assert geo.status_code == 200 and geo.json()['geojson']['points_timed'] == 6
    assert http.post('/api/location/places/check', json={'places': m['places']}).json()['places'] == 5
    assert http.post('/api/location/places/check', json={'places': {'schema': 'x'}}).status_code == 422
    assert http.post('/api/location/places/check', json={}).status_code == 422
    assert http.post('/api/location/presence/runs', json={'places': m['places_path']}).status_code == 422  # a path string
    assert http.post('/api/location/presence/runs', json={'use_server_places': True}).status_code == 422  # not configured
    run = http.post('/api/location/presence/runs', json={'places': m['places'], 'publish': True, 'actor': 'api',
                                                         'from': '2024-01-15T00:00:00Z', 'to': '2024-01-17T00:00:00Z'})
    assert run.status_code == 200 and run.json()['published']
    run_id, intervals = run.json()['run_id'], run.json()['counts']['intervals']
    detail = http.get(f'/api/location/presence/runs/{run_id}').json()
    assert intervals >= 6 and len(detail['intervals']) == intervals and detail['places'][0]['label']
    assert {i['stream_key'] for i in detail['intervals']} >= {'phone/device:-11'}  # GPX/GeoJSON streams too
    assert http.get('/api/location/presence/runs/99999').status_code == 404
    window = http.get('/api/location/window', params={'from': '2024-01-15T00:00:00Z', 'to': '2024-01-16T00:00:00Z',
                                                      'max_points': 10}).json()
    assert window['fixes']['returned'] <= 10 and window['fixes']['thinned'] and window['fixes']['total_in_window'] > 10
    assert window['presence']['run_id'] == run_id and window['fixes']['items'][0]['decimals'] == 7
    coarse = http.get('/api/location/window', params={'display_decimals': 2, 'max_points': 5}).json()
    assert all(fx['decimals'] == 2 and round(fx['lat'], 2) == fx['lat'] for fx in coarse['fixes']['items'])
    assert http.get('/api/location/window', params={'from': '2024-01-15T00:00:00'}).status_code == 422  # no offset
    lanes = http.get('/api/location/lanes', params={'from': '2024-01-15T00:00:00Z', 'to': '2024-01-16T00:00:00Z'}).json()
    lane = {item['id']: item for item in lanes['lanes']}
    assert lane['presence']['items'] and lane['samples']['items'] and lanes['config']['schema'] == 'ageds.location-lanes.v1'
    assert lanes['source_rule'].startswith('display_time@')
    presence_item = lane['presence']['items'][0]
    assert presence_item['signal'] == {'class': 'passive', 'basis': 'derived'} and presence_item['t_end'] > presence_item['t_start']
    http.post('/api/imports/comm/ics', files={'file': ('m.ics', Path(f['calendar/meetings.ics']).read_bytes())})
    corr = http.post('/api/location/correlations/runs', json={'rule': 'presence_calendar', 'presence_run_id': run_id})
    assert corr.status_code == 200 and corr.json()['counts']['hypotheses'] == 2
    hypothesis = corr.json()['hypotheses'][0]['id']
    assert http.post('/api/location/correlations/runs', json={'rule': 'nope', 'presence_run_id': run_id}).status_code == 422
    review = http.post(f'/api/location/correlations/hypotheses/{hypothesis}/review',
                       json={'action': 'accept', 'reason': 'same meeting', 'actor': 'me'})
    assert review.status_code == 200 and review.json()['actor_verified'] is False
    assert http.post(f'/api/location/correlations/hypotheses/{hypothesis}/review',
                     json={'action': 'accept', 'reason': 'again'}).status_code == 422
    listing = http.get('/api/location/correlations/runs').json()
    assert listing['runs'][0]['rule'] == 'presence_calendar@1' and {r['rule_id'] for r in listing['rules']} == \
        {'presence_calendar', 'presence_communication'}
    summary = http.get('/api/location/summary').json()
    assert summary['adapters']['fit']['status'] == 'unavailable' and summary['events_by_type']['place_presence'] == intervals
    assert any(s['key'] == 'location_fixes' for s in summary['geotag_sources'])


def test_page_has_strict_csp_by_default_and_tiles_only_after_recorded_opt_in(client):
    http, m, _ = client
    page = http.get('/location')
    assert page.status_code == 200 and 'Lokalizacja' in page.text
    csp = page.headers['content-security-policy']
    assert "img-src 'self' data:;" in csp and "connect-src 'self'" in csp and 'http' not in csp.split('img-src')[1].split(';')[0]
    assert 'nie dowód obecności osoby' in page.text and 'data-loc-tile-warning' in page.text
    assert '<a href="/location">Lokalizacja</a>' in page.text
    for bad in ({'template': 'https://tiles.example.org/{z}/{x}/{y}.png'},  # no acknowledgement
                {'template': 'http://tiles.example.org/{z}/{x}/{y}.png', 'acknowledge_disclosure': True},
                {'template': 'https://tiles.example.org/{z}/{x}.png', 'acknowledge_disclosure': True},
                {'template': 'https://tiles.example.org/{z}/{x}/{y}/{s}.png', 'acknowledge_disclosure': True},
                {'template': 'javascript:alert(1)//{z}{x}{y}', 'acknowledge_disclosure': True}):
        assert http.post('/api/location/tiles/opt-in', json=bad).status_code == 422, bad
    opt = http.post('/api/location/tiles/opt-in', json={'template': 'https://tiles.example.org/{z}/{x}/{y}.png',
                                                         'acknowledge_disclosure': True, 'actor': 'viewer'})
    assert opt.status_code == 200 and opt.json()['origin'] == 'https://tiles.example.org'
    with_tiles = http.get(opt.json()['page'])
    assert "img-src 'self' data: https://tiles.example.org;" in with_tiles.headers['content-security-policy']
    assert 'Kafelki włączone' in with_tiles.text
    unknown = opt.json()['opt_in_id'] + 12345
    assert http.get(f'/location?tile_opt_in={unknown}').headers['content-security-policy'] == csp  # no such opt-in
    recorded = http.get('/api/location/tiles/opt-ins').json()['opt_ins']
    assert recorded[0]['template'] == 'https://tiles.example.org/{z}/{x}/{y}.png' and recorded[0]['actor'] == 'viewer'
    hostile = http.post('/api/location/tiles/opt-in', headers={'Origin': 'https://attacker.example.net'},
                        json={'template': 'https://tiles.example.org/{z}/{x}/{y}.png', 'acknowledge_disclosure': True})
    assert hostile.status_code == 403


def _cli(args, env):
    return subprocess.run([sys.executable, '-m', 'server.app.cli', *args], cwd=REPO, env=env, capture_output=True,
                          text=True, timeout=180)


def test_cli_commands(tmp_path):
    manifest = corpus.build(tmp_path / 'corpus')
    f = manifest['files']
    data = tmp_path / 'data'
    env = {**os.environ, 'EW_DATA_DIR': str(data), 'EW_DB_PATH': str(data / 'e.db'), 'EW_STORE_DIR': str(data / 'store')}
    env.pop('AGEDS_LOCATION_PLACES', None)
    refused = _cli(['location-import', f['tracks/walk.gpx']], env)
    assert refused.returncode == 2 and 'LocationOptInRequired' in refused.stderr
    done = _cli(['location-import', f['Takeout/Location History/Records.json'], '--enable-location', '--stream', 'phone',
                 '--store-decimals', '6'], env)
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload['command'] == 'location-import' and payload['options']['parameters']['store_decimals'] == 6
    unavailable = _cli(['location-import', f['tracks/ride.fit'], '--enable-location'], env)
    assert unavailable.returncode == 2 and 'fitparse' in unavailable.stderr
    check = _cli(['places-check', '--places', manifest['places_path']], env)
    assert check.returncode == 0 and json.loads(check.stdout)['places'] == 5
    missing = _cli(['places-check'], env)
    assert missing.returncode == 2 and 'AGEDS_LOCATION_PLACES' in missing.stderr
    run = _cli(['presence-run', '--publish', '--params', '{"min_dwell_s": 600}'], {**env, 'AGEDS_LOCATION_PLACES':
                                                                                  manifest['places_path']})
    assert run.returncode == 0, run.stderr
    run_id = json.loads(run.stdout)['run_id']
    assert json.loads(run.stdout)['place_set']['origin'] == 'env_file'
    show = _cli(['presence-show', str(run_id)], env)
    assert show.returncode == 0 and json.loads(show.stdout)['intervals']
    out = tmp_path / 'window.json'
    window = _cli(['location-window', str(out), '--from', '2024-01-15T00:00:00Z', '--to', '2024-01-16T00:00:00Z'], env)
    assert window.returncode == 0, window.stderr
    assert json.loads(out.read_text())['fixes']['total_in_window'] == json.loads(window.stdout)['fixes_in_window']
    again = _cli(['location-window', str(out)], env)
    assert again.returncode == 2  # never overwrites
    _cli(['import-ics', f['calendar/meetings.ics']], env)
    corr = _cli(['location-correlate', '--rule', 'presence_calendar', '--presence-run', str(run_id)], env)
    assert corr.returncode == 0, corr.stderr
    hypothesis = json.loads(corr.stdout)['hypotheses'][0]['id']
    review = _cli(['location-correlation-review', str(hypothesis), 'reject', '--reason', 'not there'], env)
    assert review.returncode == 0 and json.loads(review.stdout)['to'] == 'rejected'
    status = _cli(['location-status'], env)
    assert status.returncode == 0 and json.loads(status.stdout)['adapters']['fit']['status'] == 'unavailable'


def test_import_plan_detects_and_runs_location_formats(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    settings = replace(dbmod.settings, data_dir=data, db_path=data / 'e.db', store_dir=data / 'store',
                       scan_roots=(tmp_path / 'corpus',))
    for module in (dbmod, evidence):
        monkeypatch.setattr(module, 'settings', settings)
    data.mkdir(parents=True)
    dbmod.init_db()
    manifest = corpus.build(tmp_path / 'corpus')
    collection = store.create_collection('Telefon (synthetic location)', device_or_account='phone-l1')['id']
    run_catalog_scan(tmp_path / 'corpus', collection, temp_dir=tmp_path / 'spool')
    created = plan.create_plan(actor='tester', collection_ids=[collection], temp_dir=tmp_path / 'work')
    plan_id = created['plan']['id']
    from server.app.importplan.detect import load_detectors
    assert created['plan']['detector_sha256'] == load_detectors()['_sha256']  # includes detectors.d/location.v1.json
    assert any(e['name'] == 'location.v1.json' for e in load_detectors()['_extensions'])
    items = plan.get_plan(plan_id, limit=500)['items']
    chosen = {i['locator']: i['state']['importer'] for i in items}
    assert chosen['tracks/walk.gpx'] == 'gpx_track' and chosen['tracks/places.kml'] == 'kml_track'
    assert chosen['tracks/places.kmz!/doc.kml'] == 'kml_track' and chosen['tracks/points.geojson'] == 'geojson_track'
    assert chosen['tracks/ride.fit'] == 'fit_track' and chosen['ios/location-history.json'] == 'location_history'
    assert chosen['device/Timeline.json'] == 'location_history'
    assert chosen['Takeout/Location History/Records.json'] == 'location_history'
    blocked = [i for i in items if i['state']['importer'] in ('gpx_track', 'location_history')]
    assert all('opt_in_disabled:enable_location' in i['blockers'] for i in blocked)
    wanted = [i for i in items if i['locator'] in ('tracks/walk.gpx', 'tracks/places.kmz!/doc.kml', 'ios/location-history.json',
                                                   'Takeout/Location History/Records.json', 'tracks/ride.fit')]
    plan.decide(plan_id, base_version=1, actor='tester', decisions=[
        {'item_id': i['id'], 'status': 'approved', 'opt_ins': {'enable_location': True},
         'parameters': {'stream': 'phone-l1'} if i['locator'].endswith('Records.json') else {}} for i in wanted])
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=tmp_path / 'work')
    with dbmod.session() as db:
        attempts = {r[0]: (r[1], r[2]) for r in db.execute(
            '''SELECT i.locator, a.status, a.error FROM import_plan_attempts a JOIN import_plan_items i ON i.id=a.item_id''')}
        imports = {r[0]: r[1] for r in db.execute('SELECT format, stream_key FROM location_imports')}
        locators = [r[0] for r in db.execute('SELECT source_locator FROM source_observations')]
    assert attempts['tracks/walk.gpx'][0] == 'done' and attempts['ios/location-history.json'][0] == 'done'
    assert attempts['tracks/places.kmz!/doc.kml'][0] == 'done'
    assert attempts['tracks/ride.fit'][0] in ('failed', 'refused') and 'fitparse' in (attempts['tracks/ride.fit'][1] or '')
    assert imports['records_json'] == 'phone-l1' and imports['gpx'] == f'collection:{collection}'
    assert any(loc.startswith('catalog:collection/') for loc in locators)
    assert result['plan_id'] == plan_id and result['selected_items'] == 5
    assert {'gpx_track', 'kml_track', 'geojson_track', 'fit_track'} <= set(IMPORTERS)
    assert IMPORTERS['location_history'].params[-1].name == 'coarse_decimals'


def test_location_tables_are_classified_for_metadata_packages():
    declared = set(packages.EXTENSION_TABLES) | set(packages.EXCLUDED_EXTENSION_TABLES)
    from server.app.schema import location as schema
    import re
    tables = {m for _, _, sql in schema.MIGRATIONS for m in re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', sql)}
    assert tables <= declared
    assert 'location_fixes' in packages.EXCLUDED_EXTENSION_TABLES and 'location_presence_runs' in packages.EXTENSION_TABLES

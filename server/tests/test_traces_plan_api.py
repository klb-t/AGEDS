"""Traces importers through the import plan, the CLI and the HTTP routes."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import config as configmod, db as dbmod, evidence
from server.app.catalog import store
from server.app.catalog.scan import run_catalog_scan
from server.app.importplan import execute, plan
from server.app.importplan.adapters import IMPORTERS
from server.tests.corpus import traces as corpus


@pytest.fixture()
def world(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    settings = replace(dbmod.settings, data_dir=data, db_path=data / 'evidence.db', store_dir=data / 'store',
                       scan_roots=(tmp_path / 'corpus',))
    for module in (dbmod, evidence, configmod):
        monkeypatch.setattr(module, 'settings', settings)
    from server.app.api import traces as api_traces
    monkeypatch.setattr(api_traces, 'settings', settings)
    dbmod.init_db()
    manifest = corpus.build(tmp_path / 'corpus')
    return {'tmp': tmp_path, 'corpus': tmp_path / 'corpus', 'm': manifest, 'settings': settings}


def rows(sql, params=()):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


def items_of(plan_id):
    return plan.get_plan(plan_id, limit=1000)['items']


def scan_and_plan(world, root: Path, label='Telefon (synthetic)'):
    collection = store.create_collection(label, device_or_account='phone_x')['id']
    run_catalog_scan(root, collection, temp_dir=world['tmp'] / 'spool')
    created = plan.create_plan(actor='tester', collection_ids=[collection], temp_dir=world['tmp'] / 'work')
    return collection, created['plan']['id']


def approve(plan_id, selector, *, opt_ins=None, importer=None, parameters=None):
    decisions = []
    for item in items_of(plan_id):
        if selector(item):
            decision = {'item_id': item['id'], 'status': 'approved', 'opt_ins': opt_ins or {}}
            if importer:
                decision['importer'] = importer
            if parameters:
                decision['parameters'] = parameters
            decisions.append(decision)
    version = plan.get_plan(plan_id, limit=1)['version']
    plan.decide(plan_id, base_version=version, decisions=decisions, actor='tester')
    return len(decisions)


def test_extension_importers_are_registered():
    for key in ('google_photos_sidecar', 'photo_geotags'):
        assert key in IMPORTERS
    assert IMPORTERS['google_photos_sidecar'].optional_opt_ins == ('enable_location',)


def test_plan_proposes_sidecars_and_keeps_exif_primary_for_images(world):
    root = Path(world['m']['photos']['root'])
    collection, plan_id = scan_and_plan(world, root)
    items = items_of(plan_id)
    sidecars = [i for i in items if i['state']['importer'] == 'google_photos_sidecar']
    assert len(sidecars) == 14  # every sidecar JSON; album metadata.json is not proposed
    assert not [i for i in items if i['name'] == 'metadata.json' and i['state']['importer']]
    jpg = [i for i in items if i['name'] == 'IMG_20210101_101500.jpg']
    assert jpg and all(i['state']['importer'] == 'exif' for i in jpg)
    assert {c['importer'] for c in jpg[0]['candidates']} == {'exif', 'photo_geotags'}
    assert all(i['state']['opt_ins'] == {'enable_location': False} for i in sidecars)
    # images first (so the catalog knows their artifacts), then the sidecars with the default (no location)
    approve(plan_id, lambda i: i['state']['importer'] == 'exif')
    execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work')
    approve(plan_id, lambda i: i['state']['importer'] == 'google_photos_sidecar')
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work')
    assert result['status'] == 'completed' and {o['status'] for o in result['items']} == {'done'}
    matches = rows('SELECT m.*, s.sidecar_name FROM trace_photo_matches m JOIN trace_photo_sidecars s ON s.id=m.sidecar_id')
    assert matches and {m['scope'] for m in matches} == {'catalog_directory'}
    exact = [m for m in matches if m['sidecar_name'] == 'IMG_20210101_101500.jpg.json']
    assert len(exact) == 2  # the year folder and the album folder are separate scopes
    candidates = json.loads(exact[0]['candidates_json'])
    assert candidates[0]['catalog_entry_id'] and candidates[0]['sha256'] == world['m']['photos']['sha256']['a']
    assert candidates[0]['artifact_id'] is not None  # the image was imported first
    assert rows('SELECT COUNT(*) AS n FROM trace_geotags WHERE coordinates_stored=1')[0]['n'] == 0
    observations = rows("SELECT DISTINCT collection_id, source_locator FROM source_observations WHERE "
                        "json_extract(metadata_json,'$.source_metadata.importer')='google_photos_sidecar'")
    assert observations and all(o['collection_id'] == collection and o['source_locator'].startswith('catalog:')
                                for o in observations)


def test_plan_location_opt_in_and_photo_alternative(world):
    root = Path(world['m']['photos']['root'])
    _, plan_id = scan_and_plan(world, root)
    approve(plan_id, lambda i: i['name'] == 'IMG_SOUTH.jpg', importer='photo_geotags',
            opt_ins={'enable_location': True}, parameters={'coordinate_decimals': 2})
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work')
    assert {o['status'] for o in result['items']} == {'done'}
    stored = rows('SELECT * FROM trace_geotags WHERE coordinates_stored=1')
    assert len(stored) == 1 and stored[0]['stored_precision'] == 'decimals:2' and stored[0]['lat'] == -20.12


def run_cli(*argv) -> tuple[int, dict]:
    from server.app import cli
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(list(argv))
    text = buffer.getvalue().strip()
    return code, json.loads(text) if text else {}


def test_cli_import_and_queries(world):
    root = world['m']['photos']['root']
    code, kinds = run_cli('traces-kinds')
    assert code == 0 and 'photos-takeout' in {k['kind'] for k in kinds['kinds']}
    code, out = run_cli('import-trace', 'photos-takeout', root, '--enable-location', '--coordinate-decimals', '5')
    assert code == 0 and out['source_bytes_written'] is False and out['options']['coordinate_decimals'] == 5
    code, summary = run_cli('traces-summary')
    assert code == 0 and summary['tables']['trace_photo_sidecars'] == 14
    code, tags = run_cli('traces-geotags', '--since', '2021-01-01T00:00:00Z', '--limit', '3')
    assert code == 0 and len(tags['geotags']) == 3
    code, err = run_cli('import-trace', 'photo', root)
    assert code == 2
    code, err = run_cli('import-trace', 'nope', root)
    assert code == 2


def test_http_upload_path_and_page(world):
    from server.app.main import app
    client = TestClient(app)
    sidecar = Path(world['m']['photos']['files']['b_json'])
    response = client.post('/api/traces/import/photo-sidecar',
                           files={'file': (sidecar.name, sidecar.read_bytes(), 'application/json')})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['name']['variant'] == 'supplemental_metadata' and body['geotags_stored'] == 0
    relative = Path(world['m']['photos']['root']).relative_to(world['corpus'])
    response = client.post('/api/traces/import/photos-takeout/path',
                           json={'rootId': 0, 'relativePath': str(relative), 'enableLocation': True})
    assert response.status_code == 200, response.text
    assert response.json()['geotags_stored'] > 0
    assert client.post('/api/traces/import/photos-takeout/path',
                       json={'rootId': 0, 'relativePath': '../outside'}).status_code == 422
    assert client.post('/api/traces/import/unknown', files={'file': ('x', b'{}')}).status_code == 404
    listing = client.get('/api/traces/photo-sidecars?limit=50').json()
    with_people = [i for i in listing['items'] if isinstance(i['people'], dict) and i['people']['count']]
    assert with_people and 'Przykładowa' not in json.dumps(listing, ensure_ascii=False)
    assert 'Przykładowa' in json.dumps(client.get('/api/traces/photo-sidecars?limit=50&include_people=true').json(),
                                       ensure_ascii=False)
    assert client.get('/api/traces/geotags').json()['items']
    page = client.get('/traces')
    assert page.status_code == 200 and 'Ślady cyfrowe' in page.text and 'photo_metadata' in page.text
    assert '/traces' in client.get('/').text


EXPECTED_MESSAGING = {
    'message_1.json': {'facebook_messages', 'instagram_messages'},
    'message_2.json': {'facebook_messages'},
    'ala_przykladowa_1.json': {'facebook_messages'},
    'result.json': {'telegram_desktop'},
    'messages.json': {'google_chat'},
    'tweets.js': {'twitter_x_archive'},
    'tweets-part1.js': {'twitter_x_archive'},
    'direct-messages.js': {'twitter_x_archive'},
    'direct-messages-group.js': {'twitter_x_archive'},
    'messages.csv': {'linkedin_messages'},
}


def test_plan_detects_messaging_downloads_and_runs_them(world):
    collection, plan_id = scan_and_plan(world, world['corpus'])
    items = items_of(plan_id)
    chosen = {}
    for item in items:
        if item['name'] in EXPECTED_MESSAGING:
            chosen.setdefault(item['name'], set()).add(item['state']['importer'])
            assert not item['ambiguous'], item['locator']
    assert chosen == EXPECTED_MESSAGING
    insta = [i for i in items if i['name'] == 'message_1.json' and 'instagram' in i['locator']]
    assert len(insta) == 1 and insta[0]['state']['importer'] == 'instagram_messages'
    approve(plan_id, lambda i: i['state']['importer'] in {k for v in EXPECTED_MESSAGING.values() for k in v})
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work')
    assert result['status'] == 'completed' and {o['status'] for o in result['items']} == {'done'}, result['items']
    platforms = {r['contact_label'] for r in rows("SELECT DISTINCT contact_label FROM events WHERE event_type IN "
                                                 "('chat_message','social_post')")}
    assert platforms == {'facebook_messenger', 'instagram', 'telegram', 'google_chat', 'twitter_x', 'linkedin'}
    refs = rows("""SELECT r.status, r.reference_key, r.metadata_json FROM event_media_references r
                   JOIN events e ON e.id=r.event_id WHERE e.contact_label='facebook_messenger' ORDER BY r.id""")
    by_key = {r['reference_key'].rsplit('/', 1)[-1]: r for r in refs}
    assert by_key['p_1.jpg']['status'] == 'not_extracted'
    assert json.loads(by_key['p_1.jpg']['metadata_json'])['reason'] == 'catalogued_import_as_its_own_plan_item'
    assert by_key['gone.jpg']['status'] == 'missing_from_export'
    sources = rows("""SELECT DISTINCT o.collection_id, o.source_locator FROM source_observations o
                      JOIN events e ON e.artifact_id=o.artifact_id WHERE e.event_type='chat_message'""")
    assert sources and all(s['collection_id'] == collection and s['source_locator'].startswith('catalog:') for s in sources)


def test_plan_detects_account_activity_files(world):
    root = world['corpus'] / 'account_activity'
    _, plan_id = scan_and_plan(world, root)
    items = {i['name']: i for i in items_of(plan_id)}
    expected = {'account_activity.json', 'logins_and_logouts.json', "where_you're_logged_in.json", 'authorized_logins.json',
                'login_activity.json', 'logout_activity.json', 'login_history.json',
                'Activities - A list of Google services accessed by.csv',
                'Devices - A list of devices (i.e. Nest, Pixel, iPh.csv'}
    assert {n for n, i in items.items() if i['state']['importer'] == 'account_activity'} == expected
    assert 'other.json' not in items  # unknown JSON layout: not proposed
    approve(plan_id, lambda i: i['state']['importer'] == 'account_activity', opt_ins={'enable_location': True})
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work')
    assert {o['status'] for o in result['items']} == {'done'}, result['items']
    assert rows('SELECT COUNT(*) AS n FROM trace_provider_locations')[0]['n'] >= 3


def test_plan_proposes_document_metadata(world):
    _, plan_id = scan_and_plan(world, world['corpus'] / 'documents')
    items = {i['name']: i for i in items_of(plan_id)}
    for name in ('Umowa.docx', 'Budzet.xlsx', 'Slajdy.pptx', 'Notatka.odt', 'prosty.pdf', 'strumien.pdf'):
        assert items[name]['state']['importer'] == 'document_metadata', name
    approve(plan_id, lambda i: i['state']['importer'] == 'document_metadata')
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work')
    assert {o['status'] for o in result['items']} == {'done'}, result['items']
    statuses = {r['status'] for r in rows('SELECT status FROM trace_document_metadata')}
    assert 'parsed' in statuses


def test_plan_keep_and_browser_profile_opt_in(world):
    _, plan_id = scan_and_plan(world, world['corpus'] / 'keep')
    keep = {i['name']: i for i in items_of(plan_id)}
    assert {n for n, i in keep.items() if i['state']['importer'] == 'google_keep'} == {
        'Zakupy.json', 'Pomysł.json', 'Kosz.json', 'Jednostki.json'}
    _, plan_id = scan_and_plan(world, world['corpus'] / 'browsers', label='Laptop (synthetic)')
    items = [i for i in items_of(plan_id) if i['state']['importer'] == 'browser_profile']
    assert sorted(i['name'] for i in items) == ['History', 'History', 'places.sqlite']
    assert all('opt_in_disabled:enable_browser_history' in i['blockers'] for i in items)
    approve(plan_id, lambda i: i['state']['importer'] == 'browser_profile' and 'chrome-copy' in i['locator'],
            opt_ins={'enable_browser_history': True})
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work')
    assert [o['status'] for o in result['items']] == ['done']
    assert rows("SELECT COUNT(*) AS n FROM trace_browser_visits WHERE reading_key='immutable'")[0]['n'] == 10


def test_sidecar_rerun_after_images_appends_a_new_match(world):
    root = Path(world['m']['photos']['root'])
    _, plan_id = scan_and_plan(world, root)
    exact = lambda i: i['name'] == 'IMG_20210101_101500.jpg.json' and 'Photos from 2021' in i['locator']  # noqa: E731
    approve(plan_id, exact)
    execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work')
    approve(plan_id, lambda i: i['name'] == 'IMG_20210101_101500.jpg' and 'Photos from 2021' in i['locator'])
    execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work')
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work', rerun_done=True)
    assert {o['status'] for o in result['items']} == {'done'}, result['items']
    matches = rows('SELECT * FROM trace_photo_matches ORDER BY id')
    assert len(matches) == 2
    first, second = (json.loads(m['candidates_json'])[0] for m in matches)
    assert first['artifact_id'] is None and second['artifact_id'] is not None
    from server.app.traces.geotags import sidecar_media_sha256
    with dbmod.session() as db:
        assert sidecar_media_sha256(db, matches[1]['sidecar_id']) == world['m']['photos']['sha256']['a']

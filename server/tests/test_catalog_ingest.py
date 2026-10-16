"""Ingest from the catalog: explicit acquisition with re-verified bytes and links."""
from __future__ import annotations

import hashlib

import pytest

from server.app import db as dbmod
from server.app.catalog import ingest, store
from server.app.evidence import IntegrityError
from server.tests.test_catalog_scan import entries_by_locator, make_world, tree_state


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def _ids(world, *locators, scan='scan_a'):
    entries = entries_by_locator(getattr(world, scan)['scan_id'])
    return [entries[locator]['id'] for locator in locators]


def test_ingest_files_and_nested_members_with_provenance(world):
    expected = world.expected
    locators = ['docs/report.pdf', expected['zip_depth3'], expected['tar_zip_member'], expected['gzip_member'],
                expected['xz_member'], 'archives/truncated.zip!/first.txt', 'archives/evil.zip!/dup.txt!2',
                'archives/evil.zip!/caf%82.txt']
    result = ingest.ingest_entries(_ids(world, *locators), temp_dir=world.tmp / 'work')
    assert result['ingested'] == len(locators)
    with dbmod.session() as db:
        for locator, item in zip(locators, result['results']):
            artifact = db.execute('SELECT * FROM artifacts WHERE id=?', (item['artifact_id'],)).fetchone()
            entry = db.execute('SELECT * FROM catalog_entries WHERE locator=? AND scan_id=?', (locator, world.scan_a['scan_id'])).fetchone()
            assert artifact['sha256'] == entry['sha256'] and artifact['original_name'] == entry['name']
            assert artifact['source_locator'] == f'catalog:collection/{world.a["id"]}/{locator}'
            observation = db.execute('SELECT * FROM source_observations WHERE id=?', (item['observation_id'],)).fetchone()
            assert observation['collection_id'] == world.a['id'] and observation['acquisition_kind'] == 'ingest'
            stored = db.execute('SELECT stored_path FROM artifacts WHERE id=?', (item['artifact_id'],)).fetchone()[0]
            assert hashlib.sha256(open(stored, 'rb').read()).hexdigest() == entry['sha256']
    assert tree_state(world.corpus) == world.before
    assert not list((world.tmp / 'work').iterdir())


def test_same_bytes_from_two_locations_keep_both_artifacts_and_observations(world):
    shared = world.expected['shared_pdf']
    disk = _ids(world, shared['locations'][0][1])
    nested = _ids(world, shared['locations'][2][1], scan='scan_b')
    first = ingest.ingest_entries(disk + nested, temp_dir=world.tmp / 'work')['results']
    assert first[0]['artifact_id'] != first[1]['artifact_id']
    repeat = ingest.ingest_entries(disk, temp_dir=world.tmp / 'work')['results'][0]
    assert repeat['artifact_id'] == first[0]['artifact_id'] and repeat['observation_id'] != first[0]['observation_id']
    with dbmod.session() as db:
        group_id = db.execute('SELECT id FROM catalog_content_groups WHERE sha256=?', (shared['sha256'],)).fetchone()[0]
    group = store.get_group(group_id)
    assert len(group['artifacts']) == 2 and len(group['acquisition_observations']) == 3
    assert {o['collection_id'] for o in group['acquisition_observations']} == {world.a['id'], world.b['id']}
    links = store.get_entry(disk[0])['ingested']
    assert [l['artifact_id'] for l in links] == [first[0]['artifact_id']] * 2


def test_entries_without_verified_bytes_are_not_ingested(world):
    expected = world.expected
    ids = _ids(world, expected['encrypted_member'], expected['bomb_member'], expected['crc_member'], 'loop')
    result = ingest.ingest_entries(ids, temp_dir=world.tmp / 'work')
    assert result['ingested'] == 0 and {r['status'] for r in result['results']} == {'not_ingestible'}
    with dbmod.session() as db:
        assert db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0] == 0


def test_changed_source_is_refused(world):
    entry_id = _ids(world, 'notes/todo.txt')[0]
    (world.corpus / 'phone_a' / 'notes' / 'todo.txt').write_bytes(b'edited after the scan')
    with pytest.raises(IntegrityError, match='changed since the catalog scan'):
        ingest.ingest_entries([entry_id], temp_dir=world.tmp / 'work')
    with dbmod.session() as db:
        assert db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM catalog_entry_artifacts').fetchone()[0] == 0


def test_ingest_temp_dir_must_be_outside_the_source(world):
    entry_id = _ids(world, 'docs/report.pdf')[0]
    with pytest.raises(ValueError, match='outside'):
        ingest.ingest_entries([entry_id], temp_dir=world.corpus / 'phone_a' / 'tmp')
    assert tree_state(world.corpus) == world.before
    with pytest.raises(store.NotFound):
        ingest.ingest_entries([10_000_000])

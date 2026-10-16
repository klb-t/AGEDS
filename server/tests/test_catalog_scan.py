"""Catalog scan acceptance on the hard synthetic corpus (AGEDS-TASK-20261016-A)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.app import db as dbmod
from server.app import evidence
from server.app.catalog import coverage, store
from server.app.catalog.scan import CatalogLimits, run_catalog_scan
from server.tests.corpus import catalog_files


def tree_state(root: Path) -> dict:
    """Bytes, sizes, modes and mtimes of every node; symlinks are read, never followed."""
    state = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames + ['.']:
            path = Path(directory) / name if name != '.' else Path(directory)
            st = os.lstat(path)
            rel = os.fsencode(path.relative_to(root)) if path != root else b'.'
            if stat.S_ISLNK(st.st_mode):
                state[rel] = ('link', os.readlink(path), st.st_mtime_ns)
            elif stat.S_ISREG(st.st_mode):
                state[rel] = ('file', hashlib.sha256(path.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns, st.st_mode)
            else:
                state[rel] = ('dir', st.st_mtime_ns, st.st_mode)
    return state


def make_world(tmp_path: Path, monkeypatch, *, scan: bool = True, limits: CatalogLimits | None = None) -> SimpleNamespace:
    data = tmp_path / 'data'
    data.mkdir(exist_ok=True)
    settings = replace(dbmod.settings, data_dir=data, db_path=data / 'evidence.db', store_dir=data / 'store',
                       scan_roots=(tmp_path / 'corpus',))
    for module in (dbmod, evidence):
        monkeypatch.setattr(module, 'settings', settings)
    dbmod.init_db()
    expected = catalog_files.build(tmp_path / 'corpus')
    world = SimpleNamespace(tmp=tmp_path, settings=settings, expected=expected, temp=tmp_path / 'spool',
                            corpus=tmp_path / 'corpus')
    world.before = tree_state(world.corpus)
    world.a = store.create_collection('Phone A (synthetic)', device_or_account='phone-a', export_date_raw='2026-09',
                                      acquisition_method='file copy', operator='tester', priority=2)
    world.b = store.create_collection('Drive B (synthetic)', device_or_account='drive-b', acquisition_method='takeout')
    world.collection = {'phone_a': world.a['id'], 'drive_b': world.b['id']}
    if scan:
        world.scan_a = run_catalog_scan(expected['roots']['phone_a'], world.a['id'], limits=limits, temp_dir=world.temp)
        world.scan_b = run_catalog_scan(expected['roots']['drive_b'], world.b['id'], limits=limits, temp_dir=world.temp)
    return world


def entries_by_locator(scan_id: int) -> dict:
    with dbmod.session() as db:
        return {r['locator']: dict(r) for r in db.execute('SELECT * FROM catalog_entries WHERE scan_id=?', (scan_id,))}


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def test_originals_unchanged_and_nothing_written_to_sources(world):
    assert tree_state(world.corpus) == world.before
    run_catalog_scan(world.expected['roots']['phone_a'], world.a['id'], temp_dir=world.temp)
    assert tree_state(world.corpus) == world.before
    with dbmod.session() as db:
        observations = db.execute('SELECT COUNT(*) FROM source_observations').fetchone()[0]
        artifacts = db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]
    assert observations == 0 and artifacts == 0, 'a catalog scan is not an acquisition'


def test_same_bytes_three_places_two_collections_keep_every_observation(world):
    shared = world.expected['shared_pdf']
    with dbmod.session() as db:
        group = db.execute('SELECT id FROM catalog_content_groups WHERE sha256=?', (shared['sha256'],)).fetchone()['id']
    detail = store.get_group(group)
    seen = {(e['collection_id'], e['locator'], e['depth']) for e in detail['catalog_entries']}
    assert seen == {(world.collection[c], loc, depth) for c, loc, depth in shared['locations']}
    listed = store.list_groups()['groups']
    assert any(g['id'] == group and g['entries'] == 3 and g['collections'] == 2 and g['max_depth'] == 2 for g in listed)
    duplicates = store.list_entries({'duplicates_only': True, 'all_scans': False})['entries']
    assert {e['locator'] for e in duplicates} >= {loc for _, loc, _ in shared['locations']}


def test_signature_wins_over_extension_and_conflict_is_kept(world):
    entries = entries_by_locator(world.scan_a['scan_id'])
    for locator, (sniffed, relation) in world.expected['type_conflicts'].items():
        entry = entries[locator]
        assert (entry['sniffed_type'], entry['type_relation'], entry['type_conflict']) == (sniffed, relation, 1), locator
        assert entry['extension'] == locator.rsplit('.', 1)[1]
    for locator, sniffed in world.expected['type_matches'].items():
        entry = entries[locator]
        assert entry['sniffed_type'] == sniffed and entry['type_conflict'] == 0, (locator, entry['sniffed_type'], entry['type_relation'])
    assert entries['docs/letter.docx']['sniff_basis'] == 'container_members'
    assert entries['data/table.csv']['sniff_basis'] == 'heuristic'
    assert json.loads(entries['docs/legacy.doc']['details_json'])['cfb']['cfb_streams_seen'][:2] == ['Root Entry', 'Workbook']


def test_recursive_archives_and_virtual_locators(world):
    expected = world.expected
    entries = entries_by_locator(world.scan_a['scan_id'])
    deep = entries[expected['zip_depth3']]
    assert deep['depth'] == 4 and deep['status'] == 'ok' and deep['sha256'] == hashlib.sha256(b'three zip levels down').hexdigest()
    chain = [c['locator'] for c in store.get_entry(deep['id'])['containers']]
    assert chain == ['archives/takeout.zip!/Takeout/level1.zip!/level2.zip!/level3.zip',
                     'archives/takeout.zip!/Takeout/level1.zip!/level2.zip', 'archives/takeout.zip!/Takeout/level1.zip',
                     'archives/takeout.zip']
    assert entries[expected['tar_zip_member']]['depth'] == 2
    assert entries['archives/bundle.tar.gz']['sniffed_type'] == 'tar.gz'
    link = entries[expected['tar_symlink_member']]
    assert link['status'] == 'skipped' and json.loads(link['details_json'])['link_target'] == '../../etc/passwd'
    for key in ('gzip_member', 'xz_member', 'bz2_member'):
        assert entries[expected[key]]['status'] == 'ok', key
    gz_times = store.get_entry(entries[expected['gzip_member']]['id'])['times']
    assert [(t['source'], t['tz_status']) for t in gz_times] == [('gzip_header', 'utc_epoch')]


def test_member_names_are_preserved_as_data(world):
    entries = entries_by_locator(world.scan_a['scan_id'])
    for locator, flags in world.expected['evil_members'].items():
        entry = entries[locator]
        assert set(flags) <= set(json.loads(entry['name_flags_json'])), (locator, entry['name_flags_json'])
        assert entry['status'] == 'ok'
    cp437 = entries['archives/evil.zip!/caf%82.txt']
    assert cp437['name'] == world.expected['cp437_display'] and cp437['name_raw_hex'] == b'caf\x82.txt'.hex()
    unflagged = entries['archives/evil.zip!/zażółć.txt']
    assert unflagged['name'] == world.expected['unflagged_utf8_display'] and unflagged['name_encoding'] == 'utf-8 (unflagged)'
    first, second = entries['archives/evil.zip!/dup.txt'], entries['archives/evil.zip!/dup.txt!2']
    assert first['sha256'] != second['sha256'] and second['member_index'] > first['member_index']
    disk = entries[world.expected['non_utf8_disk_name']]
    assert disk['name_raw_hex'] == b'bad\xffname.txt'.hex() and 'non_utf8' in json.loads(disk['name_flags_json'])
    assert not (world.corpus / 'evil.txt').exists() and not (world.corpus.parent / 'evil.txt').exists()


def test_damage_encryption_unsupported_and_ratio_are_visible(world):
    expected = world.expected
    entries = entries_by_locator(world.scan_a['scan_id'])
    secret = entries[expected['encrypted_member']]
    assert (secret['status'], secret['hash_status'], secret['sha256']) == ('encrypted', 'unavailable_encrypted', None)
    assert entries['archives/locked.zip!/plain.txt']['status'] == 'ok'
    truncated = entries[expected['truncated_zip']]
    assert truncated['status'] == 'damaged' and truncated['issue_code'] == 'zip_central_directory_unreadable'
    assert entries['archives/truncated.zip!/first.txt']['status'] == 'ok'
    assert entries['archives/truncated.zip!/first.txt']['sha256'] == hashlib.sha256(b'A' * 300).hexdigest()
    assert entries['archives/truncated.zip!/second.txt']['issue_code'] == 'member_data_truncated'
    assert entries[expected['crc_member']]['status'] == 'damaged'
    for locator in expected['adapter_unavailable']:
        assert (entries[locator]['status'], entries[locator]['issue_code']) == ('unsupported', 'adapter_unavailable')
        assert entries[locator]['sha256'] is not None  # the container bytes themselves are still hashed
    bomb = entries[expected['bomb_member']]
    assert bomb['status'] == 'limit_exceeded' and bomb['issue_code'] == 'compression_ratio_declared' and bomb['sha256'] is None
    assert world.scan_a['status'] == 'partial'
    reasons = world.scan_a['coverage']['incomplete_reasons']
    assert reasons['limit:compression_ratio_declared'] == 1 and reasons['container_not_expanded'] == 2
    assert reasons['encrypted_content'] == 1


def test_times_keep_kind_source_and_zone_status(world):
    entries = entries_by_locator(world.scan_a['scan_id'])
    disk = store.get_entry(entries['docs/report.pdf']['id'])['times']
    assert [(t['kind'], t['source'], t['tz_status'], t['utc']) for t in disk] == [
        ('file_mtime', 'filesystem_stat', 'utc_epoch', '2024-01-07T12:00:00Z')]
    member = store.get_entry(entries['archives/takeout.zip!/Takeout/Drive/report.pdf']['id'])['times']
    assert [(t['source'], t['tz_status'], t['local'], t['utc']) for t in member] == [
        ('zip_dos_datetime', 'unknown', world.expected['zip_dos_local'], None)]
    tar = store.get_entry(entries['archives/bundle.tar.gz!/bundle/readme.txt']['id'])['times']
    assert [(t['source'], t['tz_status'], t['utc']) for t in tar] == [('tar_header', 'utc_epoch', world.expected['tar_mtime_utc'])]


def test_symlinks_are_recorded_and_never_followed(world):
    entries = entries_by_locator(world.scan_a['scan_id'])
    for name in world.expected['symlinks']:
        assert entries[name]['status'] == 'skipped' and entries[name]['issue_code'] == 'symlink_skipped'
    assert json.loads(entries['outside-link']['details_json'])['link_target'] == world.expected['outside_target']
    assert not any('private-looking' in locator for locator in entries)
    assert not any(locator.startswith(('loop/', 'link-to-docs/')) for locator in entries)


def test_empty_files_form_a_flagged_group(world):
    entries = entries_by_locator(world.scan_a['scan_id'])
    first, second = (entries[p] for p in world.expected['empty_files'])
    assert first['content_group_id'] == second['content_group_id'] and first['size_bytes'] == 0
    assert first['content_group_id'] not in {g['id'] for g in store.list_groups()['groups']}
    flagged = [g for g in store.list_groups(include_empty=True)['groups'] if g['id'] == first['content_group_id']]
    assert flagged and flagged[0]['empty_content'] is True


def test_filename_collisions_with_different_bytes(world):
    collisions = {c['name']: c for c in store.list_collisions()['collisions']}
    for name, variants in world.expected['collision_names'].items():
        assert collisions[name]['variants'] == variants
    assert collisions['todo.txt']['collections'] == 2
    only_b = {c['name'] for c in store.list_collisions(collection_id=world.b['id'])['collisions']}
    assert 'todo.txt' in only_b and 'same-name.txt' not in only_b


def test_rescan_is_a_new_scan_and_groups_are_not_duplicated(world):
    with dbmod.session() as db:
        groups_before = db.execute('SELECT COUNT(*) FROM catalog_content_groups').fetchone()[0]
        old_entries = db.execute('SELECT COUNT(*) FROM catalog_entries WHERE scan_id=?', (world.scan_a['scan_id'],)).fetchone()[0]
    again = run_catalog_scan(world.expected['roots']['phone_a'], world.a['id'], temp_dir=world.temp)
    assert again['scan_id'] != world.scan_a['scan_id']
    with dbmod.session() as db:
        assert db.execute('SELECT COUNT(*) FROM catalog_content_groups').fetchone()[0] == groups_before
        assert db.execute('SELECT COUNT(*) FROM catalog_entries WHERE scan_id=?', (world.scan_a['scan_id'],)).fetchone()[0] == old_entries
        assert db.execute('SELECT COUNT(*) FROM catalog_entries WHERE scan_id=?', (again['scan_id'],)).fetchone()[0] == old_entries
        assert store.current_scan_ids(db, world.a['id']) == [again['scan_id']]
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE catalog_entries SET status='ok' WHERE scan_id=?", (world.scan_a['scan_id'],))
        with pytest.raises(sqlite3.DatabaseError):
            db.execute('DELETE FROM catalog_scans WHERE id=?', (world.scan_a['scan_id'],))
    group = store.list_groups()['groups'][0]
    assert group['entries'] == 3, 'current-scan view must not double count a re-scanned root'


def test_limits_produce_visible_partial_coverage(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, scan=False)
    root = world.expected['roots']['phone_a']

    def scan(**limits):
        result = run_catalog_scan(root, world.a['id'], limits=CatalogLimits(**limits), temp_dir=world.temp)
        assert result['status'] == 'partial'
        return result, entries_by_locator(result['scan_id'])

    result, entries = scan(max_members_per_archive=2, max_archive_depth=2)
    reasons = result['coverage']['incomplete_reasons']
    assert reasons['limit:max_members_per_archive'] >= 1 and reasons['limit:max_archive_depth'] >= 1
    evil = entries['archives/evil.zip']
    assert evil['status'] == 'limit_exceeded' and json.loads(evil['details_json'])['container']['members_listed'] == 2
    depth_capped = [e for e in entries.values() if e['issue_code'] == 'max_archive_depth']
    assert depth_capped and all(e['depth'] == 2 and e['is_container'] == 1 and e['sha256'] for e in depth_capped)
    assert not any(e['depth'] > 2 for e in entries.values())

    result, entries = scan(max_member_hash_bytes=20)
    assert result['coverage']['incomplete_reasons']['limit:max_member_hash_bytes'] >= 1
    capped = [e for e in entries.values() if e['issue_code'] == 'max_member_hash_bytes']
    assert capped and all(e['sha256'] is None and e['hash_status'] == 'skipped_size_limit' for e in capped)
    assert entries['archives/takeout.zip!/Takeout/level1.zip']['status'] == 'limit_exceeded'

    result, entries = scan(max_files=10)
    assert result['coverage']['incomplete_reasons']['limit:file_limit'] == 1
    assert sum(1 for e in entries.values() if e['origin'] == 'filesystem' and e['status'] != 'skipped') == 10
    with dbmod.session() as db:
        issues = {r['code'] for r in db.execute('SELECT code FROM catalog_issues WHERE scan_id=?', (result['scan_id'],))}
    assert 'file_limit' in issues

    result, entries = scan(max_compression_ratio=2, ratio_check_min_bytes=1024, max_expanded_bytes=10_000_000)
    ratio = [e for e in entries.values() if e['issue_code'] in ('compression_ratio', 'compression_ratio_declared')]
    assert ratio and all(e['status'] == 'limit_exceeded' for e in ratio)

    result, entries = scan(max_expanded_bytes=20_000)
    assert result['coverage']['incomplete_reasons']['limit:max_expanded_bytes'] >= 1
    # One bounded read (at most one sniff prefix) may overshoot before the check stops reading.
    assert result['coverage']['expanded_bytes_read'] <= 20_000 + 64 * 1024
    assert entries['archives/takeout.zip!/Takeout/Drive/report.pdf']['issue_code'] == 'max_expanded_bytes'
    report = coverage.coverage_report(world.a['id'])
    assert report['collections'][0]['totals']['limit_exceeded'] >= 1
    assert report['collections'][0]['scans'][0]['complete'] is False
    assert tree_state(world.corpus) == world.before


def test_coverage_gaps_never_mix_time_bases(world):
    gap = world.expected['gap']
    report = coverage.coverage_report(world.collection[gap['collection']], min_gap_days=30)
    collection = report['collections'][0]
    types = {t['type']: t for t in collection['types']}
    bases = {(b['source'], b['tz_status']): b for b in types[gap['type']]['time_bases']}
    fs = bases[(gap['source'], 'utc_epoch')]
    assert [(g['last_observed_day'], g['next_observed_day']) for g in fs['gaps']] == [(gap['last_before'], gap['first_after'])]
    assert fs['gaps'][0]['empty_days'] >= gap['min_days']
    dos = bases[('zip_dos_datetime', 'unknown')]
    assert dos['value_field'] == 'local' and dos['first'].startswith('2023-06-01T10:00:00') and dos['gaps'] == []
    assert coverage.coverage_report(world.a['id'], min_gap_days=400)['collections'][0]['totals']['gaps'] == 0
    assert types['pdf']['entries'] == 3 and types['7z']['by_status']['unsupported'] == 1
    assert report['parameters']['min_gap_days'] == 30 and 'gap' in report['method']
    json.dumps(report, allow_nan=False)
    with pytest.raises(ValueError):
        coverage.coverage_report(world.a['id'], min_gap_days=0)


def test_single_archive_file_as_root(world):
    archive = Path(world.expected['roots']['phone_a']) / 'archives' / 'takeout.zip'
    result = run_catalog_scan(archive, world.b['id'], temp_dir=world.temp)
    entries = entries_by_locator(result['scan_id'])
    assert 'takeout.zip' in entries and 'takeout.zip!/Takeout/Drive/report.pdf' in entries
    assert result['status'] == 'completed'
    assert tree_state(world.corpus) == world.before


def test_metadata_and_temp_locations_must_be_outside_sources(world, monkeypatch):
    root = Path(world.expected['roots']['phone_a'])
    with pytest.raises(ValueError, match='temporary directory'):
        run_catalog_scan(root, world.a['id'], temp_dir=root / 'tmp')
    monkeypatch.setattr(dbmod, 'settings', replace(world.settings, store_dir=root / 'store'))
    with pytest.raises(ValueError, match='content store'):
        run_catalog_scan(root, world.a['id'], temp_dir=world.temp)
    assert tree_state(world.corpus) == world.before
    with pytest.raises(ValueError, match='symlink'):
        run_catalog_scan(root / 'link-to-docs', world.a['id'])


def test_collection_edits_keep_previous_values_in_audit(world):
    updated = store.update_collection(world.a['id'], {'notes': 'checked once', 'priority': 5})
    updated = store.update_collection(world.a['id'], {'notes': 'checked twice'})
    assert updated['notes'] == 'checked twice' and updated['priority'] == 5
    changes = [h['details'] for h in updated['history'] if h['action'] == 'collection_updated']
    assert changes[0] == {'previous': {'notes': 'checked once'}, 'new': {'notes': 'checked twice'}}
    assert changes[1]['previous'] == {'notes': None, 'priority': 2}
    child = store.create_collection('Child export', parent_collection_id=world.a['id'])
    with pytest.raises(ValueError, match='cycle'):
        store.update_collection(world.a['id'], {'parent_collection_id': child['id']})
    with pytest.raises(ValueError):
        store.update_collection(world.a['id'], {'label': '  '})
    with pytest.raises(ValueError):
        store.update_collection(world.a['id'], {'case_id': 2})
    tree = store.list_collections()
    assert [c['id'] for c in tree][:2] == [world.a['id'], child['id']] and tree[1]['tree_depth'] == 1


def test_marks_are_append_only_and_survive_rescan(world):
    store.add_mark(world.a['id'], 'docs/report.pdf', priority=1, note='first look')
    store.add_mark(world.a['id'], 'docs/report.pdf', priority=3, note='important')
    run_catalog_scan(world.expected['roots']['phone_a'], world.a['id'], temp_dir=world.temp)
    page = store.list_entries({'collection_id': world.a['id'], 'q': 'docs/report.pdf'})
    assert page['entries'][0]['mark']['priority'] == 3
    entry = store.get_entry(page['entries'][0]['id'])
    assert [m['note'] for m in entry['mark_history']] == ['important', 'first look']
    with pytest.raises(store.NotFound):
        store.add_mark(world.a['id'], 'does/not/exist', priority=1)


def test_entry_pages_are_bounded_and_filterable(world):
    page = store.list_entries({'collection_id': world.a['id']}, limit=10)
    assert len(page['entries']) == 10 and page['has_more'] and page['next_after_id'] == page['entries'][-1]['id']
    following = store.list_entries({'collection_id': world.a['id']}, after_id=page['next_after_id'], limit=10)
    assert following['entries'][0]['id'] > page['next_after_id']
    assert store.list_entries(limit=10_000)['limit'] == store.MAX_PAGE
    assert {e['status'] for e in store.list_entries({'status': 'encrypted'})['entries']} == {'encrypted'}
    assert all(e['type_conflict'] for e in store.list_entries({'conflict': True})['entries'])
    assert {e['effective_type'] for e in store.list_entries({'type': 'pdf'})['entries']} == {'pdf'}
    with pytest.raises(ValueError):
        store.list_entries({'status': 'bogus'})

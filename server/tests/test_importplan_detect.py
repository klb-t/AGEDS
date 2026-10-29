"""Import plan generation and review on a multi-device synthetic layout (AGEDS-TASK-20261016-I)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.app import db as dbmod
from server.app import evidence
from server.app.catalog import store
from server.app.catalog.scan import run_catalog_scan
from server.app.importplan import abandon, plan
from server.app.importplan.detect import DEFAULT_DETECTORS, PlanLimits
from server.tests.corpus import import_layout
from server.tests.test_catalog_scan import tree_state

LABELS = {'phone_a': 'Telefon A (synthetic)', 'drive_b': 'Dysk B / Takeout (synthetic)', 'drive_c': 'Dysk C (kopia, synthetic)'}


def make_world(tmp_path: Path, monkeypatch, *, scan: bool = True) -> SimpleNamespace:
    data = tmp_path / 'data'
    data.mkdir(parents=True, exist_ok=True)
    settings = replace(dbmod.settings, data_dir=data, db_path=data / 'evidence.db', store_dir=data / 'store',
                       scan_roots=(tmp_path / 'corpus',))
    for module in (dbmod, evidence):
        monkeypatch.setattr(module, 'settings', settings)
    dbmod.init_db()
    expected = import_layout.build(tmp_path / 'corpus')
    world = SimpleNamespace(tmp=tmp_path, expected=expected, corpus=tmp_path / 'corpus', work=tmp_path / 'work',
                            spool=tmp_path / 'spool', settings=settings)
    world.before = tree_state(world.corpus)
    world.collections = {key: store.create_collection(label, device_or_account=key)['id'] for key, label in LABELS.items()}
    world.key_of = {v: k for k, v in world.collections.items()}
    if scan:
        world.scans = {key: run_catalog_scan(expected['roots'][key], world.collections[key], temp_dir=world.spool)
                       for key in LABELS}
    return world


def new_plan(world, **kwargs) -> dict:
    kwargs.setdefault('collection_ids', list(world.collections.values()))
    kwargs.setdefault('temp_dir', world.work)
    return plan.create_plan(actor='tester', **kwargs)


def all_items(plan_id: int, **kwargs) -> list[dict]:
    return plan.get_plan(plan_id, limit=500, **kwargs)['items']


def by_locator(world, items) -> dict:
    return {(world.key_of[i['collection_id']], i['locator']): i for i in items}


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def test_plan_contents_are_exact_and_ordered(world):
    created = new_plan(world)
    plan_id = created['plan']['id']
    items = all_items(plan_id)
    observed = [(world.key_of[i['collection_id']], i['unit_kind'], i['locator'], i['state']['importer'], i['blockers'])
                for i in items]
    expected = [(key, kind, locator, importer, blockers)
                for key in LABELS for kind, locator, importer, blockers in world.expected['expected_items'][key]]
    assert observed == expected
    assert [i['ordinal'] for i in items] == list(range(1, len(items) + 1))
    assert all(i['status'] == 'proposed' for i in items)
    header = created['plan']
    assert header['detector_schema'] == 'ageds.importplan-detectors' and header['detector_version'] == '2'
    assert len(header['detector_sha256']) == 64 and header['latest_version'] == 1
    assert sorted(header['scan_ids']) == sorted(s['scan_id'] for s in world.scans.values())
    found = by_locator(world, items)
    takeout = [i for (key, _), i in found.items() if key == 'drive_b' and i['locator'].startswith('takeout-')]
    assert takeout and all(i['context'] == ['google_takeout'] for i in takeout)
    assert found[('phone_a', 'backup/calls-20240107.xml')]['candidates'][1]['importer'] == 'sms_backup'
    assert found[('phone_a', 'backup/calls-20240107.xml')]['candidates'][1]['preference'] == 'alternative'
    assert not found[('phone_a', 'backup/calls-20240107.xml')]['ambiguous']


def test_chat_export_units_cover_their_members(world):
    plan_id = new_plan(world)['plan']['id']
    found = by_locator(world, all_items(plan_id))
    zip_item = plan.get_item(plan_id, found[('drive_b', 'chatgpt-export-2025-03.zip')]['id'])
    roles = {m['relative_path']: m['role'] for m in zip_item['members']}
    assert roles == {'conversations-000.json': 'conversations', 'conversations-001.json': 'conversations',
                     'user.json': 'attachment_candidate', 'file-DAT1.dat': 'attachment_candidate',
                     'file-ATTACH1-notes.txt': 'attachment_candidate'}
    dat = next(m for m in zip_item['members'] if m['relative_path'] == 'file-DAT1.dat')
    assert dat['sniffed_type'] == 'pdf' and dat['sha256'] == world.expected['dat']['sha256']
    assert zip_item['detection']['basis'] == 'structure_marker'
    assert zip_item['detection']['materialize'] == 'members'
    claude = found[('drive_b', 'exports/claude')]
    assert [c['importer'] for c in claude['candidates']] == ['claude_export']
    whatsapp = plan.get_item(plan_id, found[('phone_a', 'WhatsApp/folder_export')]['id'])
    assert {m['role'] for m in whatsapp['members']} == {'chat_text', 'media_candidate'}
    assert whatsapp['chat_members'] == ['_chat.txt'] and whatsapp['state']['parameters'] == {'chat_member': '_chat.txt'}
    # member entries of units are never offered to single-file rules (no EXIF item for the WhatsApp photo)
    assert not any('PHOTO' in locator for _, locator in found)


def test_ambiguous_items_are_never_auto_chosen_or_bulk_approved(world):
    plan_id = new_plan(world)['plan']['id']
    item = by_locator(world, all_items(plan_id))[world.expected['ambiguous']]
    assert item['ambiguous'] and item['state']['importer'] is None and item['default_importer'] is None
    assert sorted(c['importer'] for c in item['candidates']) == ['chatgpt_export', 'claude_export']
    assert all(c['reasons'] for c in item['candidates'])
    result = plan.decide(plan_id, base_version=1, bulk={'action': 'approve'}, actor='tester')
    assert result['version'] == 2
    refused = {n['item_id']: n['blockers'] for n in result['not_applied']}
    assert refused[item['id']] == ['importer_not_chosen:ambiguous_detection']
    assert by_locator(world, all_items(plan_id))[world.expected['ambiguous']]['status'] == 'proposed'
    with pytest.raises(ValueError, match='cannot be approved'):
        plan.decide(plan_id, base_version=2, decisions=[{'item_id': item['id'], 'status': 'approved'}], actor='tester')
    with pytest.raises(ValueError, match='not a candidate'):
        plan.decide(plan_id, base_version=2, decisions=[{'item_id': item['id'], 'importer': 'email_mbox'}], actor='tester')
    chosen = plan.decide(plan_id, base_version=2, actor='tester',
                         decisions=[{'item_id': item['id'], 'status': 'approved', 'importer': 'claude_export'}])
    assert chosen['version'] == 3
    assert by_locator(world, all_items(plan_id))[world.expected['ambiguous']]['status'] == 'approved'


def test_opt_in_items_are_proposed_but_disabled_until_enabled(world):
    plan_id = new_plan(world)['plan']['id']
    found = by_locator(world, all_items(plan_id))
    for key in world.expected['opt_in_items']:
        item = found[key]
        assert item['status'] == 'proposed'
        assert set(item['state']['opt_ins'].values()) == {False}
        assert any(b.startswith('opt_in_disabled:') for b in item['blockers'])
    location = found[('drive_b', 'takeout-20241001.zip!/Takeout/Location History/Records.json')]
    with pytest.raises(ValueError, match='opt_in_disabled:enable_location'):
        plan.decide(plan_id, base_version=1, decisions=[{'item_id': location['id'], 'status': 'approved'}], actor='tester')
    with pytest.raises(ValueError, match='unknown opt-in'):
        plan.decide(plan_id, base_version=1, actor='tester',
                    decisions=[{'item_id': location['id'], 'opt_ins': {'enable_everything': True}}])
    plan.decide(plan_id, base_version=1, actor='tester', decisions=[
        {'item_id': location['id'], 'status': 'approved', 'opt_ins': {'enable_location': True},
         'parameters': {'coarse_decimals': 1}}])
    after = by_locator(world, all_items(plan_id))[('drive_b', 'takeout-20241001.zip!/Takeout/Location History/Records.json')]
    assert after['status'] == 'approved' and after['state']['opt_ins'] == {'enable_location': True}
    assert after['state']['parameters'] == {'coarse_decimals': 1}
    # My Activity keeps browsing records off unless its optional opt-in is set
    gemini = found[('drive_b', 'takeout-20241001.zip!/Takeout/takeout-inner.zip!/My Activity/Gemini Apps/MyActivity.json')]
    assert gemini['state']['opt_ins'] == {'enable_browser_history': False} and not gemini['blockers']


def test_billing_needs_an_explicit_profile(world):
    plan_id = new_plan(world)['plan']['id']
    item = by_locator(world, all_items(plan_id))[('phone_a', 'billing/bill_daymonth.csv')]
    assert item['candidates'][0]['profiles_matching_header'] == ['example_semicolon_daymonth_v1']
    assert item['blockers'] == ['parameter_missing:profile_id']
    with pytest.raises(ValueError, match='billing profile not found'):
        plan.decide(plan_id, base_version=1, actor='tester',
                    decisions=[{'item_id': item['id'], 'parameters': {'profile_id': 'no-such-profile'}}])
    second = new_plan(world, options={'billing_profile_id': 'example_semicolon_daymonth_v1'})['plan']['id']
    explicit = by_locator(world, all_items(second))[('phone_a', 'billing/bill_daymonth.csv')]
    assert explicit['state']['parameters'] == {'profile_id': 'example_semicolon_daymonth_v1'} and not explicit['blockers']
    with pytest.raises(ValueError, match='billing profile not found'):
        new_plan(world, options={'billing_profile_id': 'missing'})


def test_cross_export_awareness_before_import(world):
    created = new_plan(world)
    plan_id = created['plan']['id']
    found = by_locator(world, all_items(plan_id))
    ordinal = {key: found[key]['ordinal'] for key in found}
    cross = created['plan']['cross_export']
    b_zip, c_zip = ordinal[('drive_b', 'chatgpt-export-2025-03.zip')], ordinal[('drive_c', 'copy/chatgpt-export-2025-03.zip')]
    later = ordinal[('drive_c', 'later-export')]
    assert [g['item_ordinals'] for g in cross['identical_conversation_files']] == [[b_zip, c_zip]]
    shared = {s['conversation_id']: s['item_ordinals'] for s in cross['shared_conversations']}
    expected = world.expected['shared_conversations']['chatgpt']
    assert {k: len(v) for k, v in shared.items()} == expected
    assert shared[import_layout.C_ID] == sorted([b_zip, c_zip, later])
    assert cross['policy'].startswith('every occurrence is imported')
    assert created['summary']['cross_export']['shared_conversations_total'] == 3


def test_not_planned_entries_are_listed_with_reasons(world):
    created = new_plan(world)
    not_planned = created['plan']['coverage']['not_planned']
    reasons = {(s['locator'], reason) for reason, samples in not_planned['samples'].items() for s in samples}
    for key, mapping in world.expected['not_planned'].items():
        for locator, reason in mapping.items():
            assert (locator, reason) in reasons
    assert created['plan']['coverage']['complete'] is True
    assert not_planned['counts']['covered_by_unit'] > 0
    assert created['summary']['not_planned'] == not_planned['counts']


def test_versions_are_append_only_and_conflicts_are_refused(world):
    plan_id = new_plan(world)['plan']['id']
    items = all_items(plan_id)
    first = items[0]
    plan.decide(plan_id, base_version=1, decisions=[{'item_id': first['id'], 'status': 'skipped', 'reason': 'not needed'}],
                actor='tester', note='skip one')
    with pytest.raises(plan.Conflict):
        plan.decide(plan_id, base_version=1, decisions=[{'item_id': first['id'], 'status': 'proposed'}], actor='other')
    assert all_items(plan_id, version=1)[0]['status'] == 'proposed'
    assert all_items(plan_id, version=2)[0]['status'] == 'skipped'
    unchanged = plan.decide(plan_id, base_version=2, decisions=[{'item_id': first['id'], 'status': 'skipped'}], actor='tester')
    assert unchanged == {'plan_id': plan_id, 'version': 2, 'changed': 0, 'not_applied': []}
    with dbmod.session() as db:
        for sql in ('UPDATE import_plans SET actor=?', 'UPDATE import_plan_items SET locator=?',
                    'UPDATE import_plan_decisions SET status=?', 'UPDATE import_plan_versions SET note=?'):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql, ('x',))
        for table in ('import_plans', 'import_plan_items', 'import_plan_item_members', 'import_plan_versions',
                      'import_plan_decisions'):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(f'DELETE FROM {table}')
        history = [r['action'] for r in db.execute("SELECT action FROM audit_log WHERE object_kind='import_plan' ORDER BY id")]
    assert history == ['import_plan_created', 'import_plan_decided']
    detail = plan.get_item(plan_id, first['id'])
    assert [h['status'] for h in detail['decision_history']] == ['skipped']


def test_summary_is_a_dry_run_without_writes(world):
    plan_id = new_plan(world)['plan']['id']
    plan.decide(plan_id, base_version=1, bulk={'action': 'approve'}, actor='tester')

    def counts():
        with dbmod.session() as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'search_fts%'")]
            return {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    before = counts()
    summary = plan.summary(plan_id)
    assert counts() == before
    assert summary['to_run']['items'] == summary['by_status']['approved'] > 0
    assert summary['to_run']['estimated_bytes'] > 0
    assert summary['blocked']['opt_in_disabled:enable_location'] == 1
    assert summary['blocked']['opt_in_disabled:enable_browser_history'] == 2
    assert summary['ambiguous'] == 1 and summary['opt_in_disabled'] == 3
    assert summary['by_importer']['recording_names']['approved'] == 4
    with dbmod.session() as db:
        assert db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0


def test_custom_detector_table_makes_overlapping_rules_ambiguous(world, tmp_path):
    table = json.loads(DEFAULT_DETECTORS.read_text(encoding='utf-8'))
    table['version'] = 'test-overlap'
    table['files'].append({'id': 'test_overlap', 'types': ['ics'],
                           'candidates': [{'importer': 'vcf', 'reason': 'deliberately overlapping test rule'}]})
    path = tmp_path / 'detectors.test.json'
    path.write_text(json.dumps(table), encoding='utf-8')
    created = new_plan(world, detectors_path=path)
    assert created['plan']['detector_version'] == 'test-overlap'
    found = by_locator(world, all_items(created['plan']['id']))
    ics = found[('drive_b', 'takeout-20241001.zip!/Takeout/takeout-inner.zip!/Calendar/Example.ics')]
    assert ics['ambiguous'] and ics['state']['importer'] is None
    assert sorted(c['importer'] for c in ics['candidates']) == ['ics', 'vcf']


def test_planning_reads_only_and_refuses_temp_inside_sources(world):
    with pytest.raises(ValueError, match='outside every source root'):
        new_plan(world, temp_dir=world.corpus / 'phone_a' / 'tmp')
    assert not (world.corpus / 'phone_a' / 'tmp').exists()
    new_plan(world)
    assert tree_state(world.corpus) == world.before
    assert list(world.work.iterdir()) == []


def test_plan_limits_are_reported(world):
    created = new_plan(world, limits=PlanLimits(max_probe_bytes=1024))
    coverage = created['plan']['coverage']
    assert coverage['complete'] is False and 'limit:max_probe_bytes' in coverage['incomplete_reasons']
    with pytest.raises(ValueError):
        PlanLimits(max_entries=0)


def test_running_scan_cannot_be_planned_and_can_be_abandoned(world):
    with dbmod.session() as db:
        db.execute('''INSERT INTO catalog_scans(case_id,collection_id,root_path,root_kind,status,started_at,catalog_version,
                      signature_table_version,scanner_version,limits_json,policy_json) VALUES(1,?,?,?, 'running', ?, 'x','x','x','{}','{}')''',
                   (world.collections['phone_a'], str(world.corpus / 'phone_a'), 'directory', '2026-10-16T08:00:00+00:00'))
        scan_id = db.execute('SELECT MAX(id) FROM catalog_scans').fetchone()[0]
        db.execute('''INSERT INTO catalog_entries(scan_id,collection_id,locator,depth,origin,name,type_relation,hash_status,status)
                      VALUES(?,?,?,0,'disk','x.txt','match','skipped','skipped')''', (scan_id, world.collections['phone_a'], 'x.txt'))
    with pytest.raises(ValueError, match='only completed or partial'):
        new_plan(world, collection_ids=None, scan_ids=[scan_id])
    from datetime import datetime, timezone
    with pytest.raises(ValueError, match='may still be running'):
        abandon.abandon_scan(scan_id, actor='tester', reason='process killed', min_idle_seconds=10 ** 9)
    with pytest.raises(ValueError, match='reason'):
        abandon.abandon_scan(scan_id, actor='tester', reason=' ')
    result = abandon.abandon_scan(scan_id, actor='tester', reason='process killed', min_idle_seconds=60,
                                  now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert result['abandoned'] and result['entries_kept'] == 1
    with dbmod.session() as db:
        row = db.execute('SELECT status, error FROM catalog_scans WHERE id=?', (scan_id,)).fetchone()
        assert row['status'] == 'failed' and row['error'] == 'abandoned: process killed'
        assert db.execute('SELECT COUNT(*) FROM catalog_entries WHERE scan_id=?', (scan_id,)).fetchone()[0] == 1
        audit = db.execute("SELECT actor, details_json FROM audit_log WHERE action='catalog_scan_abandoned'").fetchone()
        assert audit['actor'] == 'tester' and json.loads(audit['details_json'])['entries_kept'] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM catalog_scan_abandonments')
    with pytest.raises(ValueError, match='not running'):
        abandon.abandon_scan(scan_id, actor='tester', reason='again', min_idle_seconds=0)
    assert abandon.abandonments()[0]['scan_id'] == scan_id

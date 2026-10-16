"""Execution of approved plan items through the real importers (AGEDS-TASK-20261016-I)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest

from server.app import db as dbmod
from server.app import jobs
from server.app.importplan import cross, execute, plan
from server.tests.corpus import import_layout
from server.tests.test_catalog_scan import tree_state
from server.tests.test_importplan_detect import all_items, by_locator, make_world, new_plan

COUNTED = ('artifacts', 'events', 'event_times', 'logical_records', 'logical_occurrences', 'record_exports',
           'record_export_files', 'record_export_parses', 'chat_attachment_refs', 'contact_observations',
           'contact_observation_values', 'recording_name_observations', 'event_media_references',
           'catalog_entry_artifacts')
BILLING = 'example_semicolon_daymonth_v1'


class SimulatedCrash(BaseException):
    """Stands for a killed process: not an Exception, so nothing records an outcome."""


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def approve_everything(plan_id: int, *, ambiguous_importer: str = 'chatgpt_export') -> dict:
    items = all_items(plan_id)
    decisions = []
    for item in items:
        decision = {'item_id': item['id'], 'status': 'approved', 'opt_ins': {k: True for k in item['state']['opt_ins']}}
        if item['ambiguous']:
            decision['importer'] = ambiguous_importer
        if item['state']['importer'] == 'billing':
            decision['parameters'] = {'profile_id': BILLING}
        decisions.append(decision)
    version = plan.get_plan(plan_id, limit=1)['version']
    return plan.decide(plan_id, base_version=version, decisions=decisions, actor='tester', note='approve all')


def counts() -> dict:
    with dbmod.session() as db:
        return {t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in COUNTED}


def duplicates() -> list:
    with dbmod.session() as db:
        return db.execute('''SELECT artifact_id, external_id, COUNT(*) FROM events WHERE external_id IS NOT NULL
                             GROUP BY artifact_id, external_id HAVING COUNT(*) > 1''').fetchall()


def prepared(world) -> int:
    plan_id = new_plan(world)['plan']['id']
    approve_everything(plan_id)
    return plan_id


def test_execution_imports_everything_approved_with_provenance(world):
    plan_id = prepared(world)
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    assert result['status'] == 'completed' and result['selected_items'] == 24
    assert {o['status'] for o in result['items']} == {'done'}
    items = all_items(plan_id)
    assert {i['status'] for i in items} == {'done'}
    outcome = {o['item_id']: o for o in result['items']}
    found = by_locator(world, items)
    for (key, locator), item in found.items():
        footprint = outcome[item['id']]['footprint']
        assert footprint['artifacts'] >= 1, locator
    event_items = [k for k, i in found.items() if i['state']['importer'] not in ('vcf',)]
    assert sum(outcome[found[k]['id']]['footprint']['events'] for k in event_items) > 100
    with dbmod.session() as db:
        # every acquisition made by the plan carries a collection and a catalog virtual locator
        rows = db.execute('''SELECT o.artifact_id, o.collection_id, o.source_locator, o.metadata_json FROM source_observations o''').fetchall()
        assert rows and all(r['source_locator'].startswith('catalog:collection/') for r in rows)
        with_collection = {r['artifact_id'] for r in rows if r['collection_id'] is not None}
        assert with_collection == {r['artifact_id'] for r in rows}, 'every artifact has an observation with its collection'
        # The two older importers without a collection argument add a second observation of the same bytes.
        without = {json.loads(r['metadata_json'])['source_metadata'].get('importer') for r in rows if r['collection_id'] is None}
        assert without == {'whatsapp_txt', 'sms_backup'}
        dat = db.execute('SELECT a.sha256, a.source_locator, o.collection_id FROM artifacts a JOIN source_observations o ON o.artifact_id=a.id '
                         "WHERE a.source_locator LIKE '%chatgpt-export-2025-03.zip!/file-DAT1.dat'").fetchall()
        assert {r['sha256'] for r in dat} == {world.expected['dat']['sha256']}
        assert {r['collection_id'] for r in dat} == {world.collections['drive_b'], world.collections['drive_c']}
        mbox = db.execute("SELECT id FROM artifacts WHERE source_locator=?",
                          (f'catalog:collection/{world.collections["drive_b"]}/{world.expected["tampered_member"]}',)).fetchone()
        assert mbox is not None
        children = db.execute("SELECT COUNT(*) FROM artifacts WHERE parent_artifact_id=?", (mbox['id'],)).fetchone()[0]
        assert children >= 1  # attachments extracted under the virtual locator
        links = db.execute('SELECT COUNT(DISTINCT entry_id) FROM catalog_entry_artifacts').fetchone()[0]
        assert links >= 24
        exports = {r['label']: r['collection_id'] for r in db.execute("SELECT label, collection_id FROM record_exports")}
    assert exports == {
        f'catalog:collection/{world.collections["drive_b"]}/chatgpt-export-2025-03.zip': world.collections['drive_b'],
        f'catalog:collection/{world.collections["drive_b"]}/exports/ambiguous': world.collections['drive_b'],
        f'catalog:collection/{world.collections["drive_b"]}/exports/claude': world.collections['drive_b'],
        f'catalog:collection/{world.collections["drive_c"]}/copy/chatgpt-export-2025-03.zip': world.collections['drive_c'],
        f'catalog:collection/{world.collections["drive_c"]}/later-export': world.collections['drive_c'],
    }
    assert tree_state(world.corpus) == world.before
    assert list(world.work.iterdir()) == []
    assert duplicates() == []
    run = execute.get_run(result['run_id'])
    assert run['status'] == 'completed' and all(a['result']['temp_removed'] for a in run['attempts'])
    assert all(a['result']['provenance']['sha256_verified_before_import'] for a in run['attempts'])


def test_rerun_is_idempotent(world):
    plan_id = prepared(world)
    execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    first = counts()
    again = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    assert again['selected_items'] == 0 and again['status'] == 'completed'
    assert counts() == first
    with dbmod.session() as db:
        observations = db.execute('SELECT COUNT(*) FROM source_observations').fetchone()[0]
    forced = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work, rerun_done=True)
    assert forced['selected_items'] == 24 and {o['status'] for o in forced['items']} == {'done'}
    assert counts() == first, 're-running a done plan creates no new events, records or links'
    assert all(o['footprint']['events_new'] == 0 and o['footprint']['artifacts_new'] == 0 for o in forced['items'])
    with dbmod.session() as db:
        assert db.execute('SELECT COUNT(*) FROM source_observations').fetchone()[0] > observations  # re-observation audit
    assert tree_state(world.corpus) == world.before
    assert duplicates() == []


def test_tampered_archive_member_is_refused(world):
    plan_id = prepared(world)
    takeout = world.corpus / 'drive_b' / 'takeout-20241001.zip'
    takeout.write_bytes(import_layout.tampered_takeout(world.corpus / '_parts'))
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    found = by_locator(world, all_items(plan_id))
    target = found[('drive_b', world.expected['tampered_member'])]
    outcome = {o['item_id']: o for o in result['items']}
    assert outcome[target['id']]['status'] == 'refused'
    assert outcome[target['id']]['refusal']['code'] == 'content_changed_or_unreadable'
    assert 'SHA-256' in outcome[target['id']]['refusal']['message']
    assert target['status'] == 'refused' and result['status'] == 'partial'
    # members whose bytes are unchanged still verify and import
    calendar = found[('drive_b', 'takeout-20241001.zip!/Takeout/takeout-inner.zip!/Calendar/Example.ics')]
    assert outcome[calendar['id']]['status'] == 'done'
    with dbmod.session() as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts WHERE source_locator LIKE '%All mail Including Spam and Trash.mbox%'").fetchone()[0] == 0
    assert list(world.work.iterdir()) == []
    takeout.write_bytes(import_layout.tampered_takeout(world.corpus / '_parts', grow=True))
    grown = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    refusal = {o['item_id']: o for o in grown['items']}[target['id']]['refusal']
    assert 'grew beyond its catalogued size' in refusal['message']
    assert list(world.work.iterdir()) == []


def _baseline(tmp_path, monkeypatch) -> dict:
    clean = make_world(tmp_path / 'clean', monkeypatch)
    execute.execute_plan(prepared(clean), actor='tester', temp_dir=clean.work)
    return counts()


def test_exception_mid_item_is_failed_then_retried_without_duplicates(world, tmp_path, monkeypatch):
    baseline = _baseline(tmp_path, monkeypatch)
    broken = make_world(tmp_path / 'broken', monkeypatch)
    plan_id = prepared(broken)
    from server.app.importers import chat_common
    real = chat_common.write_plan
    calls = {'n': 0}

    def failing(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 2:  # the second conversation of the first export: one is already written
            raise SimulatedCrash('stopped inside the importer')
        return real(*args, **kwargs)
    monkeypatch.setattr(chat_common, 'write_plan', failing)
    first = execute.execute_plan(plan_id, actor='tester', temp_dir=broken.work)
    monkeypatch.setattr(chat_common, 'write_plan', real)
    failed = [o for o in first['items'] if o['status'] == 'failed']
    assert len(failed) == 1 and failed[0]['importer'] == 'chatgpt_export' and first['status'] == 'partial'
    with dbmod.session() as db:
        partial_events = db.execute('SELECT COUNT(*) FROM events WHERE artifact_id IN (SELECT id FROM artifacts '
                                    "WHERE source_locator LIKE '%chatgpt-export-2025-03.zip!/conversations-000.json' "
                                    'AND source_locator LIKE ?)', (f'catalog:collection/{broken.collections["drive_b"]}/%',)).fetchone()[0]
    assert partial_events > 0, 'the stopped importer had already written part of the item'
    assert {v['status'] for v in all_items(plan_id)} == {'done', 'failed'}
    retry = execute.execute_plan(plan_id, actor='tester', temp_dir=broken.work)
    assert retry['selected_items'] == 1 and retry['status'] == 'completed'
    assert {v['status'] for v in all_items(plan_id)} == {'done'}
    assert counts() == baseline
    assert duplicates() == []
    assert list(broken.work.iterdir()) == []


def test_killed_process_mid_item_resumes_without_duplicates(world, tmp_path, monkeypatch):
    baseline = _baseline(tmp_path, monkeypatch)
    crashed = make_world(tmp_path / 'crash', monkeypatch)
    plan_id = prepared(crashed)
    from server.app import records
    real = records.record_parse

    def dying(*args, **kwargs):
        raise SimulatedCrash('killed after the first shard was written')
    monkeypatch.setattr(records, 'record_parse', dying)
    with pytest.raises(SimulatedCrash):
        execute.execute_plan(plan_id, actor='tester', temp_dir=crashed.work)
    monkeypatch.setattr(records, 'record_parse', real)
    views = all_items(plan_id)
    running = [v for v in views if v['status'] == 'running']
    assert len(running) == 1 and running[0]['state']['importer'] == 'chatgpt_export'
    assert len(list(crashed.work.iterdir())) == 1, 'a killed process leaves its temporary directory'
    with dbmod.session() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE event_type='chat_message'").fetchone()[0] > 0
    with pytest.raises(plan.Conflict):
        execute.execute_plan(plan_id, actor='tester', temp_dir=crashed.work)
    resumed = execute.execute_plan(plan_id, actor='tester', temp_dir=crashed.work, recover_running=True)
    assert resumed['recovery']['interrupted_attempts'] == 1 and resumed['recovery']['workdirs_removed'] == 1
    assert resumed['status'] == 'completed'
    assert {v['status'] for v in all_items(plan_id)} == {'done'}
    assert counts() == baseline
    assert duplicates() == []
    assert list(crashed.work.iterdir()) == []
    with dbmod.session() as db:
        statuses = [r[0] for r in db.execute('SELECT status FROM import_plan_runs WHERE plan_id=? ORDER BY id', (plan_id,))]
        assert statuses == ['interrupted', 'completed']
        attempts = db.execute("SELECT status FROM import_plan_attempts WHERE item_id=? ORDER BY id", (running[0]['id'],)).fetchall()
        assert [a[0] for a in attempts] == ['interrupted', 'done']
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE import_plan_attempts SET status='done' WHERE status='interrupted'")


def test_expired_lease_is_recovered_without_operator_flag(world):
    plan_id = new_plan(world)['plan']['id']
    found = by_locator(world, all_items(plan_id))
    ics = found[('drive_b', 'takeout-20241001.zip!/Takeout/takeout-inner.zip!/Calendar/Example.ics')]
    plan.decide(plan_id, base_version=1, decisions=[{'item_id': ics['id'], 'status': 'approved'}], actor='tester')

    def crash(item, files):
        raise SimulatedCrash('killed after materialization')
    with pytest.raises(SimulatedCrash):
        execute.execute_plan(plan_id, actor='tester', temp_dir=world.work, hooks={'after_materialize': crash})
    with dbmod.session() as db:
        db.execute("UPDATE import_plan_attempts SET lease_expires_at=? WHERE status='running'", (time.time() - 1,))
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    assert result['recovery']['interrupted_attempts'] == 1 and result['status'] == 'completed'
    assert list(world.work.iterdir()) == []


def test_queue_mode_fences_a_lost_lease(world):
    plan_id = new_plan(world)['plan']['id']
    found = by_locator(world, all_items(plan_id))
    wanted = [found[('drive_b', 'takeout-20241001.zip!/Takeout/takeout-inner.zip!/Calendar/Example.ics')],
              found[('drive_c', 'later-export')]]
    plan.decide(plan_id, base_version=1, actor='tester',
                decisions=[{'item_id': i['id'], 'status': 'approved'} for i in wanted])
    queued = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work, mode='queue')
    assert queued['status'] == 'running' and queued['queued_jobs'] == 2
    with pytest.raises(plan.Conflict):
        execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    job = jobs.claim_job(kinds=(execute.JOB_KIND,), lease_seconds=30)
    assert job['kind'] == 'import_plan_item' and job['artifact_id'] is None
    jobs.recover_expired_jobs(now=time.time() + 3600)  # the lease expires while the worker is busy
    with pytest.raises(execute.LeaseLost):
        execute.run_queued_job(job)
    stale = execute.get_run(queued['run_id'])['attempts']
    assert [a['status'] for a in stale] == ['running']
    drained = execute.work(lease_seconds=60)
    assert drained['jobs'] == 2
    run = execute.get_run(queued['run_id'])
    assert run['status'] == 'completed' and run['jobs_pending'] == 0
    assert sorted(a['status'] for a in run['attempts']) == ['done', 'done', 'interrupted']
    assert {v['status'] for v in all_items(plan_id) if v['id'] in {i['id'] for i in wanted}} == {'done'}
    assert duplicates() == []
    with dbmod.session() as db:
        statuses = {r['status'] for r in db.execute('SELECT status FROM jobs WHERE kind=?', (execute.JOB_KIND,))}
    assert statuses == {'done'}


def test_cross_export_view_after_import(world):
    plan_id = prepared(world)
    execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    view = cross.post_import(plan_id)
    shared = {(c['namespace'], c['conversation_id']): c for c in view['conversations']}
    c_conv = shared[('chatgpt', import_layout.C_ID)]
    assert len(c_conv['exports']) == 3
    assert {e['collection_id'] for e in c_conv['exports']} == {world.collections['drive_b'], world.collections['drive_c']}
    assert len(shared[('chatgpt', import_layout.S_ID)]['exports']) == 2
    assert view['conversations_total'] == 3
    with dbmod.session() as db:
        occurrences = db.execute('''SELECT COUNT(DISTINCT o.export_id) FROM logical_occurrences o JOIN logical_records r
                                    ON r.id=o.logical_record_id WHERE r.container_id=? AND r.record_kind='conversation' ''',
                                 (import_layout.C_ID,)).fetchone()[0]
        events = db.execute("SELECT COUNT(*) FROM events WHERE event_type='chat_conversation' AND thread_id=?",
                            (import_layout.C_ID,)).fetchone()[0]
    assert occurrences == 3 and events == 3  # all occurrences imported, none deleted


def test_unapproved_skipped_and_blocked_items_are_not_run(world):
    plan_id = new_plan(world)['plan']['id']
    items = all_items(plan_id)
    plan.decide(plan_id, base_version=1, actor='tester', decisions=[
        {'item_id': items[0]['id'], 'status': 'approved'}, {'item_id': items[1]['id'], 'status': 'skipped'}])
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    assert [o['item_id'] for o in result['items']] == [items[0]['id']]
    statuses = {v['id']: v['status'] for v in all_items(plan_id)}
    assert statuses[items[0]['id']] == 'done' and statuses[items[1]['id']] == 'skipped'
    assert statuses[items[2]['id']] == 'proposed'


def test_execution_refuses_temp_inside_sources_and_bounds_items(world):
    plan_id = prepared(world)
    with pytest.raises(ValueError, match='outside every source root'):
        execute.execute_plan(plan_id, actor='tester', temp_dir=world.corpus / 'drive_c' / 'tmp')
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work,
                                  limits=execute.RunLimits(max_item_bytes=200))
    refused = [o for o in result['items'] if o['status'] == 'refused']
    assert refused and all(o['refusal']['code'] == 'limit_exceeded' for o in refused)
    assert any(o['status'] == 'done' for o in result['items'])
    assert tree_state(world.corpus) == world.before
    with pytest.raises(ValueError):
        execute.RunLimits(lease_seconds=0)


def test_changing_the_importer_reruns_a_done_item(world):
    plan_id = new_plan(world)['plan']['id']
    calls = by_locator(world, all_items(plan_id))[('phone_a', 'backup/calls-20240107.xml')]
    plan.decide(plan_id, base_version=1, decisions=[{'item_id': calls['id'], 'status': 'approved'}], actor='tester')
    execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    plan.decide(plan_id, base_version=2, decisions=[{'item_id': calls['id'], 'importer': 'sms_backup'}], actor='tester')
    assert by_locator(world, all_items(plan_id))[('phone_a', 'backup/calls-20240107.xml')]['status'] == 'approved'
    again = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    assert again['selected_items'] == 1 and again['items'][0]['importer'] == 'sms_backup'
    with dbmod.session() as db:
        artifacts = db.execute('SELECT COUNT(*) FROM artifacts WHERE source_locator LIKE ?', ('%calls-20240107.xml',)).fetchone()[0]
        parsers = {json.loads(r[0]).get('parser', {}).get('name') for r in db.execute('SELECT metadata_json FROM events WHERE event_type=?', ('call',))}
    assert artifacts == 1, 'both projections share one artifact (same bytes, same virtual locator)'
    assert len(parsers) == 2


@pytest.mark.xfail(strict=True, reason='review finding #5 (REVIEW_FINDINGS_b181c21.md): email/WhatsApp child locators are '
                   'derived from the container locator only, so a later export at the same virtual locator with the same '
                   'first attachment raises IntegrityError (different parent). Track R puts container identity into child '
                   'locators; this assertion flips then.')
def test_later_export_at_the_same_locator_imports_with_shared_attachments(world):
    plan_id = new_plan(world)['plan']['id']
    found = by_locator(world, all_items(plan_id))
    mbox = found[('drive_b', world.expected['tampered_member'])]
    plan.decide(plan_id, base_version=1, decisions=[{'item_id': mbox['id'], 'status': 'approved'}], actor='tester')
    assert execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)['status'] == 'completed'
    # The account is exported again later into the same place: same names, the MBOX grew.
    (world.corpus / 'drive_b' / 'takeout-20241001.zip').write_bytes(
        import_layout.tampered_takeout(world.corpus / '_parts', grow=True))
    from server.app.catalog.scan import run_catalog_scan
    run_catalog_scan(world.expected['roots']['drive_b'], world.collections['drive_b'], temp_dir=world.spool)
    later = new_plan(world, collection_ids=[world.collections['drive_b']])['plan']['id']
    item = by_locator(world, all_items(later))[('drive_b', world.expected['tampered_member'])]
    plan.decide(later, base_version=1, decisions=[{'item_id': item['id'], 'status': 'approved'}], actor='tester')
    result = execute.execute_plan(later, actor='tester', temp_dir=world.work)
    assert result['items'][0]['status'] == 'done', result['items'][0]['error']


@pytest.mark.xfail(strict=False, reason='review finding #1 (REVIEW_FINDINGS_b181c21.md): ingest_file stores the temporary '
                   "copy's mtime as source_modified_at. TODO(importplan): once track R adds the explicit stat scope / declared "
                   'mtime to ingest_file, pass the catalogued source time for materialized entries (execute._materialize).')
def test_acquisition_keeps_the_catalogued_source_time(world):
    plan_id = new_plan(world)['plan']['id']
    calls = by_locator(world, all_items(plan_id))[('phone_a', 'backup/calls-20240107.xml')]
    plan.decide(plan_id, base_version=1, decisions=[{'item_id': calls['id'], 'status': 'approved'}], actor='tester')
    execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    with dbmod.session() as db:
        observed = db.execute('SELECT source_modified_at FROM source_observations WHERE source_locator LIKE ?',
                              ('%backup/calls-20240107.xml',)).fetchone()[0]
    assert observed == world.expected['source_mtime_utc']


def test_a_run_between_items_is_not_taken_over_without_the_operator(world):
    plan_id = new_plan(world)['plan']['id']
    item = all_items(plan_id)[0]
    plan.decide(plan_id, base_version=1, decisions=[{'item_id': item['id'], 'status': 'approved'}], actor='tester')
    with dbmod.session() as db:  # another process started a sync run and has not reached its first item yet
        other = db.execute("""INSERT INTO import_plan_runs(plan_id,plan_version,mode,status,actor,started_at)
                              VALUES(?,2,'sync','running','other','2026-10-16T00:00:00+00:00')""", (plan_id,)).lastrowid
    with pytest.raises(plan.Conflict, match='still running'):
        execute.execute_plan(plan_id, actor='tester', temp_dir=world.work)
    taken = execute.execute_plan(plan_id, actor='tester', temp_dir=world.work, recover_running=True)
    assert taken['status'] == 'completed'
    assert execute.get_run(other)['status'] == 'interrupted'

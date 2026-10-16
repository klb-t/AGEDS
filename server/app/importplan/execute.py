"""Execution of approved plan items through the real importers.

One run executes the approved, unblocked items of one plan version that are
not yet ``done`` with the same importer/parameters/opt-ins. Each item is one
attempt:

1. an attempt row is written ``running`` with a lease token;
2. member entries are checked (verifiable hash, size and count bounds, free
   temporary space) — a failed check is ``refused`` before anything is read;
3. members are materialized into a new temporary directory outside every
   source root and re-verified against the catalog SHA-256 — any mismatch is
   ``refused`` and nothing is imported;
4. the importer runs with the entry's collection and virtual locator
   (``catalog:collection/<id>/<locator>``); importers are idempotent per file
   or conversation, so a re-run re-observes instead of duplicating;
5. the footprint (artifacts/events under the item's locators, new vs re-observed)
   and catalog links are recorded, and the attempt is published ``done`` or
   ``failed`` only while its lease token still owns the row;
6. the temporary directory is removed.

A process that dies mid-item leaves the attempt ``running``: it is never
claimed done. The next run marks it ``interrupted`` once its lease expired (or
when the operator passes ``recover_running``), removes its temporary
directory and runs the item again; the importers' idempotency prevents
duplicate events.

``mode='queue'`` enqueues one ``import_plan_item`` job per item in the existing
job queue instead; a worker (``import-plan-work`` or the general worker) claims
it with a lease, and the attempt's terminal state is published in the same
fenced transaction as the job completion (``jobs.publish_result``). Job
``done`` means "attempt finished"; the item outcome is the attempt status.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .. import db as dbmod
from ..evidence import IntegrityError
from .adapters import IMPORTERS, MaterializedFile, RunContext, catalog_source_locator
from .plan import Conflict, NotFound, _attempts, _items, _plan_row, _states, blockers, config_sha256, state_of, work_parent
from .reader import EntryReader, ReadLimitError

JOB_KIND = 'import_plan_item'
WORKDIR_PREFIX = 'ageds-importplan-a'
REQUIRED_ROLES = {'file', 'archive', 'conversations', 'chat_text'}
RESULT_MAX_BYTES = 64 * 1024


from .limits import RUN_LIMIT_NAMES as LIMIT_NAMES, RunLimits  # noqa: E402,F401  (re-exported)


class Refused(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.details = code, details


class LeaseLost(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)


def _bounded(value: dict) -> dict:
    text = _json(value)
    if len(text.encode('utf-8')) <= RESULT_MAX_BYTES:
        return json.loads(text)
    return {'truncated': True, 'keys': sorted(value)[:100], 'bytes': len(text.encode('utf-8')),
            **{k: v for k, v in value.items() if isinstance(v, (int, float, bool)) or v is None}}


def _audit(db, actor: str, action: str, kind: str, object_id: int | None, details: dict):
    db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)',
               (actor, action, kind, object_id, _json(details)))


# ------------------------------------------------------------------ recovery

def _remove_workdir(path: str | None) -> bool:
    if not path:
        return False
    target = Path(path)
    if not target.name.startswith(WORKDIR_PREFIX) or not target.is_dir() or target.is_symlink():
        return False
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


def recover(plan_id: int | None = None, *, force: bool = False, now: float | None = None, actor: str = 'importplan') -> dict:
    """Mark ``running`` attempts whose lease expired (or all, with ``force``) as ``interrupted``."""
    now = time.time() if now is None else now
    recovered, removed = [], 0
    with closing(dbmod.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            rows = db.execute(f'''SELECT a.id, a.run_id, a.item_id, a.workdir, a.lease_token, a.job_id, a.lease_expires_at
                                  FROM import_plan_attempts a JOIN import_plan_items i ON i.id=a.item_id
                                  WHERE a.status='running'{' AND i.plan_id=?' if plan_id is not None else ''}''',
                              [plan_id] if plan_id is not None else []).fetchall()
            for row in rows:
                if not force and _attempt_alive(db, row, now):
                    continue
                db.execute('''UPDATE import_plan_attempts SET status='interrupted', finished_at=?,
                              error='lease expired or operator recovery; process presumed dead; nothing was claimed done'
                              WHERE id=? AND status='running' ''', (_now_iso(), row['id']))
                _audit(db, actor, 'import_plan_attempt_interrupted', 'import_plan_attempt', row['id'],
                       {'run_id': row['run_id'], 'item_id': row['item_id'], 'forced': force})
                recovered.append(dict(row))
            run_ids = {r['run_id'] for r in recovered}
            for run_id in run_ids:
                run = db.execute('SELECT mode, status FROM import_plan_runs WHERE id=?', (run_id,)).fetchone()
                busy = db.execute("SELECT 1 FROM import_plan_attempts WHERE run_id=? AND status='running' LIMIT 1", (run_id,)).fetchone()
                queued = _run_jobs_pending(db, run_id)
                if run['status'] == 'running' and not busy and not queued:
                    db.execute("UPDATE import_plan_runs SET status='interrupted', finished_at=?, error=? WHERE id=? AND status='running'",
                               (_now_iso(), 'attempts interrupted; resume with a new run', run_id))
            if plan_id is not None:
                for run in db.execute("SELECT id FROM import_plan_runs WHERE plan_id=? AND status='running' AND mode='sync'", (plan_id,)).fetchall():
                    busy = db.execute("SELECT 1 FROM import_plan_attempts WHERE run_id=? AND status='running' LIMIT 1", (run['id'],)).fetchone()
                    if not busy and (force or run['id'] in run_ids or not _has_live_attempts(db, run['id'])):
                        db.execute("UPDATE import_plan_runs SET status='interrupted', finished_at=?, error=? WHERE id=? AND status='running'",
                                   (_now_iso(), 'run left running by a stopped process', run['id']))
            db.commit()
        except Exception:
            db.rollback()
            raise
    for row in recovered:
        removed += _remove_workdir(row['workdir'])
    return {'interrupted_attempts': len(recovered), 'workdirs_removed': removed,
            'attempt_ids': [r['id'] for r in recovered]}


def _has_live_attempts(db, run_id: int) -> bool:
    return db.execute("SELECT 1 FROM import_plan_attempts WHERE run_id=? AND status='running' LIMIT 1", (run_id,)).fetchone() is not None


def _run_jobs_pending(db, run_id: int) -> int:
    return db.execute("""SELECT COUNT(*) FROM jobs WHERE kind=? AND status IN ('queued','running')
                         AND json_extract(payload_json,'$.run_id')=?""", (JOB_KIND, run_id)).fetchone()[0]


# --------------------------------------------------------------------- items

def _members(db, item_id: int) -> list[dict]:
    return [dict(r) for r in db.execute('''SELECT m.entry_id, m.role, m.relative_path, e.locator, e.name, e.status, e.sha256,
            e.size_bytes, e.sniffed_mime FROM import_plan_item_members m JOIN catalog_entries e ON e.id=m.entry_id
            WHERE m.item_id=? ORDER BY m.id''', (item_id,))]


def _attempt_alive(db, row, now: float) -> bool:
    """A running attempt is alive while its own lease is valid and, when queued, its job still holds the same lease."""
    if row['lease_expires_at'] <= now:
        return False
    if row['job_id'] is None:
        return True
    job = db.execute('SELECT status, lease_token, lease_expires_at FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
    return bool(job and job['status'] == 'running' and job['lease_token'] == row['lease_token']
                and (job['lease_expires_at'] or 0) > now)


def _start_attempt(item: dict, state: dict, run_id: int, *, token: str, lease_expires_at: float, job_id: int | None,
                   worker: str) -> int:
    with closing(dbmod.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            now = time.time()
            live = db.execute("SELECT id, lease_expires_at, lease_token, job_id FROM import_plan_attempts WHERE item_id=? AND status='running'",
                              (item['id'],)).fetchall()
            for other in live:
                if _attempt_alive(db, other, now):
                    raise Conflict(f'item {item["id"]} already has a running attempt ({other["id"]}) with a valid lease')
                db.execute('''UPDATE import_plan_attempts SET status='interrupted', finished_at=?,
                              error='superseded: its lease (or its job lease) expired' WHERE id=? AND status='running' ''',
                           (_now_iso(), other['id']))
            number = db.execute('SELECT COUNT(*) FROM import_plan_attempts WHERE item_id=?', (item['id'],)).fetchone()[0] + 1
            cursor = db.execute('''INSERT INTO import_plan_attempts(run_id,item_id,attempt,status,importer,config_sha256,
                    parameters_json,opt_ins_json,lease_token,lease_expires_at,worker,job_id,started_at)
                    VALUES(?,?,?,'running',?,?,?,?,?,?,?,?,?)''',
                (run_id, item['id'], number, state['importer'],
                 config_sha256(state['importer'], state['parameters'], state['opt_ins']), _json(state['parameters']),
                 _json(state['opt_ins']), token, lease_expires_at, worker, job_id, _now_iso()))
            db.commit()
            return int(cursor.lastrowid)
        except Exception:
            db.rollback()
            raise


def _set_workdir(attempt_id: int, token: str, workdir: Path):
    with dbmod.session() as db:
        db.execute("UPDATE import_plan_attempts SET workdir=? WHERE id=? AND lease_token=? AND status='running'",
                   (str(workdir), attempt_id, token))


def _renew(attempt_id: int, token: str, lease_seconds: int) -> bool:
    with dbmod.session() as db:
        cursor = db.execute("UPDATE import_plan_attempts SET lease_expires_at=? WHERE id=? AND lease_token=? AND status='running'",
                            (time.time() + lease_seconds, attempt_id, token))
        return cursor.rowcount == 1


@contextmanager
def _heartbeat(attempt_id: int, token: str, lease_seconds: int):
    stop = threading.Event()
    interval = max(1.0, lease_seconds / 3)

    def beat():
        while not stop.wait(interval):
            try:
                if not _renew(attempt_id, token, lease_seconds):
                    return
            except Exception:  # pragma: no cover - a failed renewal only shortens the lease
                return
    thread = threading.Thread(target=beat, name=f'importplan-lease-{attempt_id}', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def _precheck(members: list[dict], limits: RunLimits, parent: Path) -> tuple[list[dict], list[dict]]:
    usable, skipped = [], []
    for member in members:
        ok = member['status'] == 'ok' and member['sha256']
        if ok:
            usable.append(member)
        elif member['role'] in REQUIRED_ROLES:
            raise Refused('member_not_verifiable', f'{member["locator"]!r} has status {member["status"]} and no verified hash',
                          entry_id=member['entry_id'])
        else:
            skipped.append({'entry_id': member['entry_id'], 'locator': member['locator'], 'status': member['status']})
    if len(usable) > limits.max_item_members:
        raise Refused('limit_exceeded', 'item has more members than max_item_members', members=len(usable),
                      limit=limits.max_item_members)
    total = sum(m['size_bytes'] or 0 for m in usable)
    if total > limits.max_item_bytes:
        raise Refused('limit_exceeded', 'item is larger than max_item_bytes', bytes=total, limit=limits.max_item_bytes)
    free = shutil.disk_usage(parent).free
    if free < total + limits.min_free_temp_bytes:
        raise Refused('insufficient_temp_space', 'not enough free space in the temporary directory', needed=total,
                      free=free, reserve=limits.min_free_temp_bytes)
    return usable, skipped


def _materialize(item: dict, members: list[dict], workdir: Path, materialize: str) -> tuple[Path, list[MaterializedFile]]:
    unit = workdir / 'unit'
    unit.mkdir()
    spool = workdir / 'spool'
    spool.mkdir()
    files = []
    with EntryReader(spool) as reader:
        for member in members:
            relative = member['relative_path']
            target = unit / relative
            if unit not in target.resolve(strict=False).parents:
                raise Refused('unsafe_member_path', 'member path escapes the temporary directory', path=relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            # TODO(importplan, review finding #1): the importers acquire this temporary copy, so
            # ingest_file records the copy's mtime as source_modified_at. When ingest_file gains an
            # explicit stat scope / declared mtime (track R), pass the catalogued source time here
            # (catalog_entry_times) through the adapters. Test: test_acquisition_keeps_the_catalogued_source_time.
            reader.copy_to(member['entry_id'], target)
            files.append(MaterializedFile(member['entry_id'], target, member['locator'],
                                          catalog_source_locator(item['collection_id'], member['locator']),
                                          member['name'], member['role'], relative, member['sniffed_mime']))
    shutil.rmtree(spool, ignore_errors=True)
    root = files[0].path if materialize == 'self' else unit
    return root, files


def _marks(db) -> dict:
    return {table: db.execute(f'SELECT COALESCE(MAX(id),0) FROM {table}').fetchone()[0]
            for table in ('artifacts', 'events', 'source_observations', 'event_times')}


def _footprint(db, files: list[MaterializedFile], root_source_locator: str, before: dict) -> dict:
    keys = {root_source_locator, *(f.source_locator for f in files)}
    artifacts: dict[int, str] = {}
    for key in sorted(keys):
        for row in db.execute('''SELECT id, source_locator FROM artifacts WHERE source_locator=?
                                 OR (source_locator >= ? AND source_locator < ?)''', (key, key + '!/', key + '!0')):
            artifacts[int(row['id'])] = row['source_locator']
    ids = sorted(artifacts)
    events = event_new = times_new = observations_new = 0
    event_ids: list[int] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ','.join('?' * len(chunk))
        row = db.execute(f'SELECT COUNT(*), COALESCE(SUM(id > ?),0) FROM events WHERE artifact_id IN ({marks})',
                         (before['events'], *chunk)).fetchone()
        events += row[0]
        event_new += row[1]
        observations_new += db.execute(f'SELECT COUNT(*) FROM source_observations WHERE artifact_id IN ({marks}) AND id > ?',
                                       (*chunk, before['source_observations'])).fetchone()[0]
        times_new += db.execute(f'''SELECT COUNT(*) FROM event_times WHERE id > ? AND event_id IN
                                    (SELECT id FROM events WHERE artifact_id IN ({marks}))''',
                                (before['event_times'], *chunk)).fetchone()[0]
        if len(event_ids) < 20:
            event_ids.extend(r[0] for r in db.execute(f'SELECT id FROM events WHERE artifact_id IN ({marks}) ORDER BY id LIMIT 20',
                                                      chunk))
    return {'artifacts': len(ids), 'artifacts_new': sum(1 for i in ids if i > before['artifacts']),
            'events': events, 'events_new': event_new, 'event_times_new': times_new,
            'acquisition_observations_new': observations_new,
            'artifact_ids': ids[:50], 'artifact_ids_truncated': max(0, len(ids) - 50), 'event_ids_sample': sorted(event_ids)[:20],
            'basis': 'artifacts whose source locator is a member locator of this item or below it (!/); events of those artifacts'}


def _link_entries(db, files: list[MaterializedFile]) -> int:
    added = 0
    for f in files:
        row = db.execute('SELECT id FROM artifacts WHERE source_locator=? ORDER BY id LIMIT 1', (f.source_locator,)).fetchone()
        if row is None:
            continue
        if db.execute('SELECT 1 FROM catalog_entry_artifacts WHERE entry_id=? AND artifact_id=?', (f.entry_id, row['id'])).fetchone():
            continue
        observation = db.execute('SELECT MAX(id) FROM source_observations WHERE artifact_id=? AND source_locator=?',
                                 (row['id'], f.source_locator)).fetchone()[0]
        db.execute('INSERT INTO catalog_entry_artifacts(entry_id,artifact_id,observation_id,source_locator) VALUES(?,?,?,?)',
                   (f.entry_id, row['id'], observation, f.source_locator))
        added += 1
    return added


def _counts(result: dict) -> dict:
    counts = {k: v for k, v in result.items() if isinstance(v, int) and not isinstance(v, bool) and not k.endswith('_id')}
    totals = result.get('totals')
    if isinstance(totals, dict):
        counts.update({f'totals.{k}': v for k, v in totals.items() if isinstance(v, int)})
    return dict(sorted(counts.items()))


def _publish(attempt_id: int, token: str, status: str, result: dict, error: str | None, *, job: dict | None,
             actor: str) -> None:
    result = _bounded(result)

    def write(db, _current=None):
        cursor = db.execute('''UPDATE import_plan_attempts SET status=?, finished_at=?, result_json=?, error=?
                               WHERE id=? AND status='running' AND lease_token=? AND lease_expires_at>?''',
                            (status, _now_iso(), _json(result), error, attempt_id, token, time.time()))
        if cursor.rowcount != 1:
            raise LeaseLost(f'attempt {attempt_id} no longer owns its lease; result was not published')
        _audit(db, actor, f'import_plan_item_{status}', 'import_plan_attempt', attempt_id,
               {'footprint': {k: v for k, v in (result.get('footprint') or {}).items() if isinstance(v, int)},
                'error': (error or '')[:300]})
        return attempt_id
    if job is not None:
        from ..jobs import LeaseLost as JobLeaseLost, publish_result
        try:
            publish_result(job, write, summary={'import_plan_attempt_id': attempt_id, 'item_status': status})
        except JobLeaseLost as exc:
            raise LeaseLost(str(exc)) from exc
        return
    with closing(dbmod.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            write(db)
            db.commit()
        except Exception:
            db.rollback()
            raise


def run_item(item: dict, state: dict, run: dict, plan: dict, *, limits: RunLimits, temp_parent: Path,
             actor: str, job: dict | None = None, hooks: dict | None = None) -> dict:
    """One attempt for one item. Returns the published outcome (or raises LeaseLost)."""
    importer = IMPORTERS[state['importer']]
    token = job['lease_token'] if job else uuid.uuid4().hex
    lease_expires = time.time() + limits.lease_seconds
    worker = job['worker_id'] if job else f'{socket.gethostname()}:{os.getpid()}'
    attempt_id = _start_attempt(item, state, run['id'], token=token, lease_expires_at=lease_expires,
                                job_id=job['id'] if job else None, worker=worker)
    outcome = {'item_id': item['id'], 'ordinal': item['ordinal'], 'attempt_id': attempt_id, 'importer': importer.key}
    workdir = None
    with _heartbeat(attempt_id, token, limits.lease_seconds):
        try:
            with closing(dbmod.connect()) as db:
                members = _members(db, item['id'])
            usable, skipped = _precheck(members, limits, temp_parent)
            workdir = Path(tempfile.mkdtemp(prefix=f'{WORKDIR_PREFIX}{attempt_id}-', dir=temp_parent))
            _set_workdir(attempt_id, token, workdir)
            try:
                root, files = _materialize(item, usable, workdir, item['detection'].get('materialize', 'self'))
            except (IntegrityError, ReadLimitError) as exc:
                raise Refused('content_changed_or_unreadable' if isinstance(exc, IntegrityError) else 'limit_exceeded',
                              str(exc)) from exc
            if hooks and hooks.get('after_materialize'):
                hooks['after_materialize'](item, files)
            ctx = RunContext(item=item, collection_id=item['collection_id'], root_locator=item['locator'],
                             root_source_locator=catalog_source_locator(item['collection_id'], item['locator']),
                             root_path=root, files=files, params=dict(state['parameters']), opt_ins=dict(state['opt_ins']),
                             plan_options=plan['options'],
                             export_label=catalog_source_locator(item['collection_id'], item['locator']), actor=actor)
            with closing(dbmod.connect()) as db:
                before = _marks(db)
            importer_result = importer.run(ctx)
            if hooks and hooks.get('after_import'):
                hooks['after_import'](item, importer_result)
            with closing(dbmod.connect()) as db:
                footprint = _footprint(db, files, ctx.root_source_locator, before)
                links = _link_entries(db, files)
                db.commit()
            result = {'footprint': footprint, 'counts': _counts(importer_result), 'catalog_links_added': links,
                      'members_used': len(files), 'members_skipped': skipped,
                      'artifact_ids': footprint['artifact_ids'][:20], 'importer_result': importer_result,
                      'provenance': {'collection_id': item['collection_id'], 'source_locator': ctx.root_source_locator,
                                     'export_label': ctx.export_label if importer.namespaces else None,
                                     'sha256_verified_before_import': True}}
            status, error = 'done', None
        except Refused as exc:
            result = {'refusal': {'code': exc.code, 'message': str(exc), **exc.details}}
            status, error = 'refused', f'{exc.code}: {exc}'
        except LeaseLost:
            raise
        except Exception as exc:  # an importer failure is recorded; other items continue
            result = {'error_type': type(exc).__name__}
            status, error = 'failed', f'{type(exc).__name__}: {exc}'[:2000]
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
            result['temp_removed'] = not workdir.exists()
        _publish(attempt_id, token, status, result, error, job=job, actor=actor)
    outcome.update(status=status, error=error, footprint=result.get('footprint'), refusal=result.get('refusal'))
    return outcome


# ---------------------------------------------------------------------- runs

def _selection(db, plan_id: int, version: int, *, rerun_done: bool, item_ids: list[int] | None) -> list[tuple[dict, dict]]:
    items = _items(db, plan_id)
    states = _states(db, plan_id, version)
    attempts = _attempts(db, plan_id)
    selected = []
    for item in items:
        if item_ids is not None and item['id'] not in item_ids:
            continue
        state = state_of(item, states)
        if state['status'] != 'approved' or blockers(item, state):
            continue
        current = config_sha256(state['importer'], state['parameters'], state['opt_ins'])
        done = any(a['status'] == 'done' and a['config_sha256'] == current for a in attempts.get(item['id'], []))
        if done and not rerun_done:
            continue
        selected.append((item, state))
    return selected


def _finish_run(run_id: int, actor: str) -> dict:
    with closing(dbmod.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            run = db.execute('SELECT * FROM import_plan_runs WHERE id=?', (run_id,)).fetchone()
            rows = db.execute('SELECT status, COUNT(*) AS n FROM import_plan_attempts WHERE run_id=? GROUP BY status',
                              (run_id,)).fetchall()
            by_status = {r['status']: r['n'] for r in rows}
            if run['status'] != 'running' or by_status.get('running') or _run_jobs_pending(db, run_id):
                db.rollback()
                return {'run_id': run_id, 'status': run['status'], 'attempts': by_status}
            options = json.loads(run['options_json'])
            selected = options.get('selected_items', 0)
            latest = {}
            for row in db.execute('SELECT item_id, status FROM import_plan_attempts WHERE run_id=? ORDER BY id', (run_id,)):
                latest[row['item_id']] = row['status']
            outcomes: dict[str, int] = {}
            for value in latest.values():
                outcomes[value] = outcomes.get(value, 0) + 1
            done = outcomes.get('done', 0)
            status = 'completed' if done == selected else 'failed' if not done and selected else 'partial'
            summary = {'attempts': by_status, 'item_outcomes': outcomes, 'selected_items': selected}
            db.execute('UPDATE import_plan_runs SET status=?, finished_at=?, summary_json=? WHERE id=?',
                       (status, _now_iso(), _json(summary), run_id))
            _audit(db, actor, 'import_plan_run_finished', 'import_plan_run', run_id, {'status': status, **summary})
            db.commit()
            return {'run_id': run_id, 'status': status, 'attempts': by_status}
        except Exception:
            db.rollback()
            raise


def execute_plan(plan_id: int, *, version: int | None = None, mode: str = 'sync', actor: str = 'user',
                 temp_dir: str | Path | None = None, recover_running: bool = False, rerun_done: bool = False,
                 item_ids: list[int] | None = None, limits: RunLimits | None = None, hooks: dict | None = None) -> dict:
    if mode not in ('sync', 'queue'):
        raise ValueError('mode must be sync or queue')
    limits = limits or RunLimits()
    temp_parent = work_parent(temp_dir)
    recovery = recover(plan_id, force=recover_running, actor=actor)
    with closing(dbmod.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            plan = _plan_row(db, plan_id)
            version = plan['latest_version'] if version is None else int(version)
            if not 1 <= version <= plan['latest_version']:
                raise NotFound('plan version does not exist')
            live = db.execute('''SELECT a.id FROM import_plan_attempts a JOIN import_plan_items i ON i.id=a.item_id
                                 WHERE i.plan_id=? AND a.status='running' LIMIT 1''', (plan_id,)).fetchone()
            pending = db.execute("""SELECT r.id FROM import_plan_runs r WHERE r.plan_id=? AND r.status='running'""",
                                 (plan_id,)).fetchall()
            if live or any(_run_jobs_pending(db, r['id']) for r in pending):
                raise Conflict('this plan has a running attempt or queued items (another process may be working); '
                               'wait, or recover after the lease expired (recover_running=True asserts the process is dead)')
            for run in pending:  # sync runs left running without attempts (process died between items)
                db.execute("UPDATE import_plan_runs SET status='interrupted', finished_at=?, error=? WHERE id=? AND status='running'",
                           (_now_iso(), 'run left running by a stopped process', run['id']))
            selected = _selection(db, plan_id, version, rerun_done=rerun_done, item_ids=item_ids)
            options = {'rerun_done': rerun_done, 'recover_running': recover_running, 'selected_items': len(selected),
                       'item_ids': item_ids, 'limits': asdict(limits), 'temp_parent': str(temp_parent)}
            run_id = int(db.execute('''INSERT INTO import_plan_runs(plan_id,plan_version,mode,status,actor,options_json,started_at)
                                       VALUES(?,?,?,'running',?,?,?)''',
                                    (plan_id, version, mode, actor, _json(options), _now_iso())).lastrowid)
            _audit(db, actor, 'import_plan_run_started', 'import_plan_run', run_id,
                   {'plan_id': plan_id, 'version': version, 'mode': mode, 'selected_items': len(selected)})
            if mode == 'queue':
                for item, _ in selected:
                    db.execute("INSERT INTO jobs(kind, artifact_id, status, payload_json) VALUES (?, NULL, 'queued', ?)",
                               (JOB_KIND, _json({'run_id': run_id, 'plan_id': plan_id, 'plan_version': version,
                                                 'item_id': item['id'], 'temp_parent': str(temp_parent),
                                                 'limits': asdict(limits)})))
            db.commit()
        except Exception:
            db.rollback()
            raise
    run = {'id': run_id, 'plan_id': plan_id, 'plan_version': version, 'mode': mode}
    outcomes = []
    if mode == 'sync':
        for item, state in selected:
            outcomes.append(run_item(item, state, run, plan, limits=limits, temp_parent=temp_parent, actor=actor, hooks=hooks))
        finished = _finish_run(run_id, actor)
    else:
        finished = {'run_id': run_id, 'status': 'running', 'queued_jobs': len(selected)}
        if not selected:
            finished = _finish_run(run_id, actor)
    return {'run_id': run_id, 'plan_id': plan_id, 'plan_version': version, 'mode': mode, 'status': finished['status'],
            'selected_items': len(selected), 'items': outcomes, 'recovery': recovery,
            'queued_jobs': len(selected) if mode == 'queue' else 0}


def run_queued_job(job: dict, *, hooks: dict | None = None) -> dict:
    """Execute one ``import_plan_item`` job claimed with a lease (worker entry point)."""
    payload = json.loads(job.get('payload_json') or '{}')
    with closing(dbmod.connect()) as db:
        plan = _plan_row(db, payload['plan_id'])
        run = db.execute('SELECT * FROM import_plan_runs WHERE id=?', (payload['run_id'],)).fetchone()
        item = next((i for i in _items(db, payload['plan_id']) if i['id'] == payload['item_id']), None)
        states = _states(db, payload['plan_id'], payload['plan_version'])
    if run is None or item is None:
        raise NotFound('queued import plan item no longer exists')
    state = state_of(item, states)
    limits = RunLimits(**payload.get('limits', {}))
    temp_parent = work_parent(payload.get('temp_parent'))
    if run['status'] != 'running' or state['status'] != 'approved' or blockers(item, state):
        def write(db, _current):
            _audit(db, 'importplan', 'import_plan_job_skipped', 'job', job['id'],
                   {'reason': 'run not running or item no longer approved', 'item_id': item['id']})
            return None
        from ..jobs import publish_result
        publish_result(job, write)
        return {'item_id': item['id'], 'status': 'skipped_by_worker'}
    outcome = run_item(item, state, dict(run), plan, limits=limits, temp_parent=temp_parent, actor='importplan-worker',
                       job=job, hooks=hooks)
    _finish_run(run['id'], 'importplan-worker')
    return outcome


def work(*, max_jobs: int | None = None, lease_seconds: float | None = None, worker_id: str | None = None) -> dict:
    """Drain ``import_plan_item`` jobs with the general worker's lease and heartbeat."""
    from ..jobs import DEFAULT_LEASE_SECONDS, LeaseLost as JobLeaseLost, claim_job
    from ..worker import process_job
    processed = []
    while max_jobs is None or len(processed) < max_jobs:
        job = claim_job(kinds=(JOB_KIND,), worker_id=worker_id, lease_seconds=lease_seconds or DEFAULT_LEASE_SECONDS)
        if job is None:
            break
        try:
            outcome = process_job(job, lease_seconds=lease_seconds or DEFAULT_LEASE_SECONDS)
            processed.append({'job_id': job['id'], **(outcome if isinstance(outcome, dict) else {})})
        except (JobLeaseLost, LeaseLost) as exc:
            processed.append({'job_id': job['id'], 'status': 'lease_lost', 'error': str(exc)})
        except Exception as exc:
            processed.append({'job_id': job['id'], 'status': 'job_failed', 'error': f'{type(exc).__name__}: {exc}'[:500]})
    return {'processed': processed, 'jobs': len(processed)}


def list_runs(plan_id: int, limit: int = 50) -> list[dict]:
    with closing(dbmod.connect()) as db:
        _plan_row(db, plan_id)
        rows = db.execute('SELECT * FROM import_plan_runs WHERE plan_id=? ORDER BY id DESC LIMIT ?', (plan_id, limit)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['options'] = json.loads(item.pop('options_json'))
        item['summary'] = json.loads(item.pop('summary_json'))
        result.append(item)
    return result


def get_run(run_id: int) -> dict:
    with closing(dbmod.connect()) as db:
        row = db.execute('SELECT * FROM import_plan_runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise NotFound('import plan run does not exist')
        run = dict(row)
        run['options'] = json.loads(run.pop('options_json'))
        run['summary'] = json.loads(run.pop('summary_json'))
        run['attempts'] = []
        for a in db.execute('''SELECT a.*, i.ordinal, i.locator FROM import_plan_attempts a JOIN import_plan_items i ON i.id=a.item_id
                               WHERE a.run_id=? ORDER BY a.id''', (run_id,)):
            attempt = dict(a)
            attempt['result'] = json.loads(attempt.pop('result_json'))
            attempt['parameters'] = json.loads(attempt.pop('parameters_json'))
            attempt['opt_ins'] = json.loads(attempt.pop('opt_ins_json'))
            attempt.pop('lease_token')
            run['attempts'].append(attempt)
        run['jobs_pending'] = _run_jobs_pending(db, run_id)
    return run

"""Case-scoped assignments of material, fragments and versions (package P1).

A source exists once. Its meaning in a case is an assignment with its own
author, status and rationale; withdrawing or deleting an assignment never
touches the source, its versions, text or citations. There is no global
evidentiary weight. Statuses and target kinds are open text: presets and
known kinds are examples, an unknown value is kept with an advisory warning.
Nothing here grants access; authorization is package P2.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
import uuid

# Preset values, not a closed list (package P9 moves them into settings).
STATUS_PRESETS = ('unknown', 'ai_proposed', 'human_reviewed', 'rejected', 'withdrawn')
TARGET_TABLES = {
    'artifact': 'artifacts', 'fragment': 'fragments', 'derived_text': 'derived_text',
    'event': 'events', 'evidence_anchor': 'evidence_anchors',
}
_UID_TABLES = ('cases', 'artifacts')


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_uid() -> str:
    """Stable identifier independent of names and paths (generator preset: uuid4 hex)."""
    return uuid.uuid4().hex


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _audit(db: sqlite3.Connection, action: str, kind: str, object_id: int, details: dict, actor: str | None) -> None:
    db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)',
               (actor or 'system', action, kind, object_id, _json(details)))


def _require(db: sqlite3.Connection, table: str, row_id: int, label: str) -> sqlite3.Row:
    row = db.execute(f'SELECT * FROM {table} WHERE id=?', (row_id,)).fetchone()
    if row is None:
        raise ValueError(f'{label} does not exist')
    return row


def ensure_uid(db: sqlite3.Connection, table: str, row_id: int, *, actor: str | None = None) -> str:
    """Return a row's uid; a row from before the model receives one on first use."""
    if table not in _UID_TABLES:
        raise ValueError(f'uid is not managed for {table}')
    row = _require(db, table, row_id, table)
    if row['uid'] is not None:
        return row['uid']
    uid = new_uid()
    db.execute(f'UPDATE {table} SET uid=? WHERE id=? AND uid IS NULL', (uid, row_id))
    stored = db.execute(f'SELECT uid FROM {table} WHERE id=?', (row_id,)).fetchone()['uid']
    if stored == uid:
        _audit(db, 'uid_assigned', table, row_id, {'uid': uid, 'reason': 'legacy_row_first_use'}, actor)
    return stored


def create_principal(db: sqlite3.Connection, display_name: str, *, kind: str = 'unknown',
                     metadata: dict | None = None, actor: str | None = None) -> int:
    uid = new_uid()
    cur = db.execute('INSERT INTO principals(uid,display_name,kind,metadata_json) VALUES(?,?,?,?)',
                     (uid, display_name, kind, _json(metadata or {})))
    _audit(db, 'principal_created', 'principal', cur.lastrowid, {'uid': uid, 'kind': kind}, actor)
    return int(cur.lastrowid)


def assign(db: sqlite3.Connection, *, case_id: int, target_kind: str, target_id: int,
           meaning_text: str | None = None, rationale_text: str | None = None,
           status: str = 'unknown', author_principal_id: int | None = None,
           author_kind: str = 'unknown', issue_ref=None, model_run_id: int | None = None,
           visibility_scope_id: int | None = None, metadata: dict | None = None,
           actor: str | None = None) -> dict:
    """Record one meaning of a target in one case; several per target are allowed."""
    _require(db, 'cases', case_id, 'case')
    warnings = []
    table = TARGET_TABLES.get(target_kind)
    if table is None:
        warnings.append({'code': 'unverified_target_kind', 'target_kind': target_kind})
    else:
        _require(db, table, target_id, target_kind)
    if status not in STATUS_PRESETS:
        warnings.append({'code': 'status_not_in_presets', 'status': status})
    if author_principal_id is not None:
        _require(db, 'principals', author_principal_id, 'principal')
    if model_run_id is not None:
        _require(db, 'processing_runs', model_run_id, 'processing run')
    uid = new_uid()
    cur = db.execute('''INSERT INTO assignments(uid,case_id,target_kind,target_id,issue_ref_json,
        meaning_text,rationale_text,status,author_principal_id,author_kind,model_run_id,
        visibility_scope_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (uid, case_id, target_kind, target_id, _json(issue_ref), meaning_text, rationale_text,
         status, author_principal_id, author_kind, model_run_id, visibility_scope_id,
         _json(metadata or {})))
    # Audit keeps identities only; meaning and rationale may carry case-private text.
    _audit(db, 'assignment_created', 'assignment', cur.lastrowid,
           {'uid': uid, 'case_id': case_id, 'target_kind': target_kind, 'target_id': target_id,
            'status': status, 'author_kind': author_kind}, actor)
    return {'id': int(cur.lastrowid), 'uid': uid, 'warnings': warnings}


def set_assignment_status(db: sqlite3.Connection, assignment_id: int, status: str, *,
                          actor: str | None = None) -> dict:
    row = _require(db, 'assignments', assignment_id, 'assignment')
    warnings = [] if status in STATUS_PRESETS else [{'code': 'status_not_in_presets', 'status': status}]
    now = _utc_now()
    # withdrawn_at mirrors the current status; the audit log keeps every change.
    withdrawn_at = now if status == 'withdrawn' else None
    db.execute('UPDATE assignments SET status=?, status_changed_at=?, withdrawn_at=? WHERE id=?',
               (status, now, withdrawn_at, assignment_id))
    _audit(db, 'assignment_status', 'assignment', assignment_id,
           {'uid': row['uid'], 'from': row['status'], 'to': status}, actor)
    return {'id': assignment_id, 'status': status, 'warnings': warnings}


def withdraw_assignment(db: sqlite3.Connection, assignment_id: int, *, actor: str | None = None) -> dict:
    """Default removal: the row and its history remain, marked withdrawn."""
    return set_assignment_status(db, assignment_id, 'withdrawn', actor=actor)


def delete_assignment(db: sqlite3.Connection, assignment_id: int, *, actor: str) -> None:
    """Explicit hard removal of one assignment; no cascade reaches the material."""
    row = _require(db, 'assignments', assignment_id, 'assignment')
    db.execute('DELETE FROM assignments WHERE id=?', (assignment_id,))
    _audit(db, 'assignment_deleted', 'assignment', assignment_id,
           {'uid': row['uid'], 'case_id': row['case_id'], 'target_kind': row['target_kind'],
            'target_id': row['target_id']}, actor)


def create_fragment(db: sqlite3.Connection, *, artifact_id: int, kind: str, selector: dict,
                    derived_text_id: int | None = None, anchor_id: int | None = None,
                    created_by_principal_id: int | None = None, metadata: dict | None = None,
                    actor: str | None = None) -> dict:
    """An addressable range in one material version; selector structure is open.

    A citation stays the evidence anchor; the fragment references it.
    """
    _require(db, 'artifacts', artifact_id, 'artifact')
    if derived_text_id is not None:
        text = _require(db, 'derived_text', derived_text_id, 'text version')
        if text['artifact_id'] != artifact_id:
            raise ValueError('text version belongs to another artifact')
    if anchor_id is not None:
        anchor = _require(db, 'evidence_anchors', anchor_id, 'citation')
        if anchor['artifact_id'] != artifact_id:
            raise ValueError('citation belongs to another artifact')
        if derived_text_id is not None and anchor['derived_text_id'] != derived_text_id:
            raise ValueError('citation belongs to another text version')
    if created_by_principal_id is not None:
        _require(db, 'principals', created_by_principal_id, 'principal')
    uid = new_uid()
    cur = db.execute('''INSERT INTO fragments(uid,artifact_id,derived_text_id,anchor_id,kind,
        selector_json,created_by_principal_id,metadata_json) VALUES(?,?,?,?,?,?,?,?)''',
        (uid, artifact_id, derived_text_id, anchor_id, kind, _json(selector),
         created_by_principal_id, _json(metadata or {})))
    _audit(db, 'fragment_created', 'fragment', cur.lastrowid,
           {'uid': uid, 'artifact_id': artifact_id, 'kind': kind}, actor)
    return {'id': int(cur.lastrowid), 'uid': uid}


def record_artifact_version(db: sqlite3.Connection, artifact_id: int, prev_artifact_id: int, *,
                            relation: str = 'supersedes', observed_at: str | None = None,
                            metadata: dict | None = None, actor: str | None = None) -> int:
    """Relate two byte versions; neither artifact nor any earlier answer basis changes."""
    _require(db, 'artifacts', artifact_id, 'artifact')
    _require(db, 'artifacts', prev_artifact_id, 'previous artifact')
    cur = db.execute('''INSERT INTO artifact_versions(artifact_id,prev_artifact_id,relation,
        observed_at,metadata_json) VALUES(?,?,?,?,?)''',
        (artifact_id, prev_artifact_id, relation, observed_at or _utc_now(), _json(metadata or {})))
    _audit(db, 'artifact_version', 'artifact', artifact_id,
           {'prev_artifact_id': prev_artifact_id, 'relation': relation}, actor)
    return int(cur.lastrowid)


def relate_cases(db: sqlite3.Connection, case_a: int, case_b: int, relation: str, *,
                 metadata: dict | None = None, actor: str | None = None) -> int:
    """Record that cases are related. A relation is data and grants no access."""
    _require(db, 'cases', case_a, 'case')
    _require(db, 'cases', case_b, 'case')
    cur = db.execute('INSERT INTO case_relations(case_a,case_b,relation,metadata_json) VALUES(?,?,?,?)',
                     (case_a, case_b, relation, _json(metadata or {})))
    _audit(db, 'case_relation', 'case', case_a, {'case_b': case_b, 'relation': relation}, actor)
    return int(cur.lastrowid)


def assignments_for_case(db: sqlite3.Connection, case_id: int, *, include_withdrawn: bool = False) -> list[dict]:
    sql = 'SELECT * FROM assignments WHERE case_id=?'
    if not include_withdrawn:
        sql += ' AND withdrawn_at IS NULL'
    return [dict(row) for row in db.execute(sql + ' ORDER BY id', (case_id,))]


def cases_for_target(db: sqlite3.Connection, target_kind: str, target_id: int, *,
                     include_withdrawn: bool = False) -> list[int]:
    sql = 'SELECT DISTINCT case_id FROM assignments WHERE target_kind=? AND target_id=?'
    if not include_withdrawn:
        sql += ' AND withdrawn_at IS NULL'
    return [row['case_id'] for row in db.execute(sql + ' ORDER BY case_id', (target_kind, target_id))]

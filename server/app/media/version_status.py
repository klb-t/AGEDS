"""Superseded text versions: an append-only mark, never an edit of the version itself.

A superseded version stays in history, exports and comparisons; default views pick the newest
version that is not superseded, and lists show the mark with its reason.
"""
from __future__ import annotations

import json
import sqlite3

# The latest status row of d decides; use in a WHERE clause on derived_text aliased as d.
NOT_SUPERSEDED_SQL = """NOT EXISTS (SELECT 1 FROM derived_text_status s WHERE s.derived_text_id=d.id AND s.status='superseded'
    AND s.id=(SELECT MAX(id) FROM derived_text_status WHERE derived_text_id=d.id))"""


def _has_table(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='derived_text_status'").fetchone() is not None


def superseded(db: sqlite3.Connection, artifact_id: int | None = None) -> dict[int, dict]:
    """{derived_text_id: {superseded_by_id, reason, actor, created_at}} for versions currently superseded."""
    if not _has_table(db):
        return {}
    where, params = ('', ()) if artifact_id is None else (' AND t.artifact_id=?', (artifact_id,))
    rows = db.execute(f'''SELECT s.* FROM derived_text_status s JOIN derived_text t ON t.id=s.derived_text_id
                          WHERE s.id=(SELECT MAX(id) FROM derived_text_status WHERE derived_text_id=s.derived_text_id){where}''',
                      params).fetchall()
    return {r['derived_text_id']: {'superseded_by_id': r['superseded_by_id'], 'reason': r['reason'], 'actor': r['actor'],
                                   'created_at': r['created_at']} for r in rows if r['status'] == 'superseded'}


def mark(db: sqlite3.Connection, derived_text_id: int, *, superseded_by_id: int | None, reason: str, actor: str,
         metadata: dict | None = None) -> int:
    """Mark superseded (superseded_by_id set) or reinstated (None). Same artifact and kind are required."""
    version = db.execute('SELECT artifact_id, kind FROM derived_text WHERE id=?', (derived_text_id,)).fetchone()
    if version is None:
        raise ValueError('text version does not exist')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('a reason is required')
    if superseded_by_id is not None:
        newer = db.execute('SELECT artifact_id, kind FROM derived_text WHERE id=?', (superseded_by_id,)).fetchone()
        if newer is None or (newer['artifact_id'], newer['kind']) != (version['artifact_id'], version['kind']):
            raise ValueError('the superseding version must be a version of the same artifact and kind')
        if superseded_by_id == derived_text_id or superseded_by_id in superseded(db, version['artifact_id']):
            raise ValueError('the superseding version must itself be current')
    status = 'superseded' if superseded_by_id is not None else 'reinstated'
    cur = db.execute('''INSERT INTO derived_text_status(derived_text_id,status,superseded_by_id,reason,actor,metadata_json)
                        VALUES (?,?,?,?,?,?)''', (derived_text_id, status, superseded_by_id, reason.strip(), actor,
                                                 json.dumps(metadata or {}, ensure_ascii=False)))
    db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES (?,?,?,?,?)',
               (actor, f'derived_text_{status}', 'derived_text', derived_text_id,
                json.dumps({'superseded_by_id': superseded_by_id, 'reason': reason.strip()}, ensure_ascii=False)))
    return int(cur.lastrowid)

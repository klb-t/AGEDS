"""Close a catalog scan that a dead process left ``running`` (explicit, audited).

A running scan is never "current", so its entries are invisible to plans and
coverage, but it stays ``running`` forever if the scanning process was killed.
There is no heartbeat in the catalog scan row, so the decision is human: an
actor states a reason, and the scan must have shown no activity (start time,
latest entry, latest issue) for ``min_idle_seconds`` (default one hour, a
preset). The scan row becomes ``failed`` with ``error='abandoned: <reason>'``
(the catalog's status CHECK has no ``abandoned`` value and rebuilding that
table would be destructive), a ``catalog_scan_abandonments`` row and an
``audit_log`` row record the decision, and no entry is changed or deleted.
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone

from .. import db as dbmod
from ..catalog.store import NotFound

DEFAULT_MIN_IDLE_SECONDS = 3600


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    text = ts.replace('Z', '+00:00')
    value = datetime.fromisoformat(text) if 'T' in text else datetime.fromisoformat(text.replace(' ', 'T'))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def abandon_scan(scan_id: int, *, actor: str, reason: str, min_idle_seconds: int = DEFAULT_MIN_IDLE_SECONDS,
                 now: datetime | None = None) -> dict:
    if not actor or not actor.strip():
        raise ValueError('actor is required')
    if not reason or not reason.strip():
        raise ValueError('reason is required')
    if not isinstance(min_idle_seconds, int) or isinstance(min_idle_seconds, bool) or min_idle_seconds < 0:
        raise ValueError('min_idle_seconds must be a non-negative integer')
    now = now or datetime.now(timezone.utc)
    with closing(dbmod.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            scan = db.execute('SELECT * FROM catalog_scans WHERE id=?', (scan_id,)).fetchone()
            if scan is None:
                raise NotFound('catalog scan does not exist')
            if scan['status'] != 'running':
                raise ValueError(f'catalog scan {scan_id} is {scan["status"]}, not running; nothing to abandon')
            last_entry = db.execute('SELECT MAX(created_at) FROM catalog_entries WHERE scan_id=?', (scan_id,)).fetchone()[0]
            last_issue = db.execute('SELECT MAX(created_at) FROM catalog_issues WHERE scan_id=?', (scan_id,)).fetchone()[0]
            moments = [m for m in (_parse(scan['started_at']), _parse(last_entry), _parse(last_issue)) if m]
            last_activity = max(moments)
            idle = (now - last_activity).total_seconds()
            if idle < min_idle_seconds:
                raise ValueError(f'catalog scan {scan_id} was active {int(idle)} s ago (< {min_idle_seconds} s); '
                                 'it may still be running')
            entries = db.execute('SELECT COUNT(*) FROM catalog_entries WHERE scan_id=?', (scan_id,)).fetchone()[0]
            db.execute("UPDATE catalog_scans SET status='failed', finished_at=?, error=? WHERE id=? AND status='running'",
                       (now.isoformat(), f'abandoned: {reason.strip()[:500]}', scan_id))
            abandonment = db.execute('''INSERT INTO catalog_scan_abandonments(scan_id,actor,reason,last_activity_at,
                                        min_idle_seconds,entries_at_abandonment) VALUES(?,?,?,?,?,?)''',
                                     (scan_id, actor, reason.strip(), last_activity.isoformat(), min_idle_seconds, entries)).lastrowid
            db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)',
                       (actor, 'catalog_scan_abandoned', 'catalog_scan', scan_id,
                        json.dumps({'reason': reason.strip(), 'previous_status': 'running', 'new_status': 'failed',
                                    'entries_kept': entries, 'idle_seconds': int(idle),
                                    'abandonment_id': abandonment}, sort_keys=True)))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'scan_id': scan_id, 'status': 'failed', 'abandoned': True, 'abandonment_id': abandonment,
            'entries_kept': entries, 'last_activity_at': last_activity.isoformat(), 'idle_seconds': int(idle),
            'note': 'stored status is failed with an abandoned: error prefix; entries are unchanged and never deleted'}


def abandonments(limit: int = 100) -> list[dict]:
    with closing(dbmod.connect()) as db:
        return [dict(r) for r in db.execute('SELECT * FROM catalog_scan_abandonments ORDER BY id DESC LIMIT ?', (limit,))]

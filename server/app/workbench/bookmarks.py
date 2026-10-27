"""Timeline bookmarks: a user-chosen time point or range with an optional reference.

A bookmark is a human annotation of a position on the common timeline, not an
event time. The typed value is kept verbatim (``*_raw``). An explicit offset or
``Z`` gives a UTC instant used for windows and ordering; a wall-clock value
without a zone keeps ``tz_status='unknown'``, has no instant, and is excluded
from instant windows (counted as ``unzoned_excluded``), never guessed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..db import session
from ..timeinfo import from_iso
from .common import (WorkbenchInputError, WorkbenchNotFound, audit, clean_name, clean_text, loads_object,
                     positive_id, utc_now)

REF_KINDS = ('event', 'transcript_segment', 'artifact', 'citation')
MAX_BOOKMARKS_PAGE = 500
DEFAULT_PAGE = 200
MAX_VIEW_BYTES = 16 * 1024
MAX_TIME_CHARS = 64


def _parse_time(raw, field: str) -> dict:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_TIME_CHARS:
        raise WorkbenchInputError(f'{field} must be ISO-8601 text up to {MAX_TIME_CHARS} characters')
    try:
        candidate = from_iso('other', raw, 'workbench_bookmark')
    except ValueError as error:
        raise WorkbenchInputError(f'{field}: {error}') from error
    key = None
    if candidate.utc is not None:
        moment = datetime.fromisoformat(candidate.utc.replace('Z', '+00:00')).astimezone(timezone.utc)
        key = moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    return {'raw': raw, 'utc': candidate.utc, 'local': candidate.local, 'tz_status': candidate.tz_status,
            'tz': candidate.tz, 'precision': candidate.precision, 'key': key}


def _window_key(raw, field: str) -> str:
    parsed = _parse_time(raw, field)
    if parsed['key'] is None:
        raise WorkbenchInputError(f'{field} needs an explicit offset or Z to bound an instant window')
    return parsed['key']


def _validate_ref(db: sqlite3.Connection, ref) -> tuple:
    if ref is None:
        return (None, None, None, None)
    if not isinstance(ref, dict):
        raise WorkbenchInputError('ref must be an object')
    kind = ref.get('kind')
    if kind not in REF_KINDS:
        raise WorkbenchInputError(f'ref.kind must be one of {", ".join(REF_KINDS)}')
    ref_id = positive_id(ref.get('id'), 'ref.id')
    version = ref.get('version_id')
    segment = ref.get('segment_index')
    if kind == 'transcript_segment':
        positive_id(version, 'ref.version_id')
        if type(segment) is not int or segment < 0:
            raise WorkbenchInputError('ref.segment_index must be a non-negative integer')
        row = db.execute("SELECT artifact_id, segments_json FROM derived_text WHERE id=? AND kind='transcript'", (version,)).fetchone()
        if row is None or row['artifact_id'] != ref_id:
            raise WorkbenchNotFound('transcript version not found for this artifact (ref.id is the artifact id)')
        try:
            segments = json.loads(row['segments_json'] or '[]')
        except ValueError as error:
            raise WorkbenchInputError('stored transcript segments are not valid JSON') from error
        if not isinstance(segments, list) or segment >= len(segments):
            raise WorkbenchInputError('ref.segment_index is outside the pinned transcript version')
        return (kind, ref_id, version, segment)
    if version is not None or segment is not None:
        raise WorkbenchInputError('version_id/segment_index apply only to transcript_segment references')
    table = {'event': 'events', 'artifact': 'artifacts', 'citation': 'evidence_anchors'}[kind]
    if db.execute(f'SELECT 1 FROM {table} WHERE id=?', (ref_id,)).fetchone() is None:
        raise WorkbenchNotFound(f'{kind} {ref_id} not found')
    return (kind, ref_id, None, None)


def _view(view) -> str:
    if view is None:
        return '{}'
    if not isinstance(view, dict):
        raise WorkbenchInputError('view must be an object (opaque client view state)')
    try:
        text = json.dumps(view, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise WorkbenchInputError('view must be JSON-serializable without NaN/Infinity') from error
    if len(text.encode('utf-8')) > MAX_VIEW_BYTES:
        raise WorkbenchInputError(f'view exceeds {MAX_VIEW_BYTES} bytes')
    return text


def _payload(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    ref = None
    if row['ref_kind']:
        ref = {'kind': row['ref_kind'], 'id': row['ref_id'], 'version_id': row['ref_version_id'],
               'segment_index': row['ref_segment_index']}
        table = {'event': 'events', 'artifact': 'artifacts', 'citation': 'evidence_anchors', 'transcript_segment': 'derived_text'}[row['ref_kind']]
        ident = row['ref_version_id'] if row['ref_kind'] == 'transcript_segment' else row['ref_id']
        ref['exists'] = db.execute(f'SELECT 1 FROM {table} WHERE id=?', (ident,)).fetchone() is not None
    def point(prefix):
        if row[f'{prefix}_raw'] is None:
            return None
        return {'raw': row[f'{prefix}_raw'], 'utc': row[f'{prefix}_utc'], 'local': row[f'{prefix}_local']}
    return {'id': row['id'], 'label': row['label'], 'note': row['note'], 'start': point('start'), 'end': point('end'),
            'tz_status': row['tz_status'], 'ref': ref, 'view': loads_object(row['view_json']),
            'created_at': row['created_at'], 'updated_at': row['updated_at'], 'epistemic_layer': 'human_annotation',
            'time_semantics': 'user-chosen timeline position; not an observed event time'}


def _times(start, end) -> tuple[dict, dict | None, str]:
    start_p = _parse_time(start, 'start')
    end_p = _parse_time(end, 'end') if end is not None else None
    if end_p is not None:
        if (start_p['key'] is None) != (end_p['key'] is None):
            raise WorkbenchInputError('start and end must both carry an offset or both be zone-less')
        if start_p['key'] is not None and end_p['key'] < start_p['key']:
            raise WorkbenchInputError('end is before start')
        if start_p['key'] is None and end_p['local'] < start_p['local']:
            raise WorkbenchInputError('end is before start (same unknown zone assumed only for this ordering check)')
    status = 'explicit_offset' if start_p['key'] is not None else 'unknown'
    return start_p, end_p, status


def create_bookmark(start, end=None, *, label=None, note=None, ref=None, view=None) -> dict:
    label = None if label is None else clean_name(label, 'label')
    note = clean_text(note, 'note')
    start_p, end_p, status = _times(start, end)
    view_json = _view(view)
    now = utc_now()
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        kind, ref_id, version, segment = _validate_ref(db, ref)
        bookmark_id = int(db.execute('''INSERT INTO workbench_bookmarks(label,note,start_raw,start_utc,start_local,start_key,
            end_raw,end_utc,end_local,end_key,tz_status,ref_kind,ref_id,ref_version_id,ref_segment_index,view_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (label, note, start_p['raw'], start_p['utc'], start_p['local'], start_p['key'],
             end_p and end_p['raw'], end_p and end_p['utc'], end_p and end_p['local'], end_p and end_p['key'],
             status, kind, ref_id, version, segment, view_json, now, now)).lastrowid)
        audit(db, 'workbench_bookmark_create', 'workbench_bookmark', bookmark_id,
              {'start': start_p['raw'], 'end': end_p and end_p['raw'], 'tz_status': status,
               'ref': {'kind': kind, 'id': ref_id, 'version_id': version, 'segment_index': segment} if kind else None})
        return _payload(db, db.execute('SELECT * FROM workbench_bookmarks WHERE id=?', (bookmark_id,)).fetchone())


_UNSET = object()


def update_bookmark(bookmark_id: int, *, start=_UNSET, end=_UNSET, label=_UNSET, note=_UNSET, ref=_UNSET, view=_UNSET) -> dict:
    positive_id(bookmark_id, 'bookmark id')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM workbench_bookmarks WHERE id=?', (bookmark_id,)).fetchone()
        if row is None:
            raise WorkbenchNotFound('bookmark not found')
        before = _payload(db, row)
        start_p, end_p, status = _times(row['start_raw'] if start is _UNSET else start,
                                        row['end_raw'] if end is _UNSET else end)
        new_label = row['label'] if label is _UNSET else (None if label is None else clean_name(label, 'label'))
        new_note = row['note'] if note is _UNSET else clean_text(note, 'note')
        view_json = row['view_json'] if view is _UNSET else _view(view)
        if ref is _UNSET:
            kind, ref_id, version, segment = row['ref_kind'], row['ref_id'], row['ref_version_id'], row['ref_segment_index']
        else:
            kind, ref_id, version, segment = _validate_ref(db, ref)
        db.execute('''UPDATE workbench_bookmarks SET label=?, note=?, start_raw=?, start_utc=?, start_local=?, start_key=?,
            end_raw=?, end_utc=?, end_local=?, end_key=?, tz_status=?, ref_kind=?, ref_id=?, ref_version_id=?, ref_segment_index=?,
            view_json=?, updated_at=? WHERE id=?''',
            (new_label, new_note, start_p['raw'], start_p['utc'], start_p['local'], start_p['key'],
             end_p and end_p['raw'], end_p and end_p['utc'], end_p and end_p['local'], end_p and end_p['key'],
             status, kind, ref_id, version, segment, view_json, utc_now(), bookmark_id))
        after = _payload(db, db.execute('SELECT * FROM workbench_bookmarks WHERE id=?', (bookmark_id,)).fetchone())
        audit(db, 'workbench_bookmark_update', 'workbench_bookmark', bookmark_id,
              {'before': {k: before[k] for k in ('label', 'note', 'start', 'end', 'ref', 'view')},
               'after': {k: after[k] for k in ('label', 'note', 'start', 'end', 'ref', 'view')}})
        return after


def delete_bookmark(bookmark_id: int) -> dict:
    positive_id(bookmark_id, 'bookmark id')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM workbench_bookmarks WHERE id=?', (bookmark_id,)).fetchone()
        if row is None:
            raise WorkbenchNotFound('bookmark not found')
        before = _payload(db, row)
        db.execute('DELETE FROM workbench_bookmarks WHERE id=?', (bookmark_id,))
        audit_id = audit(db, 'workbench_bookmark_delete', 'workbench_bookmark', bookmark_id, {'deleted': before})
        return {'deleted': bookmark_id, 'audit_id': audit_id}


def get_bookmark(bookmark_id: int) -> dict:
    positive_id(bookmark_id, 'bookmark id')
    with session() as db:
        row = db.execute('SELECT * FROM workbench_bookmarks WHERE id=?', (bookmark_id,)).fetchone()
        if row is None:
            raise WorkbenchNotFound('bookmark not found')
        return _payload(db, row)


def list_bookmarks(*, from_=None, to=None, ref_kind=None, ref_id=None, include_unzoned: bool | None = None,
                   limit: int = DEFAULT_PAGE) -> dict:
    """Bookmarks overlapping [from, to] (instants), ordered by start instant then id."""
    if type(limit) is not int or not 1 <= limit <= MAX_BOOKMARKS_PAGE:
        raise WorkbenchInputError(f'limit must be an integer from 1 to {MAX_BOOKMARKS_PAGE}')
    windowed = from_ is not None or to is not None
    if include_unzoned is None:
        include_unzoned = not windowed
    if windowed and include_unzoned:
        raise WorkbenchInputError('zone-less bookmarks have no instant and cannot be placed in an instant window')
    clauses, params = [], []
    if from_ is not None:
        clauses.append('COALESCE(end_key, start_key) >= ?')
        params.append(_window_key(from_, 'from'))
    if to is not None:
        clauses.append('start_key <= ?')
        params.append(_window_key(to, 'to'))
    if ref_kind is not None:
        if ref_kind not in REF_KINDS:
            raise WorkbenchInputError(f'ref_kind must be one of {", ".join(REF_KINDS)}')
        clauses.append('ref_kind=?')
        params.append(ref_kind)
    if ref_id is not None:
        clauses.append('ref_id=?')
        params.append(positive_id(ref_id, 'ref_id'))
    if not include_unzoned:
        clauses.append('start_key IS NOT NULL')
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    with session() as db:
        rows = db.execute(f'SELECT * FROM workbench_bookmarks{where} ORDER BY start_key IS NULL, start_key, start_local, id LIMIT ?',
                          (*params, limit + 1)).fetchall()
        unzoned_excluded = 0
        if windowed:
            filters = [c for c in clauses if 'start_key' not in c and 'end_key' not in c]
            extra = (' AND ' + ' AND '.join(filters)) if filters else ''
            extra_params = params[len(params) - len(filters):] if filters else []
            unzoned_excluded = db.execute(f'SELECT count(*) FROM workbench_bookmarks WHERE start_key IS NULL{extra}',
                                          extra_params).fetchone()[0]
        items = [_payload(db, r) for r in rows[:limit]]
    return {'items': items, 'limit': limit, 'has_more': len(rows) > limit, 'complete': len(rows) <= limit,
            'window': {'from': from_, 'to': to, 'semantics': 'overlap of [start, end or start] with [from, to] over UTC instants'},
            'include_unzoned': include_unzoned, 'unzoned_excluded': unzoned_excluded}

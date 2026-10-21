"""Read-only projections behind ``/api/read/v1``.

Every function opens the database with SQLite ``mode=ro``, runs one read
transaction under a VM-step budget and returns JSON-ready dicts with explicit
coverage flags (``has_more``, ``complete``, ``truncated``). Nothing here reads
source files, media or stored content paths; ``stored_path`` is never exposed.

Timeline windows compare the stored ``events.ts_start`` text lexicographically.
That is a raw-string window, not an instant window: SMS rows carry an offset,
WhatsApp rows carry a local wall-clock time, and the display/sort rule between
``event_times`` candidates belongs to the timeline area. This module returns the
candidates as recorded and sets ``display_time`` to ``null``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3

from ..db import connect
from ..search import MAX_RESULTS as SEARCH_MAX_RESULTS, SearchQueryError, SearchWorkLimitError, search_page

SCHEMA = 'ageds.read/v1'
MAX_SQL_INTEGER = 2**63 - 1
TIMELINE_MAX_LIMIT = 500
TIMELINE_DEFAULT_LIMIT = 100
TIMELINE_MAX_TIME_CHARS = 64
TIMELINE_MAX_FILTER_CHARS = 512
EVENT_TEXT_MAX_CHARS = 16_384
EVENT_TIME_CANDIDATES_MAX = 50
ARTIFACT_LIST_LIMIT = 200
REPORTS_LIST_LIMIT = 500
MAX_VM_STEPS = 2_000_000
PROGRESS_INTERVAL = 1000
TIMELINE_FILTERS = ('event_type', 'phone', 'direction', 'thread_id')
TIMELINE_ID_FILTERS = ('artifact_id', 'source_id')


class ReadInputError(ValueError):
    """Invalid caller input (HTTP 422)."""


class ReadNotFound(ValueError):
    """Requested object does not exist (HTTP 404)."""


class ReadLimitError(ValueError):
    """Stored data exceeds the bounded read profile (HTTP 413)."""


class ReadStoredError(ValueError):
    """Stored rows cannot form the declared projection (HTTP 409)."""


class ReadWorkLimitError(RuntimeError):
    """The SQLite work budget was exhausted; no partial result (HTTP 503)."""


LIMITS = {
    'search': {'max_limit': SEARCH_MAX_RESULTS, 'default_limit': 100, 'query_bytes': 4096},
    'timeline': {'max_limit': TIMELINE_MAX_LIMIT, 'default_limit': TIMELINE_DEFAULT_LIMIT,
                 'event_text_chars': EVENT_TEXT_MAX_CHARS, 'time_candidates_per_event': EVENT_TIME_CANDIDATES_MAX},
    'artifact': {'list_limit': ARTIFACT_LIST_LIMIT},
    'citation_packet': {'max_bytes': 2 * 1024 * 1024},
    'sqlite_vm_steps': MAX_VM_STEPS,
}


def contract() -> dict:
    """Machine-readable description of the contract a client can rely on."""
    return {
        'schema': SCHEMA,
        'authentication': {'scheme': 'Bearer', 'header': 'Authorization', 'scopes': ['read', 'propose'],
                           'token_format': 'ageds_<key>.<secret>'},
        'endpoints': {
            'contract': 'GET /api/read/v1/contract',
            'search': 'GET /api/read/v1/search?q=&limit=',
            'timeline': 'GET /api/read/v1/timeline?from=&to=&event_type=&phone=&direction=&thread_id=&artifact_id=&source_id=&limit=&cursor=',
            'artifact': 'GET /api/read/v1/artifacts/{artifact_id}',
            'citation_packet': 'GET /api/read/v1/artifacts/{artifact_id}/citations/{anchor_id}/packet',
            'reports': 'GET /api/read/v1/reports',
            'graph_export': 'GET /api/read/v1/graph/export?from=&to=',
            'intent_execute': 'POST /api/read/v1/intents/execute',
            'interpretive_validate': 'POST /api/read/v1/intents/interpretive/validate',
            'proposals': 'POST /api/integrations/v1/proposals (scope propose)',
            'hypotheses': 'POST /api/integrations/v1/hypotheses (scope propose)',
        },
        'limits': LIMITS,
        'errors': {'model': {'detail': 'text', 'code': 'stable_machine_code'},
                   'status': {'401': 'missing/unknown/revoked/expired token', '403': 'insufficient scope',
                              '404': 'object not found', '409': 'stored data cannot form the projection',
                              '413': 'bounded read profile exceeded', '422': 'invalid input',
                              '503': 'work budget exhausted or database unavailable'}},
        'never_included': ['source bytes', 'media bytes', 'stored_path', 'private seed', 'token secrets'],
        'time_semantics': {'timeline_window': 'lexicographic over stored ts_start text',
                           'display_time': 'null; projection rule owned by the timeline area',
                           'event_times': 'recorded candidates with tz_status and precision'},
    }


def _read_connection():
    db = connect(read_only=True)
    steps = 0
    exhausted = False

    def progress():
        nonlocal steps, exhausted
        steps += PROGRESS_INTERVAL
        exhausted = steps >= MAX_VM_STEPS
        return int(exhausted)
    db.set_progress_handler(progress, PROGRESS_INTERVAL)
    return db, lambda: exhausted


def run_read(reader):
    """Execute ``reader(db)`` in one read transaction with a work budget."""
    db, exhausted = _read_connection()
    try:
        db.execute('BEGIN')
        return reader(db)
    except sqlite3.OperationalError as error:
        if exhausted() and getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
            raise ReadWorkLimitError('read exceeded the SQLite work budget; narrow the request') from error
        raise
    finally:
        db.set_progress_handler(None, 0)
        db.rollback()
        db.close()


def _positive(value, name):
    if type(value) is not int or not 1 <= value <= MAX_SQL_INTEGER:
        raise ReadInputError(f'{name} must be a positive SQLite integer')
    return value


def _text_filter(value, name, max_chars):
    if value is None:
        return None
    if not isinstance(value, str) or '\x00' in value:
        raise ReadInputError(f'{name} must be text without NUL')
    if len(value) > max_chars:
        raise ReadInputError(f'{name} exceeds {max_chars} characters')
    return value


def read_search(q: str, limit: int | None = None) -> dict:
    limit = LIMITS['search']['default_limit'] if limit is None else limit
    try:
        page = search_page(q, limit)
    except SearchQueryError as error:
        raise ReadInputError(str(error)) from error
    except SearchWorkLimitError as error:
        raise ReadWorkLimitError('search exceeded the SQLite work budget; narrow the query') from error
    return {'schema': SCHEMA, 'kind': 'search', 'query': page['query'], 'limit': page['limit'],
            'has_more': page['has_more'], 'complete': page['complete'],
            'coverage_note': 'complete means the match fits the limit, not that every source is indexed',
            'items': page['results']}


def encode_cursor(ts_start, event_id: int) -> str:
    raw = json.dumps({'ts': ts_start, 'id': event_id}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_cursor(cursor: str | None):
    if cursor is None or cursor == '':
        return None
    if not isinstance(cursor, str) or len(cursor) > 512:
        raise ReadInputError('cursor is malformed')
    try:
        padded = cursor + '=' * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8'))
    except (ValueError, UnicodeError) as error:
        raise ReadInputError('cursor is malformed') from error
    if not isinstance(value, dict) or set(value) != {'ts', 'id'} or type(value['id']) is not int \
            or not (value['ts'] is None or isinstance(value['ts'], str)):
        raise ReadInputError('cursor is malformed')
    return value['ts'], value['id']


def _event_item(row) -> dict:
    item = {key: row[key] for key in ('id', 'artifact_id', 'source_id', 'event_type', 'ts_start', 'ts_end', 'direction',
                                       'sender', 'recipients', 'contact_label', 'phone_or_address', 'external_id',
                                       'thread_id', 'confidence', 'created_at')}
    for field in ('subject', 'body'):
        text = row[field]
        if isinstance(text, str) and len(text) > EVENT_TEXT_MAX_CHARS:
            item[field] = text[:EVENT_TEXT_MAX_CHARS]
            item[field + '_truncated'] = True
            item[field + '_sha256'] = hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()
        else:
            item[field] = text
            item[field + '_truncated'] = False
            item[field + '_sha256'] = hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest() if isinstance(text, str) else None
    try:
        metadata = json.loads(row['metadata_json'])
        item['metadata'] = metadata if isinstance(metadata, dict) else {'_raw': row['metadata_json']}
    except (TypeError, ValueError):
        item['metadata'] = {'_raw': row['metadata_json'], '_status': 'malformed_json_preserved'}
    item['event_times'] = []
    item['display_time'] = None
    item['time_status'] = 'stored_ts_start_text' if row['ts_start'] else 'untimed'
    return item


def read_timeline(*, from_: str | None = None, to: str | None = None, filters: dict | None = None,
                  limit: int | None = None, cursor: str | None = None) -> dict:
    limit = TIMELINE_DEFAULT_LIMIT if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= TIMELINE_MAX_LIMIT:
        raise ReadInputError(f'limit must be an integer from 1 to {TIMELINE_MAX_LIMIT}')
    from_ = _text_filter(from_, 'from', TIMELINE_MAX_TIME_CHARS)
    to = _text_filter(to, 'to', TIMELINE_MAX_TIME_CHARS)
    if from_ is not None and to is not None and from_ > to:
        raise ReadInputError('from must not exceed to')
    filters = dict(filters or {})
    unknown = set(filters) - set(TIMELINE_FILTERS) - set(TIMELINE_ID_FILTERS)
    if unknown:
        raise ReadInputError('unknown timeline filters: ' + ', '.join(sorted(unknown)))
    clauses, params = [], []
    for name in TIMELINE_FILTERS:
        value = _text_filter(filters.get(name), name, TIMELINE_MAX_FILTER_CHARS)
        if value is not None:
            column = 'phone_or_address' if name == 'phone' else name
            clauses.append(f'{column}=?')
            params.append(value)
    for name in TIMELINE_ID_FILTERS:
        if filters.get(name) is not None:
            clauses.append(f'{name}=?')
            params.append(_positive(filters[name], name))
    windowed = from_ is not None or to is not None
    if from_ is not None:
        clauses.append('ts_start >= ?')
        params.append(from_)
    if to is not None:
        clauses.append('ts_start <= ?')
        params.append(to)
    after = decode_cursor(cursor)
    if after is not None:
        ts, event_id = after
        if ts is None:
            clauses.append('((ts_start IS NULL AND id > ?) OR ts_start IS NOT NULL)')
            params.append(event_id)
        else:
            clauses.append('(ts_start IS NOT NULL AND (ts_start > ? OR (ts_start = ? AND id > ?)))')
            params.extend([ts, ts, event_id])
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''

    def reader(db):
        rows = db.execute(f'SELECT * FROM events{where} ORDER BY ts_start, id LIMIT ?', (*params, limit + 1)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [_event_item(row) for row in rows]
        by_id = {item['id']: item for item in items}
        if by_id and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_times'").fetchone():
            marks = ','.join('?' * len(by_id))
            for candidate in db.execute(f'SELECT * FROM event_times WHERE event_id IN ({marks}) ORDER BY event_id, id', tuple(by_id)):
                target = by_id[candidate['event_id']]
                if len(target['event_times']) >= EVENT_TIME_CANDIDATES_MAX:
                    target['event_times_truncated'] = True
                    continue
                try:
                    metadata = json.loads(candidate['metadata_json'])
                except (TypeError, ValueError):
                    metadata = {'_raw': candidate['metadata_json'], '_status': 'malformed_json_preserved'}
                target['event_times'].append({key: candidate[key] for key in ('id', 'kind', 'raw', 'utc', 'local', 'tz_status',
                                                                               'tz', 'precision', 'source', 'created_at')} | {'metadata': metadata})
        untimed = None
        if windowed:
            untimed = db.execute('SELECT count(*) FROM events WHERE ts_start IS NULL').fetchone()[0]
        return items, has_more, untimed

    items, has_more, untimed = run_read(reader)
    next_cursor = encode_cursor(items[-1]['ts_start'], items[-1]['id']) if has_more and items else None
    return {'schema': SCHEMA, 'kind': 'timeline_window', 'from': from_, 'to': to, 'filters': filters, 'limit': limit,
            'items': items, 'has_more': has_more, 'complete': not has_more, 'next_cursor': next_cursor,
            'order': 'stored ts_start text ascending, then id; untimed events first',
            'window_semantics': 'lexicographic over stored ts_start text; not an instant comparison',
            'display_rule': 'not_applied; the timeline area owns the display-time projection',
            'untimed_events_excluded_by_window': untimed}


def _rows(db, sql, params, limit):
    rows = db.execute(sql + ' LIMIT ?', (*params, limit + 1)).fetchall()
    return [dict(row) for row in rows[:limit]], len(rows) > limit


def _json_or_raw(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {'_raw': raw}
    except (TypeError, ValueError):
        return {'_raw': raw, '_status': 'malformed_json_preserved'}


def read_artifact(artifact_id: int) -> dict:
    _positive(artifact_id, 'artifact_id')

    def reader(db):
        row = db.execute('SELECT * FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
        if not row:
            raise ReadNotFound('artifact not found')
        artifact = {key: row[key] for key in row.keys() if key != 'stored_path'}
        artifact['metadata'] = _json_or_raw(artifact.pop('metadata_json'))
        artifact['content_available'] = bool(row['stored_path'])
        source = db.execute('SELECT id,case_id,kind,label,locator,account,imported_at FROM sources WHERE id=?', (row['source_id'],)).fetchone() if row['source_id'] else None
        observations, observations_more = _rows(db, 'SELECT * FROM source_observations WHERE artifact_id=? ORDER BY id', (artifact_id,), ARTIFACT_LIST_LIMIT)
        for observation in observations:
            observation['metadata'] = _json_or_raw(observation.pop('metadata_json'))
        if row['sha256']:
            group, group_more = _rows(db, 'SELECT id,source_id,original_name,mime_type,size_bytes,source_locator,created_at FROM artifacts WHERE sha256=? AND id!=? ORDER BY id', (row['sha256'], artifact_id), ARTIFACT_LIST_LIMIT)
        else:
            group, group_more = [], False
        versions, versions_more = _rows(db, "SELECT id,run_id,model,language,created_at,length(text) AS text_chars FROM derived_text WHERE artifact_id=? AND kind='transcript' ORDER BY id DESC", (artifact_id,), ARTIFACT_LIST_LIMIT)
        other_texts, other_more = _rows(db, "SELECT id,kind,run_id,model,language,created_at,length(text) AS text_chars FROM derived_text WHERE artifact_id=? AND kind!='transcript' ORDER BY id DESC", (artifact_id,), ARTIFACT_LIST_LIMIT)
        counts = {name: db.execute(sql, (artifact_id,)).fetchone()[0] for name, sql in (
            ('events', 'SELECT count(*) FROM events WHERE artifact_id=?'),
            ('annotations', 'SELECT count(*) FROM annotations WHERE artifact_id=?'),
            ('citations', 'SELECT count(*) FROM evidence_anchors WHERE artifact_id=?'),
        )}
        return {'schema': SCHEMA, 'kind': 'artifact', 'artifact': artifact,
                'source': dict(source) if source else None,
                'observations': observations, 'observations_truncated': observations_more,
                'content_group': {'sha256': row['sha256'], 'other_artifacts': group, 'truncated': group_more,
                                  'note': 'same bytes under another locator keep a separate artifact row; identity is the digest'},
                'transcript_versions': versions, 'transcript_versions_truncated': versions_more,
                'other_derived_text': other_texts, 'other_derived_text_truncated': other_more,
                'counts': counts, 'content_bytes_included': False, 'stored_path_included': False}
    return run_read(reader)


def read_citation_packet(artifact_id: int, anchor_id: int) -> bytes:
    """Canonical packet bytes exactly as the existing exchange producer emits them."""
    from ..exchange import PacketLimitError, PacketNotFound, canonical_packet_bytes, export_citation_packet
    _positive(artifact_id, 'artifact_id')
    _positive(anchor_id, 'anchor_id')
    try:
        return canonical_packet_bytes(export_citation_packet(artifact_id, anchor_id))
    except PacketNotFound as error:
        raise ReadNotFound(str(error)) from error
    except PacketLimitError as error:
        raise ReadLimitError(str(error)) from error
    except (ValueError, TypeError, UnicodeError) as error:
        raise ReadStoredError('stored evidence cannot form a valid citation packet') from error


def read_reports() -> dict:
    """Report register listing (reports area); output locations are never exposed.

    Rows come from the append-only ``reports`` table. ``output_dir`` (a private
    filesystem path) is withheld; files are served only by the workbench for
    registered reports after their manifest digest matches the register row.
    """
    def reader(db):
        from ..reports.store import list_reports, table_exists
        if not table_exists(db):
            return None
        return list_reports(db, limit=REPORTS_LIST_LIMIT + 1, include_output_dir=False)
    rows = run_read(reader)
    if rows is None:
        return {'schema': SCHEMA, 'kind': 'reports', 'available': False, 'items': [], 'has_more': False,
                'reason': 'the reports register table is not present (database not migrated)',
                'output_location_included': False}
    return {'schema': SCHEMA, 'kind': 'reports', 'available': True, 'items': rows[:REPORTS_LIST_LIMIT],
            'has_more': len(rows) > REPORTS_LIST_LIMIT, 'reason': None, 'output_location_included': False,
            'note': 'newest first; manifests are unsigned digests, not signatures'}

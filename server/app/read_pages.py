"""Bounded, read-only descending-ID pages over existing evidence read models."""
from __future__ import annotations

import json
import sqlite3

MAX_SQL_INTEGER = 2**63 - 1
MAX_LIMIT = 100
MAX_ROW_BYTES = 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_SELECTOR_DEPTH = 32
MAX_SELECTOR_NODES = 100_000
MAX_SQLITE_STEPS = 1_000_000
SQLITE_PROGRESS_INTERVAL = 1_000


class PageInputError(ValueError):
    """Invalid caller IDs, cursor pairing, kind or limit (HTTP 422)."""


class PageNotFound(ValueError):
    """The requested artifact does not exist (HTTP 404)."""


class PageStoredError(ValueError):
    """A selected stored row cannot form the declared projection (HTTP 409)."""


class PageLimitError(ValueError):
    """The next row cannot fit a bounded page without skipping (HTTP 413)."""


# Only fields actually exposed by the corresponding list APIs are read.
MODELS = {
    'transcripts': ('derived_text', ('id', 'artifact_id', 'run_id', 'model', 'language', 'created_at'), " AND kind='transcript'"),
    'citations': ('evidence_anchors', ('id', 'artifact_id', 'derived_text_id', 'start_ms', 'end_ms',
                                     'quote_text', 'quote_sha256', 'selector_json', 'created_at'), ''),
    'annotations': ('annotations', ('id', 'artifact_id', 'kind', 'label', 'body', 'start_ms',
                                   'end_ms', 'created_at', 'derived_text_id'), ''),
}


def _integer(value, *, zero=False):
    return type(value) is int and (0 if zero else 1) <= value <= MAX_SQL_INTEGER


def _json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
        raise PageStoredError('stored row cannot form finite UTF-8 JSON') from error


def _selector(raw):
    if not isinstance(raw, str):
        raise PageStoredError('stored selector must be JSON text')
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
            if depth > MAX_SELECTOR_DEPTH:
                raise PageLimitError('stored selector exceeds depth limit')
        elif char in ']}':
            depth -= 1
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PageStoredError('stored selector has duplicate keys')
            result[key] = value
        return result
    def constant(_):
        raise PageStoredError('stored selector has nonfinite values')
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError) as error:
        raise PageStoredError('stored selector is invalid JSON') from error
    if not isinstance(value, dict):
        raise PageStoredError('stored selector must be an object')
    pending, remaining = [value], MAX_SELECTOR_NODES
    while pending:
        item = pending.pop()
        remaining -= 1
        if remaining < 0:
            raise PageLimitError('stored selector exceeds node limit')
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    _json_bytes(value)  # also catches overflow float literals and surrogates
    return value


def _project(kind, row):
    result = dict(row)
    if not _integer(result['id']) or not _integer(result['artifact_id']):
        raise PageStoredError('stored row identity is invalid')
    if kind == 'citations':
        result['selector'] = _selector(result.pop('selector_json'))
        result['validation'] = 'matches_stored_transcript_version'
        result['audio_verification'] = 'not_performed'
    elif kind == 'annotations':
        result = {'id': row['id'], 'artifactId': row['artifact_id'], 'kind': row['kind'],
                  'label': row['label'], 'body': row['body'], 'startMs': row['start_ms'],
                  'endMs': row['end_ms'], 'createdAt': row['created_at'],
                  'derivedTextId': row['derived_text_id']}
    return result


def read_page(artifact_id, kind, *, limit=50, before_id=None, snapshot_max_id=None):
    """Return a bounded page in one read transaction without migrating/writing.

    Cursor bounds are not ownership/authentication tokens. Every query filters
    artifact and kind. An ID upper bound excludes normal later appends, not
    backfills below that bound or updates/deletions between requests.
    """
    if not _integer(artifact_id):
        raise PageInputError('artifact ID must be a positive SQLite integer')
    if not isinstance(kind, str) or kind not in MODELS:
        raise PageInputError('unsupported read-page kind')
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise PageInputError('page limit must be between 1 and 100')
    if (before_id is None) != (snapshot_max_id is None):
        raise PageInputError('before_id and snapshot_max_id must be supplied together')
    if before_id is not None:
        if not _integer(before_id) or not _integer(snapshot_max_id, zero=True):
            raise PageInputError('cursor IDs must be SQLite integers; only snapshot may be zero')
        if snapshot_max_id and before_id > snapshot_max_id:
            raise PageInputError('before_id cannot exceed snapshot_max_id')
    from .db import connect
    db = connect(read_only=True)
    work = {'steps': 0, 'exceeded': False}
    def progress():
        work['steps'] += SQLITE_PROGRESS_INTERVAL
        work['exceeded'] = work['steps'] > MAX_SQLITE_STEPS
        return int(work['exceeded'])
    db.set_progress_handler(progress, SQLITE_PROGRESS_INTERVAL)
    try:
        db.execute('BEGIN')
        if db.execute('SELECT 1 FROM artifacts WHERE id=?', (artifact_id,)).fetchone() is None:
            raise PageNotFound('artifact not found')
        table, columns, extra = MODELS[kind]
        if snapshot_max_id is None:
            maximum = db.execute(f'SELECT id FROM {table} WHERE artifact_id=?{extra} ORDER BY id DESC LIMIT 1',
                                 (artifact_id,)).fetchone()
            snapshot_max_id = maximum[0] if maximum else 0
            if not _integer(snapshot_max_id, zero=True):
                raise PageStoredError('stored maximum identity is invalid')
        where = f'artifact_id=?{extra} AND id<=?'
        params = [artifact_id, snapshot_max_id]
        if before_id is not None:
            where += ' AND id<?'
            params.append(before_id)
        # Fetch only bounded scalar IDs and byte counts before any large TEXT.
        lengths = '+'.join(f'coalesce(length(cast("{name}" as blob)),0)' for name in columns)
        candidates = db.execute(f'SELECT id,({lengths}) AS row_bytes FROM {table} WHERE {where} ORDER BY id DESC LIMIT ?',
                                (*params, limit + 1)).fetchall()
        items = []
        page = {'artifactId': artifact_id, 'items': items, 'nextBeforeId': None,
                'snapshotMaxId': snapshot_max_id, 'hasMore': False, 'limit': limit}
        for position, candidate in enumerate(candidates):
            if position == limit:
                page['hasMore'] = True
                break
            if not _integer(candidate['id']):
                raise PageStoredError('stored row identity is invalid')
            if candidate['row_bytes'] > MAX_ROW_BYTES:
                if not items:
                    raise PageLimitError('next stored row exceeds the row byte limit')
                page['hasMore'] = True
                break
            names = ','.join('"' + name + '"' for name in columns)
            row = db.execute(f'SELECT {names} FROM {table} WHERE id=? AND artifact_id=?',
                             (candidate['id'], artifact_id)).fetchone()
            try:
                item = _project(kind, row)
                item_size = len(_json_bytes(item))
            except PageLimitError:
                if not items:
                    raise
                page['hasMore'] = True
                break
            if item_size > MAX_ROW_BYTES:
                if not items:
                    raise PageLimitError('next projected row exceeds the row byte limit')
                page['hasMore'] = True
                break
            items.append(item)
            page['hasMore'] = position + 1 < len(candidates)
            page['nextBeforeId'] = item['id'] if page['hasMore'] else None
            if len(_json_bytes(page)) > MAX_PAGE_BYTES:
                items.pop()
                if not items:
                    raise PageLimitError('next projected row exceeds the page byte limit')
                page['hasMore'] = True
                break
        page['nextBeforeId'] = items[-1]['id'] if page['hasMore'] and items else None
        if len(_json_bytes(page)) > MAX_PAGE_BYTES:
            raise PageLimitError('page envelope exceeds the byte limit')
        return page
    except sqlite3.OperationalError as error:
        if work['exceeded']:
            raise PageLimitError('read page exceeds the SQLite work limit') from error
        raise
    finally:
        db.set_progress_handler(None, 0)
        db.rollback()
        db.close()

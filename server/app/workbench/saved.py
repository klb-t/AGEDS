"""Saved searches: a name for a query and its layer filter, never a stored result.

Running a saved search executes the query again against the current index.
Only counts computed by code (returned hits, facets, coverage flags) are kept,
in the saved row (last run) and in an append-only run log, so a later reader
can see how the answer changed as material was added. Result rows are not
stored: they are not evidence and would go stale silently.
"""
from __future__ import annotations

import json
import sqlite3

from ..db import session
from ..search import validate_query
from .common import (WorkbenchConflict, WorkbenchInputError, WorkbenchNotFound, audit, clean_name,
                     clean_text, loads_object, positive_id, utc_now)
from .layered_search import layered_search_page, validate_layers

MAX_SAVED_SEARCHES = 10_000
RUN_HISTORY_LIMIT = 50


def _payload(row: sqlite3.Row, runs: list[dict] | None = None) -> dict:
    filters = loads_object(row['filters_json'])
    item = {
        'id': row['id'], 'name': row['name'], 'query': row['query'], 'filters': filters,
        'layers': filters.get('layers', []), 'limit': row['result_limit'], 'note': row['note'],
        'created_at': row['created_at'], 'updated_at': row['updated_at'],
        'last_run_at': row['last_run_at'], 'last_run': loads_object(row['last_run_json']) if row['last_run_json'] else None,
        'results_stored': False, 'epistemic_layer': 'human_annotation',
    }
    if runs is not None:
        item['runs'] = runs
    return item


def _validated(query, layers, limit) -> tuple[str, list[str], int]:
    if type(limit) is not int:
        raise WorkbenchInputError('limit must be an integer')
    validate_query(query, limit)
    if not query.strip():
        raise WorkbenchInputError('a saved search needs a non-blank query')
    return query, validate_layers(layers), limit


def create_saved_search(name, query, layers=None, limit: int = 100, note=None) -> dict:
    name = clean_name(name)
    note = clean_text(note, 'note')
    query, layers, limit = _validated(query, layers, limit)
    now = utc_now()
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT count(*) FROM workbench_saved_searches').fetchone()[0] >= MAX_SAVED_SEARCHES:
            raise WorkbenchConflict(f'saved search limit reached ({MAX_SAVED_SEARCHES})')
        if db.execute('SELECT 1 FROM workbench_saved_searches WHERE name=?', (name,)).fetchone():
            raise WorkbenchConflict('a saved search with this name already exists')
        cur = db.execute('''INSERT INTO workbench_saved_searches(name,query,filters_json,result_limit,note,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?)''', (name, query, json.dumps({'layers': layers}), limit, note, now, now))
        saved_id = int(cur.lastrowid)
        audit(db, 'workbench_saved_search_create', 'workbench_saved_search', saved_id,
              {'name': name, 'query': query, 'layers': layers, 'limit': limit})
        return _payload(db.execute('SELECT * FROM workbench_saved_searches WHERE id=?', (saved_id,)).fetchone())


_UNSET = object()


def update_saved_search(saved_id: int, *, name=_UNSET, query=_UNSET, layers=_UNSET, limit=_UNSET, note=_UNSET) -> dict:
    positive_id(saved_id, 'saved search id')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM workbench_saved_searches WHERE id=?', (saved_id,)).fetchone()
        if row is None:
            raise WorkbenchNotFound('saved search not found')
        before = _payload(row)
        new_name = before['name'] if name is _UNSET else clean_name(name)
        new_note = before['note'] if note is _UNSET else clean_text(note, 'note')
        new_query, new_layers, new_limit = _validated(
            before['query'] if query is _UNSET else query,
            before['layers'] if layers is _UNSET else layers,
            before['limit'] if limit is _UNSET else limit)
        if new_name != before['name'] and db.execute('SELECT 1 FROM workbench_saved_searches WHERE name=?', (new_name,)).fetchone():
            raise WorkbenchConflict('a saved search with this name already exists')
        db.execute('''UPDATE workbench_saved_searches SET name=?, query=?, filters_json=?, result_limit=?, note=?, updated_at=?
                      WHERE id=?''', (new_name, new_query, json.dumps({'layers': new_layers}), new_limit, new_note, utc_now(), saved_id))
        audit(db, 'workbench_saved_search_update', 'workbench_saved_search', saved_id,
              {'before': {k: before[k] for k in ('name', 'query', 'layers', 'limit', 'note')},
               'after': {'name': new_name, 'query': new_query, 'layers': new_layers, 'limit': new_limit, 'note': new_note}})
        return _payload(db.execute('SELECT * FROM workbench_saved_searches WHERE id=?', (saved_id,)).fetchone())


def delete_saved_search(saved_id: int) -> dict:
    positive_id(saved_id, 'saved search id')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM workbench_saved_searches WHERE id=?', (saved_id,)).fetchone()
        if row is None:
            raise WorkbenchNotFound('saved search not found')
        before = _payload(row)
        db.execute('DELETE FROM workbench_saved_searches WHERE id=?', (saved_id,))
        audit_id = audit(db, 'workbench_saved_search_delete', 'workbench_saved_search', saved_id,
                         {'deleted': {k: before[k] for k in ('name', 'query', 'layers', 'limit', 'note', 'created_at')},
                          'run_log_kept': True})
        return {'deleted': saved_id, 'audit_id': audit_id, 'run_log_kept': True}


def list_saved_searches() -> list[dict]:
    with session() as db:
        return [_payload(row) for row in db.execute('SELECT * FROM workbench_saved_searches ORDER BY name, id')]


def get_saved_search(saved_id: int) -> dict:
    positive_id(saved_id, 'saved search id')
    with session() as db:
        row = db.execute('SELECT * FROM workbench_saved_searches WHERE id=?', (saved_id,)).fetchone()
        if row is None:
            raise WorkbenchNotFound('saved search not found')
        runs = [{'id': r['id'], 'run_at': r['run_at'], 'query': r['query'], 'filters': loads_object(r['filters_json']),
                 'counts': loads_object(r['counts_json'])}
                for r in db.execute('SELECT * FROM workbench_saved_search_runs WHERE saved_search_id=? ORDER BY id DESC LIMIT ?',
                                    (saved_id, RUN_HISTORY_LIMIT))]
        return _payload(row, runs)


def counts_of(page: dict) -> dict:
    by_epistemic: dict[str, int] = {}
    for hit in page['results']:
        by_epistemic[hit['epistemic_layer']] = by_epistemic.get(hit['epistemic_layer'], 0) + 1
    return {'returned': len(page['results']), 'has_more': page['has_more'], 'complete': page['complete'],
            'limit': page['limit'], 'facets': page['facets'], 'facets_complete': page['facets_complete'],
            'facet_scan_limit': page['facet_scan_limit'], 'returned_by_epistemic_layer': by_epistemic,
            'computed_by': 'workbench.layered_search_page', 'results_stored': False}


def run_saved_search(saved_id: int) -> dict:
    """Execute the stored query now; persist counts only; return the live page."""
    saved = get_saved_search(saved_id)
    page = layered_search_page(saved['query'], saved['layers'] or None, saved['limit'])
    counts = counts_of(page)
    now = utc_now()
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM workbench_saved_searches WHERE id=?', (saved_id,)).fetchone() is None:
            raise WorkbenchNotFound('saved search was deleted during the run')
        db.execute('UPDATE workbench_saved_searches SET last_run_at=?, last_run_json=? WHERE id=?',
                   (now, json.dumps(counts, sort_keys=True), saved_id))
        db.execute('INSERT INTO workbench_saved_search_runs(saved_search_id,run_at,query,filters_json,counts_json) VALUES (?,?,?,?,?)',
                   (saved_id, now, saved['query'], json.dumps({'layers': saved['layers']}), json.dumps(counts, sort_keys=True)))
    return {'saved_search': get_saved_search(saved_id), 'page': page, 'counts': counts}

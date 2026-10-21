"""Search across evidence layers at once, with a layer filter and facet counts.

Query semantics are those of ``server.app.search``: the same validation
(UTF-8 bytes, lexical tokens, nesting, FTS5 parser in an isolated index), the
same result bounds, ``has_more``/``complete`` and a live SQLite VM budget whose
exhaustion returns no partial result. The query text is never rewritten.

The index is ``workbench_fts`` (schema version 80), separate from the legacy
``search_fts``. Each hit names its layer and an epistemic label (raw source
field, parser output, model output, human annotation or unclassified).
Snippets are plain source text; HTML consumers must escape them.
"""
from __future__ import annotations

import sqlite3

from ..db import connect
from ..search import (MAX_RESULTS, MAX_VM_STEPS, PROGRESS_INTERVAL, SearchQueryError,
                      SearchWorkLimitError, validate_query)
from .layers import LAYER_NAMES, epistemic_of, layer_of, layer_predicate, legend

FACET_SCAN_LIMIT = 5000
INDEX_NAME = 'workbench_fts'
INDEX_SCOPE = ('artifacts (name, locator, MIME type, metadata JSON), events (type, contact, address, '
               'sender, recipients, subject, body), derived_text (all kinds), annotations (label, body)')


def validate_layers(layers) -> list[str]:
    """Return the requested layers in canonical order; unknown names are input errors."""
    if layers is None:
        return []
    if isinstance(layers, str) or not isinstance(layers, (list, tuple)):
        raise SearchQueryError('layers must be a list of layer names')
    if len(layers) > 4 * len(LAYER_NAMES):
        raise SearchQueryError('too many layer values')
    for layer in layers:
        if not isinstance(layer, str) or layer not in LAYER_NAMES:
            raise SearchQueryError(f'unknown layer: {layer!r}; expected one of {", ".join(LAYER_NAMES)}')
    wanted = set(layers)
    return [name for name in LAYER_NAMES if name in wanted]


class _Budget:
    """Per-statement VM budget; exhaustion interrupts the statement."""

    def __init__(self, db: sqlite3.Connection):
        self.steps = 0
        self.exhausted = False
        db.set_progress_handler(self, PROGRESS_INTERVAL)

    def __call__(self):
        self.steps += PROGRESS_INTERVAL
        self.exhausted = self.steps >= MAX_VM_STEPS
        return int(self.exhausted)

    def run(self, db: sqlite3.Connection, sql: str, params) -> list[sqlite3.Row]:
        self.steps, self.exhausted = 0, False
        try:
            return db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as error:
            if self.exhausted and getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
                raise SearchWorkLimitError('search exceeded SQLite work budget; simplify the query or narrow the layers') from error
            raise


def _empty_facets() -> dict:
    return {name: 0 for name in LAYER_NAMES}


def _enrich(db: sqlite3.Connection, budget: _Budget, hits: list[dict]) -> None:
    by_kind: dict[str, list[int]] = {}
    for hit in hits:
        by_kind.setdefault(hit['object_kind'], []).append(hit['object_id'])
    details: dict[tuple[str, int], dict] = {}
    queries = {
        'artifact': 'SELECT id, sha256, mime_type, size_bytes, created_at FROM artifacts WHERE id IN ({})',
        'event': 'SELECT id, event_type, ts_start, direction, source_id, created_at FROM events WHERE id IN ({})',
        'derived_text': '''SELECT d.id, d.kind, d.run_id, d.model, d.language, d.created_at,
                           r.status AS run_status, r.tool AS run_tool, r.model AS run_model, r.model_version AS run_model_version
                           FROM derived_text d LEFT JOIN processing_runs r ON r.id = d.run_id WHERE d.id IN ({})''',
        'annotation': '''SELECT id, kind, event_id, derived_text_id, start_ms, end_ms, created_at, updated_at
                         FROM annotations WHERE id IN ({})''',
    }
    for kind, ids in by_kind.items():
        unique = sorted(set(ids))
        for row in budget.run(db, queries[kind].format(','.join('?' * len(unique))), unique):
            details[(kind, row['id'])] = {key: row[key] for key in row.keys() if key != 'id'}
    for hit in hits:
        record = details.get((hit['object_kind'], hit['object_id']))
        hit['object_status'] = 'present' if record is not None else 'missing'
        record = record or {}
        if hit['object_kind'] == 'derived_text':
            hit['provenance'] = {
                'version_id': hit['object_id'], 'kind': record.get('kind'), 'run_id': record.get('run_id'),
                'model': record.get('model'), 'language': record.get('language'), 'created_at': record.get('created_at'),
                'run_status': record.get('run_status'), 'run_tool': record.get('run_tool'),
                'run_model': record.get('run_model'), 'run_model_version': record.get('run_model_version'),
                'provenance_status': 'recorded_processing_run' if record.get('run_id') else 'legacy_unknown',
            }
        else:
            hit['provenance'] = record


def layered_search_page(q: str, layers=None, limit: int = 100) -> dict:
    """Ranked hits over the selected layers (all when empty) plus facet counts over all layers."""
    validate_query(q, limit)
    selected = validate_layers(layers)
    page = {
        'query': q, 'layers': selected or list(LAYER_NAMES), 'layer_filter': bool(selected),
        'results': [], 'limit': limit, 'has_more': False, 'complete': True,
        'facets': _empty_facets(), 'facets_complete': True, 'facet_scan_limit': FACET_SCAN_LIMIT,
        'index': INDEX_NAME, 'index_scope': INDEX_SCOPE, 'epistemic_legend': legend(),
    }
    if not q.strip():
        return page
    clauses, params = [], []
    for layer in selected:
        clause, values = layer_predicate(layer)
        clauses.append(clause)
        params.extend(values)
    where = f" AND ({' OR '.join(clauses)})" if clauses else ''
    db = connect(read_only=True)
    try:
        budget = _Budget(db)
        db.execute('BEGIN')
        rows = budget.run(db, f"""SELECT d.object_kind, d.object_id, d.subkind, d.artifact_id, d.title,
                snippet(workbench_fts, 3, '', '', '…', 24) AS snippet, bm25(workbench_fts) AS rank
                FROM workbench_fts JOIN workbench_search_docs d ON d.id = workbench_fts.rowid
                WHERE workbench_fts MATCH ?{where}
                ORDER BY rank, workbench_fts.rowid LIMIT ?""", (q, *params, limit + 1))
        facet_rows = budget.run(db, f"""SELECT d.object_kind,
                CASE WHEN d.object_kind='derived_text' THEN d.subkind END AS subkind, count(*) AS n
                FROM (SELECT rowid AS r FROM workbench_fts WHERE workbench_fts MATCH ? LIMIT ?) m
                JOIN workbench_search_docs d ON d.id = m.r
                GROUP BY d.object_kind, CASE WHEN d.object_kind='derived_text' THEN d.subkind END""",
                (q, FACET_SCAN_LIMIT))
        beyond = budget.run(db, 'SELECT 1 FROM workbench_fts WHERE workbench_fts MATCH ? LIMIT 1 OFFSET ?',
                            (q, FACET_SCAN_LIMIT))
        hits = []
        for row in rows[:limit]:
            layer = layer_of(row['object_kind'], row['subkind'])
            hits.append({
                'layer': layer, 'epistemic_layer': epistemic_of(layer),
                'object_kind': row['object_kind'], 'object_id': row['object_id'], 'subkind': row['subkind'],
                'artifact_id': row['artifact_id'], 'title': row['title'], 'snippet': row['snippet'], 'rank': row['rank'],
                'url': f"/artifact/{row['artifact_id']}" if row['artifact_id'] is not None else None,
            })
        _enrich(db, budget, hits)
        db.rollback()
    finally:
        db.set_progress_handler(None, 0)
        db.close()
    facets = _empty_facets()
    for row in facet_rows:
        facets[layer_of(row['object_kind'], row['subkind'])] += row['n']
    has_more = len(rows) > limit
    page.update(results=hits, has_more=has_more, complete=not has_more, facets=facets, facets_complete=not beyond)
    return page


__all__ = ['FACET_SCAN_LIMIT', 'MAX_RESULTS', 'SearchQueryError', 'SearchWorkLimitError',
           'layered_search_page', 'validate_layers']

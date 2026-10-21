"""Consistency check and explicit rebuild of the workbench search index (schema v80).

The index is derived data: it can always be recomputed from artifacts, events,
derived_text and annotations through the ``workbench_search_source_*`` views.
A rebuild touches only workbench-owned tables.
"""
from __future__ import annotations

import sqlite3

from ..db import session
from .common import audit

KINDS = ('artifact', 'event', 'derived_text', 'annotation')
_COLUMNS = 'object_kind, object_id, subkind, artifact_id, title, content, folded'


def _counts(db: sqlite3.Connection) -> dict:
    result = {}
    for kind in KINDS:
        view = f'workbench_search_source_{kind}'
        expected = db.execute(f'SELECT count(*) FROM {view}').fetchone()[0]
        indexed = db.execute('SELECT count(*) FROM workbench_search_docs WHERE object_kind=?', (kind,)).fetchone()[0]
        missing = db.execute(f'''SELECT count(*) FROM {view} s WHERE NOT EXISTS (
            SELECT 1 FROM workbench_search_docs d WHERE d.object_kind=s.object_kind AND d.object_id=s.object_id)''').fetchone()[0]
        stale = db.execute(f'''SELECT count(*) FROM {view} s JOIN workbench_search_docs d
            ON d.object_kind=s.object_kind AND d.object_id=s.object_id
            WHERE d.subkind IS NOT s.subkind OR d.artifact_id IS NOT s.artifact_id OR d.title IS NOT s.title
               OR d.content IS NOT s.content OR d.folded IS NOT s.folded''').fetchone()[0]
        orphaned = db.execute(f'''SELECT count(*) FROM workbench_search_docs d WHERE d.object_kind=? AND NOT EXISTS (
            SELECT 1 FROM {view} s WHERE s.object_id=d.object_id)''', (kind,)).fetchone()[0]
        result[kind] = {'expected': expected, 'indexed': indexed, 'missing': missing, 'stale': stale, 'orphaned': orphaned}
    return result


def index_status() -> dict:
    with session() as db:
        by_kind = _counts(db)
        try:
            db.execute("INSERT INTO workbench_fts(workbench_fts, rank) VALUES ('integrity-check', 1)")
            fts_ok, fts_error = True, None
        except sqlite3.DatabaseError as error:
            fts_ok, fts_error = False, str(error)
        db.rollback()
    consistent = fts_ok and all(v['missing'] == 0 and v['stale'] == 0 and v['orphaned'] == 0 for v in by_kind.values())
    return {'index': 'workbench_fts', 'by_kind': by_kind, 'fts_integrity_ok': fts_ok, 'fts_integrity_error': fts_error,
            'consistent': consistent}


def rebuild_index(*, channel: str = 'cli') -> dict:
    """Recompute every index document from the source views (workbench tables only)."""
    before = index_status()
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM workbench_search_docs')
        for kind in KINDS:
            db.execute(f'INSERT INTO workbench_search_docs({_COLUMNS}) SELECT {_COLUMNS} FROM workbench_search_source_{kind}')
        db.execute("INSERT INTO workbench_fts(workbench_fts) VALUES ('rebuild')")
        audit(db, 'workbench_index_rebuild', 'workbench_index', None,
              {'channel': channel, 'before': before['by_kind'], 'evidence_changed': False})
    after = index_status()
    return {'before': before, 'after': after}

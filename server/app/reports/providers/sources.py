"""Read-only view of preserved content: artifacts, acquisition observations and
content groups (same SHA-256 under several artifact rows/locators).

Equal digests mean equal bytes, not authorship, truth or time of creation.
Acquisition times (``observed_at``) are when bytes were acquired, not when the
content was made; they form a separate basis and are never merged with event time.
"""
from __future__ import annotations

from ..blocks import kv, table
from . import Section

VERSION = 'sources.v1'


def provide(ctx) -> dict:
    section = Section(ctx, 'sources', __name__, VERSION, 'section_sources')
    db, max_rows = ctx.db, ctx.params['max_rows']
    totals = db.execute('''SELECT COUNT(*) AS artifacts, SUM(sha256 IS NULL) AS sha256_unknown,
                           COUNT(DISTINCT sha256) AS distinct_sha256 FROM artifacts''').fetchone()
    observations = db.execute('SELECT COUNT(*) FROM source_observations').fetchone()[0]
    distinct_bytes = db.execute('''SELECT COALESCE(SUM(size_bytes),0) FROM (SELECT sha256, MAX(size_bytes) AS size_bytes
                                   FROM artifacts WHERE sha256 IS NOT NULL GROUP BY sha256)''').fetchone()[0]
    groups_total = db.execute('''SELECT COUNT(*) FROM (SELECT sha256 FROM artifacts WHERE sha256 IS NOT NULL
                                 GROUP BY sha256 HAVING COUNT(*) > 1)''').fetchone()[0]
    if not totals['artifacts']:
        section.status('empty', 'no artifacts')
    section.block(kv('sources.summary', [
        ('artifacts', totals['artifacts']), ('observations', observations),
        ('sha256_unknown', totals['sha256_unknown'] or 0), ('content_groups', groups_total),
        ('bytes', distinct_bytes),
    ], ctx.lang))
    if totals['sha256_unknown']:
        section.unknown('artifacts.sha256')
    rows = [dict(r) for r in db.execute('''SELECT COALESCE(mime_type, 'unknown') AS mime_or_type, COUNT(*) AS artifacts,
                                           COALESCE(SUM(size_bytes),0) AS bytes, SUM(size_bytes IS NULL) AS size_unknown
                                           FROM artifacts GROUP BY 1 ORDER BY artifacts DESC, 1''')]
    section.block(table('sources.by_type', [('mime_or_type', 'text'), ('artifacts', 'number'), ('bytes', 'number'),
                                            ('size_unknown', 'number')], rows[:max_rows], ctx.lang,
                        title_key='by_type', total_rows=len(rows), truncated=len(rows) > max_rows))
    rows = [dict(r) for r in db.execute('''SELECT acquisition_kind, COUNT(*) AS observations,
                                           MIN(observed_at) AS first_observed_at, MAX(observed_at) AS last_observed_at
                                           FROM source_observations GROUP BY 1 ORDER BY 1''')]
    section.block(table('sources.acquisitions', [('acquisition_kind', 'text'), ('observations', 'number'),
                                                 ('first_observed_at', 'time'), ('last_observed_at', 'time')],
                        rows, ctx.lang, title_key='observations'))
    if any(row['acquisition_kind'] == 'legacy_snapshot' for row in rows):
        section.unknown('historical_acquisition_time_of_legacy_snapshots')
    has_collections = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collections'").fetchone()
    if has_collections:
        rows = [dict(r) for r in db.execute('''SELECT o.collection_id, c.label AS collection_label,
                COUNT(*) AS observations, COUNT(DISTINCT o.artifact_id) AS artifacts
                FROM source_observations o LEFT JOIN collections c ON c.id=o.collection_id
                GROUP BY o.collection_id ORDER BY o.collection_id IS NULL, o.collection_id''')]
        section.block(table('sources.collections', [('collection_id', 'id'), ('collection_label', 'text'),
                                                    ('observations', 'number'), ('artifacts', 'number')],
                            rows[:max_rows], ctx.lang, title_key='collections', total_rows=len(rows),
                            truncated=len(rows) > max_rows))
    groups = [dict(r) for r in db.execute('''SELECT sha256, MAX(size_bytes) AS size_bytes, COUNT(*) AS artifacts,
            COUNT(DISTINCT COALESCE(source_locator,'')) AS locators, GROUP_CONCAT(id) AS artifact_ids
            FROM artifacts WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*) > 1
            ORDER BY artifacts DESC, sha256 LIMIT ?''', (max_rows + 1,))]
    section.block(table('sources.content_groups', [('sha256', 'hash'), ('size_bytes', 'number'),
                                                   ('artifacts', 'number'), ('locators', 'number'),
                                                   ('artifact_ids', 'text')],
                        groups[:max_rows], ctx.lang, title_key='content_groups', total_rows=groups_total,
                        truncated=groups_total > max_rows))
    listed = db.execute('SELECT * FROM artifacts ORDER BY id LIMIT ?', (max_rows + 1,)).fetchall()
    section.input_rows('artifacts', listed[:max_rows])
    for row in listed[:max_rows]:
        section.cite('artifacts', {'id': row['id'], 'sha256': row['sha256'], 'size_bytes': row['size_bytes']})
    if len(listed) > max_rows:
        section.status('partial', 'artifact listing limited by max_rows')
        section.exclude('artifacts_not_listed_over_max_rows', totals['artifacts'] - max_rows)
    return section.result()

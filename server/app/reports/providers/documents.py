"""Documents with hashes for the material in scope (lawyer package).

Scope: artifacts of events selected by earlier sections plus artifacts of
cited anchors; without such scope every artifact (bounded by ``max_rows``).
"""
from __future__ import annotations

from ..blocks import table
from . import Section

VERSION = 'documents.v1'
COLUMNS = [('artifact_id', 'id'), ('original_name', 'text'), ('mime_type', 'text'), ('size_bytes', 'number'),
           ('sha256', 'hash'), ('source_label', 'text'), ('observations', 'number'),
           ('first_observed_at', 'time'), ('last_observed_at', 'time')]


def provide(ctx) -> dict:
    section = Section(ctx, 'documents', __name__, VERSION, 'section_documents')
    db, max_rows = ctx.db, ctx.params['max_rows']
    scoped = sorted(ctx.scope.get('artifact_ids', set()))
    if scoped:
        ids = scoped
        total = len(ids)
        section.exclude('scope', 'artifacts of events and citations selected by earlier sections')
    else:
        ids = [r[0] for r in db.execute('SELECT id FROM artifacts ORDER BY id LIMIT ?', (max_rows + 1,))]
        total = db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]
        section.exclude('scope', 'all artifacts (no earlier section selected material)')
    ids = ids[:max_rows]
    rows = []
    base = ctx.params['workbench_base_url']
    for artifact_id in ids:
        artifact = db.execute('SELECT * FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
        if artifact is None:
            continue
        source = db.execute('SELECT * FROM sources WHERE id=?', (artifact['source_id'],)).fetchone() if artifact['source_id'] else None
        observations = db.execute('SELECT * FROM source_observations WHERE artifact_id=? ORDER BY id', (artifact_id,)).fetchall()
        section.input_row('artifacts', artifact)
        if source is not None:
            section.input_row('sources', source)
        section.input_rows('source_observations', observations)
        section.cite('artifacts', {'id': artifact['id'], 'sha256': artifact['sha256'], 'size_bytes': artifact['size_bytes']})
        if artifact['sha256'] is None:
            section.unknown(f'artifact.{artifact_id}.sha256')
        if not observations:
            section.unknown(f'artifact.{artifact_id}.acquisition_history')
        observed = [o['observed_at'] for o in observations]
        rows.append({'artifact_id': artifact['id'], 'original_name': artifact['original_name'],
                     'mime_type': artifact['mime_type'], 'size_bytes': artifact['size_bytes'],
                     'sha256': artifact['sha256'], 'source_label': source['label'] if source else None,
                     'observations': len(observations), 'first_observed_at': min(observed) if observed else None,
                     'last_observed_at': max(observed) if observed else None,
                     'artifact_href': f'{base}/artifact/{artifact["id"]}' if base else None})
    if not rows:
        section.status('empty', 'no documents in scope')
    elif total > max_rows:
        section.status('partial', 'document listing limited by max_rows')
        section.exclude('documents_not_listed_over_max_rows', total - max_rows)
    section.block(table('documents.list', COLUMNS, rows, ctx.lang, total_rows=total, truncated=total > max_rows,
                        href_columns={'artifact_id': 'artifact_href'}))
    return section.result()

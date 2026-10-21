"""Human annotations (a separate layer from sources, parser and model output).

With material selected by earlier sections (events/artifacts), only
annotations on that material are listed; otherwise all (bounded).
"""
from __future__ import annotations

from ..blocks import table
from . import Section

VERSION = 'annotations.v1'
COLUMNS = [('id', 'id'), ('artifact_id', 'id'), ('event_id', 'id'), ('derived_text_id', 'id'), ('kind', 'text'),
           ('label', 'text'), ('body', 'quote'), ('start_ms', 'number'), ('end_ms', 'number'),
           ('created_at', 'time'), ('updated_at', 'time')]


def provide(ctx) -> dict:
    section = Section(ctx, 'annotations', __name__, VERSION, 'section_annotations')
    db, max_rows = ctx.db, ctx.params['max_rows']
    events, artifacts = sorted(ctx.scope.get('event_ids', ())), sorted(ctx.scope.get('artifact_ids', ()))
    if events or artifacts:
        rows = []
        seen = set()
        for column, ids in (('event_id', events), ('artifact_id', artifacts)):
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for row in db.execute(f'SELECT * FROM annotations WHERE {column} IN ({",".join("?" * len(chunk))})', chunk):
                    if row['id'] not in seen:
                        seen.add(row['id'])
                        rows.append(row)
        rows.sort(key=lambda row: row['id'])
        section.exclude('scope', 'annotations on events/artifacts selected by earlier sections')
    else:
        rows = db.execute('SELECT * FROM annotations ORDER BY id').fetchall()
        section.exclude('scope', 'all annotations')
    total = len(rows)
    rows = rows[:max_rows]
    section.input_rows('annotations', rows)
    if not rows:
        section.status('empty', 'no annotations in scope')
    elif total > max_rows:
        section.status('partial', 'annotation listing limited by max_rows')
        section.exclude('annotations_not_listed_over_max_rows', total - max_rows)
    section.block(table('annotations.list', COLUMNS, [dict(row) for row in rows], ctx.lang,
                        total_rows=total, truncated=total > max_rows))
    return section.result()

"""Quarantined external input: annotation proposals and relation hypotheses.

A proposal is not an annotation and a quarantined relation is not a link until
a person decides; this section shows their status only. Token references and
token data are never shown.
"""
from __future__ import annotations

from ..blocks import table
from . import Section

VERSION = 'integrations.v1'


def _exists(db, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def provide(ctx) -> dict:
    section = Section(ctx, 'integrations', __name__, VERSION, 'section_integrations')
    db, max_rows = ctx.db, ctx.params['max_rows']
    if not _exists(db, 'annotation_proposals') or not _exists(db, 'relation_hypotheses'):
        return section.unavailable('integration tables not present')
    proposals = db.execute('SELECT * FROM annotation_proposals ORDER BY id').fetchall()
    hypotheses = db.execute('SELECT * FROM relation_hypotheses ORDER BY id').fetchall()
    if not proposals and not hypotheses:
        section.status('empty', 'no proposals or hypotheses')
    counts: dict[tuple, int] = {}
    for row in proposals:
        key = (row['status'], row['mode'], row['provider'], row['model'])
        counts[key] = counts.get(key, 0) + 1
    section.block(table('integrations.proposal_counts', [('status', 'text'), ('mode', 'text'), ('provider', 'text'),
                                                         ('model', 'text'), ('count', 'number')],
                        [{'status': s, 'mode': m, 'provider': p, 'model': mo, 'count': n}
                         for (s, m, p, mo), n in sorted(counts.items())], ctx.lang, title_key='proposals'))
    listed = proposals[:max_rows]
    section.input_rows('annotation_proposals', listed)
    section.block(table('integrations.proposals', [('id', 'id'), ('status', 'text'), ('client_id', 'text'),
                                                   ('artifact_id', 'id'), ('derived_text_id', 'id'), ('mode', 'text'),
                                                   ('provider', 'text'), ('model', 'text'), ('label', 'text'),
                                                   ('body', 'quote'), ('created_at', 'time')],
                        [{key: row[key] for key in ('id', 'status', 'client_id', 'artifact_id', 'derived_text_id',
                                                    'mode', 'provider', 'model', 'label', 'body', 'created_at')}
                         for row in listed], ctx.lang, total_rows=len(proposals), truncated=len(proposals) > max_rows))
    for row in listed:
        if row['provider'] in (None, '', 'unknown') or row['model'] in (None, '', 'unknown'):
            section.unknown(f'annotation_proposal.{row["id"]}.provider_or_model')
    listed = hypotheses[:max_rows]
    section.input_rows('relation_hypotheses', listed)
    section.block(table('integrations.hypotheses', [('id', 'id'), ('status', 'text'), ('origin', 'text'),
                                                    ('left_ref', 'text'), ('relation', 'text'), ('right_ref', 'text'),
                                                    ('confidence', 'number'), ('rationale', 'quote'),
                                                    ('created_at', 'time')],
                        [{'id': row['id'], 'status': row['status'], 'origin': row['origin'],
                          'left_ref': f'{row["left_kind"]}:{row["left_id"]}', 'relation': row['relation'],
                          'right_ref': f'{row["right_kind"]}:{row["right_id"]}', 'confidence': row['confidence'],
                          'rationale': row['rationale'], 'created_at': row['created_at']} for row in listed],
                        ctx.lang, title_key='hypotheses', total_rows=len(hypotheses),
                        truncated=len(hypotheses) > max_rows))
    if len(proposals) > max_rows or len(hypotheses) > max_rows:
        section.status('partial', 'listing limited by max_rows')
    return section.result()

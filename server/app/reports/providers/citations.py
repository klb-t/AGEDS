"""Pinned citations: quote, interval, selector and the exact text version.

A citation points at one stored transcript version and range. The quote hash
is rechecked against the stored quote; the projection itself is re-verified
when the packet is exported into the bundle. No check here establishes what
was said, acoustic alignment or truth.
"""
from __future__ import annotations

import json

from ..blocks import table
from ..common import BUNDLE_DIR, packet_name, sha256_text
from . import Section
from ._runs import run_record

VERSION = 'citations.v1'
COLUMNS = [('anchor_id', 'id'), ('artifact_id', 'id'), ('artifact_sha256', 'hash'), ('derived_text_id', 'id'),
           ('text_sha256', 'hash'), ('run_id', 'id'), ('start_ms', 'number'), ('end_ms', 'number'),
           ('quote', 'quote'), ('quote_truncated', 'bool'), ('quote_sha256', 'hash'), ('quote_hash_status', 'text'),
           ('selector_kind', 'text'), ('precision_kind', 'text'), ('packet', 'path')]


def _selector(raw) -> tuple[str, str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return 'unparseable', 'unknown'
    if not isinstance(value, dict):
        return 'unparseable', 'unknown'
    return str(value.get('kind') or 'unknown'), str(value.get('precision') or 'unknown')


def provide(ctx) -> dict:
    section = Section(ctx, 'citations', __name__, VERSION, ctx.options.get('title_key', 'section_citations'))
    db, max_rows, params = ctx.db, ctx.params['max_rows'], ctx.params
    if params['anchor_ids']:
        wanted = list(dict.fromkeys(params['anchor_ids']))
        anchors = []
        for anchor_id in wanted:
            row = db.execute('SELECT * FROM evidence_anchors WHERE id=?', (anchor_id,)).fetchone()
            if row is None:
                section.unknown(f'evidence_anchor.{anchor_id}')
            else:
                anchors.append(row)
        section.exclude('requested_anchors_not_found', len(wanted) - len(anchors))
        section.exclude('scope', 'anchors requested by anchor_ids')
        total = len(anchors)
    elif ctx.options.get('scope') == 'scope' and ctx.scope.get('artifact_ids'):
        ids = sorted(ctx.scope['artifact_ids'])
        anchors = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            anchors.extend(db.execute(f'SELECT * FROM evidence_anchors WHERE artifact_id IN ({",".join("?" * len(chunk))}) ORDER BY id', chunk))
        anchors.sort(key=lambda row: row['id'])
        total = len(anchors)
        section.exclude('scope', 'anchors of artifacts selected by earlier sections')
    else:
        total = db.execute('SELECT COUNT(*) FROM evidence_anchors').fetchone()[0]
        anchors = db.execute('SELECT * FROM evidence_anchors ORDER BY id LIMIT ?', (max_rows,)).fetchall()
        section.exclude('scope', 'all anchors')
    anchors = anchors[:max_rows]
    if total > max_rows:
        section.status('partial', 'anchor listing limited by max_rows')
        section.exclude('anchors_not_listed_over_max_rows', total - max_rows)
    if not anchors:
        section.status('empty', 'no citations in scope')
    rows = []
    for anchor in anchors:
        version = db.execute('SELECT * FROM derived_text WHERE id=?', (anchor['derived_text_id'],)).fetchone()
        artifact = db.execute('SELECT * FROM artifacts WHERE id=?', (anchor['artifact_id'],)).fetchone()
        run = db.execute('SELECT * FROM processing_runs WHERE id=?', (version['run_id'],)).fetchone() if version and version['run_id'] else None
        section.input_row('evidence_anchors', anchor)
        for table_name, row in (('derived_text', version), ('artifacts', artifact), ('processing_runs', run)):
            if row is not None:
                section.input_row(table_name, row)
        if run is not None:
            record, unknowns = run_record(run)
            section.model_run(record)
            for name in unknowns:
                section.unknown(name)
        elif version is not None:
            section.unknown(f'derived_text.{version["id"]}.processing_run')
        text_sha = sha256_text(version['text']) if version is not None and isinstance(version['text'], str) else None
        quote = anchor['quote_text']
        status = 'match' if isinstance(quote, str) and sha256_text(quote) == anchor['quote_sha256'] else 'mismatch'
        selector_kind, precision = _selector(anchor['selector_json'])
        limit = params['quote_chars']
        section.cite('artifacts', {'id': anchor['artifact_id'], 'sha256': artifact['sha256'] if artifact else None,
                                   'size_bytes': artifact['size_bytes'] if artifact else None})
        if version is not None:
            section.cite('derived_text', {'id': version['id'], 'artifact_id': version['artifact_id'],
                                          'text_sha256': text_sha, 'run_id': version['run_id']})
        section.cite('anchors', {'id': anchor['id'], 'artifact_id': anchor['artifact_id'],
                                 'derived_text_id': anchor['derived_text_id'], 'quote_sha256': anchor['quote_sha256'],
                                 'start_ms': anchor['start_ms'], 'end_ms': anchor['end_ms']})
        section.citation(anchor['artifact_id'], anchor['id'])
        ctx.scope.setdefault('artifact_ids', set()).add(anchor['artifact_id'])
        rows.append({'anchor_id': anchor['id'], 'artifact_id': anchor['artifact_id'],
                     'artifact_sha256': artifact['sha256'] if artifact else None,
                     'derived_text_id': anchor['derived_text_id'], 'text_sha256': text_sha,
                     'run_id': version['run_id'] if version else None, 'start_ms': anchor['start_ms'],
                     'end_ms': anchor['end_ms'], 'quote': quote[:limit] if isinstance(quote, str) else None,
                     'quote_truncated': bool(isinstance(quote, str) and len(quote) > limit),
                     'quote_sha256': anchor['quote_sha256'], 'quote_hash_status': status,
                     'selector_kind': selector_kind, 'precision_kind': precision,
                     'packet': f'{BUNDLE_DIR}/{packet_name(anchor["artifact_id"], anchor["id"])}' if ctx.bundle_enabled else None,
                     'packet_href': f'{BUNDLE_DIR}/{packet_name(anchor["artifact_id"], anchor["id"])}' if ctx.bundle_enabled else None})
        if status == 'mismatch':
            section.exclude(f'quote_hash_mismatch:{anchor["id"]}', True)
    section.block(table('citations.list', COLUMNS, rows, ctx.lang, total_rows=total, truncated=total > max_rows,
                        href_columns={'packet': 'packet_href'}))
    return section.result()

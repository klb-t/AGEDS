"""Source catalog (area A): collections, types, time ranges and gaps per time
basis, duplicates (content groups), name collisions and problems.

Upstream: ``server.app.catalog.coverage.coverage_report`` and
``server.app.catalog.store.list_groups`` / ``list_collisions``. They open their
own read connections (``snapshot`` says so). Time bases are never merged; a gap
means no catalogued item carries a time there on that basis, not inactivity.
Without the catalog module the section is ``unavailable`` and the report keeps
the ``sources`` section (artifacts, acquisitions, content groups) as fallback.
"""
from __future__ import annotations

from ..blocks import kv, notice, table
from ..labels import label
from . import Section, load_upstream

VERSION = 'catalog.v1'
UPSTREAM_COVERAGE = (('server.app.catalog.coverage', 'coverage_report'),)
UPSTREAM_GROUPS = (('server.app.catalog.store', 'list_groups'),)
UPSTREAM_COLLISIONS = (('server.app.catalog.store', 'list_collisions'),)
STATUS_KEYS = ('ok', 'unsupported', 'damaged', 'limit_exceeded', 'encrypted', 'skipped', 'unreadable', 'unstable')
TYPE_COLUMNS = [('type', 'text'), ('family', 'text'), ('entries', 'number'), ('bytes', 'number'),
                ('size_unknown', 'number'), ('type_conflicts', 'number'), ('containers', 'number'),
                ('hashed', 'number'), ('type_from_extension_only', 'number')] + [(f'entries_{s}', 'number') for s in STATUS_KEYS]
BASIS_COLUMNS = [('type', 'text'), ('kind', 'text'), ('source', 'text'), ('tz_status', 'text'), ('value_field', 'text'),
                 ('values', 'number'), ('first', 'time'), ('last', 'time'), ('observed_days', 'number'),
                 ('gaps', 'number'), ('placeholder_values_excluded', 'number'), ('unparsed_values', 'number')]
GAP_COLUMNS = [('type', 'text'), ('kind', 'text'), ('source', 'text'), ('tz_status', 'text'),
               ('last_observed_day', 'time'), ('next_observed_day', 'time'), ('empty_days', 'number')]


def provide(ctx) -> dict:
    section = Section(ctx, 'catalog', __name__, VERSION, 'section_catalog')
    coverage, where = load_upstream(UPSTREAM_COVERAGE)
    if coverage is None:
        result = section.unavailable(where)
        result['blocks'].append(notice('catalog.fallback', 'info', label(ctx.lang, 'catalog_fallback')))
        return result
    section.data['snapshot'] = 'separate_read_transaction'
    params, max_rows = ctx.params, ctx.params['max_rows']
    try:
        report = coverage(params['collection_id'], min_gap_days=params['min_gap_days'])
        groups_fn, _ = load_upstream(UPSTREAM_GROUPS)
        collisions_fn, _ = load_upstream(UPSTREAM_COLLISIONS)
        groups = groups_fn(collection_id=params['collection_id'], limit=max_rows) if groups_fn else None
        collisions = collisions_fn(collection_id=params['collection_id'], limit=max_rows) if collisions_fn else None
    except Exception as exc:  # another area's code: report, do not hide
        section.status('error', f'{where} raised {type(exc).__name__}: {exc}')
        section.block(notice('catalog.error', 'error', section.data['reason']))
        return section.result()
    section.data['rule'] = report.get('schema')
    section.block(kv('catalog.method', sorted((report.get('method') or {}).items()), ctx.lang, title_key='catalog_method'))
    collections = report.get('collections') or []
    if not collections:
        section.status('empty', 'no collections')
    scan_ids: set[int] = set()
    any_scan = False
    problem_totals: dict[str, int] = {}
    for index, collection in enumerate(collections):
        cid = collection['collection_id']
        row = ctx.db.execute('SELECT * FROM collections WHERE id=?', (cid,)).fetchone()
        if row is not None:
            section.input_row('collections', row)
        scan_ids.update(collection.get('scan_ids') or [])
        any_scan = any_scan or bool(collection.get('scan_ids'))
        totals = collection.get('totals') or {}
        for key in STATUS_KEYS[1:] + ('partial_containers',):
            if isinstance(totals.get(key), int):
                problem_totals[key] = problem_totals.get(key, 0) + totals[key]
        block = kv(f'catalog.collection.{index}', [('collection_id', cid), ('collection_label', collection.get('label')),
                                                     ('scans', len(collection.get('scan_ids') or []))]
                   + [(key, value) for key, value in totals.items()], ctx.lang)
        block['title'] = collection.get('label')
        section.block(block)
        type_rows, basis_rows, gap_rows = [], [], []
        for item in collection.get('types') or []:
            statuses = item.get('by_status') or {}
            type_rows.append({**{key: item.get(key) for key, _ in TYPE_COLUMNS if not key.startswith('entries_')},
                              **{f'entries_{s}': statuses.get(s) for s in STATUS_KEYS}})
            if item.get('size_unknown'):
                section.unknown(f'catalog.collection.{cid}.size_bytes')
            if item.get('type_from_extension_only'):
                section.unknown(f'catalog.collection.{cid}.sniffed_type')
            for basis in item.get('time_bases') or []:
                basis_rows.append({'type': item.get('type'), **{key: basis.get(key) for key, _ in BASIS_COLUMNS[1:]
                                                                if key != 'gaps'}, 'gaps': len(basis.get('gaps') or [])})
                for gap in basis.get('gaps') or []:
                    gap_rows.append({'type': item.get('type'), 'kind': basis.get('kind'), 'source': basis.get('source'),
                                     'tz_status': basis.get('tz_status'), **gap})
        section.block(table(f'catalog.types.{index}', TYPE_COLUMNS, type_rows[:max_rows], ctx.lang,
                            title_key='catalog_types', total_rows=len(type_rows), truncated=len(type_rows) > max_rows))
        section.block(table(f'catalog.bases.{index}', BASIS_COLUMNS, basis_rows[:max_rows], ctx.lang,
                            title_key='time_bases', total_rows=len(basis_rows), truncated=len(basis_rows) > max_rows))
        section.block(table(f'catalog.gaps.{index}', GAP_COLUMNS, gap_rows[:max_rows], ctx.lang,
                            title_key='gaps', total_rows=len(gap_rows), truncated=len(gap_rows) > max_rows))
    for key, value in sorted(problem_totals.items()):
        section.exclude(f'catalog_entries_{key}', value)
    if collections and not any_scan:
        section.status('empty', 'no finished catalog scan')
    for scan_id in sorted(scan_ids):
        row = ctx.db.execute('SELECT * FROM catalog_scans WHERE id=?', (scan_id,)).fetchone()
        if row is not None:
            section.input_row('catalog_scans', row)
    if scan_ids:
        marks = ','.join('?' * len(scan_ids))
        issues = [dict(r) for r in ctx.db.execute(f'''SELECT code, COUNT(*) AS count FROM catalog_issues
                  WHERE scan_id IN ({marks}) GROUP BY code ORDER BY count DESC, code''', sorted(scan_ids))]
        entries = ctx.db.execute(f'SELECT COUNT(*) FROM catalog_entries WHERE scan_id IN ({marks})', sorted(scan_ids)).fetchone()[0]
        section.exclude('catalog_entries_aggregated_not_listed', entries)
    else:
        issues = []
    section.block(table('catalog.issues', [('code', 'text'), ('count', 'number')], issues, ctx.lang, title_key='issue_codes'))
    if groups is not None:
        rows = groups.get('groups') or []
        section.block(table('catalog.groups', [('id', 'id'), ('sha256', 'hash'), ('size_bytes', 'number'),
                                               ('entries', 'number'), ('collections', 'number'), ('max_depth', 'number'),
                                               ('artifacts', 'number'), ('empty_content', 'bool')],
                            rows, ctx.lang, title_key='content_groups', truncated=bool(groups.get('has_more'))))
        if groups.get('has_more'):
            section.exclude('content_groups_not_listed_over_max_rows', True)
    if collisions is not None:
        rows = [{key: item.get(key) for key in ('name', 'variants', 'entries', 'collections', 'collection_ids')}
                for item in collisions.get('collisions') or []]
        section.block(table('catalog.collisions', [('name', 'text'), ('variants', 'number'), ('entries', 'number'),
                                                   ('collections', 'number'), ('collection_ids', 'text')],
                            rows, ctx.lang, title_key='collisions', truncated=bool(collisions.get('has_more'))))
        if collisions.get('has_more'):
            section.exclude('collisions_not_listed_over_max_rows', True)
    if (groups and groups.get('has_more')) or (collisions and collisions.get('has_more')):
        section.status('partial', 'duplicate or collision listing limited by max_rows')
    return section.result()

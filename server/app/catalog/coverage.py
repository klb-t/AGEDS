"""Coverage per collection x type, with time ranges and gaps per time basis.

Callable by other areas (reports) through :func:`coverage_report`, which
returns plain JSON-serialisable data. Every number is computed here from stored
catalog rows; parameters are echoed in the result so it can be recomputed.

A *time basis* is (kind, source, tz_status). Bases are never merged: a ZIP DOS
time (local wall clock, unknown zone) and a filesystem mtime (UTC epoch) are
reported separately, and gaps are computed within one basis only. Known
placeholder values (DOS 1980-01-01 00:00:00, tar mtime 0) are excluded from
ranges and gaps and counted explicitly.
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import date

from .. import db as dbmod
from .store import TYPE_SQL, current_scan_ids, NotFound

SCHEMA = 'ageds.catalog-coverage/v1'
STATUS_KEYS = ('ok', 'unsupported', 'damaged', 'limit_exceeded', 'encrypted', 'skipped', 'unreadable', 'unstable')
METHOD = {
    'scan_selection': 'per collection and root path, the newest finished scan (completed or partial) unless scan_ids is given',
    'type': 'byte-signature type when recognized, else the type the extension claims, else "unknown"; '
            'entries typed only by extension are counted in type_from_extension_only',
    'bytes': 'sum of size_bytes where known; archive members count their uncompressed size, so containers and '
             'their members both contribute (nested bytes are not de-duplicated)',
    'time_basis': '(kind, source, tz_status); utc for epoch/offset values, local wall clock when the zone is unknown',
    'gap': 'calendar days (UTC date or local date within one basis) with no observation between two observed days; '
           'reported when the number of empty days is >= min_gap_days',
    'placeholders': 'zip DOS 1980-01-01 00:00:00 and tar mtime 0 are excluded from ranges and gaps and counted',
    'interpretation': 'a gap means no catalogued item carries a time in that period on that basis; it does not mean '
                      'nothing happened, and file times are not event times',
}


def _gaps(days: list[str], min_gap_days: int) -> list[dict]:
    gaps = []
    previous = None
    for text in days:
        current = date.fromisoformat(text)
        if previous is not None:
            empty = (current - previous).days - 1
            if empty >= min_gap_days:
                gaps.append({'last_observed_day': previous.isoformat(), 'next_observed_day': current.isoformat(),
                             'empty_days': empty})
        previous = current
    return gaps


def _time_bases(db, scan_marks: str, scan_ids: list[int], collection_id: int, type_name: str, min_gap_days: int) -> list[dict]:
    rows = db.execute(f'''SELECT t.kind, t.source, t.tz_status,
            CASE WHEN t.utc IS NOT NULL THEN substr(t.utc,1,10) ELSE substr(t.local,1,10) END AS day,
            CASE WHEN t.utc IS NOT NULL THEN 'utc' ELSE 'local' END AS field,
            (t.metadata_json LIKE '%"dos_epoch_placeholder": true%' OR t.metadata_json LIKE '%"zero_epoch": true%') AS placeholder,
            COUNT(*) AS n, MIN(COALESCE(t.utc, t.local)) AS first, MAX(COALESCE(t.utc, t.local)) AS last
            FROM catalog_entry_times t JOIN catalog_entries e ON e.id=t.entry_id
            WHERE e.scan_id IN ({scan_marks}) AND e.collection_id=? AND {TYPE_SQL}=?
            GROUP BY 1,2,3,4,5,6 ORDER BY 1,2,3,4''', (*scan_ids, collection_id, type_name)).fetchall()
    bases: dict[tuple, dict] = {}
    for row in rows:
        key = (row['kind'], row['source'], row['tz_status'])
        basis = bases.setdefault(key, {'kind': key[0], 'source': key[1], 'tz_status': key[2], 'value_field': row['field'],
                                       'values': 0, 'first': None, 'last': None, 'placeholder_values_excluded': 0,
                                       'unparsed_values': 0, '_days': []})
        if row['day'] is None:
            basis['unparsed_values'] += row['n']
            continue
        if row['placeholder']:
            basis['placeholder_values_excluded'] += row['n']
            continue
        basis['values'] += row['n']
        basis['first'] = row['first'] if basis['first'] is None else min(basis['first'], row['first'])
        basis['last'] = row['last'] if basis['last'] is None else max(basis['last'], row['last'])
        basis['_days'].append(row['day'])
    result = []
    for basis in bases.values():
        days = sorted(set(basis.pop('_days')))
        basis['observed_days'] = len(days)
        basis['gaps'] = _gaps(days, min_gap_days)
        result.append(basis)
    return result


def coverage_report(collection_id: int | None = None, *, scan_ids: list[int] | None = None, min_gap_days: int = 30,
                    case_id: int | None = None) -> dict:
    """Plain-dict coverage of one collection (or all collections of a case)."""
    if not isinstance(min_gap_days, int) or isinstance(min_gap_days, bool) or min_gap_days < 1:
        raise ValueError('min_gap_days must be a positive integer')
    with closing(dbmod.connect()) as db:
        if collection_id is not None:
            rows = db.execute('SELECT * FROM collections WHERE id=?', (collection_id,)).fetchall()
            if not rows:
                raise NotFound('collection does not exist')
        else:
            if case_id is None:
                first = db.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()
                case_id = first['id'] if first else None
            rows = db.execute('SELECT * FROM collections WHERE case_id=? ORDER BY id', (case_id,)).fetchall()
        collections = []
        for collection in rows:
            cid = collection['id']
            if scan_ids is not None:
                selected = [int(s) for s in scan_ids if db.execute('SELECT 1 FROM catalog_scans WHERE id=? AND collection_id=?',
                                                                   (s, cid)).fetchone()]
            else:
                selected = current_scan_ids(db, cid)
            scans = [dict(r) for r in db.execute(
                f'SELECT id, root_kind, status, started_at, finished_at, coverage_json FROM catalog_scans WHERE id IN ({",".join("?" * len(selected)) or "NULL"}) ORDER BY id',
                selected)]
            scan_summaries = []
            for scan in scans:
                cov = json.loads(scan.pop('coverage_json') or '{}')
                scan_summaries.append({**scan, 'complete': cov.get('complete'), 'incomplete_reasons': cov.get('incomplete_reasons', {})})
            item = {'collection_id': cid, 'label': collection['label'], 'priority': collection['priority'],
                    'parent_collection_id': collection['parent_collection_id'], 'scan_ids': selected, 'scans': scan_summaries,
                    'types': [], 'totals': {}}
            if not selected:
                item['totals'] = {'entries': 0, 'note': 'no finished scan'}
                collections.append(item)
                continue
            marks = ','.join('?' * len(selected))
            type_rows = db.execute(f'''SELECT {TYPE_SQL} AS type, MAX(e.family) AS family, COUNT(*) AS entries,
                    SUM(e.size_bytes) AS bytes, SUM(e.size_bytes IS NULL) AS size_unknown,
                    SUM(e.type_conflict) AS type_conflicts, SUM(e.is_container) AS containers,
                    SUM(e.sniffed_type IS NULL AND e.extension_type IS NOT NULL AND e.extension_type <> 'opaque') AS type_from_extension_only,
                    SUM(e.hash_status = 'complete') AS hashed,
                    {",".join(f"SUM(e.status='{s}') AS status_{s}" for s in STATUS_KEYS)}
                    FROM catalog_entries e WHERE e.scan_id IN ({marks}) AND e.collection_id=?
                    GROUP BY 1 ORDER BY entries DESC, 1''', (*selected, cid)).fetchall()
            totals = {'entries': 0, 'bytes': 0, 'type_conflicts': 0, 'containers': 0, 'hashed': 0,
                      **{s: 0 for s in STATUS_KEYS}}
            for row in type_rows:
                by_status = {s: row[f'status_{s}'] for s in STATUS_KEYS}
                type_item = {'type': row['type'], 'family': row['family'], 'entries': row['entries'],
                             'bytes': row['bytes'] or 0, 'size_unknown': row['size_unknown'],
                             'type_conflicts': row['type_conflicts'], 'containers': row['containers'],
                             'hashed': row['hashed'], 'type_from_extension_only': row['type_from_extension_only'],
                             'by_status': by_status,
                             'time_bases': _time_bases(db, marks, selected, cid, row['type'], min_gap_days)}
                item['types'].append(type_item)
                totals['entries'] += row['entries']
                totals['bytes'] += row['bytes'] or 0
                totals['type_conflicts'] += row['type_conflicts']
                totals['containers'] += row['containers']
                totals['hashed'] += row['hashed']
                for status in STATUS_KEYS:
                    totals[status] += by_status[status]
            totals['partial_containers'] = db.execute(f'''SELECT COUNT(*) FROM catalog_entries e WHERE e.scan_id IN ({marks})
                    AND e.collection_id=? AND e.is_container=1 AND e.status <> 'ok' ''', (*selected, cid)).fetchone()[0]
            totals['gaps'] = sum(len(b['gaps']) for t in item['types'] for b in t['time_bases'])
            item['totals'] = totals
            collections.append(item)
    return {'schema': SCHEMA, 'parameters': {'collection_id': collection_id, 'scan_ids': scan_ids,
                                             'min_gap_days': min_gap_days, 'case_id': case_id},
            'method': METHOD, 'collections': collections}

"""Catalog queries and the few mutable records (collections, marks).

Every list function is bounded (keyset or LIMIT/OFFSET with a hard maximum).
"Current" scans are, per collection and root path, the newest scan that
finished (completed or partial); older scans stay queryable by id.
"""
from __future__ import annotations

import json
from contextlib import closing

from .. import db as dbmod

MAX_PAGE = 500
COLLECTION_FIELDS = ('label', 'device_or_account', 'export_date_raw', 'acquisition_method', 'operator',
                     'notes', 'priority', 'parent_collection_id')
ENTRY_STATUSES = ('ok', 'unsupported', 'damaged', 'limit_exceeded', 'encrypted', 'skipped', 'unreadable', 'unstable')
TYPE_SQL = ("COALESCE(e.sniffed_type, CASE WHEN e.extension_type IS NULL OR e.extension_type='opaque' "
            "THEN NULL ELSE e.extension_type END, 'unknown')")


class NotFound(LookupError):
    pass


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _connection():
    return closing(dbmod.connect())


def _audit(db, action: str, kind: str, object_id: int | None, details: dict, actor: str = 'user'):
    db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)',
               (actor, action, kind, object_id, _json(details)))


def _case_id(db, case_id: int | None) -> int:
    if case_id is not None:
        if not db.execute('SELECT 1 FROM cases WHERE id=?', (case_id,)).fetchone():
            raise NotFound('case does not exist')
        return case_id
    row = db.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()
    if row is None:
        raise NotFound('no case exists; initialize the database first')
    return int(row['id'])


def _validate_fields(changes: dict) -> dict:
    unknown = set(changes) - set(COLLECTION_FIELDS)
    if unknown:
        raise ValueError(f'unknown collection fields: {sorted(unknown)}')
    clean = {}
    for key, value in changes.items():
        if key in ('priority', 'parent_collection_id'):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f'{key} must be an integer or null')
        elif value is not None and not isinstance(value, str):
            raise ValueError(f'{key} must be text or null')
        clean[key] = value
    if 'label' in clean and not (clean['label'] or '').strip():
        raise ValueError('label cannot be blank')
    return clean


def _check_parent(db, collection_id: int | None, parent_id: int | None, case_id: int):
    if parent_id is None:
        return
    parent = db.execute('SELECT case_id FROM collections WHERE id=?', (parent_id,)).fetchone()
    if parent is None or parent['case_id'] != case_id:
        raise ValueError('parent collection does not exist in this case')
    current, seen = parent_id, set()
    while current is not None:
        if current == collection_id or current in seen:
            raise ValueError('collection parent would create a cycle')
        seen.add(current)
        row = db.execute('SELECT parent_collection_id FROM collections WHERE id=?', (current,)).fetchone()
        current = row['parent_collection_id'] if row else None


def _collection_dict(row) -> dict:
    data = dict(row)
    data['metadata'] = json.loads(data.pop('metadata_json') or '{}')
    return data


def create_collection(label: str, *, case_id: int | None = None, metadata: dict | None = None, actor: str = 'user',
                      **fields) -> dict:
    values = _validate_fields({'label': label, **fields})
    with _connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            case = _case_id(db, case_id)
            _check_parent(db, None, values.get('parent_collection_id'), case)
            columns = ['case_id', 'metadata_json', *values]
            cursor = db.execute(f'INSERT INTO collections({",".join(columns)}) VALUES({",".join("?" * len(columns))})',
                                (case, _json(metadata or {}), *values.values()))
            collection_id = int(cursor.lastrowid)
            _audit(db, 'collection_created', 'collection', collection_id, {'fields': sorted(values)}, actor)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return get_collection(collection_id)


def update_collection(collection_id: int, changes: dict, *, actor: str = 'user') -> dict:
    """Apply mutable field changes; the audit log keeps every previous value."""
    values = _validate_fields(changes)
    if not values:
        raise ValueError('no changes given')
    with _connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('SELECT * FROM collections WHERE id=?', (collection_id,)).fetchone()
            if row is None:
                raise NotFound('collection does not exist')
            if 'parent_collection_id' in values:
                _check_parent(db, collection_id, values['parent_collection_id'], row['case_id'])
            changed = {k: v for k, v in values.items() if row[k] != v}
            if changed:
                previous = {k: row[k] for k in changed}
                assignments = ','.join(f'{k}=?' for k in changed)
                db.execute(f'UPDATE collections SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                           (*changed.values(), collection_id))
                _audit(db, 'collection_updated', 'collection', collection_id,
                       {'previous': previous, 'new': changed}, actor)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return get_collection(collection_id)


def get_collection(collection_id: int) -> dict:
    with _connection() as db:
        row = db.execute('SELECT * FROM collections WHERE id=?', (collection_id,)).fetchone()
        if row is None:
            raise NotFound('collection does not exist')
        data = _collection_dict(row)
        data['history'] = [dict(r, details=json.loads(r['details_json'])) for r in db.execute(
            "SELECT id,ts,actor,action,details_json FROM audit_log WHERE object_kind='collection' AND object_id=? ORDER BY id DESC LIMIT 50",
            (collection_id,))]
        for item in data['history']:
            item.pop('details_json')
        scans = current_scan_ids(db, collection_id)
        data['current_scan_ids'] = scans
        data['entry_count'] = _count_entries(db, scans)
    return data


def _count_entries(db, scan_ids: list[int]) -> int:
    if not scan_ids:
        return 0
    return db.execute(f'SELECT COUNT(*) FROM catalog_entries WHERE scan_id IN ({",".join("?" * len(scan_ids))})',
                      scan_ids).fetchone()[0]


def list_collections(case_id: int | None = None) -> list[dict]:
    """All collections of a case as a flat list in tree order (with depth)."""
    with _connection() as db:
        case = _case_id(db, case_id)
        rows = [_collection_dict(r) for r in db.execute('SELECT * FROM collections WHERE case_id=? ORDER BY id', (case,))]
        for row in rows:
            scans = current_scan_ids(db, row['id'])
            row['current_scan_ids'] = scans
            row['entry_count'] = _count_entries(db, scans)
            row['scan_count'] = db.execute('SELECT COUNT(*) FROM catalog_scans WHERE collection_id=?', (row['id'],)).fetchone()[0]
    children: dict[int | None, list[dict]] = {}
    ids = {r['id'] for r in rows}
    for row in rows:
        parent = row['parent_collection_id'] if row['parent_collection_id'] in ids else None
        children.setdefault(parent, []).append(row)
    ordered: list[dict] = []

    def visit(parent, depth):
        for row in children.get(parent, []):
            row['tree_depth'] = depth
            ordered.append(row)
            visit(row['id'], depth + 1)
    visit(None, 0)
    return ordered


def current_scan_ids(db, collection_id: int | None = None, case_id: int | None = None) -> list[int]:
    where, params = ["status IN ('completed','partial')"], []
    if collection_id is not None:
        where.append('collection_id=?')
        params.append(collection_id)
    if case_id is not None:
        where.append('case_id=?')
        params.append(case_id)
    rows = db.execute(f'SELECT MAX(id) FROM catalog_scans WHERE {" AND ".join(where)} GROUP BY collection_id, root_path',
                      params).fetchall()
    return sorted(int(r[0]) for r in rows)


def list_scans(collection_id: int | None = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(limit, MAX_PAGE))
    with _connection() as db:
        current = set(current_scan_ids(db, collection_id))
        params: list = []
        sql = 'SELECT * FROM catalog_scans'
        if collection_id is not None:
            sql += ' WHERE collection_id=?'
            params.append(collection_id)
        rows = db.execute(sql + ' ORDER BY id DESC LIMIT ?', (*params, limit)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ('limits_json', 'policy_json', 'adapters_json', 'coverage_json'):
            item[key[:-5]] = json.loads(item.pop(key) or '{}')
        item['current'] = row['id'] in current
        result.append(item)
    return result


def _scan_filter(db, filters: dict) -> tuple[str, list]:
    if filters.get('scan_id') is not None:
        return 'e.scan_id=?', [filters['scan_id']]
    if filters.get('all_scans'):
        return '1=1', []
    scans = current_scan_ids(db, filters.get('collection_id'))
    if not scans:
        return '0=1', []
    return f'e.scan_id IN ({",".join("?" * len(scans))})', scans


def _entry_where(db, filters: dict) -> tuple[str, list]:
    clause, params = _scan_filter(db, filters)
    where = [clause]
    if filters.get('collection_id') is not None:
        where.append('e.collection_id=?')
        params.append(filters['collection_id'])
    if filters.get('type'):
        where.append(f'{TYPE_SQL}=?')
        params.append(filters['type'])
    if filters.get('family'):
        where.append('e.family=?')
        params.append(filters['family'])
    if filters.get('status'):
        if filters['status'] not in ENTRY_STATUSES:
            raise ValueError('unknown status filter')
        where.append('e.status=?')
        params.append(filters['status'])
    if filters.get('conflict'):
        where.append('e.type_conflict=1')
    if filters.get('group_id') is not None:
        where.append('e.content_group_id=?')
        params.append(filters['group_id'])
    if filters.get('duplicates_only'):
        current = current_scan_ids(db) or [0]
        where.append(f'''e.content_group_id IN (SELECT content_group_id FROM catalog_entries
                        WHERE content_group_id IS NOT NULL AND scan_id IN ({",".join("?" * len(current))})
                        GROUP BY content_group_id HAVING COUNT(*) > 1)''')
        params.extend(current)
    if filters.get('parent_id') is not None:
        where.append('e.parent_entry_id=?')
        params.append(filters['parent_id'])
    if filters.get('containers_only'):
        where.append('e.is_container=1')
    if filters.get('q'):
        where.append('(instr(lower(e.name), lower(?)) > 0 OR instr(lower(e.locator), lower(?)) > 0)')
        params.extend([filters['q'], filters['q']])
    return ' AND '.join(where), params


def _entry_dict(row) -> dict:
    data = dict(row)
    for key in ('name_flags_json', 'issues_json', 'details_json'):
        if key in data:
            data[key[:-5]] = json.loads(data.pop(key) or 'null')
    return data


def _marks(db, pairs: list[tuple[int, str]]) -> dict:
    found = {}
    for collection_id, locator in set(pairs):
        row = db.execute('SELECT priority, note, actor, created_at FROM catalog_marks WHERE collection_id=? AND locator=? ORDER BY id DESC LIMIT 1',
                         (collection_id, locator)).fetchone()
        if row:
            found[(collection_id, locator)] = dict(row)
    return found


def list_entries(filters: dict | None = None, *, after_id: int | None = None, limit: int = 100) -> dict:
    filters = dict(filters or {})
    limit = max(1, min(int(limit), MAX_PAGE))
    with _connection() as db:
        where, params = _entry_where(db, filters)
        if after_id is not None:
            where += ' AND e.id > ?'
            params.append(after_id)
        rows = db.execute(f'''SELECT e.*, {TYPE_SQL} AS effective_type,
                (SELECT COUNT(*) FROM catalog_entries g WHERE g.content_group_id=e.content_group_id AND e.content_group_id IS NOT NULL
                   AND g.scan_id IN (SELECT MAX(id) FROM catalog_scans WHERE status IN ('completed','partial') GROUP BY collection_id, root_path)) AS group_current_observations,
                (SELECT COUNT(*) FROM artifacts a WHERE e.sha256 IS NOT NULL AND a.sha256=e.sha256) AS group_artifacts
                FROM catalog_entries e WHERE {where} ORDER BY e.id LIMIT ?''', (*params, limit + 1)).fetchall()
        entries = [_entry_dict(r) for r in rows[:limit]]
        marks = _marks(db, [(e['collection_id'], e['locator']) for e in entries])
    for entry in entries:
        entry['mark'] = marks.get((entry['collection_id'], entry['locator']))
    return {'entries': entries, 'limit': limit, 'has_more': len(rows) > limit,
            'next_after_id': entries[-1]['id'] if len(rows) > limit else None}


def get_entry(entry_id: int) -> dict:
    with _connection() as db:
        row = db.execute(f'SELECT e.*, {TYPE_SQL} AS effective_type FROM catalog_entries e WHERE e.id=?', (entry_id,)).fetchone()
        if row is None:
            raise NotFound('catalog entry does not exist')
        entry = _entry_dict(row)
        entry['times'] = [dict(r, metadata=json.loads(r['metadata_json'])) for r in db.execute(
            'SELECT kind,source,raw,utc,local,tz_status,tz,precision,metadata_json FROM catalog_entry_times WHERE entry_id=? ORDER BY id', (entry_id,))]
        for item in entry['times']:
            item.pop('metadata_json')
        chain, parent = [], entry['parent_entry_id']
        while parent is not None and len(chain) < 64:
            p = db.execute('SELECT id, locator, parent_entry_id, sniffed_type, status FROM catalog_entries WHERE id=?', (parent,)).fetchone()
            chain.append({k: p[k] for k in ('id', 'locator', 'sniffed_type', 'status')})
            parent = p['parent_entry_id']
        entry['containers'] = chain
        entry['children'] = db.execute('SELECT COUNT(*) FROM catalog_entries WHERE parent_entry_id=?', (entry_id,)).fetchone()[0]
        entry['ingested'] = [dict(r) for r in db.execute(
            'SELECT artifact_id, observation_id, source_locator, created_at FROM catalog_entry_artifacts WHERE entry_id=? ORDER BY id', (entry_id,))]
        entry['mark'] = _marks(db, [(entry['collection_id'], entry['locator'])]).get((entry['collection_id'], entry['locator']))
        entry['mark_history'] = [dict(r) for r in db.execute(
            'SELECT priority, note, actor, created_at FROM catalog_marks WHERE collection_id=? AND locator=? ORDER BY id DESC LIMIT 20',
            (entry['collection_id'], entry['locator']))]
    return entry


def list_groups(*, collection_id: int | None = None, include_empty: bool = False, limit: int = 100, offset: int = 0) -> dict:
    """Content groups with more than one observation (catalog entries in current scans + artifacts)."""
    limit = max(1, min(int(limit), MAX_PAGE))
    offset = max(0, int(offset))
    with _connection() as db:
        scans = current_scan_ids(db)
        if not scans:
            return {'groups': [], 'limit': limit, 'offset': offset, 'has_more': False}
        marks = ','.join('?' * len(scans))
        extra = '' if include_empty else ' AND COALESCE(g.size_bytes, 1) > 0'
        having_collection = ' AND SUM(e.collection_id=?) > 0' if collection_id is not None else ''
        params = [*scans, *([collection_id] if collection_id is not None else []), limit + 1, offset]
        rows = db.execute(f'''SELECT g.id, g.sha256, g.size_bytes, COUNT(*) AS entries,
                COUNT(DISTINCT e.collection_id) AS collections, MAX(e.depth) AS max_depth,
                (SELECT COUNT(*) FROM artifacts a WHERE a.sha256=g.sha256) AS artifacts
                FROM catalog_content_groups g JOIN catalog_entries e ON e.content_group_id=g.id
                WHERE e.scan_id IN ({marks}){extra}
                GROUP BY g.id HAVING (COUNT(*) + (SELECT COUNT(*) FROM artifacts a WHERE a.sha256=g.sha256)) > 1{having_collection}
                ORDER BY entries DESC, g.id LIMIT ? OFFSET ?''', params).fetchall()
    groups = [dict(r) for r in rows[:limit]]
    for group in groups:
        group['empty_content'] = group['size_bytes'] == 0
    return {'groups': groups, 'limit': limit, 'offset': offset, 'has_more': len(rows) > limit}


def get_group(group_id: int, *, limit: int = 200) -> dict:
    limit = max(1, min(int(limit), MAX_PAGE))
    with _connection() as db:
        group = db.execute('SELECT * FROM catalog_content_groups WHERE id=?', (group_id,)).fetchone()
        if group is None:
            raise NotFound('content group does not exist')
        current = set(current_scan_ids(db))
        entries = [dict(r) for r in db.execute('''SELECT e.id, e.scan_id, e.collection_id, c.label AS collection_label,
                e.locator, e.depth, e.name, e.status, s.root_path, s.started_at
                FROM catalog_entries e JOIN collections c ON c.id=e.collection_id JOIN catalog_scans s ON s.id=e.scan_id
                WHERE e.content_group_id=? ORDER BY e.id LIMIT ?''', (group_id, limit))]
        for entry in entries:
            entry['current_scan'] = entry['scan_id'] in current
        artifacts = [dict(r) for r in db.execute('''SELECT a.id, a.source_id, a.source_locator, a.original_name, a.created_at
                FROM artifacts a WHERE a.sha256=? ORDER BY a.id LIMIT ?''', (group['sha256'], limit))]
        observations = [dict(r) for r in db.execute('''SELECT o.id, o.artifact_id, o.source_id, o.source_locator, o.observed_at,
                o.acquisition_kind, o.collection_id FROM source_observations o WHERE o.sha256=? ORDER BY o.id LIMIT ?''',
                                                    (group['sha256'], limit))]
    return {'group': dict(group), 'catalog_entries': entries, 'artifacts': artifacts, 'acquisition_observations': observations,
            'note': 'equal bytes; identity of content only, not authorship, truth or time of creation'}


def list_collisions(*, collection_id: int | None = None, limit: int = 100, offset: int = 0) -> dict:
    """Same file name, different bytes (current scans). Unhashed entries are excluded as unknown."""
    limit = max(1, min(int(limit), MAX_PAGE))
    with _connection() as db:
        scans = current_scan_ids(db)
        if not scans:
            return {'collisions': [], 'limit': limit, 'offset': offset, 'has_more': False}
        params: list = [*scans]
        having = ''
        if collection_id is not None:
            having = ' AND SUM(collection_id=?) > 0'
            params.append(collection_id)
        rows = db.execute(f'''SELECT name, COUNT(DISTINCT sha256) AS variants, COUNT(*) AS entries,
                COUNT(DISTINCT collection_id) AS collections, GROUP_CONCAT(DISTINCT collection_id) AS collection_ids
                FROM catalog_entries WHERE scan_id IN ({",".join("?" * len(scans))}) AND sha256 IS NOT NULL
                GROUP BY name HAVING COUNT(DISTINCT sha256) > 1{having}
                ORDER BY variants DESC, name LIMIT ? OFFSET ?''', (*params, limit + 1, offset)).fetchall()
        result = []
        for row in rows[:limit]:
            item = dict(row)
            item['locations'] = [dict(r) for r in db.execute(
                f'''SELECT id, collection_id, locator, sha256, size_bytes FROM catalog_entries
                    WHERE name=? AND sha256 IS NOT NULL AND scan_id IN ({",".join("?" * len(scans))}) ORDER BY id LIMIT 50''',
                (row['name'], *scans))]
            result.append(item)
    return {'collisions': result, 'limit': limit, 'offset': offset, 'has_more': len(rows) > limit}


def add_mark(collection_id: int, locator: str, *, priority: int | None = None, note: str | None = None,
             actor: str = 'user') -> dict:
    """Append a human mark (priority/note) for a locator; the previous mark stays in history."""
    if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool)):
        raise ValueError('priority must be an integer or null')
    if note is not None and not isinstance(note, str):
        raise ValueError('note must be text or null')
    with _connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if not db.execute('SELECT 1 FROM catalog_entries WHERE collection_id=? AND locator=? LIMIT 1',
                              (collection_id, locator)).fetchone():
                raise NotFound('no catalog entry with this locator in the collection')
            previous = db.execute('SELECT priority, note FROM catalog_marks WHERE collection_id=? AND locator=? ORDER BY id DESC LIMIT 1',
                                  (collection_id, locator)).fetchone()
            cursor = db.execute('INSERT INTO catalog_marks(collection_id,locator,priority,note,actor) VALUES(?,?,?,?,?)',
                                (collection_id, locator, priority, note, actor))
            _audit(db, 'catalog_mark', 'catalog_mark', int(cursor.lastrowid),
                   {'collection_id': collection_id, 'locator': locator, 'previous': dict(previous) if previous else None,
                    'new': {'priority': priority, 'note': note}}, actor)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'collection_id': collection_id, 'locator': locator, 'priority': priority, 'note': note}


def type_options() -> list[str]:
    with _connection() as db:
        scans = current_scan_ids(db)
        if not scans:
            return []
        return [r[0] for r in db.execute(f'SELECT DISTINCT {TYPE_SQL} FROM catalog_entries e WHERE e.scan_id IN ({",".join("?" * len(scans))}) ORDER BY 1', scans)]


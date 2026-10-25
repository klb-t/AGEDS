"""Packages and scoped exports (package P5).

A package is a set of references (cases, materials, fragments, assignments),
never a copy of bytes. It is shared by granting capabilities on a scope of
kind 'package'. In `snapshot` mode recipients see exactly the object versions
frozen at a moment; in `live` mode they follow the current state. Removing an
item or revoking a grant never touches the material.

Occurrences of the same material in several cases of a package are not
independent evidence: independence groups are keyed by content hash, else uid.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from . import authz
from .assignments import new_uid
from .packages import SCHEMA_ID, TABLES, ORDER_COLUMNS, _integrity

PACKAGE_MODES = ('snapshot', 'live')
FREEZE_KINDS = ('case', 'source', 'artifact', 'source_observation', 'event', 'derived_text',
                'evidence_anchor', 'fragment', 'assignment', 'link', 'case_relation',
                'processing_run', 'job')
# Relations a scoped export keeps only when the target row is exported as well.
REFERENCES = {
    'sources': ('case_id',), 'artifacts': ('source_id', 'parent_artifact_id'),
    'source_observations': ('source_id',), 'events': ('artifact_id', 'source_id'),
    'processing_runs': ('job_id', 'artifact_id'), 'derived_text': ('run_id',),
    'annotations': ('artifact_id', 'event_id', 'derived_text_id'), 'jobs': ('artifact_id',),
    'links': ('case_id',),
}
REFERENCE_TARGETS = {'case_id': 'cases', 'source_id': 'sources', 'parent_artifact_id': 'artifacts',
                     'artifact_id': 'artifacts', 'job_id': 'jobs', 'run_id': 'processing_runs',
                     'event_id': 'events', 'derived_text_id': 'derived_text'}
# Server-internal values that mean nothing outside this installation.
INTERNAL_FIELDS = {'artifacts': ('stored_path',), 'jobs': ('lease_token', 'worker_id'),
                   'processing_runs': ('lease_token',)}
TABLE_KINDS = {table: kind for kind, table in authz.KIND_TABLES.items()}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _bucket(count: int) -> str:
    return '0' if count == 0 else '1-9' if count < 10 else '10-99' if count < 100 else '100+'


GROWTH_POLICIES = ('frozen', 'notify_only', 'propose_to_owner', 'auto_include_by_rule:<kind>')


def set_growth_policy(db: sqlite3.Connection, package_id: int, policy: str, *, actor: str | None = None) -> dict:
    """How material added after a snapshot reaches recipients (scope.growth_policy, per package).

    frozen (preset): only an explicit snapshot adds it; notify_only and
    propose_to_owner: package_growth() lists it for the owner, a snapshot
    approves it; auto_include_by_rule:<kind>: new objects of that kind reached
    through the snapshot items are included live. Other values are kept and
    behave as frozen.
    """
    row = db.execute('SELECT metadata_json FROM packages WHERE id=?', (package_id,)).fetchone()
    if row is None:
        raise ValueError('package does not exist')
    metadata = json.loads(row['metadata_json'] or '{}')
    previous = metadata.get('growth_policy', 'frozen')
    metadata['growth_policy'] = policy
    db.execute('UPDATE packages SET metadata_json=? WHERE id=?', (_json(metadata), package_id))
    known = policy in GROWTH_POLICIES[:3] or policy.startswith('auto_include_by_rule:')
    authz._audit(db, 'package_growth_policy', 'package', package_id, {'from': previous, 'to': policy}, actor)
    return {'from': previous, 'to': policy, 'warnings': [] if known else [{'code': 'value_not_in_presets', 'value': policy}]}


def package_growth(db: sqlite3.Connection, package_id: int) -> dict:
    """Objects the package's snapshot items reach now but its latest snapshot does not."""
    row = db.execute('SELECT metadata_json FROM packages WHERE id=?', (package_id,)).fetchone()
    if row is None:
        raise ValueError('package does not exist')
    latest = db.execute('SELECT objects_json FROM package_snapshots WHERE package_id=? ORDER BY id DESC LIMIT 1',
                        (package_id,)).fetchone()
    frozen = {tuple(item) for item in json.loads(latest['objects_json'])} if latest else set()
    pending = [item for item in _reachable(db, package_id) if tuple(item) not in frozen]
    policy = json.loads(row['metadata_json'] or '{}').get('growth_policy', 'frozen')
    return {'policy': policy, 'pending': pending,
            'action': {'notify_only': 'notify', 'propose_to_owner': 'approve_with_snapshot'}.get(policy, 'none')}


def create_package(db: sqlite3.Connection, name: str, *, mode: str = 'snapshot',
                   owner_principal_id: int | None = None, metadata: dict | None = None,
                   actor: str | None = None) -> dict:
    warnings = [] if mode in PACKAGE_MODES else [{'code': 'mode_not_in_presets', 'mode': mode}]
    uid = new_uid()
    cur = db.execute('INSERT INTO packages(uid,name,owner_principal_id,mode,metadata_json) VALUES(?,?,?,?,?)',
                     (uid, name, owner_principal_id, mode, _json(metadata or {})))
    scope_id = authz.create_scope(db, 'package', {'package_id': cur.lastrowid}, actor=actor)
    authz._audit(db, 'package_created', 'package', cur.lastrowid, {'uid': uid, 'mode': mode}, actor)
    return {'id': int(cur.lastrowid), 'uid': uid, 'scope_id': scope_id, 'warnings': warnings}


def package_scope(db: sqlite3.Connection, package_id: int) -> int:
    for row in db.execute("SELECT id,spec_json FROM scopes WHERE kind='package' ORDER BY id"):
        if json.loads(row['spec_json']).get('package_id') == package_id:
            return int(row['id'])
    raise ValueError('package has no scope')


def add_item(db: sqlite3.Connection, package_id: int, item_kind: str, item_id: int, *,
             mode: str | None = None, selector: dict | None = None, actor: str | None = None) -> int:
    if not db.execute('SELECT 1 FROM packages WHERE id=?', (package_id,)).fetchone():
        raise ValueError('package does not exist')
    if authz._row(db, item_kind, item_id) is None:
        raise ValueError(f'{item_kind} does not exist')
    include = {'mode': mode} if mode is not None else {}
    cur = db.execute('''INSERT INTO package_items(package_id,item_kind,item_id,selector_json,include_json,added_at)
                        VALUES(?,?,?,?,?,?)''', (package_id, item_kind, item_id, _json(selector or {}),
                                                 _json(include), authz.utc_now()))
    authz._audit(db, 'package_item_added', 'package', package_id, {'item_kind': item_kind, 'item_id': item_id}, actor)
    return int(cur.lastrowid)


def withdraw_item(db: sqlite3.Connection, item_id: int, *, actor: str | None = None) -> None:
    row = db.execute('SELECT package_id FROM package_items WHERE id=?', (item_id,)).fetchone()
    if row is None:
        raise ValueError('package item does not exist')
    db.execute('UPDATE package_items SET withdrawn_at=? WHERE id=? AND withdrawn_at IS NULL', (authz.utc_now(), item_id))
    authz._audit(db, 'package_item_withdrawn', 'package', row['package_id'], {'item': item_id}, actor)
    latest = db.execute('SELECT objects_json FROM package_snapshots WHERE package_id=? ORDER BY id DESC LIMIT 1',
                        (row['package_id'],)).fetchone()
    if latest is not None:
        snapshot_package(db, row['package_id'], actor=actor, _narrow_to=json.loads(latest['objects_json']))


def _reachable(db: sqlite3.Connection, package_id: int) -> list:
    package = db.execute('SELECT mode FROM packages WHERE id=?', (package_id,)).fetchone()
    if package is None:
        raise ValueError('package does not exist')
    items = [(r['item_kind'], r['item_id']) for r in db.execute(
        'SELECT * FROM package_items WHERE package_id=? AND withdrawn_at IS NULL', (package_id,))
        if json.loads(r['include_json']).get('mode', package['mode']) == 'snapshot']
    cover = authz.item_coverage(db, items, authz.Coverage())
    objects = []
    for kind in FREEZE_KINDS:
        for row in db.execute(f'SELECT id FROM {authz.KIND_TABLES[kind]} ORDER BY id'):
            if any(cover.covers(home) for home in authz.homes(db, kind, row['id'])):
                objects.append([kind, row['id']])
    return objects


def snapshot_package(db: sqlite3.Connection, package_id: int, *, actor: str | None = None,
                     _narrow_to: list | None = None) -> dict:
    """Freeze the objects reachable now through the package's snapshot-mode items.

    An explicit snapshot may include material added since the last one; it is
    an audited owner action. A withdrawal only narrows (see withdraw_item).
    """
    objects = _reachable(db, package_id)
    if _narrow_to is not None:
        previous = {tuple(item) for item in _narrow_to}
        objects = [item for item in objects if tuple(item) in previous]
    raw = _json(objects)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    cur = db.execute('INSERT INTO package_snapshots(package_id,taken_at,objects_json,objects_sha256) VALUES(?,?,?,?)',
                     (package_id, authz.utc_now(), raw, digest))
    authz._audit(db, 'package_snapshot', 'package', package_id, {'snapshot': cur.lastrowid, 'objects_sha256': digest}, actor)
    return {'id': int(cur.lastrowid), 'objects': len(objects), 'objects_sha256': digest}


def independence_summary(db: sqlite3.Connection, gate: authz.ContentGate) -> dict:
    """Distinct sources among visible material; occurrences are not independence."""
    groups: dict[str, dict] = {}
    for artifact in gate.list('artifact'):
        key = f"sha256:{artifact['sha256']}" if artifact['sha256'] else f"uid:{artifact['uid']}"
        group = groups.setdefault(key, {'independence_group': key, 'artifacts': [], 'case_occurrences': 0})
        group['artifacts'].append(artifact['uid'])
    for assignment in gate.list('assignment'):
        if assignment['target_kind'] == 'artifact' and assignment['withdrawn_at'] is None:
            row = db.execute('SELECT sha256,uid FROM artifacts WHERE id=?', (assignment['target_id'],)).fetchone()
            key = f"sha256:{row['sha256']}" if row['sha256'] else f"uid:{row['uid']}"
            if key in groups:
                groups[key]['case_occurrences'] += 1
    return {'independent_sources': len(groups), 'groups': sorted(groups.values(), key=lambda g: g['independence_group']),
            'note': 'same content in several cases or assignments counts once; hash agreement is not authorship or truth'}


def export_scope_package(db: sqlite3.Connection, principal_id: int, *, capability: str = 'export') -> dict:
    """The canonical metadata package shape, built only from rows this principal may export.

    References to rows outside the export are set to null and listed by
    table, row and field, without the hidden value. Omitted categories are
    named without counts (count_disclosure); validate_metadata_package accepts it.
    """
    gate = authz.ContentGate(db, principal_id)
    tables: dict[str, list] = {}
    for name in TABLES:
        kind = TABLE_KINDS.get(name)
        order = ORDER_COLUMNS.get(name, 'id')
        if name == 'audit_log' and not gate.coverage(capability).installation:
            tables[name] = []  # audit details may name other cases
        elif name == 'schema_migrations':
            tables[name] = [dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY {order}')]
        elif kind is None:
            tables[name] = []  # join tables of tags: tags have no scope before the model
        else:
            tables[name] = [dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY {order}')
                            if gate.allows(kind, r['id'], capability)]
    exported = {name: {row['id'] for row in rows} for name, rows in tables.items() if name != 'schema_migrations'}
    redacted = []
    for name, rows in tables.items():
        for row in rows:
            for field in REFERENCES.get(name, ()):
                target = REFERENCE_TARGETS[field]
                if row.get(field) is not None and row[field] not in exported.get(target, set()):
                    row[field] = None
                    redacted.append({'table': name, 'id': row['id'], 'field': field, 'reason': 'outside_scope'})
            for field in INTERNAL_FIELDS.get(name, ()):
                if row.get(field) is not None:
                    row[field] = None
                    redacted.append({'table': name, 'id': row['id'], 'field': field, 'reason': 'installation_internal'})
    if tables['links']:
        # Polymorphic link ends must also be exported, or the link is left out.
        kept = []
        for link in tables['links']:
            ends = [(link[f'{side}_kind'], link[f'{side}_id']) for side in ('left', 'right')]
            if all(authz.KIND_TABLES.get(kind) in exported and object_id in exported[authz.KIND_TABLES[kind]]
                   for kind, object_id in ends):
                kept.append(link)
        tables['links'] = kept
    disclosure = authz.get_setting(db, 'count_disclosure', 'none')
    omitted_counts = None
    if disclosure in ('bucketed', 'exact'):
        omitted_counts = {}
        for name in TABLES:
            if name == 'schema_migrations':
                continue
            total = db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
            hidden = total - len(tables[name])
            omitted_counts[name] = hidden if disclosure == 'exact' else _bucket(hidden)
    payload = {
        'schema': SCHEMA_ID, 'metadata_only': True, 'source_bytes_included': False,
        'replay_supported': False, 'signed': False, 'exported_at': authz.utc_now(),
        'missing_tables': [],
        'tables': tables,
        'scope': {'kind': 'principal_export', 'principal_uid': db.execute(
            'SELECT uid FROM principals WHERE id=?', (principal_id,)).fetchone()['uid'],
            'capability': capability, 'coverage_key': gate.coverage(capability).key(),
            'omitted_categories': ['audit_log', 'tags', 'annotations_without_scope', 'tables_after_metadata_package_v1'],
            'counts_disclosed': omitted_counts is not None, 'count_disclosure': disclosure},
        'redacted_references': redacted,
    }
    if omitted_counts is not None:
        payload['scope']['omitted_counts'] = omitted_counts
    payload['integrity'] = _integrity(payload)
    return payload

"""Authorization before content (package P2).

`visible()` is the single decision and `ContentGate` the single read path for
content that reaches a recipient or a model. Access is derived only from
grants: default deny, the installation owner holds an ordinary grant, unknown
scope kinds and unregistered capabilities are kept and cover nothing.

An object is reachable through its *homes*: the cases it belongs to by
provenance, the cases it is assigned to, the object itself (for an explicit
set) and, for rows from before the model, the home chosen by
`legacy_scope_policy`. A scope covers homes; access is the intersection of a
grant's capability and a covered home, never the union of related cases. A
case relation is not access. Hash or citation agreement is not truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from .assignments import new_uid

KIND_TABLES = {
    'case': 'cases', 'source': 'sources', 'artifact': 'artifacts',
    'source_observation': 'source_observations', 'event': 'events',
    'derived_text': 'derived_text', 'evidence_anchor': 'evidence_anchors',
    'fragment': 'fragments', 'assignment': 'assignments', 'annotation': 'annotations',
    'link': 'links', 'tag': 'tags', 'case_relation': 'case_relations',
    'processing_run': 'processing_runs', 'job': 'jobs', 'audit_log': 'audit_log',
    'principal': 'principals', 'artifact_version': 'artifact_versions',
}
INSTALLATION = ('installation',)
LEGACY = ('legacy',)


class Denied(Exception):
    """Absent and forbidden look the same to the caller unless a setting says otherwise."""

    def __init__(self, kind: str, object_id, capability: str, *, exists: bool):
        super().__init__('not available')
        self.kind, self.object_id, self.capability, self.exists = kind, object_id, capability, exists


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace(' ', 'T'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _audit(db, action: str, kind: str, object_id: int | None, details: dict, actor: str | None) -> None:
    db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)',
               (actor or 'system', action, kind, object_id, _json(details)))


# ---------------------------------------------------------------- settings

def get_setting(db: sqlite3.Connection, key: str, default=None):
    row = db.execute('SELECT value_json FROM authz_settings WHERE key=?', (key,)).fetchone()
    return default if row is None else json.loads(row['value_json'])


def set_setting(db: sqlite3.Connection, key: str, value, *, actor: str | None = None) -> dict:
    """Record every change with its previous value; a widening is never silent."""
    previous = get_setting(db, key)
    db.execute('''INSERT INTO authz_settings(key,value_json,updated_at) VALUES(?,?,?)
                  ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at''',
               (key, _json(value), utc_now()))
    change = {'key': key, 'from': previous, 'to': value}
    _audit(db, 'authz_setting', 'authz_setting', None, change, actor)
    return change


# ---------------------------------------------------------------- scopes and grants

def create_scope(db: sqlite3.Connection, kind: str, spec: dict | None = None, *,
                 metadata: dict | None = None, actor: str | None = None) -> int:
    uid = new_uid()
    cur = db.execute('INSERT INTO scopes(uid,kind,spec_json,metadata_json) VALUES(?,?,?,?)',
                     (uid, kind, _json(spec or {}), _json(metadata or {})))
    _audit(db, 'scope_created', 'scope', cur.lastrowid, {'uid': uid, 'kind': kind}, actor)
    return int(cur.lastrowid)


def grant(db: sqlite3.Connection, principal_id: int, scope_id: int, capabilities, *,
          granted_by: str | None = None, valid_from: str | None = None, valid_to: str | None = None,
          conditions: dict | None = None, metadata: dict | None = None) -> dict:
    if not db.execute('SELECT 1 FROM principals WHERE id=?', (principal_id,)).fetchone():
        raise ValueError('principal does not exist')
    if not db.execute('SELECT 1 FROM scopes WHERE id=?', (scope_id,)).fetchone():
        raise ValueError('scope does not exist')
    capabilities = sorted(set(capabilities))
    registered = {row['name'] for row in db.execute('SELECT name FROM capability_registry')}
    uid = new_uid()
    cur = db.execute('''INSERT INTO grants(uid,principal_id,scope_id,capabilities_json,granted_by,granted_at,
        valid_from,valid_to,conditions_json,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (uid, principal_id, scope_id, _json(capabilities), granted_by or 'system', utc_now(),
         valid_from, valid_to, _json(conditions or {}), _json(metadata or {})))
    unknown = [name for name in capabilities if name not in registered]
    _audit(db, 'grant_created', 'grant', cur.lastrowid,
           {'uid': uid, 'principal_id': principal_id, 'scope_id': scope_id,
            'capabilities': capabilities, 'unregistered': unknown}, granted_by)
    warnings = [{'code': 'unregistered_capability', 'capability': name} for name in unknown]
    return {'id': int(cur.lastrowid), 'uid': uid, 'warnings': warnings}


def revoke_grant(db: sqlite3.Connection, grant_id: int, *, revoked_by: str) -> None:
    """Effective at once on every read path. Copies already taken are not recalled."""
    row = db.execute('SELECT uid,revoked_at FROM grants WHERE id=?', (grant_id,)).fetchone()
    if row is None:
        raise ValueError('grant does not exist')
    if row['revoked_at'] is not None:
        return
    db.execute('UPDATE grants SET revoked_at=?, revoked_by=? WHERE id=?', (utc_now(), revoked_by, grant_id))
    _audit(db, 'grant_revoked', 'grant', grant_id, {'uid': row['uid']}, revoked_by)


def bootstrap_owner(db: sqlite3.Connection, display_name: str, *, actor: str | None = None) -> dict:
    """Create the installation owner as a principal with an ordinary, revocable grant."""
    from .assignments import create_principal
    existing = get_setting(db, 'installation.owner_principal_uid')
    if existing is not None:
        raise ValueError('installation owner already exists')
    principal_id = create_principal(db, display_name, kind='human', actor=actor)
    uid = db.execute('SELECT uid FROM principals WHERE id=?', (principal_id,)).fetchone()['uid']
    scope_id = create_scope(db, 'installation', {}, actor=actor)
    capabilities = [row['name'] for row in db.execute('SELECT name FROM capability_registry ORDER BY name')]
    result = grant(db, principal_id, scope_id, capabilities, granted_by=actor or 'bootstrap',
                   metadata={'preset': 'installation_owner'})
    set_setting(db, 'installation.owner_principal_uid', uid, actor=actor)
    return {'principal_id': principal_id, 'principal_uid': uid, 'scope_id': scope_id, 'grant_id': result['id']}


# ---------------------------------------------------------------- homes

def _row(db, kind: str, object_id) -> sqlite3.Row | None:
    table = KIND_TABLES.get(kind)
    if table is None or type(object_id) is not int:
        return None
    return db.execute(f'SELECT * FROM {table} WHERE id=?', (object_id,)).fetchone()


def _assigned_cases(db, kind: str, object_id: int) -> set:
    return {('case', row['case_id']) for row in db.execute(
        'SELECT case_id FROM assignments WHERE target_kind=? AND target_id=? AND withdrawn_at IS NULL '
        'AND visibility_scope_id IS NULL', (kind, object_id))} | {('scope', row['visibility_scope_id']) for row in db.execute(
        'SELECT visibility_scope_id FROM assignments WHERE target_kind=? AND target_id=? AND withdrawn_at IS NULL '
        'AND visibility_scope_id IS NOT NULL', (kind, object_id))}


def _legacy_home(db, kind: str, object_id: int) -> tuple:
    policy = get_setting(db, 'legacy_scope_policy', 'owner_only')
    if isinstance(policy, str) and policy.startswith('treat_as_case:'):
        try:
            return ('case', int(policy.split(':', 1)[1]))
        except ValueError:
            return LEGACY
    if policy == 'explicit_map':
        mapped = (get_setting(db, 'legacy_scope_map', {}) or {}).get(f'{kind}:{object_id}')
        return ('case', mapped) if type(mapped) is int else LEGACY
    return LEGACY


def _source_case(db, source_id) -> set:
    if source_id is None:
        return set()
    row = db.execute('SELECT case_id FROM sources WHERE id=?', (source_id,)).fetchone()
    return {('case', row['case_id'])} if row else set()


def _contained(parent_homes: set) -> set:
    """Homes inherited from a containing object: its own identity becomes a container home."""
    return {('container',) + h[1:] if h[0] == 'object' else h for h in parent_homes}


def homes(db: sqlite3.Connection, kind: str, object_id) -> set:
    """Contexts through which an existing object can be reached; empty if absent.

    ('object', k, i) is the object itself; ('container', k, i) is an object that
    contains it (a text version inside its artifact). A frozen snapshot covers
    only the former, so later versions inside a frozen artifact stay outside.
    """
    row = _row(db, kind, object_id)
    if row is None:
        return set()
    own = {('object', kind, object_id)}
    if kind == 'case':
        return own | {('case', object_id)}
    if kind == 'source':
        return own | {('case', row['case_id'])}
    if kind == 'artifact':
        provenance = _source_case(db, row['source_id'])
        for observation in db.execute('SELECT DISTINCT source_id FROM source_observations WHERE artifact_id=?', (object_id,)):
            provenance |= _source_case(db, observation['source_id'])
        found = provenance | _assigned_cases(db, 'artifact', object_id)
        return own | (found or {_legacy_home(db, kind, object_id)})
    if kind == 'source_observation':
        return own | (_source_case(db, row['source_id']) or {_legacy_home(db, kind, object_id)})
    if kind in ('derived_text', 'processing_run', 'job'):
        base = _contained(homes(db, 'artifact', row['artifact_id'])) if row['artifact_id'] is not None else {_legacy_home(db, kind, object_id)}
        return own | base | (_assigned_cases(db, kind, object_id) if kind == 'derived_text' else set())
    if kind == 'evidence_anchor':
        base = _contained(homes(db, 'derived_text', row['derived_text_id']))
        for fragment in db.execute('SELECT id FROM fragments WHERE anchor_id=?', (object_id,)):
            base |= _assigned_cases(db, 'fragment', fragment['id'])
        return own | base | _assigned_cases(db, kind, object_id)
    if kind == 'fragment':
        return own | _contained(homes(db, 'artifact', row['artifact_id'])) | _assigned_cases(db, kind, object_id)
    if kind == 'event':
        base = _source_case(db, row['source_id'])
        if row['artifact_id'] is not None:
            base |= _contained(homes(db, 'artifact', row['artifact_id']))
        return own | (base | _assigned_cases(db, kind, object_id) or {_legacy_home(db, kind, object_id)})
    if kind == 'assignment':
        # A withdrawn assignment stays part of its case's history.
        if row['visibility_scope_id'] is not None:
            return own | {('scope', row['visibility_scope_id'])}
        return own | {('case', row['case_id'])}
    if kind == 'link':
        return own | ({('case', row['case_id'])} if row['case_id'] is not None else {_legacy_home(db, kind, object_id)})
    if kind == 'case_relation':
        # Both cases must be visible; represented by an all-of marker.
        return own | {('all_cases', row['case_a'], row['case_b'])}
    # annotations, tags, audit_log, principals, artifact versions: before the model,
    # without author or scope; the legacy policy decides.
    return own | {_legacy_home(db, kind, object_id)}


# ---------------------------------------------------------------- coverage

@dataclass
class Coverage:
    installation: bool = False
    legacy: bool = False
    cases: set = field(default_factory=set)
    objects: set = field(default_factory=set)
    frozen: set = field(default_factory=set)
    scopes: set = field(default_factory=set)

    def covers(self, home: tuple) -> bool:
        if self.installation:
            return True
        tag = home[0]
        if tag == 'case':
            return home[1] in self.cases
        if tag == 'object':
            return (home[1], home[2]) in self.objects or (home[1], home[2]) in self.frozen
        if tag == 'container':
            return (home[1], home[2]) in self.objects
        if tag == 'scope':
            return home[1] in self.scopes
        if tag == 'legacy':
            return self.legacy
        if tag == 'all_cases':
            return home[1] in self.cases and home[2] in self.cases
        return False

    def key(self) -> str:
        value = [self.installation, self.legacy, sorted(self.cases), sorted(map(list, self.objects)),
                 sorted(map(list, self.frozen)), sorted(self.scopes)]
        return hashlib.sha256(_json(value).encode()).hexdigest()


def _active(grant_row, at: datetime) -> bool:
    if grant_row['revoked_at'] is not None and _time(grant_row['revoked_at']) <= at:
        return False
    if grant_row['valid_from'] is not None and _time(grant_row['valid_from']) > at:
        return False
    if grant_row['valid_to'] is not None and _time(grant_row['valid_to']) <= at:
        return False
    return _time(grant_row['granted_at']) <= at


def coverage(db: sqlite3.Connection, principal_id: int | None, capability: str, at: str | None = None) -> Coverage:
    result = Coverage()
    if principal_id is None:
        return result
    principal = db.execute('SELECT status FROM principals WHERE id=?', (principal_id,)).fetchone()
    if principal is None or principal['status'] != 'active':
        return result
    if not db.execute('SELECT 1 FROM capability_registry WHERE name=?', (capability,)).fetchone():
        return result
    moment = _time(at) if at else datetime.now(timezone.utc)
    for row in db.execute('''SELECT g.*, s.kind AS scope_kind, s.spec_json FROM grants g
                             JOIN scopes s ON s.id=g.scope_id WHERE g.principal_id=? ORDER BY g.id''', (principal_id,)):
        if capability not in json.loads(row['capabilities_json']) or not _active(row, moment):
            continue
        result.scopes.add(row['scope_id'])
        spec = json.loads(row['spec_json'])
        kind = row['scope_kind']
        if kind == 'installation':
            result.installation = True
        elif kind == 'legacy_unscoped':
            result.legacy = True
        elif kind == 'case' and type(spec.get('case_id')) is int:
            result.cases.add(spec['case_id'])
        elif kind == 'case' and isinstance(spec.get('case_uid'), str):
            case = db.execute('SELECT id FROM cases WHERE uid=?', (spec['case_uid'],)).fetchone()
            if case:
                result.cases.add(case['id'])
        elif kind == 'owned_cases' and type(spec.get('owner_principal_id')) is int:
            result.cases |= {r['id'] for r in db.execute('SELECT id FROM cases WHERE owner_principal_id=?',
                                                         (spec['owner_principal_id'],))}
        elif kind == 'package' and type(spec.get('package_id')) is int:
            add_package_coverage(db, spec['package_id'], result)
        elif kind == 'set':
            for item in spec.get('items', []) if isinstance(spec.get('items'), list) else []:
                if isinstance(item, dict) and isinstance(item.get('kind'), str) and type(item.get('id')) is int:
                    result.objects.add((item['kind'], item['id']))
        # Unknown kinds are kept in data and cover nothing.
    return result


def item_coverage(db: sqlite3.Connection, items, into: Coverage) -> Coverage:
    """Live meaning of package items: a case item covers that case, others the object."""
    for kind, object_id in items:
        if kind == 'case':
            into.cases.add(object_id)
        else:
            into.objects.add((kind, object_id))
    return into


def add_package_coverage(db: sqlite3.Connection, package_id: int, into: Coverage) -> None:
    package = db.execute('SELECT mode FROM packages WHERE id=?', (package_id,)).fetchone()
    if package is None:
        return
    live = []
    for item in db.execute('SELECT * FROM package_items WHERE package_id=? AND withdrawn_at IS NULL', (package_id,)):
        mode = json.loads(item['include_json']).get('mode', package['mode'])
        if mode == 'live':
            live.append((item['item_kind'], item['item_id']))
        # Snapshot items reach recipients only through frozen snapshots below;
        # any other mode is kept in data and covers nothing.
    item_coverage(db, live, into)
    # Only the latest snapshot counts; withdrawals write a narrower one.
    snapshot = db.execute('SELECT objects_json FROM package_snapshots WHERE package_id=? ORDER BY id DESC LIMIT 1',
                          (package_id,)).fetchone()
    if snapshot is not None:
        into.frozen |= {(kind, object_id) for kind, object_id in json.loads(snapshot['objects_json'])}


def visible(db: sqlite3.Connection, principal_id: int | None, kind: str, object_id, capability: str,
            at: str | None = None, *, _coverage: Coverage | None = None) -> bool:
    found = homes(db, kind, object_id)
    if not found:
        return False
    cover = _coverage if _coverage is not None else coverage(db, principal_id, capability, at)
    return any(cover.covers(home) for home in found)


class ContentGate:
    """The only read path for content shown to a principal or sent to a model."""

    def __init__(self, db: sqlite3.Connection, principal_id: int | None, *, at: str | None = None):
        self.db, self.principal_id, self.at = db, principal_id, at
        self._coverage: dict[str, Coverage] = {}

    def coverage(self, capability: str) -> Coverage:
        if capability not in self._coverage:
            self._coverage[capability] = coverage(self.db, self.principal_id, capability, self.at)
        return self._coverage[capability]

    def allows(self, kind: str, object_id, capability: str) -> bool:
        return visible(self.db, self.principal_id, kind, object_id, capability, _coverage=self.coverage(capability))

    def get(self, kind: str, object_id, capability: str = 'read_metadata') -> dict:
        if not self.allows(kind, object_id, capability):
            raise Denied(kind, object_id, capability, exists=_row(self.db, kind, object_id) is not None)
        return dict(_row(self.db, kind, object_id))

    def list(self, kind: str, capability: str = 'read_metadata', *, where: str = '', params: tuple = ()) -> list[dict]:
        """Filter by visibility before limits are applied; callers page the result."""
        table = KIND_TABLES[kind]
        rows = self.db.execute(f'SELECT * FROM {table} {where} ORDER BY id', params).fetchall()
        return [dict(row) for row in rows if self.allows(kind, row['id'], capability)]

    def installation(self, capability: str) -> bool:
        """Installation-wide operations (global lists, ingest, admin export)."""
        return self.coverage(capability).installation

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
    'conversation': 'conversations', 'advocate_turn': 'advocate_turns',
    'advocate_answer': 'advocate_answers', 'derived_state': 'derived_states',
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


# Open settings. `order` lists preset values from narrowest to widest disclosure;
# `access` marks settings whose change can alter which objects a principal sees,
# checked by an explicit before/after diff. Values outside the presets are kept.
SETTINGS = {
    'legacy_scope_policy': {'presets': ['owner_only', 'treat_as_case:<case id>', 'explicit_map'], 'access': True},
    'legacy_scope_map': {'presets': ['{"<kind>:<id>": <case id>}'], 'access': True},
    'fragment.open_policy': {'presets': ['fragment_only', 'context_window:<N>', 'whole_artifact_version',
                                         'whole_document', 'predicate:<text>'], 'access': True,
                             'order': ['fragment_only', 'whole_artifact_version', 'whole_document']},
    'denial.reveal_existence': {'order': ['never', 'category_only', 'always']},
    'count_disclosure': {'order': ['none', 'bucketed', 'exact']},
    'shared_source.cross_case_hint': {'order': ['never', 'own_cases_only']},
    'conversation.owner_visibility': {'order': ['none', 'metadata_only', 'opt_in_by_recipient', 'full'],
                                      'note': 'applies to conversations started afterwards; each keeps its own'},
    'auth.mode': {'order': ['tokens', 'external', 'legacy_open']},
    'advocate.log_prompt_content': {'order': [False, True]},
    # A wider vocabulary or depth can make an existing predicate match more: diffed like access settings.
    'predicate.fields': {'access': True}, 'predicate.operators': {'access': True}, 'predicate.max_depth': {'access': True},
}
DIFF_KINDS = ('case', 'artifact', 'derived_text', 'evidence_anchor', 'fragment', 'assignment', 'event',
              'link', 'annotation', 'source_observation', 'conversation', 'advocate_turn')


class SettingWidensAccess(PermissionError):
    """A change would widen access or disclosure; it needs explicit confirmation."""

    def __init__(self, change: dict):
        super().__init__(f"setting {change['key']} widens access; confirm to apply")
        self.change = change


def _visible_snapshot(db: sqlite3.Connection) -> dict:
    result = {}
    for principal in db.execute("SELECT id, uid FROM principals WHERE status='active' ORDER BY id").fetchall():
        for capability in ('read_metadata', 'read_text'):
            cover = coverage(db, principal['id'], capability)
            for kind in DIFF_KINDS:
                for row in db.execute(f'SELECT id FROM {KIND_TABLES[kind]} ORDER BY id').fetchall():
                    if visible(db, principal['id'], kind, row['id'], capability, _coverage=cover):
                        result.setdefault((principal['uid'], capability, kind), set()).add(row['id'])
    return result


def effective_access_diff(before: dict, after: dict) -> list[dict]:
    changes = []
    for key in sorted(set(before) | set(after)):
        gained = sorted(after.get(key, set()) - before.get(key, set()))
        lost = sorted(before.get(key, set()) - after.get(key, set()))
        if gained or lost:
            principal, capability, kind = key
            changes.append({'principal_uid': principal, 'capability': capability, 'kind': kind,
                            'gained': gained, 'lost': lost})
    return changes


def _rank(order: list, value) -> int | None:
    for index, preset in enumerate(order):
        if value == preset:
            return index
    return None


def set_setting(db: sqlite3.Connection, key: str, value, *, actor: str | None = None,
                confirm_widening: bool = False, preview: bool = False) -> dict:
    """Record every change with its previous value and its effect on access.

    A change that gives anyone access to more objects, or moves a disclosure
    setting to a wider preset, is refused unless confirmed; the diff is
    returned and, when applied, written to the audit log. preview=True
    computes the same report without applying.
    """
    previous = get_setting(db, key)
    spec = SETTINGS.get(key, {})
    before = _visible_snapshot(db) if spec.get('access') else None
    db.execute('SAVEPOINT setting_change')
    try:
        db.execute('''INSERT INTO authz_settings(key,value_json,updated_at) VALUES(?,?,?)
                      ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at''',
                   (key, _json(value), utc_now()))
        diff = effective_access_diff(before, _visible_snapshot(db)) if before is not None else []
    except Exception:
        db.execute('ROLLBACK TO setting_change')
        db.execute('RELEASE setting_change')
        raise
    widening = any(item['gained'] for item in diff)
    if 'order' in spec:
        old_rank, new_rank = _rank(spec['order'], previous), _rank(spec['order'], value)
        if new_rank is None or (old_rank is not None and new_rank > old_rank) or (old_rank is None and new_rank > 0):
            widening = True if value != previous else widening
    warnings = []
    known = spec.get('presets', spec.get('order'))
    if known is not None and value not in known and not any(
            isinstance(p, str) and '<' in p and isinstance(value, str) and value.startswith(p.split('<')[0]) for p in known):
        warnings.append({'code': 'value_not_in_presets', 'value': value})
    if key == 'fragment.open_policy' and isinstance(value, str) and value.startswith('predicate:'):
        problems = validate_predicate(value.split(':', 1)[1], predicate_vocabulary(db))
        if problems:
            warnings.append({'code': 'predicate_invalid_opens_nothing_extra', 'errors': problems})
    change = {'key': key, 'from': previous, 'to': value, 'widens': widening,
              'effective_access_diff': diff, 'warnings': warnings, 'applied': False}
    if preview or (widening and not confirm_widening):
        db.execute('ROLLBACK TO setting_change')
        db.execute('RELEASE setting_change')
        if preview:
            return change
        raise SettingWidensAccess(change)
    db.execute('RELEASE setting_change')
    change['applied'] = True
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


def delegate(db: sqlite3.Connection, granter_id: int, scope_id: int, grantee_id: int, capabilities, *,
             valid_to: str | None = None) -> dict:
    """Pass on part of one's own grant (capability 'invite'), within its delegation depth.

    The delegated grant never exceeds its parent: same scope, a subset of its
    capabilities, no later expiry, and it stops when the parent is revoked or
    expires. max_delegation_depth comes from the parent's conditions; absent
    means 1 (preset), explicit null means unlimited.
    """
    moment = datetime.now(timezone.utc)
    wanted = sorted(set(capabilities))
    for row in db.execute('SELECT * FROM grants WHERE principal_id=? AND scope_id=? ORDER BY id', (granter_id, scope_id)).fetchall():
        held = set(json.loads(row['capabilities_json']))
        if not _active(row, moment, db) or 'invite' not in held or not set(wanted) <= held:
            continue
        conditions = json.loads(row['conditions_json'] or '{}')
        depth = json.loads(row['metadata_json'] or '{}').get('delegation_depth', 0)
        limit = conditions.get('max_delegation_depth', 1)
        if limit is not None and depth + 1 > limit:
            continue
        if valid_to is not None and row['valid_to'] is not None and _time(valid_to) > _time(row['valid_to']):
            valid_to = row['valid_to']
        granter_uid = db.execute('SELECT uid FROM principals WHERE id=?', (granter_id,)).fetchone()['uid']
        return grant(db, grantee_id, scope_id, wanted, granted_by=granter_uid,
                     valid_to=valid_to if valid_to is not None else row['valid_to'],
                     conditions={'max_delegation_depth': limit},
                     metadata={'delegated_from': row['uid'], 'delegation_depth': depth + 1})
    raise Denied('scope', scope_id, 'invite', exists=True)


def fragment_context(db: sqlite3.Connection, principal_id: int, fragment_id: int, *,
                     capability: str = 'read_text', gate: 'ContentGate | None' = None) -> dict | None:
    """The readable content of a fragment under fragment.open_policy, or None if not visible.

    fragment_only and wider: the pinned quote; context_window:N additionally
    the N segments before and after the quoted segments of that text version.
    """
    allowed = gate.allows('fragment', fragment_id, capability) if gate is not None else \
        visible(db, principal_id, 'fragment', fragment_id, capability)
    if not allowed:
        return None
    fragment = _row(db, 'fragment', fragment_id)
    result = {'fragment_id': fragment_id, 'kind': fragment['kind'], 'selector': json.loads(fragment['selector_json']),
              'quote': None, 'context': [], 'derived_text_id': fragment['derived_text_id']}
    anchor = _row(db, 'evidence_anchor', fragment['anchor_id']) if fragment['anchor_id'] is not None else None
    if anchor is not None:
        result.update(quote=anchor['quote_text'], start_ms=anchor['start_ms'], end_ms=anchor['end_ms'],
                      quote_sha256=anchor['quote_sha256'])
    # The widest context among the assignments through which this principal sees the fragment.
    width = None
    cover = gate.coverage(capability) if gate is not None else coverage(db, principal_id, capability)
    for assignment in _fragment_assignments(db, fragment_id):
        policy = effective_open_policy(db, fragment, assignment)
        if policy.startswith('context_window:') and cover.covers(_assignment_home(assignment)):
            try:
                width = max(width or 0, max(0, int(policy.split(':', 1)[1])))
            except ValueError:
                width = max(width or 0, 0)
    if width is None:
        policy = get_setting(db, 'fragment.open_policy', 'fragment_only')
        if isinstance(policy, str) and policy.startswith('context_window:'):
            try:
                width = max(0, int(policy.split(':', 1)[1]))
            except ValueError:
                width = 0
    if width is not None and anchor is not None:
        selector = json.loads(anchor['selector_json'])
        indices = selector.get('indices') if selector.get('kind') == 'segments' else \
            sorted({ref['segment_index'] for ref in selector.get('word_refs', [])})
        text = _row(db, 'derived_text', anchor['derived_text_id'])
        segments = json.loads(text['segments_json'] or '[]')
        if indices:
            low, high = max(0, min(indices) - width), min(len(segments) - 1, max(indices) + width)
            result['context'] = [{'segment_index': i, 'text': segments[i].get('text', ''),
                                  'start': segments[i].get('start'), 'end': segments[i].get('end')}
                                 for i in range(low, high + 1)]
    return result


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


def _private(conversation, level: str) -> tuple:
    """A conversation keeps the owner-visibility mode shown to its recipient when it started."""
    settings = json.loads(conversation['settings_json'] or '{}')
    mode = settings.get('owner_visibility', 'none')
    if mode == 'opt_in_by_recipient':
        mode = 'full' if settings.get('shared_with_owner') is True else 'none'
    return ('private', conversation['principal_id'], level, mode if isinstance(mode, str) else 'none')


OPEN_LEVELS = ('fragment_only', 'whole_artifact_version', 'whole_document')
# Predicate vocabulary is data (settings `predicate.fields`, `predicate.operators`,
# `predicate.max_depth`); the values below are only the seed written at migration.
# Closed on purpose (security, 2026-10-26): nothing from a setting is executed. A
# field maps to a column of a row the interpreter already loads (fragment,
# assignment, case), optionally one key of a JSON column; an operator is enabled by
# data but must exist in OPERATORS, so a new operator needs code.
PREDICATE_ROWS = ('fragment', 'assignment', 'case')
PREDICATE_FIELDS_SEED = {
    'fragment.kind': {'row': 'fragment', 'column': 'kind'},
    'fragment.id': {'row': 'fragment', 'column': 'id'},
    'assignment.status': {'row': 'assignment', 'column': 'status'},
    'assignment.author_kind': {'row': 'assignment', 'column': 'author_kind'},
    'assignment.case_id': {'row': 'assignment', 'column': 'case_id'},
    'assignment.issue': {'row': 'assignment', 'column': 'issue_ref_json', 'json_key': 'issue'},
    'case.uid': {'row': 'case', 'column': 'uid'},
}
OPERATORS = {
    'eq': lambda value, operand: value == operand,
    'in': lambda value, operand: isinstance(operand, list) and value in operand,
    'prefix': lambda value, operand: isinstance(value, str) and isinstance(operand, str) and value.startswith(operand),
}
PREDICATE_OPERATORS_SEED = ['eq', 'in', 'prefix']
PREDICATE_MAX_DEPTH_SEED = 16


def predicate_vocabulary(db) -> dict:
    fields = get_setting(db, 'predicate.fields', PREDICATE_FIELDS_SEED)
    operators = get_setting(db, 'predicate.operators', PREDICATE_OPERATORS_SEED)
    depth = get_setting(db, 'predicate.max_depth', PREDICATE_MAX_DEPTH_SEED)
    return {
        'fields': {name: spec for name, spec in (fields if isinstance(fields, dict) else {}).items()
                   if isinstance(spec, dict) and spec.get('row') in PREDICATE_ROWS and isinstance(spec.get('column'), str)},
        'operators': [op for op in (operators if isinstance(operators, list) else []) if op in OPERATORS],
        'max_depth': depth if type(depth) is int and depth >= 0 else 0,
        'ignored_operators': [op for op in (operators if isinstance(operators, list) else []) if op not in OPERATORS],
    }


def _open_value(value) -> str | None:
    if value in OPEN_LEVELS:
        return value
    if isinstance(value, str) and value.startswith('context_window:'):
        return value
    return None


def validate_predicate(raw: str, vocabulary: dict | None = None) -> list[str]:
    """Errors in a 'predicate:<json>' open policy; an invalid predicate opens nothing extra."""
    if vocabulary is None:
        vocabulary = {'fields': PREDICATE_FIELDS_SEED, 'operators': PREDICATE_OPERATORS_SEED,
                      'max_depth': PREDICATE_MAX_DEPTH_SEED, 'ignored_operators': []}
    try:
        spec = json.loads(raw)
    except ValueError:
        return ['predicate is not JSON']
    if not isinstance(spec, dict) or 'when' not in spec or 'then' not in spec:
        return ['predicate needs "when" and "then"']
    errors = []
    if _open_value(spec['then']) is None:
        errors.append(f'unknown "then": {spec["then"]!r}')
    if 'else' in spec and _open_value(spec['else']) is None:
        errors.append(f'unknown "else": {spec["else"]!r}')
    operators = vocabulary['operators']

    def check(node, depth):
        if depth > vocabulary['max_depth']:
            errors.append(f'predicate nests deeper than predicate.max_depth={vocabulary["max_depth"]}')
            return
        if not isinstance(node, dict) or len([k for k in ('all', 'any', 'not', 'field') if k in node]) != 1:
            errors.append(f'condition must have exactly one of all/any/not/field: {node!r}')
            return
        if 'all' in node or 'any' in node:
            items = node.get('all', node.get('any'))
            if not isinstance(items, list):
                errors.append('all/any takes a list')
                return
            for item in items:
                check(item, depth + 1)
        elif 'not' in node:
            check(node['not'], depth + 1)
        else:
            if node['field'] not in vocabulary['fields']:
                errors.append(f'unknown field {node["field"]!r} (see predicate.fields)')
            used = [k for k in OPERATORS if k in node]
            if len(used) != 1 or used[0] not in operators:
                errors.append(f'field condition needs exactly one enabled operator of {operators}: {node!r}')
    check(spec['when'], 0)
    return errors


def _field_value(spec: dict, rows: dict):
    row = rows.get(spec['row'])
    if row is None or spec['column'] not in row.keys():
        return None
    value = row[spec['column']]
    if 'json_key' in spec:
        try:
            decoded = json.loads(value) if isinstance(value, str) else None
        except ValueError:
            return None
        value = decoded.get(spec['json_key']) if isinstance(decoded, dict) else decoded
    return value if isinstance(value, (str, int, float, bool)) or value is None else None


def _evaluate(node, rows: dict, vocabulary: dict, depth: int = 0) -> bool:
    """A fixed interpreter over data; nothing from a setting is ever executed."""
    if depth > vocabulary['max_depth'] or not isinstance(node, dict):
        return False
    if 'all' in node:
        return isinstance(node['all'], list) and all(_evaluate(n, rows, vocabulary, depth + 1) for n in node['all'])
    if 'any' in node:
        return isinstance(node['any'], list) and any(_evaluate(n, rows, vocabulary, depth + 1) for n in node['any'])
    if 'not' in node:
        return not _evaluate(node['not'], rows, vocabulary, depth + 1)
    spec = vocabulary['fields'].get(node.get('field'))
    if spec is None:
        return False
    value = _field_value(spec, rows)
    for operator in vocabulary['operators']:
        if operator in node:
            return OPERATORS[operator](value, node[operator])
    return False


def effective_open_policy(db, fragment, assignment) -> str:
    """What one fragment assignment opens under fragment.open_policy.

    predicate:{"when": <condition>, "then": <level>, "else": <level>} is evaluated per
    (fragment, assignment), so a document opens only in the case whose assignment
    matches. An invalid predicate or an unknown value opens nothing beyond the fragment.
    """
    policy = get_setting(db, 'fragment.open_policy', 'fragment_only')
    if not isinstance(policy, str):
        return 'fragment_only'
    if not policy.startswith('predicate:'):
        return _open_value(policy) or 'fragment_only'
    raw = policy.split(':', 1)[1]
    vocabulary = predicate_vocabulary(db)
    if validate_predicate(raw, vocabulary):
        return 'fragment_only'
    spec = json.loads(raw)
    case = db.execute('SELECT * FROM cases WHERE id=?', (assignment['case_id'],)).fetchone()
    rows = {'fragment': fragment, 'assignment': assignment, 'case': case}
    return spec['then'] if _evaluate(spec['when'], rows, vocabulary) else spec.get('else', 'fragment_only')


def _assignment_home(assignment) -> tuple:
    if assignment['visibility_scope_id'] is not None:
        return ('scope', assignment['visibility_scope_id'])
    return ('case', assignment['case_id'])


def _fragment_assignments(db, fragment_id: int) -> list:
    return db.execute("SELECT * FROM assignments WHERE target_kind='fragment' AND target_id=? AND withdrawn_at IS NULL",
                      (fragment_id,)).fetchall()


def _fragment_openings(db, column: str, value: int, level: str) -> set:
    """Homes of fragment assignments whose effective policy opens `level` (column is a fixed name)."""
    found = set()
    for fragment in db.execute(f'SELECT * FROM fragments WHERE {column}=?', (value,)).fetchall():
        for assignment in _fragment_assignments(db, fragment['id']):
            if effective_open_policy(db, fragment, assignment) == level:
                found.add(_assignment_home(assignment))
    return found


def _inputs(raw: str) -> tuple:
    refs = tuple(sorted((kind, object_id) for kind, object_id in json.loads(raw)))
    return ('require_inputs', refs)


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
        found |= _fragment_openings(db, 'artifact_id', object_id, 'whole_document')
        return own | (found or {_legacy_home(db, kind, object_id)})
    if kind == 'source_observation':
        return own | (_source_case(db, row['source_id']) or {_legacy_home(db, kind, object_id)})
    if kind in ('derived_text', 'processing_run', 'job'):
        base = _contained(homes(db, 'artifact', row['artifact_id'])) if row['artifact_id'] is not None else {_legacy_home(db, kind, object_id)}
        extra = set()
        if kind == 'derived_text':
            extra = _assigned_cases(db, kind, object_id)
            extra |= _fragment_openings(db, 'derived_text_id', object_id, 'whole_artifact_version')
        return own | base | extra
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
    if kind == 'conversation':
        # No object home: an installation or set grant must not open a private conversation.
        return {_private(row, 'meta')}
    if kind == 'advocate_turn':
        conversation = db.execute('SELECT * FROM conversations WHERE id=?', (row['conversation_id'],)).fetchone()
        return {_private(conversation, 'content')}
    if kind == 'advocate_answer':
        conversation = db.execute('''SELECT c.* FROM advocate_turns t JOIN conversations c ON c.id=t.conversation_id
                                     WHERE t.id=?''', (row['turn_id'],)).fetchone()
        return {_private(conversation, 'content'), _inputs(row['derived_scope_json'])}
    if kind == 'derived_state':
        # Shareable with whoever can read every input, and only then.
        return own | {('anyone',), _inputs(row['input_refs_json'])}
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
    principal_id: int | None = None
    scopes: set = field(default_factory=set)

    def covers(self, home: tuple) -> bool:
        tag = home[0]
        if tag == 'private':
            # Installation scope opens another principal's conversation only as its own mode allows.
            if home[1] == self.principal_id:
                return True
            level, mode = home[2], home[3]
            return self.installation and (mode == 'full' or (mode == 'metadata_only' and level == 'meta'))
        if tag == 'anyone':
            return True
        if self.installation:
            return True
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
                 sorted(map(list, self.frozen)), sorted(self.scopes), self.principal_id]
        return hashlib.sha256(_json(value).encode()).hexdigest()


# Every table whose rows can change what a principal sees or reads.
GENERATION_TABLES = ('audit_log', 'cases', 'sources', 'artifacts', 'source_observations', 'events',
                     'derived_text', 'evidence_anchors', 'fragments', 'assignments', 'links', 'annotations',
                     'grants', 'scopes', 'principals', 'packages', 'package_items', 'package_snapshots',
                     'case_relations', 'search_fts')


def data_generation(db: sqlite3.Connection) -> str:
    """A fingerprint that changes whenever data affecting visibility or content changes.

    Counts and maxima catch inserts and deletes; the mutable columns that decide
    visibility (withdrawals, revocations, status, settings) are hashed directly.
    """
    parts = []
    for table in GENERATION_TABLES:
        key = 'rowid' if table == 'search_fts' else 'id'
        parts.append(tuple(db.execute(f'SELECT count(*), max({key}) FROM {table}').fetchone()))
    for sql in ('SELECT group_concat(id || ":" || COALESCE(withdrawn_at,"") || ":" || status || ":" || COALESCE(visibility_scope_id,"")) FROM assignments',
                'SELECT group_concat(id || ":" || COALESCE(revoked_at,"")) FROM grants',
                'SELECT group_concat(id || ":" || status) FROM principals',
                'SELECT group_concat(id || ":" || COALESCE(withdrawn_at,"")) FROM package_items',
                'SELECT group_concat(key || "=" || value_json) FROM authz_settings',
                'SELECT group_concat(id || ":" || COALESCE(owner_principal_id,"")) FROM cases',
                'SELECT group_concat(id || ":" || COALESCE(case_id,"")) FROM links'):
        parts.append(db.execute(sql).fetchone()[0])
    return hashlib.sha256(_json(parts).encode()).hexdigest()


def _active(grant_row, at: datetime, db: sqlite3.Connection | None = None, _depth: int = 0) -> bool:
    if not _active_own(grant_row, at):
        return False
    parent = json.loads(grant_row['metadata_json'] or '{}').get('delegated_from')
    if parent is None or db is None:
        return True
    row = db.execute('SELECT * FROM grants WHERE uid=?', (parent,)).fetchone()
    return row is not None and _depth < 16 and _active(row, at, db, _depth + 1)


def _active_own(grant_row, at: datetime) -> bool:
    if grant_row['revoked_at'] is not None and _time(grant_row['revoked_at']) <= at:
        return False
    if grant_row['valid_from'] is not None and _time(grant_row['valid_from']) > at:
        return False
    if grant_row['valid_to'] is not None and _time(grant_row['valid_to']) <= at:
        return False
    return _time(grant_row['granted_at']) <= at


def apply_scope(db: sqlite3.Connection, scope_id: int, kind: str, spec: dict, result: Coverage) -> Coverage:
    """Add what one scope covers; unknown kinds are kept in data and cover nothing."""
    result.scopes.add(scope_id)
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
    return result


def scope_coverage(db: sqlite3.Connection, scope_ids) -> Coverage:
    """What a set of scopes covers, independent of any principal (e.g. provider consent)."""
    result = Coverage()
    for scope_id in scope_ids:
        row = db.execute('SELECT kind,spec_json FROM scopes WHERE id=?', (scope_id,)).fetchone()
        if row is not None:
            apply_scope(db, scope_id, row['kind'], json.loads(row['spec_json']), result)
    return result


def covered_by(db: sqlite3.Connection, cover: Coverage, kind: str, object_id) -> bool:
    found = homes(db, kind, object_id)
    return bool(found) and any(cover.covers(h) for h in found if h[0] not in ('require_inputs', 'private', 'anyone'))


def coverage(db: sqlite3.Connection, principal_id: int | None, capability: str, at: str | None = None) -> Coverage:
    result = Coverage(principal_id=principal_id)
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
        if capability not in json.loads(row['capabilities_json']) or not _active(row, moment, db):
            continue
        apply_scope(db, row['scope_id'], row['scope_kind'], json.loads(row['spec_json']), result)
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
    metadata = json.loads(db.execute('SELECT metadata_json FROM packages WHERE id=?', (package_id,)).fetchone()['metadata_json'] or '{}')
    policy = metadata.get('growth_policy', 'frozen')
    if isinstance(policy, str) and policy.startswith('auto_include_by_rule:'):
        rule_kind = policy.split(':', 1)[1]
        snapshot_items = []
        for item in db.execute('SELECT * FROM package_items WHERE package_id=? AND withdrawn_at IS NULL', (package_id,)):
            if json.loads(item['include_json']).get('mode', package['mode']) == 'snapshot':
                snapshot_items.append((item['item_kind'], item['item_id']))
        reach = item_coverage(db, snapshot_items, Coverage())
        table = KIND_TABLES.get(rule_kind)
        if table is not None:
            for row in db.execute(f'SELECT id FROM {table} ORDER BY id').fetchall():
                if any(reach.covers(h) for h in homes(db, rule_kind, row['id']) if h[0] not in ('require_inputs', 'private', 'anyone')):
                    into.frozen.add((rule_kind, row['id']))
    # Only the latest snapshot counts; withdrawals write a narrower one.
    snapshot = db.execute('SELECT objects_json FROM package_snapshots WHERE package_id=? ORDER BY id DESC LIMIT 1',
                          (package_id,)).fetchone()
    if snapshot is not None:
        into.frozen |= {(kind, object_id) for kind, object_id in json.loads(snapshot['objects_json'])}


def visible(db: sqlite3.Connection, principal_id: int | None, kind: str, object_id, capability: str,
            at: str | None = None, *, _coverage: Coverage | None = None, _depth: int = 0) -> bool:
    found = homes(db, kind, object_id)
    if not found:
        return False
    cover = _coverage if _coverage is not None else coverage(db, principal_id, capability, at)
    required = [home for home in found if home[0] == 'require_inputs']
    if not any(cover.covers(home) for home in found if home[0] != 'require_inputs'):
        return False
    if not required:
        return True
    if _depth >= 8:
        return False
    # derived_scope: a derived state is visible only when every input is readable.
    text_cover = coverage(db, principal_id, 'read_text', at)
    return all(visible(db, principal_id, input_kind, input_id, 'read_text', at,
                       _coverage=text_cover, _depth=_depth + 1)
               for home in required for input_kind, input_id in home[1])


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

"""Package P2: grants, scopes and the single visibility decision.

All data is synthetic and generated in memory. Every text field carries a
canary naming its object; a canary in an output proves which object leaked.
The property tests compare ContentGate with an oracle written independently
from the generated configuration, not from authz.py.
"""
import json
import sqlite3
import unittest

from hypothesis import HealthCheck, given, settings, strategies as st

from server.app import assignments as model
from server.app import authz
from server.app import db as database

CANARY = 'CANARY'
LIST_KINDS = ('case', 'artifact', 'derived_text', 'evidence_anchor', 'fragment',
              'assignment', 'annotation', 'link', 'case_relation')


def memory_db():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(database.SCHEMA)
    database.migrate_db(db)
    return db


def canary(kind, number):
    return f'{CANARY}-{kind}-{number}'


class World:
    """Builds rows and remembers the configuration for the oracle."""

    def __init__(self, db):
        self.db = db
        self.cases, self.artifacts, self.assignments = [], {}, {}
        self.texts, self.anchors, self.fragments = {}, {}, {}
        self.annotations, self.links, self.relations = {}, {}, {}

    def case(self):
        cid = self.db.execute('INSERT INTO cases(name) VALUES(?)', ('pending',)).lastrowid
        self.db.execute('UPDATE cases SET name=? WHERE id=?', (canary('case', cid), cid))
        self.db.execute("INSERT INTO sources(id,case_id,kind,label) VALUES(?,?,'synthetic',?)", (cid, cid, canary('source', cid)))
        self.cases.append(cid)
        return cid

    def artifact(self, case_id):
        aid = self.db.execute("INSERT INTO artifacts(source_id,sha256,original_name,source_locator) VALUES(?,?,?,?)",
                              (case_id, f'{len(self.artifacts):064x}', 'pending', f'synthetic://{len(self.artifacts)}')).lastrowid
        self.db.execute('UPDATE artifacts SET original_name=? WHERE id=?', (canary('artifact', aid), aid))
        tid = self.db.execute("INSERT INTO derived_text(artifact_id,kind,text) VALUES(?,'transcript',?)",
                              (aid, 'pending')).lastrowid
        self.db.execute('UPDATE derived_text SET text=? WHERE id=?', (canary('derived_text', tid), tid))
        nid = self.db.execute('''INSERT INTO evidence_anchors(artifact_id,derived_text_id,start_ms,end_ms,quote_text,
            quote_sha256,selector_json) VALUES(?,?,0,1,?,'x','{}')''', (aid, tid, 'pending')).lastrowid
        fragment = model.create_fragment(self.db, artifact_id=aid, kind='range', selector={'note': canary('fragment', 'next')},
                                         derived_text_id=tid, anchor_id=nid)
        self.artifacts[aid] = case_id
        self.texts[tid] = aid
        self.anchors[nid] = (aid, tid)
        self.fragments[fragment['id']] = (aid, nid)
        return aid

    def assign(self, case_id, kind, target, withdrawn=False):
        result = model.assign(self.db, case_id=case_id, target_kind=kind, target_id=target, meaning_text='pending')
        # meaning_text is mutable only through tests here; identity columns stay fixed.
        self.db.execute('UPDATE assignments SET meaning_text=? WHERE id=?', (canary('assignment', result['id']), result['id']))
        if withdrawn:
            model.withdraw_assignment(self.db, result['id'])
        self.assignments[result['id']] = (case_id, kind, target, withdrawn)

    def annotate(self, artifact_id):
        nid = self.db.execute("INSERT INTO annotations(artifact_id,body) VALUES(?,?)", (artifact_id, 'pending')).lastrowid
        self.db.execute('UPDATE annotations SET body=? WHERE id=?', (canary('annotation', nid), nid))
        self.annotations[nid] = artifact_id

    def link(self, case_id):
        lid = self.db.execute("INSERT INTO links(left_kind,left_id,right_kind,right_id,relation,rationale,case_id) VALUES('artifact',1,'artifact',1,'same',?,?)",
                              ('pending', case_id)).lastrowid
        self.db.execute('UPDATE links SET rationale=? WHERE id=?', (canary('link', lid), lid))
        self.links[lid] = case_id

    def relate(self, a, b):
        rid = model.relate_cases(self.db, a, b, 'pending')
        self.relations[rid] = (a, b)


def oracle(world, grants, policy, legacy_map):
    """Expected visible ids per kind, derived from the generated configuration only."""
    if any(g['scope'][0] == 'installation' for g in grants):
        return {
            'case': set(world.cases), 'artifact': set(world.artifacts), 'derived_text': set(world.texts),
            'evidence_anchor': set(world.anchors), 'fragment': set(world.fragments),
            'assignment': set(world.assignments), 'annotation': set(world.annotations),
            'link': set(world.links), 'case_relation': set(world.relations),
        }
    cases = {g['scope'][1] for g in grants if g['scope'][0] == 'case'}
    objects = {g['scope'][1] for g in grants if g['scope'][0] == 'set'}
    legacy = any(g['scope'][0] == 'legacy_unscoped' for g in grants)

    def assigned(kind, target):
        return {c for c, k, t, w in world.assignments.values() if k == kind and t == target and not w}

    def legacy_visible(kind, number):
        if policy == 'owner_only':
            return legacy
        if policy.startswith('treat_as_case:'):
            return int(policy.split(':')[1]) in cases
        mapped = legacy_map.get(f'{kind}:{number}')
        return mapped in cases if mapped is not None else legacy

    art = {a for a, c in world.artifacts.items() if c in cases or assigned('artifact', a) & cases or a in objects}
    text = {t for t, a in world.texts.items() if a in art or assigned('derived_text', t) & cases}
    frag = {f for f, (a, n) in world.fragments.items() if a in art or assigned('fragment', f) & cases}
    anchor = {n for n, (a, t) in world.anchors.items()
              if t in text or assigned('evidence_anchor', n) & cases
              or any(assigned('fragment', f) & cases for f, (_, fn) in world.fragments.items() if fn == n)}
    return {
        'case': set(world.cases) & cases, 'artifact': art, 'derived_text': text,
        'evidence_anchor': anchor, 'fragment': frag,
        'assignment': {i for i, (c, *_) in world.assignments.items() if c in cases},
        'annotation': {i for i in world.annotations if legacy_visible('annotation', i)},
        'link': {i for i, c in world.links.items() if (c in cases if c is not None else legacy_visible('link', i))},
        'case_relation': {i for i, (a, b) in world.relations.items() if a in cases and b in cases},
    }


@st.composite
def configurations(draw):
    n_cases = draw(st.integers(1, 3))
    n_artifacts = draw(st.integers(1, 4))
    provenance = [draw(st.integers(0, n_cases - 1)) for _ in range(n_artifacts)]
    assignments = draw(st.lists(st.tuples(st.integers(0, n_cases - 1), st.sampled_from(['artifact', 'fragment', 'derived_text']),
                                          st.integers(0, n_artifacts - 1), st.booleans()), max_size=5))
    annotations = draw(st.lists(st.integers(0, n_artifacts - 1), max_size=3))
    links = draw(st.lists(st.one_of(st.none(), st.integers(0, n_cases - 1)), max_size=3))
    relations = draw(st.lists(st.tuples(st.integers(0, n_cases - 1), st.integers(0, n_cases - 1)), max_size=2))
    scope = st.one_of(
        st.just(('installation', None)), st.just(('legacy_unscoped', None)), st.just(('unknown_kind', None)),
        st.tuples(st.just('case'), st.integers(0, n_cases - 1)),
        st.tuples(st.just('set'), st.integers(0, n_artifacts - 1)))
    grants = draw(st.lists(st.fixed_dictionaries({
        'scope': scope,
        'caps': st.sets(st.sampled_from(['read_metadata', 'read_text', 'not_registered']), max_size=3),
        'revoked': st.booleans(),
        'window': st.sampled_from(['open', 'expired', 'future']),
    }), max_size=4))
    policy = draw(st.sampled_from(['owner_only', 'treat_as_case:0', 'explicit_map']))
    return dict(n_cases=n_cases, provenance=provenance, assignments=assignments, annotations=annotations,
                links=links, relations=relations, grants=grants, policy=policy)


def build(config):
    db = memory_db()
    world = World(db)
    cases = [world.case() for _ in range(config['n_cases'])]
    artifacts = [world.artifact(cases[i]) for i in config['provenance']]
    for case_index, kind, artifact_index, withdrawn in config['assignments']:
        aid = artifacts[artifact_index]
        target = {'artifact': aid, 'derived_text': next(t for t, a in world.texts.items() if a == aid),
                  'fragment': next(f for f, (a, _) in world.fragments.items() if a == aid)}[kind]
        world.assign(cases[case_index], kind, target, withdrawn)
    for index in config['annotations']:
        world.annotate(artifacts[index])
    for index in config['links']:
        world.link(None if index is None else cases[index])
    for a, b in config['relations']:
        world.relate(cases[a], cases[b])
    policy = config['policy']
    legacy_map = {}
    if policy.startswith('treat_as_case:'):
        policy = f'treat_as_case:{cases[0]}'
    elif policy == 'explicit_map':
        legacy_map = {f'annotation:{i}': cases[-1] for i in list(world.annotations)[:1]}
        authz.set_setting(db, 'legacy_scope_map', legacy_map)
    authz.set_setting(db, 'legacy_scope_policy', policy)
    principal = model.create_principal(db, 'recipient')
    granted = []
    for spec in config['grants']:
        kind, ref = spec['scope']
        resolved = {'case': cases[ref] if kind == 'case' else None,
                    'set': artifacts[ref] if kind == 'set' else None}.get(kind)
        scope_spec = {'case_id': resolved} if kind == 'case' else (
            {'items': [{'kind': 'artifact', 'id': resolved}]} if kind == 'set' else {})
        scope_id = authz.create_scope(db, kind, scope_spec)
        window = {'open': {}, 'expired': {'valid_to': '2000-01-01T00:00:00+00:00'},
                  'future': {'valid_from': '2999-01-01T00:00:00+00:00'}}[spec['window']]
        gid = authz.grant(db, principal, scope_id, spec['caps'], **window)['id']
        if spec['revoked']:
            authz.revoke_grant(db, gid, revoked_by='test')
        granted.append({**spec, 'scope': (kind, resolved), 'id': gid})
    return db, world, principal, granted, policy, legacy_map


def effective(granted, capability):
    return [g for g in granted if capability in g['caps'] and not g['revoked'] and g['window'] == 'open']


def outputs(db, principal, capability):
    gate = authz.ContentGate(db, principal)
    return {kind: gate.list(kind, capability) for kind in LIST_KINDS}


def canaries_in(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    found, start = set(), 0
    while (index := text.find(CANARY + '-', start)) >= 0:
        end = index + len(CANARY) + 1
        while end < len(text) and (text[end].isalnum() or text[end] in '-_'):
            end += 1
        found.add(text[index:end])
        start = end
    return found


PROPERTY_SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


class PropertyTests(unittest.TestCase):
    @PROPERTY_SETTINGS
    @given(configurations(), st.sampled_from(['read_metadata', 'read_text']))
    def test_p1_gate_matches_oracle_and_no_canary_leaks(self, config, capability):
        db, world, principal, granted, policy, legacy_map = build(config)
        expected = oracle(world, effective(granted, capability), policy, legacy_map)
        result = outputs(db, principal, capability)
        for kind in LIST_KINDS:
            self.assertEqual({row['id'] for row in result[kind]}, expected[kind], kind)
        allowed = {canary(kind, i) for kind, ids in expected.items() for i in ids}
        allowed |= {canary('fragment', 'next')} if expected['fragment'] else set()
        leaked = canaries_in(result) - allowed
        self.assertEqual(leaked, set())

    @PROPERTY_SETTINGS
    @given(configurations(), st.sampled_from(['read_metadata', 'read_text']), st.data())
    def test_p2_monotonic_grant_and_revoke(self, config, capability, data):
        db, world, principal, granted, *_ = build(config)
        before = {k: {r['id'] for r in v} for k, v in outputs(db, principal, capability).items()}
        target = data.draw(st.sampled_from(world.cases))
        scope_id = authz.create_scope(db, 'case', {'case_id': target})
        extra = authz.grant(db, principal, scope_id, [capability])['id']
        widened = {k: {r['id'] for r in v} for k, v in outputs(db, principal, capability).items()}
        for kind in LIST_KINDS:
            self.assertLessEqual(before[kind], widened[kind], kind)
        victims = [g['id'] for g in granted if not g['revoked']] + [extra]
        authz.revoke_grant(db, data.draw(st.sampled_from(victims)), revoked_by='test')
        narrowed = {k: {r['id'] for r in v} for k, v in outputs(db, principal, capability).items()}
        for kind in LIST_KINDS:
            self.assertLessEqual(narrowed[kind], widened[kind], kind)

    @PROPERTY_SETTINGS
    @given(configurations(), st.sampled_from(['read_metadata', 'read_text']))
    def test_p3_changes_outside_scope_do_not_change_output(self, config, capability):
        db, world, principal, granted, *_ = build(config)
        if any(g['scope'][0] == 'installation' for g in effective(granted, capability)):
            return
        before = json.dumps(outputs(db, principal, capability), sort_keys=True, default=str)
        hidden = world.case()
        aid = world.artifact(hidden)
        world.assign(hidden, 'artifact', aid)
        world.link(hidden)
        # Also mutate existing material only through a case the recipient cannot see.
        world.assign(hidden, 'artifact', next(iter(world.artifacts)))
        after = json.dumps(outputs(db, principal, capability), sort_keys=True, default=str)
        self.assertEqual(before, after)


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.world = World(self.db)
        self.case_a, self.case_b = self.world.case(), self.world.case()
        self.shared = self.world.artifact(self.case_a)
        self.private_b = self.world.artifact(self.case_b)
        self.world.assign(self.case_a, 'artifact', self.shared)
        self.world.assign(self.case_b, 'artifact', self.shared)
        self.world.annotate(self.shared)
        self.world.link(self.case_b)
        self.world.relate(self.case_a, self.case_b)
        self.owner = authz.bootstrap_owner(self.db, 'owner')['principal_id']
        self.recipient = model.create_principal(self.db, 'recipient')
        self.scope_a = authz.create_scope(self.db, 'case', {'case_id': self.case_a})
        self.grant_a = authz.grant(self.db, self.recipient, self.scope_a, ['read_metadata', 'read_text'])['id']

    def test_s4_shared_material_does_not_open_the_other_case(self):
        gate = authz.ContentGate(self.db, self.recipient)
        self.assertTrue(gate.allows('artifact', self.shared, 'read_text'))
        result = outputs(self.db, self.recipient, 'read_text')
        found = canaries_in(result)
        for forbidden in (canary('case', self.case_b), canary('artifact', self.private_b)):
            self.assertNotIn(forbidden, found)
        self.assertEqual({r['case_id'] for r in result['assignment']}, {self.case_a})
        self.assertEqual(result['annotation'], [])
        self.assertEqual(result['link'], [])
        self.assertEqual(result['case_relation'], [])
        for kind in ('audit_log', 'tag', 'principal', 'source_observation'):
            self.assertEqual(gate.list(kind), [], kind)
        with self.assertRaises(authz.Denied) as denied:
            gate.get('case', self.case_b)
        self.assertTrue(denied.exception.exists)
        with self.assertRaises(authz.Denied) as missing:
            gate.get('case', 10_000)
        self.assertFalse(missing.exception.exists)
        self.assertEqual(str(denied.exception), str(missing.exception))

    def test_owner_holds_an_ordinary_revocable_grant(self):
        gate = authz.ContentGate(self.db, self.owner)
        self.assertTrue(gate.installation('administer'))
        self.assertEqual(len(gate.list('annotation')), 1)
        grant_id = self.db.execute('SELECT id FROM grants WHERE principal_id=?', (self.owner,)).fetchone()[0]
        authz.revoke_grant(self.db, grant_id, revoked_by='owner')
        self.assertFalse(authz.ContentGate(self.db, self.owner).installation('administer'))
        with self.assertRaises(ValueError):
            authz.bootstrap_owner(self.db, 'second owner')

    def test_capabilities_are_independent(self):
        gate = authz.ContentGate(self.db, self.recipient)
        for capability in ('read_bytes', 'export', 'invite', 'annotate'):
            self.assertFalse(gate.allows('artifact', self.shared, capability), capability)

    def test_unregistered_capability_is_kept_but_grants_nothing_until_registered(self):
        scope = authz.create_scope(self.db, 'case', {'case_id': self.case_b})
        result = authz.grant(self.db, self.recipient, scope, ['listen_aloud'])
        self.assertEqual(result['warnings'][0]['code'], 'unregistered_capability')
        self.assertFalse(authz.visible(self.db, self.recipient, 'case', self.case_b, 'listen_aloud'))
        self.db.execute("INSERT INTO capability_registry(name) VALUES('listen_aloud')")
        self.assertTrue(authz.visible(self.db, self.recipient, 'case', self.case_b, 'listen_aloud'))

    def test_time_window_and_revocation(self):
        self.assertTrue(authz.visible(self.db, self.recipient, 'case', self.case_a, 'read_metadata'))
        self.assertFalse(authz.visible(self.db, self.recipient, 'case', self.case_a, 'read_metadata',
                                       at='2000-01-01T00:00:00+00:00'))
        authz.revoke_grant(self.db, self.grant_a, revoked_by='owner')
        authz.revoke_grant(self.db, self.grant_a, revoked_by='owner')  # idempotent
        self.assertFalse(authz.visible(self.db, self.recipient, 'case', self.case_a, 'read_metadata'))
        actions = [r[0] for r in self.db.execute("SELECT action FROM audit_log WHERE object_kind='grant' ORDER BY id")]
        self.assertEqual(actions.count('grant_revoked'), 1)

    def test_grants_and_scopes_are_immutable_except_revocation(self):
        for statement in ("UPDATE grants SET capabilities_json='[\"administer\"]' WHERE id=?",
                          "UPDATE grants SET scope_id=1 WHERE id=?", 'DELETE FROM grants WHERE id=?'):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement, (self.grant_a,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE scopes SET spec_json='{}' WHERE id=?", (self.scope_a,))

    def test_inactive_principal_sees_nothing(self):
        self.db.execute("UPDATE principals SET status='suspended' WHERE id=?", (self.recipient,))
        self.assertFalse(authz.visible(self.db, self.recipient, 'case', self.case_a, 'read_metadata'))

    def test_unknown_scope_kind_covers_nothing(self):
        scope = authz.create_scope(self.db, 'everything_please', {'case_id': self.case_b})
        authz.grant(self.db, self.recipient, scope, ['read_metadata'])
        self.assertFalse(authz.visible(self.db, self.recipient, 'case', self.case_b, 'read_metadata'))

    def test_setting_changes_are_audited_with_previous_value(self):
        with self.assertRaises(authz.SettingWidensAccess) as refused:
            authz.set_setting(self.db, 'legacy_scope_policy', f'treat_as_case:{self.case_a}', actor='owner')
        gained = refused.exception.change['effective_access_diff']
        self.assertEqual([(d['kind'], d['capability']) for d in gained if d['principal_uid'] == self.recipient_uid()],
                         [('annotation', 'read_metadata'), ('annotation', 'read_text')])
        self.assertEqual(authz.ContentGate(self.db, self.recipient).list('annotation'), [])  # not applied
        change = authz.set_setting(self.db, 'legacy_scope_policy', f'treat_as_case:{self.case_a}', actor='owner',
                                   confirm_widening=True)
        self.assertEqual(change['from'], 'owner_only')
        self.assertTrue(change['widens'])
        self.assertEqual(len(authz.ContentGate(self.db, self.recipient).list('annotation')), 1)
        row = self.db.execute("SELECT details_json FROM audit_log WHERE action='authz_setting' ORDER BY id DESC").fetchone()
        self.assertEqual(json.loads(row[0])['from'], 'owner_only')
        self.assertTrue(json.loads(row[0])['effective_access_diff'])
        narrowing = authz.set_setting(self.db, 'legacy_scope_policy', 'owner_only', actor='owner')
        self.assertFalse(narrowing['widens'])
        preview = authz.set_setting(self.db, 'legacy_scope_policy', f'treat_as_case:{self.case_a}', preview=True)
        self.assertTrue(preview['widens'] and not preview['applied'])
        self.assertEqual(authz.get_setting(self.db, 'legacy_scope_policy'), 'owner_only')

    def recipient_uid(self):
        return self.db.execute('SELECT uid FROM principals WHERE id=?', (self.recipient,)).fetchone()[0]

    def test_assignment_with_own_visibility_scope(self):
        private = authz.create_scope(self.db, 'case', {'case_id': self.case_b})
        result = model.assign(self.db, case_id=self.case_a, target_kind='artifact', target_id=self.shared,
                              visibility_scope_id=private)
        self.assertFalse(authz.visible(self.db, self.recipient, 'assignment', result['id'], 'read_metadata'))
        authz.grant(self.db, self.recipient, private, ['read_metadata'])
        self.assertTrue(authz.visible(self.db, self.recipient, 'assignment', result['id'], 'read_metadata'))


if __name__ == '__main__':
    unittest.main()

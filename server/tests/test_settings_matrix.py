"""Package P9: every open setting in every value, including unhelpful and unknown ones (S12, S13).

Synthetic in-memory data. A widening change needs confirmation and records its diff.
"""
import json
import unittest

from server.app import advocate, authz, citations, sharing
from server.app import assignments as model
from server.tests.test_authz import memory_db


class Base(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        db = self.db
        self.case_a = db.execute("INSERT INTO cases(name) VALUES('sprawa A')").lastrowid
        self.case_b = db.execute("INSERT INTO cases(name) VALUES('sprawa B')").lastrowid
        db.execute("INSERT INTO sources(id,case_id,kind,label) VALUES(1,?,'synthetic','A')", (self.case_a,))
        self.artifact = db.execute("INSERT INTO artifacts(source_id,sha256,original_name,source_locator) VALUES(1,?,?,?)",
                                   ('a' * 64, 'nagranie.wav', 'synthetic://1')).lastrowid
        segments = [{'start': i, 'end': i + 1, 'text': text} for i, text in enumerate(
            ['wstęp rozmowy', 'umowa była ustna', 'czynsz płatny gotówką', 'świadek obecny', 'koniec'])]
        self.v1 = db.execute("INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES(?, 'transcript', ?, ?)",
                             (self.artifact, ' '.join(s['text'] for s in segments), json.dumps(segments))).lastrowid
        self.v2 = db.execute("INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES(?, 'transcript', 'v2', '[]')",
                             (self.artifact,)).lastrowid
        db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES('derived_text',?,'t',?)",
                   (self.v1, ' '.join(s['text'] for s in segments)))
        projection = citations.projection_from_segments(segments, [2])
        self.anchor = db.execute('''INSERT INTO evidence_anchors(artifact_id,derived_text_id,start_ms,end_ms,quote_text,
            quote_sha256,selector_json) VALUES(?,?,?,?,?,?,?)''', (self.artifact, self.v1, projection['start_ms'],
            projection['end_ms'], projection['quote_text'], projection['quote_sha256'], json.dumps(projection['selector']))).lastrowid
        self.fragment = model.create_fragment(db, artifact_id=self.artifact, kind='citation', selector={'segment': 2},
                                              derived_text_id=self.v1, anchor_id=self.anchor)['id']
        model.assign(db, case_id=self.case_b, target_kind='fragment', target_id=self.fragment, meaning_text='płatność')
        self.owner = authz.bootstrap_owner(db, 'owner')['principal_id']
        self.recipient = model.create_principal(db, 'odbiorca B')
        authz.grant(db, self.recipient, authz.create_scope(db, 'case', {'case_id': self.case_b}),
                    ['read_metadata', 'read_text', 'chat_with_advocate', 'export'])

    def sees(self, kind, object_id, principal=None):
        return authz.visible(self.db, principal or self.recipient, kind, object_id, 'read_text')

    def set(self, key, value):
        return authz.set_setting(self.db, key, value, actor='owner', confirm_widening=True)


class OpenPolicyTests(Base):
    def test_s12_every_open_policy_value(self):
        expected = {
            'fragment_only': (True, False, False, False, []),
            'context_window:1': (True, False, False, False, [1, 2, 3]),
            'context_window:0': (True, False, False, False, [2]),
            'context_window:nonsense': (True, False, False, False, [2]),
            'whole_artifact_version': (True, True, False, False, []),
            'whole_document': (True, True, True, True, []),
            'predicate:case_status=open': (True, False, False, False, []),
            'everything_please': (True, False, False, False, []),
        }
        for value, (fragment, v1, v2, artifact, context) in expected.items():
            with self.subTest(value=value):
                self.set('fragment.open_policy', value)
                self.assertEqual((self.sees('fragment', self.fragment), self.sees('derived_text', self.v1),
                                  self.sees('derived_text', self.v2), self.sees('artifact', self.artifact)),
                                 (fragment, v1, v2, artifact))
                result = authz.fragment_context(self.db, self.recipient, self.fragment)
                self.assertEqual(result['quote'], 'czynsz płatny gotówką')
                self.assertEqual([c['segment_index'] for c in result['context']], context)
                self.set('fragment.open_policy', 'fragment_only')

    def test_widening_open_policy_needs_confirmation_and_records_the_diff(self):
        with self.assertRaises(authz.SettingWidensAccess) as refused:
            authz.set_setting(self.db, 'fragment.open_policy', 'whole_document', actor='owner')
        kinds = {d['kind'] for d in refused.exception.change['effective_access_diff'] if d['gained']}
        self.assertTrue({'artifact', 'derived_text'} <= kinds)
        self.assertFalse(self.sees('artifact', self.artifact))
        with self.assertRaises(authz.SettingWidensAccess):
            authz.set_setting(self.db, 'fragment.open_policy', 'context_window:2', actor='owner')
        self.set('fragment.open_policy', 'whole_document')
        self.assertFalse(authz.set_setting(self.db, 'fragment.open_policy', 'fragment_only')['widens'])

    def test_advocate_reaches_a_fragment_shared_on_its_own(self):
        answer = self.ask('czynsz gotówką')
        entries = [e for e in answer['basis'] if e['kind'] == 'fragment_quote']
        self.assertEqual([e['text'] for e in entries], ['czynsz płatny gotówką'])
        self.assertNotIn('context', entries[0])
        self.assertNotIn('świadek', json.dumps(answer, ensure_ascii=False))
        self.set('fragment.open_policy', 'context_window:1')
        widened = [e for e in self.ask('czynsz gotówką')['basis'] if e['kind'] == 'fragment_quote'][0]
        self.assertIn('świadek obecny', widened['context'])
        self.assertNotIn('wstęp', widened['context'])

    def ask(self, question):
        conversation = advocate.start_conversation(self.db, self.recipient)['uid']
        return advocate.ask(self.db, self.recipient, conversation, question)


class DisclosureTests(Base):
    def test_count_disclosure_every_value(self):
        for value, check in (('none', lambda s: 'omitted_counts' not in s),
                             ('bucketed', lambda s: s['omitted_counts']['artifacts'] == '1-9'),
                             ('exact', lambda s: s['omitted_counts']['artifacts'] == 1),
                             ('roughly', lambda s: 'omitted_counts' not in s)):
            with self.subTest(value=value):
                self.set('count_disclosure', value)
                scope = sharing.export_scope_package(self.db, self.recipient)['scope']
                self.assertTrue(check(scope), scope)
                self.assertEqual(scope['count_disclosure'], value)

    def test_disclosure_settings_widen_only_with_confirmation(self):
        authz.set_setting(self.db, 'auth.mode', 'tokens')
        for key, wider in (('count_disclosure', 'exact'), ('denial.reveal_existence', 'always'),
                           ('shared_source.cross_case_hint', 'own_cases_only'), ('auth.mode', 'legacy_open'),
                           ('advocate.log_prompt_content', True), ('count_disclosure', 'made_up_value')):
            with self.subTest(key=key, value=wider), self.assertRaises(authz.SettingWidensAccess):
                authz.set_setting(self.db, key, wider)

    def test_cross_case_hint_names_only_visible_cases(self):
        self.set('fragment.open_policy', 'whole_document')
        model.assign(self.db, case_id=self.case_a, target_kind='artifact', target_id=self.artifact, meaning_text='A')
        model.assign(self.db, case_id=self.case_b, target_kind='artifact', target_id=self.artifact, meaning_text='B')
        both = model.create_principal(self.db, 'A i B')
        for case in (self.case_a, self.case_b):
            authz.grant(self.db, both, authz.create_scope(self.db, 'case', {'case_id': case}),
                        ['read_metadata', 'read_text', 'chat_with_advocate'])
        def hints(principal):
            conversation = advocate.start_conversation(self.db, principal)['uid']
            basis = advocate.ask(self.db, principal, conversation, 'umowa ustna')['basis']
            return [e.get('also_in_cases') for e in basis if 'artifact' in e]
        self.assertEqual(hints(both), [None])
        self.set('shared_source.cross_case_hint', 'own_cases_only')
        self.assertEqual(hints(both), [['sprawa A', 'sprawa B']])
        self.assertEqual(hints(self.recipient), [None])  # sees only B: no hint about A
        self.set('shared_source.cross_case_hint', 'sometimes')
        self.assertEqual(hints(both), [None])


class GrowthTests(Base):
    def package(self, policy):
        package = sharing.create_package(self.db, f'growth {policy}')
        sharing.add_item(self.db, package['id'], 'case', self.case_a)
        sharing.snapshot_package(self.db, package['id'])
        reader = model.create_principal(self.db, f'reader {policy}')
        authz.grant(self.db, reader, package['scope_id'], ['read_text'])
        sharing.set_growth_policy(self.db, package['id'], policy)
        return package, reader

    def test_every_growth_policy(self):
        for policy, sees_new, action in (('frozen', False, 'none'), ('notify_only', False, 'notify'),
                                         ('propose_to_owner', False, 'approve_with_snapshot'),
                                         ('auto_include_by_rule:derived_text', True, 'none'),
                                         ('auto_include_by_rule:no_such_kind', False, 'none'),
                                         ('whenever', False, 'none')):
            with self.subTest(policy=policy):
                package, reader = self.package(policy)
                newer = self.db.execute("INSERT INTO derived_text(artifact_id,kind,text) VALUES(?, 'transcript', 'v3')",
                                        (self.artifact,)).lastrowid
                self.assertEqual(self.sees('derived_text', newer, reader), sees_new)
                growth = sharing.package_growth(self.db, package['id'])
                self.assertIn(['derived_text', newer], growth['pending'])
                self.assertEqual(growth['action'], action)
                sharing.snapshot_package(self.db, package['id'])  # the owner's approval
                self.assertTrue(self.sees('derived_text', newer, reader))


class DelegationTests(Base):
    def test_invite_within_depth_subset_and_parent_lifetime(self):
        scope = authz.create_scope(self.db, 'case', {'case_id': self.case_a})
        lead = model.create_principal(self.db, 'adwokat prowadzący')
        parent = authz.grant(self.db, lead, scope, ['read_text', 'invite'], valid_to='2999-01-01T00:00:00+00:00')
        aide = model.create_principal(self.db, 'aplikant')
        third = model.create_principal(self.db, 'osoba trzecia')
        with self.assertRaises(authz.Denied):
            authz.delegate(self.db, lead, scope, aide, ['read_bytes'])  # not held
        child = authz.delegate(self.db, lead, scope, aide, ['read_text', 'invite'], valid_to='3999-01-01T00:00:00+00:00')
        row = self.db.execute('SELECT valid_to,metadata_json FROM grants WHERE id=?', (child['id'],)).fetchone()
        self.assertEqual(row['valid_to'], '2999-01-01T00:00:00+00:00')  # never outlives the parent
        self.assertTrue(self.sees('case', self.case_a, aide))
        with self.assertRaises(authz.Denied):
            authz.delegate(self.db, aide, scope, third, ['read_text'])  # preset depth 1
        authz.revoke_grant(self.db, parent['id'], revoked_by='owner')
        self.assertFalse(self.sees('case', self.case_a, aide))

    def test_explicit_unlimited_depth(self):
        scope = authz.create_scope(self.db, 'case', {'case_id': self.case_a})
        people = [model.create_principal(self.db, f'p{i}') for i in range(4)]
        authz.grant(self.db, people[0], scope, ['read_text', 'invite'], conditions={'max_delegation_depth': None})
        for granter, grantee in zip(people, people[1:]):
            authz.delegate(self.db, granter, scope, grantee, ['read_text', 'invite'])
        self.assertTrue(self.sees('case', self.case_a, people[-1]))

    def test_no_invite_no_delegation(self):
        scope = authz.create_scope(self.db, 'case', {'case_id': self.case_a})
        holder = model.create_principal(self.db, 'holder')
        authz.grant(self.db, holder, scope, ['read_text'])
        with self.assertRaises(authz.Denied):
            authz.delegate(self.db, holder, scope, self.recipient, ['read_text'])


if __name__ == '__main__':
    unittest.main()

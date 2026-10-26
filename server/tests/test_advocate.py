"""Package P6: the advocate core without a model (retrieval_only).

Synthetic Polish-like transcripts; canaries mark material that must not reach
a recipient. Quoted passages are stored derived text, not verified truth.
"""
from dataclasses import replace
import json
from pathlib import Path
import re
import socket
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import advocate, auth, authz, db, evidence, main
from server.app import assignments as model

HIDDEN = 'CANARY-adwokat-ukryte'


class AdvocateFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'store').mkdir()
        self.settings = replace(db.settings, data_dir=self.root, db_path=self.root / 'db.sqlite',
                                store_dir=self.root / 'store', scan_roots=())
        self.patches = [patch.object(m, 'settings', self.settings) for m in (db, evidence, main)]
        for item in self.patches:
            item.start()
        db.init_db()
        with db.session() as conn:
            self.case_a = conn.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()[0]
            self.case_b = conn.execute('INSERT INTO cases(name) VALUES(?)', (HIDDEN,)).lastrowid
        self.shared = self.material('rozmowa.wav', self.case_a, [
            'Umowa najmu została podpisana w marcu.', 'Termin płatności minął w kwietniu.'])
        self.private_b = self.material('ukryte.wav', self.case_b, [f'Umowa {HIDDEN} poufna.'])
        with db.session() as conn:
            model.assign(conn, case_id=self.case_a, target_kind='artifact', target_id=self.shared,
                         meaning_text='potwierdza datę podpisania umowy', author_kind='human')
            model.assign(conn, case_id=self.case_b, target_kind='artifact', target_id=self.shared,
                         meaning_text=f'{HIDDEN} znaczenie w sprawie B')
            self.owner = authz.bootstrap_owner(conn, 'owner')['principal_id']
            self.recipient = model.create_principal(conn, 'adwokat', kind='human')
            self.scope_a = authz.create_scope(conn, 'case', {'case_id': self.case_a})
            self.grant_a = authz.grant(conn, self.recipient, self.scope_a,
                                       ['read_metadata', 'read_text', 'chat_with_advocate'])['id']

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def material(self, name, case_id, sentences):
        path = self.root / name
        path.write_bytes(name.encode() + b' synthetic')
        source = evidence.ensure_source('synthetic', f'source-{case_id}', case_id=case_id)
        artifact = evidence.ingest_file(path, source_id=source, source_locator=f'synthetic://{name}', mime_type='audio/wav')
        segments = [{'start': i * 2.0, 'end': i * 2.0 + 1.5, 'text': text} for i, text in enumerate(sentences)]
        evidence.add_derived_text(artifact, 'transcript', ' '.join(sentences), model='synthetic', segments=segments)
        return artifact

    def ask(self, principal, question, conversation=None):
        with db.session() as conn:
            conversation = conversation or advocate.start_conversation(conn, principal)['uid']
            return conversation, advocate.ask(conn, principal, conversation, question)


class AdvocateCoreTests(AdvocateFixture):
    def test_s3_recipient_checks_the_basis(self):
        conversation, answer = self.ask(self.recipient, 'Kiedy podpisano umowę najmu?')
        self.assertEqual(answer['mode'], 'retrieval_only')
        self.assertTrue(answer['basis'])
        refs = {entry['ref'] for entry in answer['basis']}
        for claim in answer['claims']:
            self.assertTrue(set(claim['citations']) <= refs)
        segment = next(e for e in answer['basis'] if e['kind'] == 'transcript_segment')
        self.assertEqual(segment['artifact']['id'], self.shared)
        with db.session() as conn:
            gate = authz.ContentGate(conn, self.recipient)
            text = gate.get('derived_text', segment['derived_text_id'], 'read_text')
            stored = json.loads(text['segments_json'])[segment['segment_index']]['text']
            self.assertEqual(advocate._sha(stored.strip()), segment['text_sha256'])
            basis = advocate.answer_basis(conn, self.recipient, answer['answer_uid'])
        self.assertEqual([e['ref'] for e in basis['entries']], [e['ref'] for e in answer['basis']])

    def test_s4_shared_material_does_not_open_case_b(self):
        _, answer = self.ask(self.recipient, f'umowa poufna {HIDDEN} znaczenie')
        self.assertNotIn(HIDDEN, json.dumps(answer, ensure_ascii=False))
        meanings = [e['text'] for e in answer['basis'] if e['kind'] == 'assignment']
        self.assertEqual(meanings, ['potwierdza datę podpisania umowy'])
        with db.session() as conn:
            other = model.create_principal(conn, 'odbiorca B')
            authz.grant(conn, other, authz.create_scope(conn, 'case', {'case_id': self.case_b}),
                        ['read_text', 'chat_with_advocate'])
        _, answer_b = self.ask(other, 'umowa najmu')
        texts = json.dumps(answer_b, ensure_ascii=False)
        self.assertIn('Umowa najmu', texts)  # the shared material is reachable from B
        self.assertNotIn('potwierdza datę podpisania', texts)  # A's interpretation is not

    def test_s6_new_version_keeps_old_basis_and_answers_are_append_only(self):
        conversation, first = self.ask(self.recipient, 'umowa najmu')
        old_version = first['basis'][0]['derived_text_id']
        evidence.add_derived_text(self.shared, 'transcript', 'Umowa najmu poprawiona wersja.', model='synthetic',
                                  segments=[{'start': 0, 'end': 1, 'text': 'Umowa najmu poprawiona wersja.'}])
        _, second = self.ask(self.recipient, 'umowa najmu', conversation)
        self.assertIn(old_version, {e['derived_text_id'] for e in first['basis'] if 'derived_text_id' in e})
        self.assertGreater(len({e.get('derived_text_id') for e in second['basis']}), 1)
        with db.session() as conn:
            stored = conn.execute('SELECT basis_json FROM advocate_answers WHERE uid=?', (first['answer_uid'],)).fetchone()[0]
            self.assertEqual(json.loads(stored), first['basis'])
            with self.assertRaises(Exception):
                conn.execute("UPDATE advocate_answers SET claims_json='[]'")
        history = self.history(self.recipient, conversation)
        self.assertEqual(len(history), 2)

    def history(self, principal, conversation):
        with db.session() as conn:
            return advocate.conversation_history(conn, principal, conversation)

    def test_s7_revocation_withholds_old_answers_and_basis(self):
        conversation, answer = self.ask(self.recipient, 'umowa najmu')
        with db.session() as conn:
            authz.revoke_grant(conn, self.grant_a, revoked_by='owner')
        with db.session() as conn, self.assertRaises(authz.Denied):
            advocate.ask(conn, self.recipient, conversation, 'umowa')
        # History stays the recipient's own, but content is withheld.
        with db.session() as conn:
            authz.grant(conn, self.recipient, authz.create_scope(conn, 'case', {'case_id': self.case_b}), ['chat_with_advocate'])
            turns = advocate.conversation_history(conn, self.recipient, conversation)
            basis = advocate.answer_basis(conn, self.recipient, answer['answer_uid'])
        self.assertEqual(turns[0]['answers'][0], {'answer_uid': answer['answer_uid'], 'unavailable': True,
                                                  'reason': 'material_no_longer_available'})
        self.assertTrue(all(entry.get('unavailable') for entry in basis['entries']))
        self.assertNotIn('Umowa', json.dumps([turns, basis], ensure_ascii=False))

    def test_s8_derived_state_needs_every_input(self):
        with db.session() as conn:
            owner_gate = advocate.AdvocateGate(conn, self.owner)
            inputs = [('artifact', self.shared), ('artifact', self.private_b)]
            summary = advocate.record_derived_state(conn, self.owner, 'summary', {'text': 'streszczenie'}, inputs)
            self.assertTrue(owner_gate.allows('derived_state', summary['id'], 'read_text'))
            self.assertFalse(authz.visible(conn, self.recipient, 'derived_state', summary['id'], 'read_text'))
            authz.grant(conn, self.recipient, authz.create_scope(conn, 'case', {'case_id': self.case_b}), ['read_text'])
            self.assertTrue(authz.visible(conn, self.recipient, 'derived_state', summary['id'], 'read_text'))
            with self.assertRaises(authz.Denied):
                advocate.record_derived_state(conn, model.create_principal(conn, 'x'), 'summary', {}, inputs)

    def test_s15_retrieval_only_makes_no_network_call(self):
        def refuse(*args, **kwargs):
            raise AssertionError('network call attempted')
        with patch.object(socket.socket, 'connect', refuse), patch.object(socket, 'create_connection', refuse):
            _, answer = self.ask(self.recipient, 'termin płatności')
        self.assertEqual(answer['mode'], 'retrieval_only')
        self.assertTrue(any('Termin' in c['text'] for c in answer['claims']))

    def test_chat_capability_and_intersection(self):
        with db.session() as conn:
            reader = model.create_principal(conn, 'reader only')
            authz.grant(conn, reader, authz.create_scope(conn, 'case', {'case_id': self.case_a}), ['read_text'])
            with self.assertRaises(authz.Denied):
                advocate.start_conversation(conn, reader)
            authz.grant(conn, reader, authz.create_scope(conn, 'case', {'case_id': self.case_b}), ['chat_with_advocate'])
        _, answer = self.ask(reader, 'umowa najmu')
        # read_text on A, chat on B: the shared material (in both) is admitted, A-only interpretation is not.
        self.assertTrue(answer['basis'])
        self.assertEqual([e for e in answer['basis'] if e['kind'] == 'assignment'], [])
        self.assertNotIn('potwierdza', json.dumps(answer, ensure_ascii=False))

    def test_owner_visibility_is_fixed_per_conversation(self):
        old, _ = self.ask(self.recipient, 'umowa')
        with db.session() as conn, self.assertRaises(authz.Denied):
            advocate.conversation_history(conn, self.owner, old)
        with db.session() as conn:
            with self.assertRaises(authz.SettingWidensAccess):
                authz.set_setting(conn, 'conversation.owner_visibility', 'full', actor='owner')
            authz.set_setting(conn, 'conversation.owner_visibility', 'full', actor='owner', confirm_widening=True)
            # The earlier conversation keeps the mode its recipient saw: still private.
            with self.assertRaises(authz.Denied):
                advocate.conversation_history(conn, self.owner, old)
        new, _ = self.ask(self.recipient, 'umowa')
        with db.session() as conn:
            self.assertEqual(len(advocate.conversation_history(conn, self.owner, new)), 1)

    def test_metadata_only_and_opt_in_visibility(self):
        with db.session() as conn:
            authz.set_setting(conn, 'conversation.owner_visibility', 'metadata_only', confirm_widening=True)
        meta, _ = self.ask(self.recipient, 'umowa')
        with db.session() as conn:
            row = conn.execute('SELECT id FROM conversations WHERE uid=?', (meta,)).fetchone()
            self.assertTrue(authz.visible(conn, self.owner, 'conversation', row['id'], 'read_metadata'))
            turn = conn.execute('SELECT id FROM advocate_turns WHERE conversation_id=?', (row['id'],)).fetchone()
            self.assertFalse(authz.visible(conn, self.owner, 'advocate_turn', turn['id'], 'read_text'))
            authz.set_setting(conn, 'conversation.owner_visibility', 'opt_in_by_recipient', confirm_widening=True)
        opt, _ = self.ask(self.recipient, 'umowa')
        with db.session() as conn:
            with self.assertRaises(authz.Denied):
                advocate.conversation_history(conn, self.owner, opt)
            advocate.share_with_owner(conn, self.recipient, opt, True)
            self.assertEqual(len(advocate.conversation_history(conn, self.owner, opt)), 1)
            advocate.share_with_owner(conn, self.recipient, opt, False)
            with self.assertRaises(authz.Denied):
                advocate.conversation_history(conn, self.owner, opt)
            with self.assertRaises(authz.Denied):
                advocate.share_with_owner(conn, self.owner, opt, True)  # only the recipient decides
        with db.session() as conn:
            authz.set_setting(conn, 'conversation.owner_visibility', 'custom:anything', confirm_widening=True)
        custom, _ = self.ask(self.recipient, 'umowa')
        with db.session() as conn, self.assertRaises(authz.Denied):
            advocate.conversation_history(conn, self.owner, custom)

    def test_verifier_removes_foreign_citations_and_never_upgrades(self):
        basis = [{'ref': 'p1', 'object': ['artifact', self.shared], 'text': 'Umowa najmu została podpisana w marcu.'}]
        claims = [{'text': 'A [cit:p1] [cit:p9]', 'status': 'proven', 'citations': ['p2']},
                  {'text': 'B', 'status': 'proven', 'citations': []},
                  {'text': 'C', 'status': 'pewne_na_100', 'citations': ['p1']}, 'garbage',
                  {'text': 'Podpisano: „umowa najmu została podpisana w marcu”.', 'status': 'proven', 'citations': ['p1']},
                  {'text': 'Przyznał: „oskarżony przyznał się do winy”.', 'status': 'proven', 'citations': ['p1']}]
        with db.session() as conn:
            checked, report = advocate.verify(claims, basis, advocate.AdvocateGate(conn, self.recipient))
        self.assertEqual(checked[0], {'text': 'A', 'status': 'claim', 'citations': ['p1']})  # no verbatim quote
        self.assertEqual(checked[1]['status'], 'claim_without_basis')
        self.assertEqual(checked[2]['status'], 'pewne_na_100')
        self.assertEqual(checked[3]['status'], 'proven')  # quoted span occurs in p1 (case and diacritics folded)
        self.assertEqual(checked[4]['status'], 'claim')  # quoted span is not in the cited passage
        self.assertEqual({r['ref'] for r in report['removed_citations']}, {'p2', 'p9'})
        self.assertEqual({w['code'] for w in report['warnings']},
                         {'status_not_in_presets', 'malformed_claim', 'proven_downgraded'})

    def test_proven_policy_values(self):
        basis = [{'ref': 'p1', 'object': ['artifact', self.shared], 'text': 'Termin płatności minął w kwietniu.'}]
        quoted = {'text': 'Wynika: „termin płatności minął w kwietniu”', 'status': 'proven', 'citations': ['p1']}
        bare = {'text': 'Termin minął.', 'status': 'proven', 'citations': ['p1']}
        with db.session() as conn:
            gate = advocate.AdvocateGate(conn, self.recipient)
            results = {policy: [c['status'] for c in advocate.verify([quoted, bare], basis, gate, proven_policy=policy)[0]]
                       for policy in ('require_verbatim_quote', 'downgrade_all', 'accept_model', 'whatever')}
            system = advocate.verify([bare], basis, gate, from_model=False, proven_policy='downgrade_all')[0]
        self.assertEqual(results, {'require_verbatim_quote': ['proven', 'claim'], 'downgrade_all': ['claim', 'claim'],
                                   'accept_model': ['proven', 'proven'], 'whatever': ['claim', 'claim']})
        self.assertEqual(system[0]['status'], 'proven')  # only model output is checked
        with db.session() as conn, self.assertRaises(authz.SettingWidensAccess):
            authz.set_setting(conn, 'advocate.proven_policy', 'accept_model')


class StaticTests(unittest.TestCase):
    def test_advocate_reads_evidence_only_through_the_gate(self):
        source = Path(advocate.__file__).read_text()
        tables = 'cases|sources|artifacts|source_observations|events|derived_text|evidence_anchors|fragments|' \
                 'assignments|annotations|links|tags|audit_log|search_fts|processing_runs|jobs'
        self.assertEqual(re.findall(rf'(?i)\b(?:FROM|JOIN|INTO|UPDATE)\s+({tables})\b', source), [])


class AdvocateHttpTests(AdvocateFixture):
    def test_end_to_end_over_http_in_tokens_mode(self):
        with db.session() as conn:
            token = auth.issue_token(conn, self.recipient)
            authz.set_setting(conn, 'auth.mode', 'tokens')
        headers = {'X-AGEDS-Token': token}
        with TestClient(main.app) as client:
            conversation = client.post('/api/advocate/conversations', json={'title': 'sprawa A'}, headers=headers).json()
            answer = client.post(f"/api/advocate/conversations/{conversation['uid']}/turns",
                                 json={'question': 'Kiedy minął termin płatności?'}, headers=headers)
            self.assertEqual(answer.status_code, 200, answer.text)
            answer = answer.json()
            basis = client.get(f"/api/advocate/answers/{answer['answer_uid']}/basis", headers=headers).json()
            history = client.get(f"/api/advocate/conversations/{conversation['uid']}", headers=headers).json()
            self.assertEqual(len(history['turns']), 1)
            for entry in basis['entries']:
                if entry.get('kind') == 'transcript_segment':
                    transcript = client.get(f"/api/artifacts/{entry['artifact']['id']}/transcript",
                                            params={'derived_text_id': entry['derived_text_id']}, headers=headers)
                    self.assertEqual(transcript.status_code, 200)
                    self.assertIn(entry['text'], transcript.json()['text'])
            stranger = client.get(f"/api/advocate/conversations/{conversation['uid']}",
                                  headers={'X-AGEDS-Token': self.owner_token_for_test()})
            self.assertEqual(stranger.status_code, 404)
            missing = client.get('/api/advocate/conversations/nope', headers=headers)
            self.assertEqual((missing.status_code, missing.content), (stranger.status_code, stranger.content))
            self.assertNotIn(HIDDEN, json.dumps([answer, basis, history], ensure_ascii=False))

    def owner_token_for_test(self):
        with db.session() as conn:
            return auth.issue_token(conn, self.owner)


if __name__ == '__main__':
    unittest.main()

"""Regression tests from the independent authorization review of 7152be2 (2026-10-26).

Each class started as a reproduction that passed while the leak existed
(AGEDS_AUTHZ_REVIEW.md, coordinator session); assertions now state the fixed
behaviour. Synthetic data only.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import assignments as model
from server.app import advocate, auth, authz, db, evidence, main, providers, sharing

CANARY = 'CANARY-HIDDEN'


class Recorder:
    def __init__(self):
        self.requests = []

    def __call__(self, config, body):
        self.requests.append(json.dumps(body, ensure_ascii=False))
        reply = {'claims': [{'text': 'odpowiedz ' + body['contents'][-1]['parts'][0]['text'][-200:], 'status': 'claim', 'citations': ['p1']}]}
        return {'candidates': [{'content': {'parts': [{'text': json.dumps(reply, ensure_ascii=False)}]}}],
                'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 10}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'store').mkdir()
        self.settings = replace(db.settings, data_dir=self.root, db_path=self.root / 'db.sqlite',
                                store_dir=self.root / 'store', scan_roots=())
        self.patches = [patch.object(m, 'settings', self.settings) for m in (db, evidence, main)]
        for p in self.patches:
            p.start()
        self.context = TestClient(main.app)
        self.client = self.context.__enter__()
        with db.session() as conn:
            self.case_a = conn.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()[0]
            self.case_b = conn.execute("INSERT INTO cases(name) VALUES('case B')").lastrowid
        self.art_a = self.ingest('a.wav', b'RIFF a', self.case_a)
        self.art_b = self.ingest('b.wav', b'RIFF b', self.case_b)
        evidence.add_derived_text(self.art_a, 'transcript', 'umowa najmu jawna',
                                  segments=[{'start': 0.0, 'end': 1.0, 'text': 'umowa najmu jawna'}])
        evidence.add_derived_text(self.art_b, 'transcript', f'umowa najmu {CANARY}',
                                  segments=[{'start': 0.0, 'end': 1.0, 'text': f'umowa najmu {CANARY}'}])
        with db.session() as conn:
            self.owner = authz.bootstrap_owner(conn, 'owner')['principal_id']
            self.owner_token = auth.issue_token(conn, self.owner)
            self.rec = model.create_principal(conn, 'recipient')
            self.rec_token = auth.issue_token(conn, self.rec)
            authz.set_setting(conn, 'auth.mode', 'tokens')

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def ingest(self, name, content, case_id):
        path = self.root / name
        path.write_bytes(content)
        source = evidence.ensure_source('synthetic', f'source-{case_id}', case_id=case_id)
        return evidence.ingest_file(path, source_id=source, source_locator=f'synthetic://{name}', mime_type='audio/wav')

    def grant_case(self, principal, case_id, caps):
        with db.session() as conn:
            scope = authz.create_scope(conn, 'case', {'case_id': case_id})
            authz.grant(conn, principal, scope, caps)

    def h(self, token):
        return {'X-AGEDS-Token': token}


class R1FrozenSnapshotLater(Base):
    def test_later_transcript_of_frozen_artifact_is_served(self):
        with db.session() as conn:
            pkg = sharing.create_package(conn, 'p', mode='snapshot')
            sharing.add_item(conn, pkg['id'], 'artifact', self.art_a)
            sharing.snapshot_package(conn, pkg['id'])
            authz.grant(conn, self.rec, pkg['scope_id'], ['read_metadata', 'read_text'])
        later = evidence.add_derived_text(self.art_a, 'transcript', f'later {CANARY}',
                                          segments=[{'start': 0.0, 'end': 1.0, 'text': f'later {CANARY}'}])
        with db.session() as conn:
            gate_says = authz.ContentGate(conn, self.rec).allows('derived_text', later, 'read_text')
        r = self.client.get(f'/api/artifacts/{self.art_a}/transcript', headers=self.h(self.rec_token))
        lst = self.client.get('/api/artifacts', headers=self.h(self.rec_token)).text
        self.assertFalse(gate_says)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(CANARY, r.text)  # the latest *visible* version
        self.assertNotIn(CANARY, lst)
        headers = self.h(self.rec_token)
        explicit = self.client.get(f'/api/artifacts/{self.art_a}/transcript', params={'derived_text_id': later}, headers=headers)
        self.assertEqual(explicit.status_code, 404)
        for path in ('transcripts', 'transcripts/page', 'citations', 'citations/page'):
            with self.subTest(path=path):
                response = self.client.get(f'/api/artifacts/{self.art_a}/{path}', headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(CANARY, response.text)
                self.assertNotIn(f'"id":{later}', response.text.replace(' ', ''))
        page = self.client.get(f'/api/artifacts/{self.art_a}/transcripts/page', headers=headers).json()
        self.assertFalse(page['hasMore'])
        self.assertLess(page['snapshotMaxId'], later)  # hidden rows do not set the snapshot bound
        cite = self.client.post(f'/api/artifacts/{self.art_a}/citations', headers=headers,
                                json={'derivedTextId': later, 'segmentIndices': [0]})
        self.assertIn(cite.status_code, (403, 404))
        self.assertNotIn(CANARY, cite.text)


class R2MetadataOnlyLeaksQuestions(Base):
    def test_owner_reads_questions_under_metadata_only(self):
        self.grant_case(self.rec, self.case_a, ['read_metadata', 'read_text', 'chat_with_advocate'])
        with db.session() as conn:
            authz.set_setting(conn, 'conversation.owner_visibility', 'metadata_only', confirm_widening=True)
        conv = self.client.post('/api/advocate/conversations', json={}, headers=self.h(self.rec_token)).json()['uid']
        self.client.post(f'/api/advocate/conversations/{conv}/turns', json={'question': f'najmu {CANARY}-question'},
                         headers=self.h(self.rec_token))
        listed = self.client.get('/api/advocate/conversations?others=true', headers=self.h(self.owner_token)).json()
        r = self.client.get(f'/api/advocate/conversations/{conv}', headers=self.h(self.owner_token))
        print('\nR2 owner lists:', [c['uid'] == conv for c in listed], '| history status', r.status_code,
              '| question visible to owner:', f'{CANARY}-question' in r.text)
        self.assertNotIn(f'{CANARY}-question', r.text)


class R2EveryOwnerVisibilityMode(Base):
    def test_question_canary_follows_the_mode_fixed_at_start(self):
        self.grant_case(self.rec, self.case_a, ['read_metadata', 'read_text', 'chat_with_advocate'])
        expected = {'none': 404, 'metadata_only': 'withheld', 'full': 'shown', 'opt_in_by_recipient': 404, 'custom:x': 404}
        for mode, outcome in expected.items():
            with self.subTest(mode=mode):
                with db.session() as conn:
                    authz.set_setting(conn, 'conversation.owner_visibility', mode, confirm_widening=True)
                conv = self.client.post('/api/advocate/conversations', json={}, headers=self.h(self.rec_token)).json()['uid']
                self.client.post(f'/api/advocate/conversations/{conv}/turns', json={'question': f'najmu {CANARY}-{mode}'},
                                 headers=self.h(self.rec_token))
                r = self.client.get(f'/api/advocate/conversations/{conv}', headers=self.h(self.owner_token))
                if outcome == 404:
                    self.assertEqual(r.status_code, 404)
                elif outcome == 'withheld':
                    turn = r.json()['turns'][0]
                    self.assertEqual((turn['question'], turn['content_withheld'], turn['answer_count']), (None, True, 1))
                    self.assertNotIn(CANARY, r.text)
                else:
                    self.assertIn(f'{CANARY}-{mode}', r.text)
                own = self.client.get(f'/api/advocate/conversations/{conv}', headers=self.h(self.rec_token))
                self.assertIn(f'{CANARY}-{mode}', own.text)  # the recipient always reads their own


class R3ExportWithoutReadText(Base):
    def test_export_capability_alone_returns_text(self):
        self.grant_case(self.rec, self.case_b, ['export'])
        r = self.client.get('/api/export/scoped', headers=self.h(self.rec_token))
        t = self.client.get(f'/api/artifacts/{self.art_b}/transcript', headers=self.h(self.rec_token))
        print('\nR3 export status', r.status_code, '| canary in export:', CANARY in r.text,
              '| transcript route (read_text) status:', t.status_code)
        self.assertNotIn(CANARY, r.text)


class R4RejectedAssignmentOpens(Base):
    def test_rejected_or_ai_proposed_assignment_opens_material(self):
        self.grant_case(self.rec, self.case_a, ['read_metadata', 'read_text'])
        with db.session() as conn:
            a = model.assign(conn, case_id=self.case_a, target_kind='artifact', target_id=self.art_b,
                             status='ai_proposed', author_kind='ai')['id']
            model.set_assignment_status(conn, a, 'rejected')
        r = self.client.get(f'/api/artifacts/{self.art_b}/transcript', headers=self.h(self.rec_token))
        self.assertEqual(r.status_code, 404)
        self.assertNotIn(CANARY, r.text)
        with db.session() as conn:
            authz.set_setting(conn, 'assignment.opening_statuses', ['unknown', 'human_reviewed', 'rejected'],
                              confirm_widening=True)  # an explicit owner decision would open it
        self.assertIn(CANARY, self.client.get(f'/api/artifacts/{self.art_b}/transcript', headers=self.h(self.rec_token)).text)

    def test_ai_proposal_opens_only_after_a_human_accepts_it(self):
        self.grant_case(self.rec, self.case_a, ['read_metadata', 'read_text'])
        with db.session() as conn:
            a = model.assign(conn, case_id=self.case_a, target_kind='artifact', target_id=self.art_b,
                             status='ai_proposed', author_kind='ai')['id']
        url = f'/api/artifacts/{self.art_b}/transcript'
        self.assertEqual(self.client.get(url, headers=self.h(self.rec_token)).status_code, 404)
        with db.session() as conn:
            model.set_assignment_status(conn, a, 'human_reviewed')
        self.assertEqual(self.client.get(url, headers=self.h(self.rec_token)).status_code, 200)


class R5ConsentRevocationHistory(Base):
    def test_history_resent_after_consent_revoked(self):
        self.grant_case(self.rec, self.case_a, ['read_metadata', 'read_text', 'chat_with_advocate'])
        self.grant_case(self.rec, self.case_b, ['read_metadata', 'read_text', 'chat_with_advocate'])
        with db.session() as conn:
            authz.set_setting(conn, 'advocate.provider', {'type': 'vertex_gemini', 'project': 'synthetic'})
            consents = {}
            for case in (self.case_a, self.case_b):
                scope = authz.create_scope(conn, 'case', {'case_id': case})
                consents[case] = providers.record_consent(conn, 'vertex', scope, granted_by='owner', basis_text='t')
        rec = Recorder()
        with db.session() as conn:
            conv = advocate.start_conversation(conn, self.rec)['uid']
            p = providers.configured_provider(conn, self.rec); p.transport = rec
            first = advocate.ask(conn, self.rec, conv, 'hidden', provider=p)  # question has no canary; matches only case B text
            assert CANARY not in rec.requests[0].split('Pytanie:')[-1]
            providers.revoke_consent(conn, consents[self.case_b], actor='owner')
            p = providers.configured_provider(conn, self.rec); p.transport = rec
            ans = advocate.ask(conn, self.rec, conv, 'jawna', provider=p)  # matches only case A text
        print('\nR5 requests:', len(rec.requests), '| second mode:', ans['mode'],
              '| case-B canary in 2nd prompt after consent revoked:', CANARY in rec.requests[-1])
        self.assertNotIn(CANARY, rec.requests[-1])
        with db.session() as conn:
            refs = json.loads(conn.execute('SELECT input_refs_json FROM provider_calls ORDER BY id DESC LIMIT 1').fetchone()[0])
            first_refs = json.loads(conn.execute('SELECT input_refs_json FROM provider_calls ORDER BY id LIMIT 1').fetchone()[0])
        self.assertTrue(all(ref not in refs for ref in first_refs if ref[0] == 'derived_text'))


class R6SummaryTopicCrossPrincipal(Base):
    def test_summary_topic_visible_to_other_principals(self):
        with db.session() as conn:
            rec2 = model.create_principal(conn, 'recipient2')
            tok2 = auth.issue_token(conn, rec2)
            authz.set_setting(conn, 'advocate.provider', {'type': 'vertex_gemini', 'project': 'synthetic'})
            scope = authz.create_scope(conn, 'installation', {})
            providers.record_consent(conn, 'vertex', scope, granted_by='owner', basis_text='t')
        for p in (self.rec, rec2):
            self.grant_case(p, self.case_a, ['read_metadata', 'read_text', 'chat_with_advocate'])
        with db.session() as conn:
            prov = providers.configured_provider(conn, self.rec); prov.transport = Recorder()
            advocate.summarize(conn, self.rec, f'jawna {CANARY}-private-strategy', provider=prov)
        r2 = self.client.get('/api/advocate/summaries', headers=self.h(tok2))
        ro = self.client.get('/api/advocate/summaries', headers=self.h(self.owner_token))
        print('\nR6 owner_visibility default none; topic visible to recipient2:', f'{CANARY}-private-strategy' in r2.text,
              '| to owner:', f'{CANARY}-private-strategy' in ro.text)
        self.assertNotIn(f'{CANARY}-private-strategy', r2.text)
        self.assertNotIn(f'{CANARY}-private-strategy', ro.text)


class R7HostAndOrigin(Base):
    def test_unknown_host_is_refused(self):
        response = self.client.get('/advocate', headers={'Host': 'rebinding.example'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get('/advocate', headers={'Host': '127.0.0.1:8090'}).status_code, 200)

    def test_configured_host_is_accepted(self):
        with db.session() as conn:
            authz.set_setting(conn, 'http.allowed_hosts', ['ageds.example'])
        self.assertEqual(self.client.get('/advocate', headers={'Host': 'ageds.example'}).status_code, 200)

    def test_cross_site_state_changes_are_refused_in_every_mode(self):
        for mode in ('legacy_open', 'tokens'):
            with db.session() as conn:
                authz.set_setting(conn, 'auth.mode', mode, confirm_widening=True)
            for headers in ({'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'},
                            {'Origin': 'https://evil.example'}, {'Origin': 'null'}):
                with self.subTest(mode=mode, headers=headers):
                    response = self.client.post('/upload', headers=headers, files={'file': ('x.wav', b'x', 'audio/wav')})
                    self.assertEqual(response.status_code, 403)
        with db.session() as conn:
            before = conn.execute('SELECT count(*) FROM artifacts').fetchone()[0]
        self.assertEqual(before, 2)  # nothing planted

    def test_same_origin_and_non_browser_clients_pass(self):
        ok = self.client.post('/advocate/login', data={'token': self.rec_token}, follow_redirects=False,
                              headers={'Origin': 'http://testserver', 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(ok.status_code, 303)
        cli = self.client.post('/api/packages/validate', json={}, headers=self.h(self.owner_token))
        self.assertEqual(cli.status_code, 200)


class R8SearchTitleOfHiddenArtifact(Base):
    def test_title_does_not_name_an_invisible_artifact(self):
        self.grant_case(self.rec, self.case_a, ['read_metadata', 'read_text'])
        with db.session() as conn:
            text_b = conn.execute('SELECT id FROM derived_text WHERE artifact_id=?', (self.art_b,)).fetchone()[0]
            model.assign(conn, case_id=self.case_a, target_kind='derived_text', target_id=text_b)
            self.assertFalse(authz.visible(conn, self.rec, 'artifact', self.art_b, 'read_metadata'))
        hits = self.client.get('/api/search', params={'q': 'najmu'}, headers=self.h(self.rec_token)).json()
        titles = {h['title'] for h in hits}
        self.assertIn('transcript', titles)
        self.assertNotIn('b.wav', ' '.join(titles))


class R9LegacyAndGrowthSpread(Base):
    def test_blanket_mapping_keeps_owner_notes_and_audit_private(self):
        self.grant_case(self.rec, self.case_a, ['read_metadata', 'read_text', 'export'])
        with db.session() as conn:
            conn.execute("INSERT INTO annotations(artifact_id,body) VALUES(?,?)", (self.art_b, f'{CANARY}-note'))
            authz.set_setting(conn, 'legacy_scope_policy', f'treat_as_case:{self.case_a}', confirm_widening=True)
            gate = authz.ContentGate(conn, self.rec)
            self.assertEqual(gate.list('annotation'), [])
            self.assertEqual(gate.list('audit_log'), [])
            self.assertEqual(gate.list('principal'), [])
        export = self.client.get('/api/export/scoped', headers=self.h(self.rec_token)).text
        self.assertNotIn(f'{CANARY}-note', export)

    def test_auto_include_accepts_only_freezable_kinds(self):
        with db.session() as conn:
            package = sharing.create_package(conn, 'p')
            sharing.add_item(conn, package['id'], 'case', self.case_a)
            sharing.snapshot_package(conn, package['id'])
            authz.grant(conn, self.rec, package['scope_id'], ['read_metadata'])
            change = sharing.set_growth_policy(conn, package['id'], 'auto_include_by_rule:audit_log')
            self.assertEqual(change['warnings'][0]['code'], 'value_not_in_presets')
            self.assertEqual(authz.ContentGate(conn, self.rec).list('audit_log'), [])


class R10BrowserSessions(Base):
    def test_cookie_is_a_separate_expiring_session_revoked_on_logout(self):
        login = self.client.post('/advocate/login', data={'token': self.rec_token}, follow_redirects=False)
        cookie = login.cookies.get('ageds_token') or login.headers['set-cookie'].split('ageds_token=')[1].split(';')[0].strip('"')
        self.assertNotEqual(cookie, self.rec_token)
        self.assertIn('Max-Age=43200', login.headers['set-cookie'])
        self.assertEqual(self.client.get('/api/artifacts', headers=self.h(cookie)).status_code, 200)
        self.client.cookies.set('ageds_token', cookie)
        self.client.post('/advocate/logout', follow_redirects=False)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/artifacts', headers=self.h(cookie)).status_code, 401)
        self.assertEqual(self.client.get('/api/artifacts', headers=self.h(self.rec_token)).status_code, 200)

    def test_expired_sessions_and_tokens_are_refused(self):
        with db.session() as conn:
            stale = auth.issue_token(conn, self.rec, kind='session', expires_at='2000-01-01T00:00:00+00:00')
            old = auth.issue_token(conn, self.rec, expires_at='2000-01-01T00:00:00+00:00')
            fresh = auth.issue_token(conn, self.rec, expires_at='2999-01-01T00:00:00+00:00')
        for token, status in ((stale, 401), (old, 401), (fresh, 200)):
            with self.subTest(status=status):
                self.assertEqual(self.client.get('/api/artifacts', headers=self.h(token)).status_code, status)

    def test_logout_never_revokes_an_api_token(self):
        self.client.cookies.set('ageds_token', self.rec_token)
        self.client.post('/advocate/logout', follow_redirects=False)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/artifacts', headers=self.h(self.rec_token)).status_code, 200)


if __name__ == '__main__':
    unittest.main(verbosity=1)

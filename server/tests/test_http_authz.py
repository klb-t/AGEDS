"""Package P3: authentication and the HTTP gate before any stored data.

Synthetic bytes and names only; canaries mark text that must stay hidden.
"""
from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from server.app import assignments as model
from server.app import auth, authz, db, evidence, main

HIDDEN = 'CANARY-hidden-case'


def guards(route):
    return [d.call._ageds_guard for d in route.dependant.dependencies if hasattr(d.call, '_ageds_guard')]


def concrete(path, value=1):
    return re.sub(r'\{[^}]+\}', str(value), path)


class StaticGateTests(unittest.TestCase):
    def test_every_route_declares_exactly_one_guard(self):
        routes = [r for r in main.app.routes if isinstance(r, APIRoute)]
        self.assertGreater(len(routes), 25)
        for route in routes:
            with self.subTest(path=route.path, methods=sorted(route.methods)):
                self.assertEqual(len(guards(route)), 1, guards(route))

    def test_public_routes_are_an_explicit_list(self):
        public = sorted(r.path for r in main.app.routes if isinstance(r, APIRoute) and guards(r) == ['public'])
        self.assertEqual(public, ['/advocate', '/advocate/login', '/advocate/logout'])

    def test_object_routes_are_not_left_unscoped(self):
        for route in main.app.routes:
            if isinstance(route, APIRoute) and '{artifact_id}' in route.path:
                with self.subTest(path=route.path):
                    self.assertRegex(guards(route)[0], r'^(artifact|installation):')


class HttpAuthzTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'store').mkdir()
        self.settings = replace(db.settings, data_dir=self.root, db_path=self.root / 'db.sqlite',
                                store_dir=self.root / 'store', scan_roots=())
        self.patches = [patch.object(module, 'settings', self.settings) for module in (db, evidence, main)]
        for item in self.patches:
            item.start()
        self.context = TestClient(main.app)
        self.client = self.context.__enter__()
        with db.session() as conn:
            self.case_a = conn.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()[0]
            self.case_b = conn.execute('INSERT INTO cases(name) VALUES(?)', (HIDDEN,)).lastrowid
        self.shared = self.ingest('shared.wav', b'RIFF shared synthetic', self.case_a)
        self.hidden = self.ingest(f'{HIDDEN}.wav', b'RIFF hidden synthetic', self.case_b)
        evidence.add_derived_text(self.shared, 'transcript', 'jawny tekst',
                                  segments=[{'start': 0.0, 'end': 1.0, 'text': 'jawny tekst'}])
        evidence.add_derived_text(self.hidden, 'transcript', HIDDEN,
                                  segments=[{'start': 0.0, 'end': 1.0, 'text': HIDDEN}])
        with db.session() as conn:
            conn.execute("INSERT INTO annotations(artifact_id,body) VALUES(?,?)", (self.shared, HIDDEN))
            model.assign(conn, case_id=self.case_b, target_kind='artifact', target_id=self.shared,
                         meaning_text=HIDDEN)
            self.owner = authz.bootstrap_owner(conn, 'owner')['principal_id']
            self.owner_token = auth.issue_token(conn, self.owner)
            self.recipient = model.create_principal(conn, 'recipient')
            self.recipient_token = auth.issue_token(conn, self.recipient)
            scope = authz.create_scope(conn, 'case', {'case_id': self.case_a})
            self.grant = authz.grant(conn, self.recipient, scope, ['read_metadata', 'read_text'])['id']

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def ingest(self, name, content, case_id):
        path = self.root / name
        path.write_bytes(content)
        source = evidence.ensure_source('synthetic', f'source-{case_id}', case_id=case_id)
        return evidence.ingest_file(path, source_id=source, source_locator=f'synthetic://{name}', mime_type='audio/wav')

    def tokens_mode(self, **extra):
        with db.session() as conn:
            authz.set_setting(conn, 'auth.mode', 'tokens')
            for key, value in extra.items():
                authz.set_setting(conn, key.replace('__', '.'), value)

    def as_(self, token):
        return {'X-AGEDS-Token': token}

    def test_legacy_open_is_announced_and_warns_about_unenforced_grants(self):
        response = self.client.get('/api/artifacts')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-AGEDS-Auth-Mode'], 'legacy_open')
        self.assertEqual(response.headers['X-AGEDS-Auth-Warning'], 'recipient_grants_unenforced')

    def test_tokens_mode_rejects_anonymous_and_bad_tokens_on_every_route(self):
        self.tokens_mode()
        for route in main.app.routes:
            if not isinstance(route, APIRoute):
                continue
            public = guards(route) == ['public']
            for method in route.methods:
                for headers in ({}, self.as_('nope'), self.as_(self.owner_token + 'x'), {'Authorization': 'Bearer x.y'}):
                    with self.subTest(path=route.path, method=method, headers=headers):
                        response = self.client.request(method, concrete(route.path, self.shared), headers=headers,
                                                       follow_redirects=False)
                        if not public:
                            self.assertEqual(response.status_code, 401, response.text)
                        self.assertNotIn(HIDDEN, response.text)
                        self.assertNotIn('set-cookie', {k.lower() for k in response.headers} if response.status_code != 303
                                         or 'error' in response.headers.get('location', '') else set())

    def test_owner_keeps_full_access_through_an_ordinary_grant(self):
        self.tokens_mode()
        headers = self.as_(self.owner_token)
        self.assertEqual(len(self.client.get('/api/artifacts', headers=headers).json()), 2)
        self.assertEqual(self.client.get('/export/manifest.json', headers=headers).status_code, 200)
        self.assertEqual(self.client.get(f'/api/artifacts/{self.hidden}/content', headers=headers).content,
                         b'RIFF hidden synthetic')
        bearer = self.client.get('/api/artifacts', headers={'Authorization': f'Bearer {self.owner_token}'})
        self.assertEqual(bearer.status_code, 200)
        cookie = TestClient(main.app, cookies={'ageds_token': self.owner_token}).get('/api/artifacts')
        self.assertEqual(cookie.status_code, 200)

    def test_recipient_sees_only_granted_case(self):
        self.tokens_mode()
        headers = self.as_(self.recipient_token)
        listed = self.client.get('/api/artifacts', headers=headers).json()
        self.assertEqual([row['id'] for row in listed], [self.shared])
        self.assertEqual(self.client.get(f'/api/artifacts/{self.shared}/transcript', headers=headers).json()['text'], 'jawny tekst')
        for path in ('/', '/api/timeline', '/export/events.csv', '/export/manifest.json',
                     f'/api/artifacts/{self.shared}/annotations', f'/api/artifacts/{self.shared}/annotations/page',
                     f'/artifact/{self.shared}', f'/api/artifacts/{self.shared}/citations/1/packet'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 403)
        # No read_bytes: content is hidden like a missing artifact.
        self.assertEqual(self.client.get(f'/api/artifacts/{self.shared}/content', headers=headers).status_code, 404)
        self.assertEqual(self.client.post('/api/artifacts/upload', headers=headers,
                                          files={'file': ('x.wav', b'x', 'audio/wav')}).status_code, 403)

    def test_recipient_search_is_scoped(self):
        self.tokens_mode()
        recipient = self.client.get('/api/search', params={'q': 'CANARY'}, headers=self.as_(self.recipient_token))
        self.assertEqual((recipient.status_code, recipient.json()), (200, []))
        self.assertEqual(recipient.headers['X-AGEDS-Search-Has-More'], 'false')
        found = self.client.get('/api/search', params={'q': 'jawny'}, headers=self.as_(self.recipient_token)).json()
        self.assertEqual([(r['object_kind'], r['snippet']) for r in found], [('derived_text', 'jawny tekst')])
        owner = self.client.get('/api/search', params={'q': 'CANARY'}, headers=self.as_(self.owner_token)).json()
        self.assertTrue(owner)

    def test_scoped_export_route(self):
        self.assertEqual(self.client.get('/api/export/scoped').status_code, 409)
        self.tokens_mode()
        headers = self.as_(self.recipient_token)
        self.assertEqual(self.client.get('/api/export/scoped', headers=headers).json()['tables']['artifacts'], [])
        with db.session() as conn:
            scope = authz.create_scope(conn, 'case', {'case_id': self.case_a})
            authz.grant(conn, self.recipient, scope, ['export'])
        package = self.client.get('/api/export/scoped', headers=headers).json()
        self.assertEqual([r['id'] for r in package['tables']['artifacts']], [self.shared])
        self.assertNotIn(HIDDEN, json.dumps(package))
        from server.app.packages import validate_metadata_package
        self.assertTrue(validate_metadata_package(package)['valid'])

    def test_hidden_and_missing_are_indistinguishable_by_default(self):
        self.tokens_mode()
        headers = self.as_(self.recipient_token)
        for suffix in ('transcript', 'transcripts', 'citations', 'transcripts/page', 'source-observations', 'content'):
            with self.subTest(suffix=suffix):
                hidden = self.client.get(f'/api/artifacts/{self.hidden}/{suffix}', headers=headers)
                missing = self.client.get(f'/api/artifacts/999999/{suffix}', headers=headers)
                self.assertEqual((hidden.status_code, hidden.content), (missing.status_code, missing.content))
                self.assertEqual(hidden.status_code, 404)

    def test_reveal_existence_setting(self):
        self.tokens_mode(denial__reveal_existence='always')
        headers = self.as_(self.recipient_token)
        self.assertEqual(self.client.get(f'/api/artifacts/{self.hidden}/transcript', headers=headers).status_code, 403)
        self.assertEqual(self.client.get('/api/artifacts/999999/transcript', headers=headers).status_code, 404)

    def test_s4_material_shared_into_another_case(self):
        """A recipient of case B sees the shared material but nothing private to case A or to B's owner notes."""
        with db.session() as conn:
            scope = authz.create_scope(conn, 'case', {'case_id': self.case_b})
            other = model.create_principal(conn, 'recipient of B')
            token = auth.issue_token(conn, other)
            authz.grant(conn, other, scope, ['read_metadata', 'read_text'])
        self.tokens_mode()
        headers = self.as_(token)
        self.assertEqual(self.client.get(f'/api/artifacts/{self.shared}/transcript', headers=headers).status_code, 200)
        self.assertEqual(self.client.get(f'/api/artifacts/{self.shared}/source-observations', headers=headers).json(), [])
        self.assertEqual(self.client.get(f'/api/artifacts/{self.shared}/annotations', headers=headers).status_code, 403)

    def test_no_canary_reaches_the_recipient_through_any_get_route(self):
        self.tokens_mode()
        headers = self.as_(self.recipient_token)
        seen = 0
        for route in main.app.routes:
            if not isinstance(route, APIRoute) or 'GET' not in route.methods:
                continue
            for artifact_id in (self.shared, self.hidden):
                response = self.client.get(concrete(route.path, artifact_id), headers=headers, params={'q': 'CANARY'})
                seen += 1
                with self.subTest(path=route.path, artifact=artifact_id):
                    self.assertNotIn(HIDDEN, response.text)
                    self.assertNotIn(HIDDEN, json.dumps(dict(response.headers)))
        self.assertGreater(seen, 30)

    def test_revocation_applies_to_the_next_request(self):
        self.tokens_mode()
        headers = self.as_(self.recipient_token)
        self.assertEqual(self.client.get(f'/api/artifacts/{self.shared}/transcript', headers=headers).status_code, 200)
        with db.session() as conn:
            authz.revoke_grant(conn, self.grant, revoked_by='owner')
        self.assertEqual(self.client.get(f'/api/artifacts/{self.shared}/transcript', headers=headers).status_code, 404)
        self.assertEqual(self.client.get('/api/artifacts', headers=headers).json(), [])

    def test_token_revocation_and_storage(self):
        self.tokens_mode()
        method_uid, secret = self.recipient_token.split('.')
        with db.session() as conn:
            stored = json.dumps([dict(r) for r in conn.execute('SELECT * FROM auth_methods')])
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, json.dumps([dict(r) for r in conn.execute('SELECT * FROM audit_log')]))
            auth.revoke_token(conn, method_uid, actor='owner')
        self.assertEqual(self.client.get('/api/artifacts', headers=self.as_(self.recipient_token)).status_code, 401)

    def test_advocate_login_sets_a_strict_http_only_cookie(self):
        self.tokens_mode()
        bad = self.client.post('/advocate/login', data={'token': 'nope'}, follow_redirects=False)
        self.assertEqual((bad.status_code, bad.headers['location']), (303, '/advocate?error=token'))
        self.assertNotIn('set-cookie', bad.headers)
        good = self.client.post('/advocate/login', data={'token': self.recipient_token}, follow_redirects=False,
                                headers={'X-Forwarded-Proto': 'https'})
        cookie = good.headers['set-cookie']
        for flag in ('HttpOnly', 'SameSite=strict', 'Secure', 'ageds_token='):
            self.assertIn(flag.lower(), cookie.lower())
        out = self.client.post('/advocate/logout', follow_redirects=False)
        self.assertIn('ageds_token=""', out.headers['set-cookie'])

    def test_unknown_auth_mode_admits_nobody(self):
        with db.session() as conn:
            authz.set_setting(conn, 'auth.mode', 'external')
        self.assertEqual(self.client.get('/api/artifacts', headers=self.as_(self.owner_token)).status_code, 401)

    def test_citing_needs_cite_and_read_text(self):
        self.tokens_mode()
        with db.session() as conn:
            text_id = conn.execute('SELECT id FROM derived_text WHERE artifact_id=?', (self.shared,)).fetchone()[0]
        body = {'derivedTextId': text_id, 'segmentIndices': [0]}
        headers = self.as_(self.recipient_token)
        self.assertEqual(self.client.post(f'/api/artifacts/{self.shared}/citations', json=body, headers=headers).status_code, 404)
        with db.session() as conn:
            scope = authz.create_scope(conn, 'case', {'case_id': self.case_a})
            authz.grant(conn, self.recipient, scope, ['cite'])
        self.assertEqual(self.client.post(f'/api/artifacts/{self.shared}/citations', json=body, headers=headers).status_code, 200)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'store').mkdir()
        self.settings = replace(db.settings, data_dir=root, db_path=root / 'db.sqlite', store_dir=root / 'store')
        self.patch = patch.object(db, 'settings', self.settings)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        import contextlib, io
        from server.app import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_owner_flow_and_lockout_guard(self):
        code, refused = self.run_cli('authz-mode', 'tokens')
        self.assertNotEqual(code, 0)
        code, owner = self.run_cli('authz-owner-init', '--name', 'owner')
        self.assertEqual(code, 0)
        self.assertTrue(owner['token_shown_once'])
        code, recipient = self.run_cli('authz-principal-add', '--name', 'recipient', '--kind', 'human')
        code, granted = self.run_cli('authz-grant', '--principal-uid', recipient['principal_uid'], '--case-id', '1',
                                     '--capability', 'read_text', '--capability', 'read_metadata')
        self.assertEqual((code, granted['warnings']), (0, []))
        self.assertEqual(self.run_cli('authz-mode', 'tokens')[0], 0)
        code, shown = self.run_cli('authz-show')
        self.assertEqual(shown['settings']['auth.mode'], 'tokens')
        self.assertNotIn(owner['token'].split('.')[1], json.dumps(shown))
        with db.session() as conn:
            self.assertEqual(auth.principal_for_token(conn, owner['token']), owner['principal_id'])
        code, package = self.run_cli('authz-package-create', '--name', 'dla adwokata')
        self.assertEqual(code, 0)
        self.assertEqual(self.run_cli('authz-package-add', '--package-uid', package['package_uid'], '--kind', 'case', '--id', '1')[0], 0)
        code, frozen = self.run_cli('authz-package-snapshot', '--package-uid', package['package_uid'])
        self.assertEqual((code, frozen['objects']), (0, 1))
        code, shared = self.run_cli('authz-package-grant', '--package-uid', package['package_uid'],
                                    '--principal-uid', recipient['principal_uid'], '--capability', 'read_text')
        self.assertEqual(code, 0)


if __name__ == '__main__':
    unittest.main()

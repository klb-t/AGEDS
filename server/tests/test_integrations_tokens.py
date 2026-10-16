"""Local tokens: hash-only storage, scopes, revocation/expiry, audit, enforcement."""
from __future__ import annotations

import json
from unittest.mock import patch

from server.app import db, main
from server.app.integrations import tokens
from server.tests.test_integrations_support import IntegrationCase


class TokenStorageTests(IntegrationCase):
    def test_token_value_exists_only_in_creation_result(self):
        created = self.token(scopes=('read', 'propose'), client_id='chatadhd', expires_in_days=30)
        self.assertTrue(created['token'].startswith('ageds_'))
        self.assertTrue(created['shown_once'])
        secret = created['token'].split('.', 1)[1]
        with db.session() as conn:
            row = dict(conn.execute('SELECT * FROM api_tokens WHERE id=?', (created['id'],)).fetchone())
            everything = json.dumps([dict(r) for r in conn.execute('SELECT * FROM audit_log')]) + json.dumps(row)
        self.assertNotIn(secret, everything)
        self.assertEqual(len(row['secret_sha256']), 64)
        self.assertNotEqual(row['salt'], '')
        self.assertEqual(json.loads(row['scopes_json']), ['read', 'propose'])
        self.assertIsNotNone(row['expires_at'])
        listed = tokens.list_tokens()[0]
        self.assertNotIn('token', listed)
        self.assertNotIn('salt', listed)
        self.assertNotIn('secret_sha256', listed)
        audit = self.audit('token_create')
        self.assertEqual(len(audit), 1)
        self.assertNotIn(secret, audit[0]['details_json'])
        self.assertIn(created['token_key'], audit[0]['details_json'])

    def test_verify_accepts_valid_and_rejects_wrong_revoked_expired_scope(self):
        created = self.token()
        record = tokens.verify_token(created['token'])
        self.assertEqual(record['use_count'], 1)
        self.assertIsNotNone(record['last_used_at'])
        key = created['token'].split('.', 1)[0]
        for bad, reason in ((None, 'missing_token'), ('', 'missing_token'), ('nonsense', 'malformed_token'),
                            ('ageds_zz.secret', 'malformed_token'), ('ageds_000000000000.secret', 'unknown_token'),
                            (key + '.wrongsecret', 'unknown_token'), (created['token'] + 'x', 'unknown_token')):
            with self.subTest(bad=bad):
                with self.assertRaises(tokens.TokenRejected) as ctx:
                    tokens.verify_token(bad)
                self.assertEqual(ctx.exception.reason, reason)
                self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(tokens.TokenRejected) as ctx:
            tokens.verify_token(created['token'], scope='propose')
        self.assertEqual((ctx.exception.reason, ctx.exception.status), ('insufficient_scope', 403))
        tokens.revoke_token(created['id'])
        with self.assertRaises(tokens.TokenRejected) as ctx:
            tokens.verify_token(created['token'])
        self.assertEqual(ctx.exception.reason, 'revoked_token')
        with self.assertRaises(tokens.TokenError):
            tokens.revoke_token(created['id'])
        expired = self.token(expires_in_days=1)
        with db.session() as conn:
            conn.execute("UPDATE api_tokens SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (expired['id'],))
        with self.assertRaises(tokens.TokenRejected) as ctx:
            tokens.verify_token(expired['token'])
        self.assertEqual(ctx.exception.reason, 'expired_token')
        rejected = self.audit('token_rejected')
        # Missing/malformed tokens are refused before any database access. Review finding 8:
        # the two secret mismatches of one key in one hour share one audit row and a count of 2.
        self.assertEqual(len(rejected), 5)
        with db.session() as conn:
            counts = {(r['reason'], r['count']) for r in conn.execute('SELECT reason, count FROM api_token_rejections')}
        self.assertEqual(counts, {('unknown_key', 1), ('secret_mismatch', 2), ('scope', 1), ('revoked', 1), ('expired', 1)})
        secret = created['token'].split('.', 1)[1]
        self.assertFalse(any(secret in row['details_json'] for row in rejected))
        self.assertEqual({json.loads(r['details_json'])['reason'] for r in rejected} >= {'unknown_key', 'secret_mismatch', 'revoked', 'expired', 'scope'}, True)

    def test_constant_time_comparison_is_used(self):
        created = self.token()
        with patch.object(tokens.hmac, 'compare_digest', wraps=tokens.hmac.compare_digest) as compare:
            tokens.verify_token(created['token'])
            self.assertEqual(compare.call_count, 1)
            with self.assertRaises(tokens.TokenRejected):
                tokens.verify_token('ageds_000000000000.nothing')
            self.assertEqual(compare.call_count, 2)

    def test_input_validation(self):
        for kwargs in ({'label': ''}, {'label': 'x' * 201}, {'label': 'ok', 'scopes': ()}, {'label': 'ok', 'scopes': ('admin',)},
                       {'label': 'ok', 'expires_in_days': 0}, {'label': 'ok', 'client_id': ''}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(tokens.TokenError):
                    tokens.create_token(**kwargs)


class HttpAuthTests(IntegrationCase):
    def test_read_routes_require_bearer_with_error_model(self):
        for path in ('/api/read/v1/contract', '/api/read/v1/search?q=x', '/api/read/v1/timeline', f'/api/read/v1/artifacts/{self.artifact}',
                     '/api/read/v1/reports', '/api/read/v1/graph/export', '/api/read/v1/intents/schema'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers['www-authenticate'], 'Bearer')
                self.assertEqual(response.json()['detail']['code'], 'missing_token')
        response = self.client.get('/api/read/v1/contract', headers={'Authorization': 'Basic abc'})
        self.assertEqual(response.json()['detail']['code'], 'missing_token')
        response = self.client.get('/api/read/v1/contract', headers={'Authorization': 'Bearer ageds_000000000000.nope'})
        self.assertEqual((response.status_code, response.json()['detail']['code']), (401, 'unknown_token'))
        created = self.token()
        self.assertEqual(self.get('/api/read/v1/contract', created).status_code, 200)
        posted = self.client.post('/api/integrations/v1/proposals', headers=self.headers(created), json={'artifact_id': self.artifact, 'body': 'x'})
        self.assertEqual((posted.status_code, posted.json()['detail']['code']), (403, 'insufficient_scope'))
        self.assertNotIn('www-authenticate', posted.headers)
        tokens.revoke_token(created['id'])
        self.assertEqual(self.get('/api/read/v1/contract', created).json()['detail']['code'], 'revoked_token')

    def test_existing_routes_stay_open_by_default_and_closed_with_ew_require_token(self):
        self.assertEqual(self.client.get('/api/search', params={'q': 'koniec'}).status_code, 200)
        self.assertEqual(self.client.get(f'/api/artifacts/{self.artifact}/transcript').status_code, 200)
        self.assertEqual(self.client.get('/').status_code, 200)
        created = self.token()
        with patch.dict('os.environ', {'EW_REQUIRE_TOKEN': '1'}):
            self.assertTrue(tokens.token_required_everywhere())
            for path in ('/api/search?q=koniec', f'/api/artifacts/{self.artifact}/transcript', '/api/timeline', '/api/artifacts'):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers['www-authenticate'], 'Bearer')
                    self.assertEqual(response.json()['detail']['code'], 'missing_token')
            self.assertEqual(self.client.get('/').status_code, 200, 'HTML UI is not covered by the switch')
            self.assertEqual(self.client.get('/api/search', params={'q': 'koniec'}, headers=self.headers(created)).status_code, 200)
            content = self.client.get(f'/api/artifacts/{self.artifact}/content', headers=self.headers(created))
            self.assertEqual((content.status_code, content.content), (200, b'RIFF synthetic wav bytes'))
            upload = self.client.post('/api/artifacts/upload', headers=self.headers(created), files={'file': ('n.wav', b'new bytes', 'audio/wav')})
            self.assertEqual(upload.status_code, 200)
            self.assertEqual(self.get('/api/read/v1/contract', created).status_code, 200)
            tokens.revoke_token(created['id'])
            self.assertEqual(self.client.get('/api/search', params={'q': 'koniec'}, headers=self.headers(created)).json()['detail']['code'], 'revoked_token')
        self.assertEqual(self.client.get('/api/search', params={'q': 'koniec'}).status_code, 200)

    def test_middleware_is_installed_through_the_router_hook(self):
        names = [getattr(m.cls, '__name__', '') for m in main.app.user_middleware]
        self.assertIn('RequireTokenMiddleware', names)

    def test_integrations_page_lists_tokens_without_secret_and_revokes(self):
        created = self.token()
        page = self.client.get('/integrations')
        self.assertEqual(page.status_code, 200)
        self.assertIn(created['token_key'], page.text)
        self.assertNotIn(created['token'].split('.', 1)[1], page.text)
        self.assertEqual(self.client.post(f'/integrations/tokens/{created["id"]}/revoke', follow_redirects=False).status_code, 303)
        self.assertEqual(tokens.list_tokens()[0]['status'], 'revoked')

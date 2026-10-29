"""The owner's path in the browser: upload, plan, originals, ASR/OCR queue, report, settings (synthetic data)."""
import io
import json
import os
import unicodedata
import zipfile
from pathlib import Path

from server.tests.test_review_regressions import Base
from server.app import authz, db, owner


def whatsapp_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('WhatsApp-chat met Testcontact.txt',
                   '14-10-2026 10:42 - Testcontact: Hallo\n14-10-2026 10:43 - Testcontact: IMG-20261014-WA0003.jpg (bestand bijgevoegd)\n')
        z.writestr('IMG-20261014-WA0003.jpg', b'\xff\xd8\xff\xe0' + b'\x00' * 40)
    return buffer.getvalue()


class OwnerWebTests(Base):
    def owner_headers(self):
        return self.h(self.owner_token)

    def upload(self, files, **data):
        return self.client.post('/start/upload', files=[('files', f) for f in files], data=data,
                                headers=self.owner_headers(), follow_redirects=False)

    def test_upload_plan_originals_queue_and_report(self):
        wav = b'RIFF' + b'\x00' * 200
        r = self.upload([('WhatsApp-chat met Testcontact.zip', io.BytesIO(whatsapp_zip()), 'application/zip'),
                         ('call.wav', io.BytesIO(wav), 'audio/wav')],
                        label='paczka testowa', browser_times=json.dumps({'call.wav': 1760000000000}))
        self.assertEqual(r.status_code, 303, r.text)
        batch_url = r.headers['location'].split('?')[0]
        collection_id = int(batch_url.rsplit('/', 1)[1])
        view = owner.batch_view(collection_id)
        self.assertEqual(view['collection']['acquisition_method'], 'browser_upload')
        self.assertIn('upload times', view['collection']['notes'])
        self.assertTrue(view['collection']['metadata']['browser_last_modified']['call.wav'].startswith('2025-10-09'))
        self.assertEqual(view['plan_summary']['by_importer']['whatsapp_export']['items'], 1)
        # the plan runs through the existing import API
        plan_id = view['plan_id']
        decide = self.client.post(f'/api/import-plans/{plan_id}/decisions', headers=self.owner_headers(),
                                  json={'baseVersion': 1, 'actor': 'owner', 'bulk': {'action': 'approve'}})
        self.assertEqual(decide.status_code, 200, decide.text)
        run = self.client.post(f'/api/import-plans/{plan_id}/runs', headers=self.owner_headers(),
                               json={'version': 2, 'mode': 'sync', 'actor': 'owner'})
        self.assertEqual(run.status_code, 200, run.text)
        page = self.client.get(batch_url, headers=self.owner_headers()).text
        self.assertIn('call.wav', page)
        kept = self.client.post(f'{batch_url}/originals', headers=self.owner_headers(), follow_redirects=False)
        self.assertIn('kept', kept.headers['location'])
        names = {a['original_name'] for a in owner.batch_view(collection_id)['artifacts']}
        self.assertTrue({'call.wav', 'WhatsApp-chat met Testcontact.zip'} <= names)
        queued = self.client.post(f'{batch_url}/process', headers=self.owner_headers(), follow_redirects=False)
        self.assertIn('queued%3A', queued.headers['location'])
        with db.session() as conn:
            kinds = [r[0] for r in conn.execute("SELECT kind FROM jobs WHERE status='queued'")]
        self.assertIn('transcribe', kinds)
        from server.app.media.capabilities import media_capabilities
        if media_capabilities()['ocr_tesseract']['status'] != 'available':  # the default OCR fallback cannot run here
            self.assertIn('data-owner-preset-unavailable="ocr"', self.client.get(batch_url, headers=self.owner_headers()).text)
            result = owner.process_new(collection_id)
            self.assertTrue(any(s.get('reason') == 'preset unavailable here' for s in result['skipped']))
            with db.session() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE kind='ocr'").fetchone()[0], 0)
        waiting = self.client.get(batch_url, headers=self.owner_headers()).text
        self.assertIn('data-owner-autorefresh', waiting)  # refreshes itself while jobs are open
        self.assertIn('zadań w toku: ', waiting)
        again = self.client.post(f'{batch_url}/process', headers=self.owner_headers(), follow_redirects=False)
        self.assertIn('queued%3A0', again.headers['location'])  # open jobs are not queued twice
        # the same recording uploaded again is a new acquisition, but its bytes already have a transcript
        from server.app import evidence
        call = next(a for a in owner.batch_view(collection_id)['artifacts'] if a['original_name'] == 'call.wav')
        evidence.add_derived_text(call['id'], 'transcript', 'tekst syntetyczny')
        second = self.upload([('call.wav', io.BytesIO(wav), 'audio/wav')])
        second_url = second.headers['location'].split('?')[0]
        self.client.post(f'{second_url}/originals', headers=self.owner_headers())
        result = owner.process_new(int(second_url.rsplit('/', 1)[1]))
        self.assertEqual(result['queued'], [])
        self.assertEqual(result['skipped'][0]['reason'], 'the same bytes already have this text')
        settled = self.client.get(second_url, headers=self.owner_headers()).text
        self.assertIn('te same bajty mają już', settled)
        self.assertNotIn('data-owner-autorefresh', settled)  # nothing open in this batch
        report = self.client.get('/report', headers=self.owner_headers())
        self.assertEqual(report.status_code, 200)
        self.assertIn('whatsapp_message', report.text)
        self.assertIn('Pokrycie: poczta i czaty', report.text)

    def test_upload_names_are_contained_and_the_limit_leaves_nothing(self):
        r = self.upload([('../../etc/evil\u202ename.txt', io.BytesIO(b'x'), 'text/plain')])  # RTL override
        self.assertEqual(r.status_code, 303)
        batch = sorted(owner.incoming_root().iterdir())[-1]
        self.assertEqual([p.name for p in batch.iterdir()], ['evilname.txt'])
        self.assertEqual(oct(os.stat(batch / 'evilname.txt').st_mode & 0o777), '0o400')
        before = set(owner.incoming_root().iterdir())
        old = owner.MAX_UPLOAD_BYTES
        owner.MAX_UPLOAD_BYTES = 10
        try:
            r = self.upload([('big.bin', io.BytesIO(b'x' * 50), 'application/octet-stream')])
        finally:
            owner.MAX_UPLOAD_BYTES = old
        self.assertIn('error=', r.headers['location'])
        self.assertEqual(set(owner.incoming_root().iterdir()), before)

    def test_recipients_are_refused_everywhere(self):
        self.grant_case(self.rec, self.case_a, ['read_metadata', 'read_text', 'chat_with_advocate'])
        h = self.h(self.rec_token)
        for path in ('/start', '/settings', '/report'):
            self.assertEqual(self.client.get(path, headers=h).status_code, 403, path)
        for path, data in (('/settings/consent', {'provider': 'vertex', 'basis': 'x'}), ('/settings/budget', {'eur': '99'})):
            self.assertEqual(self.client.post(path, data=data, headers=h, follow_redirects=False).status_code, 403, path)
        self.assertEqual(self.client.post('/start/upload', files=[('files', ('a.txt', io.BytesIO(b'x'), 'text/plain'))],
                                          headers=h, follow_redirects=False).status_code, 403)

    def test_settings_consents_budget_and_presets(self):
        h = self.owner_headers()
        missing = self.client.post('/settings/consent', data={'provider': 'vertex', 'basis': '  '}, headers=h, follow_redirects=False)
        self.assertIn('error=', missing.headers['location'])
        unknown = self.client.post('/settings/consent', data={'provider': 'openai', 'basis': 'x'}, headers=h, follow_redirects=False)
        self.assertIn('error=', unknown.headers['location'])
        for provider in ('vertex', 'google_tts'):
            self.client.post('/settings/consent', data={'provider': provider, 'basis': 'R100 owner'}, headers=h)
        with db.session() as conn:
            rows = [dict(r) for r in conn.execute('SELECT uid, provider_name, data_policy_json FROM provider_consents')]
        self.assertEqual({r['provider_name'] for r in rows}, {'vertex', 'google_tts'})
        self.assertEqual(json.loads(rows[0]['data_policy_json'])['training'], False)
        self.client.post(f"/settings/consent/{rows[0]['uid']}/revoke", headers=h)
        page = self.client.get('/settings', headers=h).text
        self.assertIn('data-consent-state="revoked"', page)
        self.assertIn('error=', self.client.post('/settings/budget', data={'eur': 'dużo'}, headers=h,
                                                 follow_redirects=False).headers['location'])
        self.client.post('/settings/budget', data={'eur': '2,5'}, headers=h)
        self.assertIn('error=', self.client.post('/settings/recipes', data={'asr': 'nope', 'ocr': 'ocr-vertex-gemini'},
                                                 headers=h, follow_redirects=False).headers['location'])
        refused = self.client.post('/settings/recipes', data={'asr': 'asr-vertex-gemini-aligned', 'ocr': 'ocr-vertex-gemini'},
                                   headers=h, follow_redirects=False)
        self.assertIn('error=', refused.headers['location'])  # the Vertex consent was revoked above
        self.assertIn('data-availability="unavailable"', self.client.get('/settings', headers=h).text)
        self.client.post('/settings/consent', data={'provider': 'vertex', 'basis': 'again'}, headers=h)
        self.client.post('/settings/recipes', data={'asr': 'asr-vertex-gemini-aligned', 'ocr': 'ocr-vertex-gemini'}, headers=h)
        from server.app.media.recipe import default_recipe_id
        self.assertEqual((default_recipe_id('asr'), default_recipe_id('ocr')), ('asr-vertex-gemini-aligned', 'ocr-vertex-gemini'))
        page = self.client.get('/settings', headers=h).text
        self.assertIn('2.5 €', page)


class UploadSurfaceTests(Base):
    """The upload is a new attack surface (coordinator, 2026-10-27): names, archives, size, CSRF, overwrites."""

    def owner_headers(self):
        return self.h(self.owner_token)

    def upload(self, files, headers=None, **data):
        return self.client.post('/start/upload', files=[('files', f) for f in files], data=data,
                                headers=headers if headers is not None else self.owner_headers(), follow_redirects=False)

    def latest_batch(self):
        return sorted(owner.incoming_root().iterdir())[-1]

    def test_hostile_names_stay_plain_names_inside_the_batch(self):
        used = set()
        names = [owner.safe_filename(n, used) for n in (
            '../../etc/passwd', '/etc/shadow', 'C:\\Windows\\system.ini', 'a\x00b.txt', '..', '...hidden',
            unicodedata.normalize('NFD', 'zażółć.txt'), 'zażółć.txt', 'ZAŻÓŁĆ.TXT', '\u202etxt.exe', '')]
        self.assertEqual(names[:6], ['passwd', 'shadow', 'C__Windows_system.ini', 'ab.txt', 'upload', 'hidden'])
        # the NFD and NFC spellings are one name after normalization; case-insensitive collisions get a suffix
        self.assertEqual(names[6:9], ['zażółć.txt', 'zażółć (2).txt', 'ZAŻÓŁĆ (3).TXT'])
        self.assertEqual(names[9:], ['txt.exe', 'upload (2)'])
        for name in names:
            self.assertNotIn('/', name)
            self.assertNotIn('\x00', name)

    def test_same_name_twice_never_overwrites(self):
        r = self.upload([('dup.txt', io.BytesIO(b'first'), 'text/plain'), ('dup.txt', io.BytesIO(b'second'), 'text/plain')])
        self.assertEqual(r.status_code, 303)
        batch = self.latest_batch()
        contents = {p.name: p.read_bytes() for p in batch.iterdir()}
        self.assertEqual(contents, {'dup.txt': b'first', 'dup (2).txt': b'second'})
        self.assertTrue(all(os.stat(p).st_mode & 0o777 == 0o400 for p in batch.iterdir()))
        with self.assertRaises(FileExistsError):  # O_EXCL: an existing name is never reopened for writing
            owner.write_upload(batch, 'dup.txt', io.BytesIO(b'x'), set(), [10 ** 6])

    def test_zip_slip_and_symlink_members_write_nothing_outside(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('../../escaped.txt', b'slip')
            z.writestr('/abs/evil.txt', b'absolute')
            z.writestr('ok.txt', b'fine')
            link = zipfile.ZipInfo('link-to-passwd')
            link.external_attr = (0o120777 << 16)  # a symbolic link member
            link.create_system = 3
            z.writestr(link, '/etc/passwd')
        import tempfile
        temp_before = {p.name for p in Path(tempfile.gettempdir()).iterdir()}
        r = self.upload([('evil.zip', io.BytesIO(buffer.getvalue()), 'application/zip')])
        url = r.headers['location'].split('?')[0]
        self.client.post(f'{url}/originals', headers=self.owner_headers())
        batch = self.latest_batch()
        self.assertEqual([p.name for p in batch.iterdir()], ['evil.zip'])  # nothing extracted into the batch
        named = [p for p in self.root.rglob('*') if p.name in ('escaped.txt', 'evil.txt', 'link-to-passwd')]
        self.assertEqual(named, [])  # members are only ever written to a fixed temporary name, never their own path
        self.assertFalse({p.name for p in Path(tempfile.gettempdir()).iterdir()} - temp_before
                         & {'escaped.txt', 'evil.txt', 'link-to-passwd', 'abs'})
        with db.session() as conn:
            stored = {r['original_name']: open(r['stored_path'], 'rb').read() for r in conn.execute(
                "SELECT original_name, stored_path FROM artifacts WHERE original_name IN ('link-to-passwd','escaped.txt','evil.txt','ok.txt')")}
        self.assertEqual(stored.get('ok.txt'), b'fine')
        if 'link-to-passwd' in stored:  # a link member is its own bytes (the target text), never the target file
            self.assertEqual(stored['link-to-passwd'], b'/etc/passwd')
        self.assertNotIn(b'root:', b''.join(stored.values()))

    def test_zip_bomb_is_cut_by_the_catalog_limits(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('zeros.bin', b'\x00' * (20 * 1024 * 1024))  # ratio ~1000:1, above the 200:1 limit
        self.assertLess(len(buffer.getvalue()), 200_000)
        r = self.upload([('bomb.zip', io.BytesIO(buffer.getvalue()), 'application/zip')])
        collection = int(r.headers['location'].split('?')[0].rsplit('/', 1)[1])
        view = owner.batch_view(collection)
        self.assertFalse(view['coverage']['complete'])
        self.assertEqual(view['coverage']['limit_hits'], {'compression_ratio_declared': 1})
        with db.session() as conn:
            member = conn.execute("SELECT status, sha256 FROM catalog_entries WHERE name='zeros.bin'").fetchone()
        self.assertTrue(member is None or member['sha256'] is None)  # never hashed, so never ingested
        self.client.post(f"{r.headers['location'].split('?')[0]}/originals", headers=self.owner_headers())
        with db.session() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM artifacts WHERE original_name='zeros.bin'").fetchone())

    def test_cross_site_upload_is_refused(self):
        with db.session() as conn:
            authz.set_setting(conn, 'auth.mode', 'legacy_open', confirm_widening=True)
        before = set(owner.incoming_root().iterdir()) if owner.incoming_root().exists() else set()
        cross = {'Origin': 'https://evil.example', 'Sec-Fetch-Site': 'cross-site'}
        for extra in ({}, {'Authorization': 'Basic dXNlcjpwYXNz'}):
            r = self.upload([('x.txt', io.BytesIO(b'x'), 'text/plain')], headers={**cross, **extra})
            self.assertEqual(r.status_code, 403, extra)
        after = set(owner.incoming_root().iterdir()) if owner.incoming_root().exists() else set()
        self.assertEqual(after, before)

    def test_upload_limit_is_a_setting(self):
        h = self.owner_headers()
        self.assertIn('error=', self.client.post('/settings/upload-limit', data={'gib': 'x'}, headers=h,
                                                 follow_redirects=False).headers['location'])
        self.client.post('/settings/upload-limit', data={'gib': '0,001'}, headers=h)
        self.assertEqual(owner.max_upload_bytes(), int(0.001 * 1024 ** 3))
        self.assertIn('0.001 GiB', self.client.get('/settings', headers=h).text)
        big = self.upload([('big.bin', io.BytesIO(b'x' * (2 * 1024 * 1024)), 'application/octet-stream')])
        self.assertIn('error=', big.headers['location'])


class InviteFromSettingsTests(Base):
    """The owner invites a recipient (e.g. an advocate) from /settings without the CLI."""

    def test_invite_shows_the_link_once_and_the_recipient_sees_only_that_case(self):
        h = self.h(self.owner_token)
        r = self.client.post('/settings/invite', headers=h, follow_redirects=False,
                             data={'name': 'mec. Testowy', 'case_ids': [str(self.case_a)], 'mode': 'snapshot', 'expires_days': '30'})
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual((r.headers['cache-control'], r.headers['referrer-policy']), ('no-store', 'no-referrer'))
        import re
        token = re.search(r'data-owner-invite-token>([^<]+)<', r.text).group(1)
        self.assertIn('/advocate#t=' + token, r.text)  # in the fragment, never sent to a server
        mine = self.client.get(f'/api/artifacts/{self.art_a}/transcript', headers=self.h(token))
        other = self.client.get(f'/api/artifacts/{self.art_b}/transcript', headers=self.h(token))
        self.assertEqual((mine.status_code, other.status_code), (200, 404))
        page = self.client.get('/settings', headers=h).text
        self.assertIn('mec. Testowy', page)
        self.assertNotIn(token, page)  # never shown again
        with db.session() as conn:
            grant = conn.execute("SELECT g.uid FROM grants g JOIN principals p ON p.id=g.principal_id WHERE p.display_name='mec. Testowy'").fetchone()['uid']
        self.client.post(f'/settings/grant/{grant}/revoke', headers=h)
        self.assertEqual(self.client.get(f'/api/artifacts/{self.art_a}/transcript', headers=self.h(token)).status_code, 404)

    def test_invite_is_refused_in_legacy_open_and_owner_tokens_cannot_be_revoked_here(self):
        h = self.h(self.owner_token)
        with db.session() as conn:
            authz.set_setting(conn, 'auth.mode', 'legacy_open', confirm_widening=True)
        r = self.client.post('/settings/invite', data={'name': 'x', 'case_ids': [str(self.case_a)]}, follow_redirects=False)
        self.assertIn('error=', r.headers['location'])
        self.assertIn('data-owner-invite-disabled', self.client.get('/settings').text)
        with db.session() as conn:
            authz.set_setting(conn, 'auth.mode', 'tokens')
            owner_token_uid = conn.execute("SELECT m.uid FROM auth_methods m WHERE m.principal_id=?", (self.owner,)).fetchone()['uid']
        r = self.client.post(f'/settings/token/{owner_token_uid}/revoke', headers=h, follow_redirects=False)
        self.assertIn('error=', r.headers['location'])
        self.assertEqual(self.client.get('/settings', headers=h).status_code, 200)  # still in

    def test_invite_validation(self):
        h = self.h(self.owner_token)
        for data in ({'name': '', 'case_ids': [str(self.case_a)]}, {'name': 'x'}, {'name': 'x', 'case_ids': ['99999']},
                     {'name': 'x', 'case_ids': [str(self.case_a)], 'mode': 'forever'},
                     {'name': 'x', 'case_ids': [str(self.case_a)], 'expires_days': '0'}):
            r = self.client.post('/settings/invite', data=data, headers=h, follow_redirects=False)
            self.assertEqual(r.status_code, 303, data)
            self.assertIn('error=', r.headers['location'])
        with db.session() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM principals WHERE display_name='x'").fetchone())

"""The owner's path in the browser: upload, plan, originals, ASR/OCR queue, report, settings (synthetic data)."""
import io
import json
import os
import zipfile

from server.tests.test_review_regressions import Base
from server.app import db, owner


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
        self.assertIn('te same bajty mają już', self.client.get(second_url, headers=self.owner_headers()).text)
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
        self.client.post('/settings/recipes', data={'asr': 'asr-vertex-gemini-aligned', 'ocr': 'ocr-vertex-gemini'}, headers=h)
        from server.app.media.recipe import default_recipe_id
        self.assertEqual((default_recipe_id('asr'), default_recipe_id('ocr')), ('asr-vertex-gemini-aligned', 'ocr-vertex-gemini'))
        page = self.client.get('/settings', headers=h).text
        self.assertIn('2.5 €', page)

"""WhatsApp and Gmail/EML export format variants through the owner's upload path (synthetic fixtures).

Every fixture below is built here, modelled on publicly documented or commonly observed
export layouts (WhatsApp Android/iOS "Export chat" in several locales, Google Takeout Mail,
Outlook/Thunderbird ``.eml``). No real export, name, number or message is used. Results are
summarized in docs/validation/REAL_EXPORT_FORMATS_2026-10-30.md.
"""
import hashlib
import io
import json
import zipfile

from server.tests.test_review_regressions import Base
from server.app import db, owner
from server.app.importplan import plan as planmod

NNBSP = ' '
LRM = '‎'


def make_zip(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buffer.getvalue()


JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 40
OPUS = b'OggS' + b'\x00' * 40
PDF = b'%PDF-1.4\n%synthetic\n'


class ExportFormatBase(Base):
    def owner_headers(self):
        return self.h(self.owner_token)

    def run_upload(self, files: list[tuple[str, bytes]], *, chat_members: dict | None = None) -> dict:
        """Upload → catalogue → plan → approve all → run (sync) → keep originals, as the owner does."""
        r = self.client.post('/start/upload', headers=self.owner_headers(), follow_redirects=False,
                             files=[('files', (name, io.BytesIO(data), 'application/octet-stream')) for name, data in files])
        self.assertEqual(r.status_code, 303, r.text)
        self.assertNotIn('error=', r.headers['location'])
        collection_id = int(r.headers['location'].split('?')[0].rsplit('/', 1)[1])
        view = owner.batch_view(collection_id)
        plan_id = view['plan_id']
        full = planmod.get_plan(plan_id, full=True)
        decisions = []
        for item in full['items']:
            params = (chat_members or {}).get(item['name'])
            if params:
                decisions.append({'itemId': item['id'], 'status': 'approved', 'parameters': params})
        version = 1
        if decisions:
            d = self.client.post(f'/api/import-plans/{plan_id}/decisions', headers=self.owner_headers(),
                                 json={'baseVersion': version, 'actor': 'owner', 'decisions': decisions})
            self.assertEqual(d.status_code, 200, d.text)
            version = d.json()['version']
        d = self.client.post(f'/api/import-plans/{plan_id}/decisions', headers=self.owner_headers(),
                             json={'baseVersion': version, 'actor': 'owner', 'bulk': {'action': 'approve'}})
        self.assertEqual(d.status_code, 200, d.text)
        version = d.json()['version']
        run = self.client.post(f'/api/import-plans/{plan_id}/runs', headers=self.owner_headers(),
                               json={'version': version, 'mode': 'sync', 'actor': 'owner'})
        self.assertEqual(run.status_code, 200, run.text)
        kept = owner.keep_originals(collection_id)
        return {'collection_id': collection_id, 'plan_id': plan_id, 'summary': planmod.summary(plan_id),
                'items': planmod.get_plan(plan_id, full=True)['items'], 'run': run.json(), 'kept': kept}

    def events(self, event_type: str) -> list[dict]:
        with db.session() as conn:
            rows = conn.execute('SELECT id, ts_start, sender, body, metadata_json, artifact_id FROM events '
                                'WHERE event_type=? ORDER BY id', (event_type,)).fetchall()
        return [dict(r) | {'metadata': json.loads(r['metadata_json'])} for r in rows]

    def media_refs(self) -> list[dict]:
        with db.session() as conn:
            return [dict(r) for r in conn.execute(
                'SELECT event_id, reference_name, status, artifact_id FROM event_media_references ORDER BY id')]

    def importers(self, result) -> dict:
        return {k: v['items'] for k, v in result['summary']['by_importer'].items()}

    def assert_all_done(self, result, importers: dict):
        self.assertEqual(self.importers(result), importers)
        self.assertEqual(result['summary']['by_status'].get('done'), sum(importers.values()), result['summary'])

    def times(self, event_type='whatsapp_message'):
        return [(e['ts_start'], e['metadata']['timestamp']['status']) for e in self.events(event_type)]


class WhatsAppLineFormatTests(ExportFormatBase):
    """Android/iOS chat line formats across locales; each fixture is uploaded as the phone shares it."""

    def test_android_nl_dash_24h_as_plain_txt(self):
        # Fix: "WhatsApp-chat met …" (NL, hyphen) was not a chat name, so a .txt shared alone was not detected.
        r = self.run_upload([('WhatsApp-chat met Testcontact.txt',
                              b'14-10-2026 10:42 - Testcontact: Hallo\n14-10-2026 10:43 - Testcontact: tweede\nregel twee\n')])
        self.assert_all_done(r, {'whatsapp_export': 1})
        events = self.events('whatsapp_message')
        self.assertEqual([e['body'] for e in events], ['Hallo', 'tweede\nregel twee'])
        self.assertEqual(self.times(), [('2026-10-14T10:42:00', 'parsed_local_time'), ('2026-10-14T10:43:00', 'parsed_local_time')])
        self.assertEqual(events[0]['metadata']['timestamp']['timezone_status'], 'unknown')
        # "14" can only be a day, so the file itself confirms day-first; no per-line ambiguity flag.
        self.assertEqual(events[0]['metadata']['timestamp']['date_order_status'], 'inferred_from_file_first_field_above_12')
        self.assertFalse(events[0]['metadata']['timestamp']['date_order_ambiguous'])

    def test_android_pl_dot_24h_as_plain_txt_with_polish_markers(self):
        # Fix: "Czat WhatsApp z …" (PL) was not a chat name either.
        text = ('29.10.2026, 21:41 - Testowy Kontakt: cześć\n'
                '29.10.2026, 21:42 - Testowy Kontakt: <Pominięto multimedia>\n'
                '29.10.2026, 21:43 - Testowy Kontakt: IMG-20261029-WA0001.jpg (załączono plik)\n')
        r = self.run_upload([('Czat WhatsApp z Testowy Kontakt.txt', text.encode())])
        self.assert_all_done(r, {'whatsapp_export': 1})
        self.assertEqual([t for t, _ in self.times()], ['2026-10-29T21:41:00', '2026-10-29T21:42:00', '2026-10-29T21:43:00'])
        meta = self.events('whatsapp_message')[0]['metadata']['timestamp']
        self.assertEqual((meta['date_order'], meta['date_order_status'], meta['date_order_ambiguous']),
                         ('day_first', 'inferred_from_file_first_field_above_12', False))
        self.assertEqual([(m['reference_name'], m['status']) for m in self.media_refs()],
                         [(None, 'omitted_by_export'), ('IMG-20261029-WA0001.jpg', 'missing_from_export')])

    def test_android_en_slash_comma_24h_with_system_message_and_media_omitted(self):
        text = ('29/10/2026, 21:41 - Messages and calls are end-to-end encrypted. No one outside of this chat, '
                'not even WhatsApp, can read or listen to them. Tap to learn more.\n'
                '29/10/2026, 21:42 - Test Contact: hello\nsecond line\n\nfourth line\n'
                '29/10/2026, 21:43 - Test Contact: <Media omitted>\n')
        r = self.run_upload([('WhatsApp Chat with Test Contact.zip', make_zip({'WhatsApp Chat with Test Contact.txt': text}))])
        self.assert_all_done(r, {'whatsapp_export': 1})
        events = self.events('whatsapp_message')
        self.assertEqual([e['sender'] for e in events], [None, 'Test Contact', 'Test Contact'])
        self.assertEqual(events[1]['body'], 'hello\nsecond line\n\nfourth line')
        self.assertEqual(events[1]['metadata']['parser']['line_end'], 5)
        self.assertEqual([m['status'] for m in self.media_refs()], ['omitted_by_export'])

    def test_android_dot_two_digit_year_stays_untimed(self):
        r = self.run_upload([('WhatsApp Chat mit Testkontakt.txt', b'29.10.26, 21:41 - Testkontakt: hallo\n')])
        self.assert_all_done(r, {'whatsapp_export': 1})
        self.assertEqual(self.times(), [(None, 'unknown_century')])  # the century is not assumed

    def test_us_12h_month_first_with_narrow_no_break_space(self):
        # Fix: version 2 read 10/29/26 day-first, so a valid US date was reported as "invalid".
        text = f'10/29/26, 9:41{NNBSP}PM - Test US: hi\n3/4/26, 9:42{NNBSP}AM - Test US: second\n'
        r = self.run_upload([('WhatsApp Chat with Test US.txt', text.encode())])
        self.assert_all_done(r, {'whatsapp_export': 1})
        self.assertEqual(self.times(), [(None, 'unknown_century'), (None, 'unknown_century')])
        meta = self.events('whatsapp_message')[0]['metadata']['timestamp']
        self.assertEqual((meta['date_order'], meta['raw_ampm']), ('month_first', 'PM'))

    def test_month_first_four_digit_year_is_read_month_first(self):
        text = f'10/29/2026, 9:41{NNBSP}PM - Test US: hi\n11/3/2026, 9:42 AM - Test US: later\n'
        self.run_upload([('WhatsApp Chat with Test US.txt', text.encode())])
        self.assertEqual(self.times(), [('2026-10-29T21:41:00', 'parsed_local_time'), ('2026-11-03T09:42:00', 'parsed_local_time')])

    def test_12h_with_dotted_am_pm_day_first(self):
        text = '29/10/2026, 9:41 p.m. - Test CA: hi\n30/10/2026, 12:05 a.m. - Test CA: midnight\n'
        self.run_upload([('WhatsApp Chat with Test CA.txt', text.encode())])
        self.assertEqual([t for t, _ in self.times()], ['2026-10-29T21:41:00', '2026-10-30T00:05:00'])

    def test_conflicting_date_orders_leave_every_line_untimed(self):
        text = '29/10/2026, 21:41 - A: x\n10/29/2026, 21:42 - A: y\n'
        self.run_upload([('WhatsApp Chat with A.txt', text.encode())])
        self.assertEqual(self.times(), [(None, 'date_order_conflict'), (None, 'date_order_conflict')])

    def test_bom_and_bidi_marks_at_line_start(self):
        text = '﻿' + LRM + '29/10/2026, 21:41 - C: x\n' + LRM + '29/10/2026, 21:42 - C: y\n'
        r = self.run_upload([('WhatsApp Chat with C.txt', text.encode())])
        self.assert_all_done(r, {'whatsapp_export': 1})
        self.assertEqual([e['body'] for e in self.events('whatsapp_message')], ['x', 'y'])

    def test_ios_dot_two_digit_year_with_seconds_and_attached(self):
        text = (f'﻿[29.10.26, 21:41:05] Testkontakt: hallo\n'
                f'{LRM}[29.10.26, 21:41:06] Testkontakt: {LRM}<attached: 00000012-PHOTO-2026-10-29-21-41-06.jpg>\n')
        r = self.run_upload([('WhatsApp Chat - Testkontakt.zip',
                              make_zip({'_chat.txt': text, '00000012-PHOTO-2026-10-29-21-41-06.jpg': JPEG}))])
        self.assert_all_done(r, {'whatsapp_export': 1})
        self.assertEqual(self.times(), [(None, 'unknown_century'), (None, 'unknown_century')])
        refs = self.media_refs()
        self.assertEqual([(m['reference_name'], m['status']) for m in refs], [('00000012-PHOTO-2026-10-29-21-41-06.jpg', 'present')])
        self.assertIsNotNone(refs[0]['artifact_id'])

    def test_ios_us_12h_bracket(self):
        self.run_upload([('_chat.txt', f'[10/29/26, 9:41:05{NNBSP}PM] Test US: hi\n'.encode())])
        self.assertEqual(self.times(), [(None, 'unknown_century')])
        self.assertEqual(self.events('whatsapp_message')[0]['metadata']['timestamp']['date_order'], 'month_first')

    def test_ios_pl_four_digit_year_with_polish_attached_marker(self):
        text = (f'﻿[29.10.2026, 21:41:05] Testowy: cześć\n'
                f'{LRM}[29.10.2026, 21:41:06] Testowy: {LRM}<załączono: 00000013-PHOTO-2026-10-29-21-41-06.jpg>\n')
        self.run_upload([('WhatsApp Chat - Testowy.zip',
                          make_zip({'_chat.txt': text, '00000013-PHOTO-2026-10-29-21-41-06.jpg': JPEG}))])
        self.assertEqual([t for t, _ in self.times()], ['2026-10-29T21:41:05', '2026-10-29T21:41:06'])
        self.assertEqual([m['status'] for m in self.media_refs()], ['present'])


class WhatsAppExportLayoutTests(ExportFormatBase):
    """Media markers in three languages, a nested chat folder and several chats in one upload."""

    def test_file_attached_en_nl_pl_and_omitted_markers(self):
        text = ('29/10/2026, 21:41 - M: IMG-20261029-WA0001.jpg (file attached)\n'
                '29/10/2026, 21:42 - M: PTT-20261029-WA0002.opus (bestand bijgevoegd)\n'
                '29/10/2026, 21:43 - M: DOC-20261029-WA0003.pdf (załączono plik)\nopis dokumentu\n'
                '29/10/2026, 21:44 - M: <Media weggelaten>\n'
                '29/10/2026, 21:45 - M: VID-20261029-WA0004.mp4 (file attached)\n')
        r = self.run_upload([('WhatsApp Chat with M.zip', make_zip({
            'WhatsApp Chat with M.txt': text, 'IMG-20261029-WA0001.jpg': JPEG, 'PTT-20261029-WA0002.opus': OPUS,
            'DOC-20261029-WA0003.pdf': PDF}))])
        self.assert_all_done(r, {'whatsapp_export': 1})
        self.assertEqual([(m['reference_name'], m['status']) for m in self.media_refs()], [
            ('IMG-20261029-WA0001.jpg', 'present'), ('PTT-20261029-WA0002.opus', 'present'),
            ('DOC-20261029-WA0003.pdf', 'present'), (None, 'omitted_by_export'),
            ('VID-20261029-WA0004.mp4', 'missing_from_export')])
        self.assertEqual(self.events('whatsapp_message')[2]['body'], 'DOC-20261029-WA0003.pdf (załączono plik)\nopis dokumentu')

    def test_zip_with_the_chat_in_a_subfolder(self):
        r = self.run_upload([('WhatsApp Chat with Sub.zip', make_zip({
            'WhatsApp Chat with Sub/WhatsApp Chat with Sub.txt': '29/10/2026, 21:41 - Sub: IMG-20261029-WA0001.jpg (file attached)\n',
            'WhatsApp Chat with Sub/IMG-20261029-WA0001.jpg': JPEG}))])
        self.assert_all_done(r, {'whatsapp_export': 1})
        self.assertEqual([m['status'] for m in self.media_refs()], ['present'])

    def chats(self):
        with db.session() as conn:
            return {r['original_name']: r['n'] for r in conn.execute(
                "SELECT a.original_name, COUNT(e.id) AS n FROM events e JOIN artifacts a ON a.id=e.artifact_id "
                "WHERE e.event_type='whatsapp_message' GROUP BY a.id")}

    def test_several_chat_texts_uploaded_together_are_each_imported(self):
        # Fix: one folder unit with two chats needed chat_member, so at most one chat could be imported.
        r = self.run_upload([('WhatsApp Chat with A.txt', b'29/10/2026, 21:41 - A: a\n29/10/2026, 21:42 - A: IMG-20261029-WA0009.jpg (file attached)\n'),
                             ('Czat WhatsApp z B.txt', b'29.10.2026, 21:43 - B: b\n'),
                             ('IMG-20261029-WA0009.jpg', JPEG)])
        self.assert_all_done(r, {'whatsapp_export': 1})
        self.assertEqual(r['summary']['blocked'], {})
        self.assertEqual(self.chats(), {'WhatsApp Chat with A.txt': 2, 'Czat WhatsApp z B.txt': 1})
        self.assertEqual([(m['reference_name'], m['status']) for m in self.media_refs()], [('IMG-20261029-WA0009.jpg', 'present')])
        with db.session() as conn:
            children = {r[0] for r in conn.execute(
                "SELECT a.original_name FROM artifacts a JOIN artifacts p ON p.id=a.parent_artifact_id "
                "WHERE p.original_name LIKE '%.txt'")}
        self.assertNotIn('Czat WhatsApp z B.txt', children)  # another chat is never media of this one
        self.assertNotIn('WhatsApp Chat with A.txt', children)
        with db.session() as conn:
            result = json.loads(conn.execute("SELECT result_json FROM import_plan_attempts WHERE status='done'").fetchone()[0])
        self.assertEqual(sorted(c['messages'] for c in result['importer_result']['chats']), [1, 2])
        self.assertEqual(result['counts']['totals.messages'], 3)

    def test_zip_with_two_chats_imports_both_or_the_chosen_one(self):
        archive = make_zip({'WhatsApp Chat with A.txt': '29/10/2026, 21:41 - A: a\n',
                            'WhatsApp Chat with B.txt': '29/10/2026, 21:42 - B: b\n'})
        self.run_upload([('export.zip', archive)])
        self.assertEqual(self.chats(), {'WhatsApp Chat with A.txt': 1, 'WhatsApp Chat with B.txt': 1})
        self.tearDown()
        self.setUp()
        self.run_upload([('export.zip', archive)], chat_members={'export.zip': {'chat_member': 'WhatsApp Chat with B.txt'}})
        self.assertEqual(self.chats(), {'WhatsApp Chat with B.txt': 1})

    def test_a_chat_imported_by_version_2_keeps_version_2(self):
        from pathlib import Path
        from server.app.importers import whatsapp
        path = Path(self.root) / 'chat.txt'
        path.write_bytes(b'01/02/2026, 10:00 - A: x\n')  # no field above 12: the policy order either way
        first = whatsapp.import_whatsapp_txt(path, source_locator='synthetic://chat')
        self.assertEqual((first['parser_version'], first['date_order']['status']), ('3', 'parser_policy_not_source_locale'))
        with db.session() as conn:  # as an installation that imported this chat before version 3
            for row in conn.execute('SELECT id, metadata_json FROM events WHERE artifact_id=?', (first['artifact_id'],)).fetchall():
                meta = json.loads(row['metadata_json'])
                meta['parser']['version'] = '2'
                conn.execute('UPDATE events SET metadata_json=? WHERE id=?',
                             (json.dumps(meta, ensure_ascii=False, sort_keys=True), row['id']))
        again = whatsapp.import_whatsapp_txt(path, source_locator='synthetic://chat')
        self.assertEqual((again['artifact_id'], again['parser_version'], again['messages']), (first['artifact_id'], '2', 1))
        self.assertEqual(len(self.events('whatsapp_message')), 1)


def mbox_message(i, *, labels='Inbox', extra='', date='Thu, 29 Oct 2026 21:41:05 +0100', body='Tresc testowa.\n'):
    """One Takeout-shaped mbox message: From separator, X-GM-THRID, X-Gmail-Labels, then RFC 5322 headers."""
    return (f'From 18{i:015d}@xxx Thu Oct 29 20:41:05 +0000 2026\n'
            f'X-GM-THRID: 18{i:015d}\nX-Gmail-Labels: {labels}\n'
            f'Message-ID: <m{i}@example.invalid>\nDate: {date}\nFrom: Nadawca <a@example.invalid>\n'
            f'To: b@example.invalid\nSubject: Wiadomosc {i}\n{extra}'
            f'MIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\n\n{body}\n')


MBOX_NAME = 'Takeout/Mail/All mail Including Spam and Trash.mbox'
OUTLOOK_EML = (
    'Received: from EX01.example.invalid (10.0.0.1) by EX02.example.invalid with Microsoft SMTP Server; '
    'Thu, 29 Oct 2026 21:41:05 +0100\r\n'
    'From: =?utf-8?Q?Zbigniew_Testowy?= <z@example.invalid>\r\nTo: Odbiorca <o@example.invalid>\r\n'
    'Subject: =?utf-8?Q?Za=C5=BC=C3=B3=C5=82=C4=87_g=C4=99=C5=9Bl=C4=85_ja=C5=BA=C5=84?=\r\n'
    'Thread-Topic: =?utf-8?Q?Za=C5=BC=C3=B3=C5=82=C4=87?=\r\nThread-Index: AQHZtest==\r\n'
    'Date: Thu, 29 Oct 2026 20:41:05 +0000\r\nMessage-ID: <AM0PR01MB0001@example.invalid>\r\n'
    'Accept-Language: pl-PL, en-US\r\nContent-Language: pl-PL\r\nX-MS-Has-Attach: yes\r\n'
    'X-MS-TNEF-Correlator: <AM0PR01MB0001@example.invalid>\r\n'
    'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="_004_AM0PR_"\r\n\r\n'
    '--_004_AM0PR_\r\nContent-Type: text/plain; charset="utf-8"\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n'
    'Dzie=C5=84 dobry, w za=C5=82=C4=85czniku plik.=\r\n Druga linia.\r\n\r\n'
    '--_004_AM0PR_\r\nContent-Type: application/ms-tnef; name="winmail.dat"\r\nContent-Description: winmail.dat\r\n'
    'Content-Disposition: attachment; filename="winmail.dat"; size=12\r\nContent-Transfer-Encoding: base64\r\n\r\n'
    'eJ8+IgAAAAAAAAAA\r\n\r\n--_004_AM0PR_--\r\n')
THUNDERBIRD_EML = (
    'From - Thu Oct 29 21:41:05 2026\r\nX-Mozilla-Status: 0001\r\nX-Mozilla-Status2: 00000000\r\n'
    'Message-ID: <tb1@example.invalid>\r\nDate: Thu, 29 Oct 2026 21:41:05 +0100\r\nMIME-Version: 1.0\r\n'
    'User-Agent: Mozilla Thunderbird\r\nFrom: T <t@example.invalid>\r\nTo: o@example.invalid\r\n'
    'Subject: =?UTF-8?Q?=C5=81=C3=B3d=C5=BA?=\r\nContent-Type: text/plain; charset=UTF-8; format=flowed\r\n'
    'Content-Transfer-Encoding: quoted-printable\r\n\r\nTre=C5=9B=C4=87\r\n')


class MailExportFormatTests(ExportFormatBase):
    """Google Takeout Mail variants and single .eml files from desktop mail clients."""

    def labels(self):
        return [e['metadata']['gmail']['labels'] for e in self.events('email')]

    def test_takeout_split_in_two_parts_with_the_mbox_in_one(self):
        r = self.run_upload([
            ('takeout-20261029T200000Z-001.zip', make_zip({MBOX_NAME: mbox_message(1) + mbox_message(2),
                                                           'Takeout/archive_browser.html': '<html></html>'})),
            ('takeout-20261029T200000Z-002.zip', make_zip({'Takeout/Drive/notatka.txt': 'notatka'}))])
        self.assert_all_done(r, {'email_mbox': 1})
        self.assertEqual([i['name'] for i in r['items']], [MBOX_NAME])
        self.assertEqual(r['items'][0]['detection']['context'], ['google_takeout'])
        self.assertEqual([e['ts_start'] for e in self.events('email')], ['2026-10-29T20:41:05Z'] * 2)
        with db.session() as conn:
            kept = {row[0] for row in conn.execute('SELECT original_name FROM artifacts')}
        self.assertTrue({'takeout-20261029T200000Z-001.zip', 'takeout-20261029T200000Z-002.zip',
                         'Takeout/Drive/notatka.txt'} <= kept)  # the other part is kept as an original

    def test_mbox_with_crlf_line_endings(self):
        r = self.run_upload([('takeout-crlf.zip', make_zip({MBOX_NAME: (mbox_message(1) + mbox_message(2)).replace('\n', '\r\n')}))])
        self.assert_all_done(r, {'email_mbox': 1})
        events = self.events('email')
        self.assertEqual([e['metadata']['subject']['decoded'] for e in events], ['Wiadomosc 1', 'Wiadomosc 2'])
        self.assertEqual(self.labels(), [['Inbox'], ['Inbox']])
        self.assertEqual([e['metadata']['issues'] for e in events], [[], []])

    def test_non_ascii_gmail_labels_raw_quoted_encoded_and_folded(self):
        # Fix: an encoded-word label stayed "=?UTF-8?Q?…?=" and a folded label kept its line break.
        mbox = (mbox_message(1, labels='Odebrane,Ważne,"Sąd, pisma",Kategoria: Społeczności')
                + mbox_message(2, labels='=?UTF-8?Q?Wa=C5=BCne?=,Otwarte')
                + mbox_message(3, labels='Archiwum,Kategoria:\n Aktualizacje,Sprawa\n =?UTF-8?Q?s=C4=85dowa?='))
        r = self.run_upload([('takeout-labels.zip', make_zip({MBOX_NAME: mbox}))])
        self.assert_all_done(r, {'email_mbox': 1})
        self.assertEqual(self.labels(), [['Odebrane', 'Ważne', 'Sąd, pisma', 'Kategoria: Społeczności'],
                                         ['Ważne', 'Otwarte'], ['Archiwum', 'Kategoria: Aktualizacje', 'Sprawa sądowa']])
        self.assertEqual(self.events('email')[1]['metadata']['gmail']['labels_raw'], '=?UTF-8?Q?Wa=C5=BCne?=,Otwarte')
        self.assertEqual({e['metadata']['parser']['version'] for e in self.events('email')}, {'4'})

    def test_very_long_header_lines(self):
        refs = ' '.join(f'<ref{i}@example.invalid>' for i in range(400))
        # Incompressible filler: a repeated character would trip the catalog's compression-ratio limit.
        huge = ''.join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(24576))  # 1.5 MiB, one line
        extra = f'References: {refs}\nX-Long: {"x" * 20000}\nX-Huge: {huge}\n'
        labels = ','.join(f'Etykieta-{i}' for i in range(500))
        r = self.run_upload([('takeout-long.zip', make_zip({MBOX_NAME: mbox_message(1, extra=extra, labels=labels)
                                                            + mbox_message(2)}))])
        self.assert_all_done(r, {'email_mbox': 1})
        events = self.events('email')
        self.assertEqual(len(events), 2)
        first = events[0]['metadata']
        self.assertEqual(len(first['gmail']['labels']), 500)
        self.assertFalse(first['header_parse']['header_complete'])  # 1.5 MiB > max_header_bytes; flagged, not lost silently
        self.assertEqual(len(first['thread']['references']), 200)  # bounded list, the original keeps all
        self.assertIn('truncated', [h for h in first['headers_raw'] if h[0] == 'X-Long'][0])
        self.assertEqual(events[1]['metadata']['subject']['decoded'], 'Wiadomosc 2')

    def test_mailbox_with_zero_messages(self):
        r = self.run_upload([('takeout-empty.zip', make_zip({MBOX_NAME: b'', 'Takeout/Mail/Pusta etykieta.mbox': b''})),
                             ('Spam.mbox', b'')])
        self.assert_all_done(r, {'email_mbox': 3})
        self.assertEqual(self.events('email'), [])
        with db.session() as conn:
            runs = [json.loads(row[0]) for row in conn.execute(
                "SELECT details_json FROM audit_log WHERE action='import_run' ORDER BY id")]
        self.assertEqual([(d['importer'], d['counts']['messages'], d['coverage']['complete']) for d in runs],
                         [('email_rfc5322', 0, True)] * 3)

    def test_outlook_eml_crlf_quoted_printable_and_winmail_dat(self):
        r = self.run_upload([('Zażółć.eml', OUTLOOK_EML.encode())])
        self.assert_all_done(r, {'email_eml': 1})
        [event] = self.events('email')
        self.assertEqual(event['metadata']['subject']['decoded'], 'Zażółć gęślą jaźń')
        self.assertEqual(event['sender'], 'Zbigniew Testowy <z@example.invalid>')
        self.assertTrue(event['body'].startswith('Dzień dobry, w załączniku plik. Druga linia.'))
        self.assertEqual(event['ts_start'], '2026-10-29T20:41:05Z')
        self.assertEqual([(m['reference_name'], m['status']) for m in self.media_refs()], [('winmail.dat', 'present')])

    def test_thunderbird_eml_starting_with_an_mbox_from_line(self):
        # Fix: the From line made the file sniff as mbox; with the .eml name both importers were primary,
        # so the item was ambiguous and the owner's "approve all" skipped it.
        r = self.run_upload([('Lodz.eml', THUNDERBIRD_EML.encode())])
        self.assert_all_done(r, {'email_eml': 1})
        self.assertEqual([c['importer'] for c in r['items'][0]['candidates']], ['email_eml', 'email_mbox'])
        [event] = self.events('email')
        self.assertEqual((event['metadata']['subject']['decoded'], event['body'].strip(), event['ts_start']),
                         ('Łódź', 'Treść', '2026-10-29T20:41:05Z'))

    def test_a_mailbox_imported_by_version_3_keeps_version_3(self):
        from pathlib import Path
        from server.app.importers import email_mbox
        path = Path(self.root) / 'old.mbox'
        path.write_bytes(mbox_message(1, labels='=?UTF-8?Q?Wa=C5=BCne?=').encode())
        first = email_mbox.import_mbox(path, source_locator='synthetic://old.mbox')
        self.assertEqual(first['parser_version'], '4')
        with db.session() as conn:  # as an installation that imported this mailbox with version 3
            for row in conn.execute("SELECT id, metadata_json FROM events WHERE event_type='email'").fetchall():
                meta = json.loads(row['metadata_json'])
                meta['parser']['version'] = '3'
                meta['gmail']['labels'] = [meta['gmail']['labels_raw']]
                conn.execute('UPDATE events SET metadata_json=? WHERE id=?',
                             (json.dumps(meta, ensure_ascii=False, sort_keys=True), row['id']))
        again = email_mbox.import_mbox(path, source_locator='synthetic://old.mbox')
        self.assertEqual((again['parser_version'], again['messages']), ('3', 1))
        self.assertEqual(len(self.events('email')), 1)

"""Synthetic source occurrences, timestamps and repeatable import integration."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import db, evidence
from server.app.config import settings
from server.app.importers import sms_backup, whatsapp


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        test_settings = replace(
            settings, data_dir=self.root, db_path=self.root / 'evidence.db',
            store_dir=self.root / 'store',
        )
        for module in (db, evidence):
            patcher = patch.object(module, 'settings', test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()

    def source(self, name, text):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def events(self):
        with db.session() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM events ORDER BY id')]

    def test_sms_reimport_preserves_two_identical_source_occurrences(self):
        row = '<sms date="0" address="+31000000" type="1" body="same" />'
        path = self.source('sms.xml', f'<smses>{row}{row}</smses>')
        original = path.read_bytes()
        first = sms_backup.import_sms_backup(path)
        before = self.events()
        second = sms_backup.import_sms_backup(path)
        self.assertEqual(first, second)
        self.assertEqual(first['sms'], 2)
        self.assertEqual(before, self.events())
        self.assertEqual(len(before), 2)
        self.assertNotEqual(before[0]['external_id'], before[1]['external_id'])
        self.assertEqual(before[0]['ts_start'], '1970-01-01T00:00:00+00:00')
        self.assertIsNone(before[0]['confidence'])
        self.assertEqual(path.read_bytes(), original)
        raw = json.loads(before[0]['metadata_json'])
        self.assertEqual(raw['raw_attributes']['date'], '0')
        self.assertEqual(raw['parser']['version'], sms_backup.PARSER_VERSION)

    def test_sms_stable_context_ignores_transient_upload_path(self):
        content = '<smses><sms date="0" body="same" /></smses>'
        first = self.source('upload-one.xml', content)
        second = self.source('upload-two.xml', content)
        result_one = sms_backup.import_sms_backup(
            first, source_locator='upload:sms:backup.xml', original_name='backup.xml',
        )
        result_two = sms_backup.import_sms_backup(
            second, source_locator='upload:sms:backup.xml', original_name='backup.xml',
        )
        self.assertEqual(result_one['artifact_id'], result_two['artifact_id'])
        self.assertEqual(len(self.events()), 1)

    def test_separate_sms_source_context_is_retained(self):
        path = self.source('sms.xml', '<smses><sms date="0" body="same" /></smses>')
        first = sms_backup.import_sms_backup(path, source_locator='acquisition:one')
        second = sms_backup.import_sms_backup(path, source_locator='acquisition:two')
        self.assertNotEqual(first['artifact_id'], second['artifact_id'])
        self.assertEqual(len(self.events()), 2)

    def test_sms_missing_invalid_and_out_of_range_dates_do_not_get_times(self):
        path = self.source('sms.xml', '''<smses>
          <sms body="missing" />
          <sms date="bad" body="invalid" />
          <sms date="99999999999999999999999" body="out of range" />
        </smses>''')
        result = sms_backup.import_sms_backup(path)
        self.assertEqual(result['sms'], 3)
        self.assertEqual(len(result['issues']), 3)
        for event in self.events():
            self.assertIsNone(event['ts_start'])
            timestamp = json.loads(event['metadata_json'])['timestamp']
            self.assertIn(timestamp['status'], {'missing', 'invalid'})
            self.assertIsNone(timestamp['timezone'])

    def test_calls_bad_or_missing_duration_do_not_fabricate_end(self):
        path = self.source('calls.xml', '''<calls>
          <call date="0" duration="60" number="one" />
          <call date="0" duration="broken" number="two" />
          <call date="0" number="three" />
          <call date="0" duration="-4" number="four" />
          <call date="invalid" duration="60" number="five" />
        </calls>''')
        result = sms_backup.import_sms_backup(path)
        events = self.events()
        self.assertEqual(result['calls'], 5)
        self.assertEqual(events[0]['ts_end'], '1970-01-01T00:01:00+00:00')
        for event in events[1:]:
            self.assertIsNone(event['ts_end'])
        self.assertEqual(json.loads(events[1]['metadata_json'])['raw_attributes']['duration'], 'broken')

    def test_mms_keeps_raw_nontext_parts_and_text(self):
        path = self.source('mms.xml', '''<smses><mms date="0" address="person">
          <parts><part ct="text/plain" text="hello" />
          <part ct="image/jpeg" data="raw-reference" /></parts>
        </mms></smses>''')
        sms_backup.import_sms_backup(path)
        event = self.events()[0]
        self.assertEqual(event['body'], 'hello')
        self.assertEqual(json.loads(event['metadata_json'])['raw_parts'][1]['data'], 'raw-reference')

    def test_whatsapp_am_pm_midnight_noon_and_dotted_marker(self):
        path = self.source('chat.txt', '''26/09/2026, 8:15 PM - Klb: evening
26/09/2026, 12:00 AM - Klb: midnight
26/09/2026, 12:00 PM - Klb: noon
26/09/2026, 8:15 p.m. - Klb: dotted
''')
        result = whatsapp.import_whatsapp_txt(path)
        self.assertEqual(result['messages'], 4)
        self.assertEqual(
            [row['ts_start'] for row in self.events()],
            ['2026-09-26T20:15:00', '2026-09-26T00:00:00',
             '2026-09-26T12:00:00', '2026-09-26T20:15:00'],
        )
        for event in self.events():
            self.assertIsNone(event['confidence'])
            metadata = json.loads(event['metadata_json'])
            self.assertEqual(metadata['timestamp']['timezone_status'], 'unknown')
            self.assertIsNone(metadata['timestamp']['timezone'])
            self.assertIn('timezone_unknown', metadata['issues'])

    def test_whatsapp_multiline_and_bracket_headers_keep_raw_ranges(self):
        header = '[26/09/2026, 8:15:30 PM] Klb: first'
        path = self.source('chat.txt', header + '\nsecond line\n\n26/09/2026, 21:00 - Other: next\n')
        whatsapp.import_whatsapp_txt(path)
        first, second = self.events()
        self.assertEqual(first['body'], 'first\nsecond line\n')
        self.assertEqual(first['ts_start'], '2026-09-26T20:15:30')
        metadata = json.loads(first['metadata_json'])
        self.assertEqual(metadata['raw_header'], header)
        self.assertEqual(metadata['raw_lines'], [header, 'second line', ''])
        self.assertEqual(metadata['parser']['line_start'], 1)
        self.assertEqual(metadata['parser']['line_end'], 3)
        self.assertEqual(second['sender'], 'Other')

    def test_whatsapp_reimport_preserves_identical_messages(self):
        line = '26/09/2026, 8:15 PM - Klb: same\n'
        path = self.source('chat.txt', line + line)
        original = path.read_bytes()
        first = whatsapp.import_whatsapp_txt(path)
        before = self.events()
        second = whatsapp.import_whatsapp_txt(path)
        self.assertEqual(first, second)
        self.assertEqual(before, self.events())
        self.assertEqual(len(before), 2)
        self.assertNotEqual(before[0]['external_id'], before[1]['external_id'])
        self.assertEqual(path.read_bytes(), original)

    def test_whatsapp_invalid_dates_and_time_remain_records_without_timestamp(self):
        path = self.source('chat.txt', '''26/09/2026, 25:61 - Klb: invalid clock
31/02/2026, 8:15 PM - Klb: invalid date
26/09/2026, 13:15 PM - Klb: invalid 12-hour clock
''')
        result = whatsapp.import_whatsapp_txt(path)
        self.assertEqual(result['messages'], 3)
        for event in self.events():
            self.assertIsNone(event['ts_start'])
            self.assertIn('timestamp_invalid', json.loads(event['metadata_json'])['issues'])

    def test_whatsapp_ambiguous_date_order_and_unparsed_preamble_are_explicit(self):
        path = self.source('chat.txt', 'unparsed preamble\n01/02/2026, 8:15 PM - Klb: ambiguous\n')
        result = whatsapp.import_whatsapp_txt(path)
        metadata = json.loads(self.events()[0]['metadata_json'])
        self.assertTrue(metadata['timestamp']['date_order_ambiguous'])
        self.assertEqual(metadata['timestamp']['date_order_status'], 'parser_policy_not_source_locale')
        self.assertEqual(result['issues'][0]['issues'], ['unparsed_line_before_first_message'])

    def test_whatsapp_two_digit_year_does_not_invent_a_century(self):
        path = self.source('chat.txt', '''26/09/26, 8:15 PM - Klb: unknown century
26/09/99, 8:15 PM - Klb: also unknown
''')
        result = whatsapp.import_whatsapp_txt(path)
        self.assertEqual(result['messages'], 2)
        for event in self.events():
            self.assertIsNone(event['ts_start'])
            metadata = json.loads(event['metadata_json'])
            self.assertEqual(metadata['timestamp']['status'], 'unknown_century')
            self.assertEqual(metadata['timestamp']['two_digit_year_policy'], 'century_not_assumed')
            self.assertIn('timestamp_unknown_century', metadata['issues'])

    def test_whatsapp_stable_context_uses_original_label_not_temporary_name(self):
        content = '26/09/2026, 8:15 PM - Klb: same\n'
        first = self.source('upload-one.txt', content)
        second = self.source('upload-two.txt', content)
        result_one = whatsapp.import_whatsapp_txt(
            first, original_name='chat.txt', source_locator='upload:whatsapp:chat.txt',
        )
        result_two = whatsapp.import_whatsapp_txt(
            second, original_name='chat.txt', source_locator='upload:whatsapp:chat.txt',
        )
        self.assertEqual(result_one['artifact_id'], result_two['artifact_id'])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]['thread_id'], 'chat')

    def test_new_parser_version_does_not_silently_duplicate_logical_events(self):
        path = self.source('sms.xml', '<smses><sms date="0" body="same" /></smses>')
        sms_backup.import_sms_backup(path)
        with patch.object(sms_backup, 'PARSER_VERSION', 'different-version'):
            with self.assertRaises(ValueError):
                sms_backup.import_sms_backup(path)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(json.loads(self.events()[0]['metadata_json'])['parser']['version'], '2')


if __name__ == '__main__':
    unittest.main()

"""Read contract: search coverage, timeline windows with cursors, artifact groups, packets, stubs."""
from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

from server.app import db
from server.app.integrations import read_api
from server.tests.test_integrations_support import IntegrationCase


class ReadApiTests(IntegrationCase):
    def setUp(self):
        super().setUp()
        self.read = self.token()

    def test_contract_descriptor_names_limits_and_exclusions(self):
        contract = self.get('/api/read/v1/contract', self.read).json()
        self.assertEqual(contract['schema'], 'ageds.read/v1')
        self.assertEqual(contract['limits']['search']['max_limit'], 200)
        self.assertEqual(contract['limits']['timeline']['max_limit'], 500)
        self.assertIn('stored_path', contract['never_included'])
        self.assertEqual(contract['authentication']['scheme'], 'Bearer')
        for key in ('search', 'timeline', 'artifact', 'citation_packet', 'reports', 'intent_execute'):
            self.assertIn(key, contract['endpoints'])

    def test_search_keeps_page_semantics_and_errors(self):
        page = self.get('/api/read/v1/search', self.read, q='koniec').json()
        self.assertEqual(page['kind'], 'search')
        self.assertTrue(page['complete'])
        self.assertFalse(page['has_more'])
        kinds = {(item['object_kind']) for item in page['items']}
        self.assertEqual(kinds, {'derived_text', 'event'})
        self.assertIn('koniec', ''.join(item['snippet'] for item in page['items']))
        self.assertEqual(self.get('/api/read/v1/search', self.read, q='"unterminated').status_code, 422)
        self.assertEqual(self.get('/api/read/v1/search', self.read, q='koniec', limit=201).status_code, 422)
        self.assertEqual(self.get('/api/read/v1/search', self.read, q='koniec', limit=0).status_code, 422)
        blank = self.get('/api/read/v1/search', self.read, q='   ').json()
        self.assertEqual((blank['items'], blank['complete']), ([], True))
        with patch.object(read_api, 'search_page', side_effect=read_api.SearchWorkLimitError('private')):
            response = self.get('/api/read/v1/search', self.read, q='koniec')
        self.assertEqual((response.status_code, response.json()['detail']['code']), (503, 'work_limit'))
        self.assertNotIn('private', response.text)
        with db.session() as conn:
            conn.executemany('INSERT INTO search_fts(object_kind,object_id,title,content) VALUES (?,?,?,?)',
                             [('event', 10_000 + i, 'raw', 'needle') for i in range(205)])
        page = self.get('/api/read/v1/search', self.read, q='needle', limit=200).json()
        self.assertEqual((len(page['items']), page['has_more'], page['complete']), (200, True, False))

    def test_timeline_window_cursor_candidates_and_untimed(self):
        window = self.get('/api/read/v1/timeline', self.read, **{'from': '2026-03-01', 'to': '2026-03-01T23:59:59+00:00', 'limit': 2}).json()
        self.assertEqual(window['kind'], 'timeline_window')
        # Raw-string order: '2026-03-01 10:15' (space) sorts before '2026-03-01T08:00:00+00:00' (T).
        # The read API documents this; instant ordering is the timeline area's display rule.
        self.assertEqual([item['id'] for item in window['items']], [self.events[2], self.events[0]])
        self.assertTrue(window['has_more'])
        self.assertIsNotNone(window['next_cursor'])
        self.assertEqual(window['untimed_events_excluded_by_window'], 1)
        self.assertIn('lexicographic', window['window_semantics'])
        whatsapp, sms = window['items']
        self.assertIsNone(whatsapp['display_time'])
        self.assertEqual(whatsapp['event_times'][0]['tz_status'], 'unknown')
        self.assertIsNone(whatsapp['event_times'][0]['utc'])
        self.assertEqual(whatsapp['event_times'][0]['local'], '2026-03-01T10:15:00')
        self.assertIn('<script>', whatsapp['body'], 'source text is data; the JSON client must not render it as HTML')
        self.assertEqual(sms['event_times'][0]['tz_status'], 'explicit_offset')
        self.assertEqual(sms['event_times'][0]['utc'], '2026-03-01T08:00:00Z')
        second = self.get('/api/read/v1/timeline', self.read, **{'from': '2026-03-01', 'to': '2026-03-01T23:59:59+00:00', 'limit': 2, 'cursor': window['next_cursor']}).json()
        self.assertEqual([item['id'] for item in second['items']], [self.events[1]])
        self.assertFalse(second['has_more'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(second['items'][0]['event_times'], [])
        everything = self.get('/api/read/v1/timeline', self.read, limit=500).json()
        self.assertEqual(everything['items'][0]['id'], self.events[3], 'untimed events sort first when no window is given')
        self.assertEqual(everything['items'][0]['time_status'], 'untimed')
        self.assertEqual(everything['items'][0]['metadata']['_status'], 'malformed_json_preserved')
        self.assertIsNone(everything['untimed_events_excluded_by_window'])
        paged = self.get('/api/read/v1/timeline', self.read, limit=1).json()
        ids = [paged['items'][0]['id']]
        while paged['next_cursor']:
            paged = self.get('/api/read/v1/timeline', self.read, limit=1, cursor=paged['next_cursor']).json()
            ids.append(paged['items'][0]['id'])
        self.assertEqual(sorted(ids), sorted(self.events))
        self.assertEqual(len(ids), len(set(ids)), 'keyset pagination neither skips nor repeats')

    def test_timeline_filters_and_invalid_input(self):
        filtered = self.get('/api/read/v1/timeline', self.read, phone='+48 000 000 001', event_type='sms').json()
        self.assertEqual(len(filtered['items']), 3)
        self.assertEqual(filtered['filters'], {'phone': '+48 000 000 001', 'event_type': 'sms'})
        by_artifact = self.get('/api/read/v1/timeline', self.read, artifact_id=self.artifact).json()
        self.assertEqual(by_artifact['items'], [])
        for params in ({'from': 'b', 'to': 'a'}, {'cursor': 'not-a-cursor'}, {'limit': 501}, {'limit': 0}, {'artifact_id': 0},
                       {'cursor': read_api.encode_cursor('x', 1) + 'garbage'}):
            with self.subTest(params=params):
                self.assertEqual(self.get('/api/read/v1/timeline', self.read, **params).status_code, 422)
        with self.assertRaises(read_api.ReadInputError):
            read_api.read_timeline(filters={'unknown': 'x'})

    def test_timeline_truncates_long_text_with_hash(self):
        from server.app import evidence
        long_body = 'x' * (read_api.EVENT_TEXT_MAX_CHARS + 5)
        event_id = evidence.add_event(source_id=self.source, artifact_id=None, event_type='note', ts_start='2026-04-01T00:00:00+00:00', body=long_body)
        window = self.get('/api/read/v1/timeline', self.read, **{'from': '2026-04-01'}).json()
        item = window['items'][0]
        self.assertEqual(item['id'], event_id)
        self.assertTrue(item['body_truncated'])
        self.assertEqual(len(item['body']), read_api.EVENT_TEXT_MAX_CHARS)
        self.assertEqual(item['body_sha256'], hashlib.sha256(long_body.encode()).hexdigest())

    def test_artifact_metadata_groups_content_without_stored_path(self):
        payload = self.get(f'/api/read/v1/artifacts/{self.artifact}', self.read).json()
        self.assertEqual(payload['kind'], 'artifact')
        self.assertNotIn('stored_path', payload['artifact'])
        self.assertNotIn(str(self.root / 'store'), json.dumps(payload))
        self.assertTrue(payload['artifact']['content_available'])
        self.assertEqual(payload['artifact']['sha256'], hashlib.sha256(b'RIFF synthetic wav bytes').hexdigest())
        self.assertEqual([a['id'] for a in payload['content_group']['other_artifacts']], [self.artifact_twin])
        self.assertEqual(payload['content_group']['other_artifacts'][0]['source_locator'], 'file:///b/copy.wav')
        self.assertEqual(len(payload['observations']), 1)
        self.assertEqual(payload['observations'][0]['source_locator'], 'content://a/recording.wav')
        self.assertEqual([v['id'] for v in payload['transcript_versions']], [self.transcript_v2, self.transcript_v1])
        self.assertEqual(payload['counts'], {'events': 0, 'annotations': 1, 'citations': 1})
        self.assertEqual(payload['source']['label'], 'Phone A export')
        self.assertFalse(payload['content_bytes_included'])
        missing = self.get('/api/read/v1/artifacts/999999', self.read)
        self.assertEqual((missing.status_code, missing.json()['detail']['code']), (404, 'not_found'))
        self.assertEqual(self.get('/api/read/v1/artifacts/0', self.read).status_code, 422)

    def test_citation_packet_is_byte_identical_to_exchange_route(self):
        native = self.client.get(f'/api/artifacts/{self.artifact}/citations/{self.anchor["id"]}/packet')
        mine = self.get(f'/api/read/v1/artifacts/{self.artifact}/citations/{self.anchor["id"]}/packet', self.read)
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(mine.content, native.content)
        self.assertEqual(mine.headers['cache-control'], 'no-store')
        packet = json.loads(mine.content)
        self.assertEqual(packet['payload']['anchor']['quote_sha256'], self.anchor['quote_sha256'])
        missing = self.get(f'/api/read/v1/artifacts/{self.artifact_twin}/citations/{self.anchor["id"]}/packet', self.read)
        self.assertEqual((missing.status_code, missing.json()['detail']['code']), (404, 'not_found'))

    def test_reports_stub_is_explicit(self):
        reports = self.get('/api/read/v1/reports', self.read).json()
        self.assertEqual((reports['available'], reports['items']), (False, []))
        self.assertIn('not implemented', reports['reason'])
        self.assertEqual(reports['detected_report_tables'], [])

    def test_reads_never_write_or_touch_sources(self):
        from pathlib import Path
        conn = db.connect()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
        before = self.settings.db_path.read_bytes()
        audit_before = self.count('audit_log')
        with patch('builtins.open', side_effect=AssertionError('no source IO')), patch.object(Path, 'open', side_effect=AssertionError('no source IO')):
            read_api.read_search('koniec')
            read_api.read_timeline(limit=10)
            read_api.read_artifact(self.artifact)
            read_api.read_citation_packet(self.artifact, self.anchor['id'])
            read_api.read_reports()
        conn = db.connect()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
        self.assertEqual(self.settings.db_path.read_bytes(), before)
        self.assertEqual(self.count('audit_log'), audit_before)

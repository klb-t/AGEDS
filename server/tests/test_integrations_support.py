"""Shared synthetic fixture for the integrations tests (no private data, no model).

The corpus plants hard cases: the same bytes under two locators (one content
group, two artifact rows), SMS events with an explicit UTC offset next to
WhatsApp-like local times without a zone, an untimed event, recorded
``event_times`` candidates with ``tz_status='unknown'``, a transcript with
Polish diacritics and a legacy segment carrying NaN word data, a second
transcript version, an annotation and a pinned citation.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import citations, db, evidence, main, timeinfo
from server.app.integrations import tokens

SEGMENTS = [
    {'start': 0.0, 'end': 1.25, 'text': ' Zażółć gęślą jaźń'},
    {'start': 1.25, 'end': 2.5, 'text': ' spotkanie w biurze o dziewiątej'},
    {'start': 2.5, 'end': 3.0, 'text': ' umowa podpisana', 'words': [{'start': float('nan'), 'end': 3.0, 'word': ' umowa podpisana'}]},
    {'start': 3.0, 'end': 4.0, 'text': ' koniec nagrania'},
]


class IntegrationCase(unittest.TestCase):
    """Fresh database per test; ``self.client`` is an authenticated-capable TestClient."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'store').mkdir()
        self.settings = replace(db.settings, data_dir=self.root, db_path=self.root / 'evidence.db', store_dir=self.root / 'store', scan_roots=())
        self.patches = [patch.object(module, 'settings', self.settings) for module in (db, evidence, main)]
        for p in self.patches:
            p.start()
        self.env = patch.dict('os.environ', {'EW_REQUIRE_TOKEN': ''})
        self.env.start()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        self.plant()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.env.stop()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # --- corpus -----------------------------------------------------------------
    def plant(self):
        self.source = evidence.ensure_source('synthetic', 'Phone A export')
        self.source_b = evidence.ensure_source('synthetic', 'Laptop export')
        sample = self.root / 'recording.wav'
        sample.write_bytes(b'RIFF synthetic wav bytes')
        self.artifact = evidence.ingest_file(sample, source_id=self.source, source_locator='content://a/recording.wav', mime_type='audio/wav')
        self.artifact_twin = evidence.ingest_file(sample, source_id=self.source_b, source_locator='file:///b/copy.wav', mime_type='audio/wav')
        xml = self.root / 'messages.xml'
        xml.write_bytes(b'<smses/>')
        self.artifact_sms = evidence.ingest_file(xml, source_id=self.source, source_locator='content://a/messages.xml', mime_type='application/xml')
        self.transcript_v1 = evidence.add_derived_text(self.artifact, 'transcript', ''.join(s['text'] for s in SEGMENTS), model='synthetic', language='pl', segments=SEGMENTS)
        self.transcript_v2 = evidence.add_derived_text(self.artifact, 'transcript', ' nowa wersja', model='synthetic-2', language='pl',
                                                       segments=[{'start': 0.0, 'end': 0.5, 'text': ' nowa wersja'}])
        self.events = []
        rows = [
            ('sms', '2026-03-01T08:00:00+00:00', 'in', '+48 000 000 001', 'Osoba A', 'spotkanie potwierdzone', 'sms-1'),
            ('sms', '2026-03-01T09:30:00+00:00', 'out', '+48 000 000 001', 'Osoba A', 'umowa podpisana wczoraj', 'sms-2'),
            ('whatsapp', '2026-03-01 10:15', 'in', '+31 6 0000 0002', 'Osoba B', 'biuro zamknięte <script>x</script>', None),
            ('call', None, 'in', '+48 000 000 003', None, None, 'call-untimed'),
            ('sms', '2026-03-02T07:00:00+00:00', 'in', '+48 000 000 001', 'Osoba A', 'koniec', 'sms-3'),
        ]
        for event_type, ts, direction, phone, label, body, external in rows:
            self.events.append(evidence.add_event(source_id=self.source, artifact_id=self.artifact_sms, event_type=event_type, ts_start=ts,
                                                  direction=direction, phone_or_address=phone, contact_label=label, body=body, external_id=external))
        with db.session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            timeinfo.record_event_times(conn, self.events[0], [timeinfo.from_iso('sent', '2026-03-01T08:00:00+00:00', 'sms_backup:date')])
            timeinfo.record_event_times(conn, self.events[2], [timeinfo.from_iso('sent', '2026-03-01 10:15', 'whatsapp:line')])
            conn.execute("UPDATE events SET metadata_json='{not json' WHERE id=?", (self.events[3],))
        self.annotation = self.client.post(f'/api/artifacts/{self.artifact}/annotations',
                                           json={'body': 'ważne', 'derivedTextId': self.transcript_v1, 'startMs': 0, 'endMs': 1000}).json()['id']
        self.anchor = citations.create_citation(self.artifact, self.transcript_v1, [1])

    # --- helpers ----------------------------------------------------------------
    def token(self, scopes=('read',), client_id='chatadhd', **kwargs):
        return tokens.create_token('test', scopes, client_id=client_id, **kwargs)

    def headers(self, token):
        return {'Authorization': f'Bearer {token["token"]}'}

    def get(self, path, token, **params):
        return self.client.get(path, headers=self.headers(token), params=params or None)

    def audit(self, action=None):
        with db.session() as conn:
            rows = conn.execute('SELECT * FROM audit_log ORDER BY id').fetchall()
        return [dict(r) for r in rows if action is None or r['action'] == action]

    def count(self, table):
        with db.session() as conn:
            return conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    @staticmethod
    def dump(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

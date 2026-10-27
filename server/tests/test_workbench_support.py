"""Shared synthetic fixture for the workbench tests (generated data only; no private material).

Hard cases planted: the same bytes under two locators, source markup in names
and bodies, Polish diacritics including ł/Ł (which unicode61 does not fold),
a legacy transcript without a processing run next to one with a recorded run,
OCR text, a derived-text kind unknown to the layer classification, an untimed
event, an e-mail-like event with sender/recipients on example.org, an
annotation and a pinned citation.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import citations, config, db, evidence, main

SOURCE_MARKUP = '<img src=x onerror="window.__wbSourceExecuted=1">'
SEGMENTS = [
    {'start': 0.0, 'end': 1.5, 'text': ' Zażółć gęślą jaźń'},
    {'start': 1.5, 'end': 3.0, 'text': ' spotkanie w Łodzi o dziewiątej'},
    {'start': 3.0, 'end': 4.0, 'text': ' ' + SOURCE_MARKUP},
]


class WorkbenchCase(unittest.TestCase):
    """Fresh database per test with a planted corpus; ``self.client`` is a TestClient."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'store').mkdir()
        self.settings = replace(db.settings, data_dir=self.root, db_path=self.root / 'evidence.db',
                                store_dir=self.root / 'store', scan_roots=(self.root / 'sources',))
        self.patches = [patch.object(module, 'settings', self.settings) for module in (db, evidence, main, config)]
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

    def plant(self):
        self.source = evidence.ensure_source('synthetic', 'Phone A export')
        self.source_b = evidence.ensure_source('synthetic', 'Laptop export')
        sample = self.root / 'recording.wav'
        sample.write_bytes(b'RIFF synthetic workbench bytes')
        self.artifact = evidence.ingest_file(sample, source_id=self.source, source_locator='content://a/nagranie-Łódź.wav',
                                             original_name='nagranie <b>Łódź</b>.wav', mime_type='audio/wav')
        self.artifact_twin = evidence.ingest_file(sample, source_id=self.source_b, source_locator='file:///b/copy.wav', mime_type='audio/wav')
        xml = self.root / 'messages.xml'
        xml.write_bytes(b'<smses/>')
        self.artifact_sms = evidence.ingest_file(xml, source_id=self.source, source_locator='content://a/messages.xml', mime_type='application/xml')
        text = ''.join(s['text'] for s in SEGMENTS)
        self.transcript_legacy = evidence.add_derived_text(self.artifact, 'transcript', text, model='synthetic-legacy',
                                                           language='pl', segments=SEGMENTS)
        with db.session() as conn:
            run = conn.execute('''INSERT INTO processing_runs(artifact_id,lease_token,status,started_at,finished_at,tool,model)
                                  VALUES (?,?,?,?,?,?,?)''', (self.artifact, 'synthetic-lease', 'done', '2026-03-01T00:00:00Z',
                                                                '2026-03-01T00:01:00Z', 'synthetic-asr', 'synthetic-v2')).lastrowid
        self.run_id = run
        self.transcript_run = evidence.add_derived_text(self.artifact, 'transcript', ' nowa wersja umowy', model='synthetic-v2',
                                                        language='pl', run_id=run,
                                                        segments=[{'start': 0.0, 'end': 1.0, 'text': ' nowa wersja umowy'}])
        self.ocr = evidence.add_derived_text(self.artifact_sms, 'ocr', 'Faktura numer 7 z Łodzi', model='synthetic-ocr')
        self.unclassified = evidence.add_derived_text(self.artifact_sms, 'speaker_notes', 'notatka mówcy: umowa', model=None)
        rows = [
            ('sms', '2026-03-01T08:00:00+00:00', 'in', '+48 000 000 001', 'Osoba A', None, None, 'umowa podpisana', 'sms-1'),
            ('whatsapp', '2026-03-01 10:15', 'in', '+31 6 0000 0002', 'Osoba B', None, None, 'biuro <script>x</script> zamknięte', None),
            ('email', '2026-03-02T09:00:00+01:00', 'out', None, None, 'osoba@example.org', 'kancelaria@example.net', 'Spotkanie w Łodzi', 'mail-1'),
            ('call', None, 'in', '+48 000 000 003', None, None, None, None, 'call-untimed'),
        ]
        self.events = []
        for event_type, ts, direction, phone, label, sender, recipients, body, external in rows:
            subject = 'Termin' if event_type == 'email' else None
            self.events.append(evidence.add_event(source_id=self.source, artifact_id=self.artifact_sms, event_type=event_type, ts_start=ts,
                                                  direction=direction, phone_or_address=phone, contact_label=label, sender=sender,
                                                  recipients=recipients, subject=subject, body=body, external_id=external))
        self.annotation = self.client.post(f'/api/artifacts/{self.artifact}/annotations',
                                           json={'label': 'Ważne', 'body': 'Łukasz potwierdził spotkanie',
                                                 'derivedTextId': self.transcript_legacy, 'startMs': 0, 'endMs': 1500}).json()['id']
        self.anchor = citations.create_citation(self.artifact, self.transcript_legacy, [1])

    # --- helpers ----------------------------------------------------------------
    def audit(self, action=None):
        with db.session() as conn:
            rows = conn.execute('SELECT * FROM audit_log ORDER BY id').fetchall()
        return [dict(r) | {'details': json.loads(r['details_json'])} for r in rows if action is None or r['action'] == action]

    def count(self, table, where='1', params=()):
        with db.session() as conn:
            return conn.execute(f'SELECT count(*) FROM {table} WHERE {where}', params).fetchone()[0]

    def evidence_digest(self):
        """Hash of every evidence table row; workbench operations must never change it."""
        digest = hashlib.sha256()
        with db.session() as conn:
            for table in ('artifacts', 'source_observations', 'events', 'derived_text', 'annotations', 'evidence_anchors',
                          'processing_runs', 'sources', 'links'):
                for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid'):
                    digest.update(json.dumps([table, list(row)], ensure_ascii=False, default=str).encode('utf-8'))
        return digest.hexdigest()

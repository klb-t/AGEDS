"""Pilot results carried into another installation without a model call (fake transport, generated data)."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

from server.app import db, pilot_transfer, worker
from server.app.jobs import claim_job
from server.app.media import alignment
from server.app.media.queue import enqueue_media_job
from server.app.transcription import enqueue_transcription
from server.tests.test_media_ocr_diarization import png_bytes
from server.tests import test_media_vertex as vertex_tests


class PilotTransferTests(vertex_tests.VertexCase):
    def make_pilot(self) -> tuple[Path, bytes, bytes]:
        self.transport(vertex_tests.VertexAsrTests.SEGMENTS)
        audio = Path(self.corpus['audio_codeswitch']).read_bytes()
        enqueue_transcription(self.add_artifact(audio, 'call.wav', 'audio/wav'), recipe='asr-vertex-gemini')
        worker.process_job(claim_job())
        self.transport({'pages': [{'page': 1, 'lines': ['Kontakt A', 'Hallo 10:42']}],
                        'messages': [{'page': 1, 'sender': 'Kontakt A', 'time': '10:42', 'text': 'Hallo', 'side': 'left'}]})
        image = png_bytes()
        enqueue_media_job('ocr', self.add_artifact(image, 'chat.png', 'image/png'), recipe_id='ocr-vertex-gemini-chat')
        worker.process_job(claim_job())
        pilot = self.root / 'pilot.db'
        shutil.copy(self.settings.db_path, pilot)
        target = self.root / 'production'
        (target / 'store').mkdir(parents=True)
        self.use_settings(db_path=target / 'evidence.db', store_dir=target / 'store', data_dir=target)
        db.init_db()
        self.store = target / 'store'
        return pilot, audio, image

    def test_dry_run_writes_nothing_and_missing_bytes_are_skipped(self):
        pilot, audio, _ = self.make_pilot()
        self.add_artifact(audio, 'call.wav', 'audio/wav')
        report = pilot_transfer.transfer(pilot)
        self.assertEqual([c['kind'] for c in report['copied']], ['transcript'])
        self.assertTrue(report['copied'][0]['dry_run'])
        self.assertEqual([s['reason'][:12] for s in report['skipped']], ['no artifact '])
        self.assertEqual(self.rows('derived_text'), [])

    def test_apply_copies_versions_runs_candidates_once_with_provenance(self):
        pilot, audio, image = self.make_pilot()
        pilot_before = pilot.read_bytes()
        self.add_artifact(audio, 'call.wav', 'audio/wav')
        self.add_artifact(image, 'chat.png', 'image/png')
        first = pilot_transfer.transfer(pilot, apply=True)
        self.assertEqual(len(first['copied']), 2)
        versions = self.rows('derived_text')
        self.assertEqual([v['kind'] for v in versions], ['transcript', 'ocr'])
        runs = self.rows('processing_runs')
        self.assertEqual([(r['tool'], r['model'], r['status']) for r in runs],
                         [('ageds-pilot-transfer', 'gemini-3.5-flash', 'done')] * 2)
        provenance = json.loads(runs[0]['provenance_json'])['transferred_from']
        self.assertEqual(provenance['pilot_db_sha256'], first['pilot_db_sha256'])
        self.assertIn('no new model call', provenance['note'])
        self.assertNotIn('lease_token', provenance['pilot_run'])
        self.assertEqual(json.loads(versions[0]['segments_json'])[0]['speaker_hypothesis'], 'S1')
        candidates = self.rows('event_candidates')
        self.assertEqual([(c['sender_text'], c['derived_text_id']) for c in candidates], [('Kontakt A', versions[1]['id'])])
        self.assertEqual(self.rows('events'), [])
        self.assertEqual(self.rows('provider_calls'), [])  # nothing was sent anywhere
        second = pilot_transfer.transfer(pilot, apply=True)
        self.assertEqual((second['copied'], {s['reason'] for s in second['skipped']}), ([], {'already transferred'}))
        self.assertEqual(len(self.rows('derived_text')), 2)
        self.assertEqual(pilot.read_bytes(), pilot_before)

    def test_acquire_from_source_then_copy_and_refuse_incomplete_source(self):
        pilot, audio, image = self.make_pilot()
        source = self.root / 'uploads'
        source.mkdir()
        (source / 'call.wav').write_bytes(audio)
        with self.assertRaises(ValueError):
            pilot_transfer.acquire_and_transfer(pilot, source, apply=True)
        self.assertEqual(self.rows('artifacts'), [])
        (source / 'chat.png').write_bytes(image)
        (source / 'unrelated.txt').write_bytes(b'not a pilot original')
        plan = pilot_transfer.acquire_and_transfer(pilot, source)
        self.assertEqual((sorted(plan['to_acquire']), plan['source_files_not_in_pilot']), (['call.wav', 'chat.png'], 1))
        self.assertEqual(self.rows('artifacts'), [])
        done = pilot_transfer.acquire_and_transfer(pilot, source, apply=True)
        self.assertEqual((done['acquisition']['ingested'], len(done['transfer']['copied'])), (2, 2))
        self.assertEqual(len(self.rows('artifacts')), 2)  # the unrelated file is catalogued, not ingested
        self.assertEqual(len(self.rows('source_observations')), 2)
        again = pilot_transfer.acquire_and_transfer(pilot, source, apply=True)
        self.assertEqual((again['to_acquire'], again['transfer']['copied']), ([], []))
        self.assertEqual(len(self.rows('derived_text')), 2)
        self.assertEqual(sorted(p.name for p in source.iterdir()), ['call.wav', 'chat.png', 'unrelated.txt'])

    def test_superseded_marks_follow_and_a_changed_pilot_does_not_duplicate(self):
        from server.app.media.version_status import mark, superseded
        self.transport(vertex_tests.VertexAsrTests.SEGMENTS)
        audio = Path(self.corpus['audio_codeswitch']).read_bytes()
        aid = self.add_artifact(audio, 'call.wav', 'audio/wav')
        enqueue_transcription(aid, recipe='asr-vertex-gemini')
        worker.process_job(claim_job())
        enqueue_transcription(aid, recipe='asr-vertex-gemini-aligned')
        with patch.object(alignment, 'faster_whisper_words', side_effect=RuntimeError('offline')):
            worker.process_job(claim_job())
        old, new = [r['id'] for r in self.rows('derived_text')]
        pilot_live = self.settings.db_path
        target = self.root / 'production'
        (target / 'store').mkdir(parents=True)
        pilot = self.root / 'pilot.db'
        shutil.copy(pilot_live, pilot)
        self.use_settings(db_path=target / 'evidence.db', store_dir=target / 'store', data_dir=target)
        db.init_db()
        self.store = target / 'store'
        self.add_artifact(audio, 'call.wav', 'audio/wav')
        first = pilot_transfer.transfer(pilot, apply=True)
        self.assertEqual((len(first['copied']), first['marks']), (2, []))
        # the pilot changes afterwards: a mark is added, so the pilot file hash differs
        conn = sqlite3.connect(pilot)
        conn.row_factory = sqlite3.Row
        mark(conn, old, superseded_by_id=new, reason='validator defect in revision 1', actor='owner')
        conn.commit()
        conn.close()
        second = pilot_transfer.transfer(pilot, apply=True)
        self.assertEqual((second['copied'], len(second['marks'])), ([], 1))
        self.assertEqual(len(self.rows('derived_text')), 2)
        copies = [r['id'] for r in self.rows('derived_text')]
        with db.session() as conn:
            self.assertEqual(superseded(conn), {copies[0]: superseded(conn)[copies[0]]})
            self.assertEqual(superseded(conn)[copies[0]]['superseded_by_id'], copies[1])
        self.assertEqual(pilot_transfer.transfer(pilot, apply=True)['marks'], [])

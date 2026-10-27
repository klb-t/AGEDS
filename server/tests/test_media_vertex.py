"""Vertex Gemini ASR and OCR engines behind the recipe and run contracts (fake transport, generated data)."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from server.app import authz, db, providers, worker
from server.app.jobs import claim_job
from server.app.media import vertex
from server.app.media.queue import enqueue_media_job
from server.app.transcription import enqueue_transcription
from server.tests.test_media_ocr_diarization import pdf_bytes, png_bytes
from server.tests.test_media_support import MediaCase


class FakeVertex:
    def __init__(self, reply):
        self.reply, self.bodies, self.configs = reply, [], []

    def __call__(self, config, body):
        self.configs.append(config)
        self.bodies.append(body)
        return {'candidates': [{'content': {'parts': [{'text': json.dumps(self.reply, ensure_ascii=False)}]}}],
                'usageMetadata': {'promptTokenCount': 900, 'candidatesTokenCount': 120}}


class VertexCase(MediaCase):
    def setUp(self):
        super().setUp()
        with db.session() as conn:
            providers.record_consent(conn, 'vertex', authz.create_scope(conn, 'installation', {}),
                                     granted_by='test', basis_text='synthetic test consent')

    def transport(self, reply):
        fake = FakeVertex(reply)
        patcher = patch.object(providers, 'vertex_transport', fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def calls(self):
        with db.session() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM provider_calls ORDER BY id')]


class VertexAsrTests(VertexCase):
    SEGMENTS = {'segments': [
        {'start': 0.0, 'end': 1.4, 'speaker': 'S1', 'language': 'pl', 'text': 'Dzień dobry, dzwonię w sprawie umowy.'},
        {'start': 1.4, 'end': 2.9, 'speaker': 'S2', 'language': 'nl', 'text': 'Goedemiddag, waarmee kan ik helpen?'}]}

    def test_wav_transcript_is_published_with_model_estimates_and_booked(self):
        fake = self.transport(self.SEGMENTS)
        aid = self.add_audio()
        enqueue_transcription(aid, recipe='asr-vertex-gemini')
        did = worker.process_job(claim_job())
        version = self.rows('derived_text')[0]
        self.assertEqual((version['id'], version['kind'], version['model']), (did, 'transcript', 'gemini-3.5-flash'))
        segments = json.loads(version['segments_json'])
        self.assertEqual([s['speaker_hypothesis'] for s in segments], ['S1', 'S2'])
        self.assertEqual({s['timing_source'] for s in segments}, {'model_estimate'})
        self.assertTrue(all(s['words'] == [] for s in segments))
        metadata = json.loads(version['metadata_json'])
        self.assertEqual(metadata['word_timing']['status'], 'unavailable')
        self.assertIsNone(metadata['transcoding'])  # WAV is sent as it is
        part = fake.bodies[0]['contents'][0]['parts'][0]['inlineData']
        self.assertEqual(part['mimeType'], 'audio/wav')
        self.assertEqual(fake.configs[0]['location'], 'global')
        statuses = [c['status'] for c in self.calls()]
        self.assertEqual(statuses, ['reserved', 'ok'])
        with db.session() as conn:
            self.assertAlmostEqual(providers.spent_today_eur(conn), (900 * 1.5 + 120 * 9.0) / 1e6)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg needed for the transcoding case')
    def test_unsupported_format_is_transcoded_in_memory_and_recorded(self):
        fake = self.transport(self.SEGMENTS)
        wav = Path(self.corpus['audio_codeswitch'])
        target = self.root / 'generated.m4a'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(wav), '-c:a', 'aac', str(target)],
                       check=True, capture_output=True, timeout=120)
        m4a = target.read_bytes()
        aid = self.add_artifact(m4a, 'call.m4a', 'audio/mp4')
        enqueue_transcription(aid, recipe='asr-vertex-gemini')
        worker.process_job(claim_job())
        metadata = json.loads(self.rows('derived_text')[0]['metadata_json'])
        self.assertEqual(metadata['transcoding']['to'], 'audio/flac')
        self.assertEqual(fake.bodies[0]['contents'][0]['parts'][0]['inlineData']['mimeType'], 'audio/flac')
        with db.session() as conn:  # the original bytes are untouched
            stored = conn.execute('SELECT stored_path, sha256 FROM artifacts WHERE id=?', (aid,)).fetchone()
        import hashlib
        self.assertEqual(hashlib.sha256(Path(stored['stored_path']).read_bytes()).hexdigest(), stored['sha256'])

    def test_without_consent_nothing_is_sent_or_published(self):
        fake = self.transport(self.SEGMENTS)
        with db.session() as conn:
            for row in conn.execute('SELECT uid FROM provider_consents').fetchall():
                providers.revoke_consent(conn, row['uid'], actor='test')
        aid = self.add_audio()
        enqueue_transcription(aid, recipe='asr-vertex-gemini')
        with self.assertRaises(vertex.VertexUnavailable):
            worker.process_job(claim_job())
        self.assertEqual((fake.bodies, self.rows('derived_text')), ([], []))


class VertexOcrTests(VertexCase):
    def test_chat_screenshot_lines_and_candidates(self):
        fake = self.transport({'pages': [{'page': 1, 'lines': ['Kontakt A', 'Dzień dobry 10:42']}],
                               'messages': [{'page': 1, 'sender': 'Kontakt A', 'time': '10:42', 'text': 'Dzień dobry',
                                             'side': 'left'}]})
        aid = self.add_artifact(png_bytes(), 'screenshot.png', 'image/png')
        enqueue_media_job('ocr', aid, recipe_id='ocr-vertex-gemini-chat')
        did = worker.process_job(claim_job())
        version = self.rows('derived_text')[0]
        self.assertEqual((version['id'], version['kind'], version['text']), (did, 'ocr', 'Kontakt A\nDzień dobry 10:42'))
        segments = json.loads(version['segments_json'])
        self.assertEqual([(s['line_index'], s['box'], s['source']) for s in segments],
                         [(0, None, 'vertex_model'), (1, None, 'vertex_model')])
        candidates = self.rows('event_candidates')
        self.assertEqual([(c['sender_text'], c['time_text'], c['body_text'], c['layout_side']) for c in candidates],
                         [('Kontakt A', '10:42', 'Dzień dobry', 'left')])
        self.assertEqual(self.rows('events'), [])  # candidates are never events
        self.assertEqual(fake.bodies[0]['contents'][0]['parts'][0]['inlineData']['mimeType'], 'image/png')

    def test_pdf_text_layer_first_and_model_only_for_pages_without_text(self):
        fake = self.transport({'pages': [{'page': 2, 'lines': ['skan strony drugiej']}]})
        full = self.add_artifact(pdf_bytes(['Strona pierwsza z warstwa tekstowa', 'Strona druga z warstwa tekstowa']),
                                 'pismo.pdf', 'application/pdf')
        enqueue_media_job('ocr', full, recipe_id='ocr-vertex-gemini')
        worker.process_job(claim_job())
        self.assertEqual(fake.bodies, [])  # every page has a text layer: no model call
        version = self.rows('derived_text')[0]
        self.assertIn('Strona pierwsza z warstwa tekstowa', version['text'])
        self.assertEqual({s['source'] for s in json.loads(version['segments_json'])}, {'pdf_text_layer'})
        mixed = self.add_artifact(pdf_bytes(['Strona pierwsza z warstwa tekstowa', '']), 'skan.pdf', 'application/pdf')
        enqueue_media_job('ocr', mixed, recipe_id='ocr-vertex-gemini')
        worker.process_job(claim_job())
        self.assertEqual(len(fake.bodies), 1)
        second = [r for r in self.rows('derived_text') if r['artifact_id'] == mixed][0]
        sources = {(s['page'], s['source']) for s in json.loads(second['segments_json'])}
        self.assertEqual(sources, {(1, 'pdf_text_layer'), (2, 'vertex_model')})

    def test_recipe_validation(self):
        from server.app.media.recipe import RecipeError, effective_recipe
        for overrides in ({'engine': 'vertex-gemini:'}, {'psm': 4}, {'pdf': {'rasterizer': 'pdftoppm', 'dpi': 200, 'max_pages': 1}}):
            with self.subTest(overrides=overrides), self.assertRaises(RecipeError):
                from server.app.media.recipe import validate_effective
                effective = dict(effective_recipe('ocr-vertex-gemini', kind='ocr')['effective'], **overrides)
                validate_effective(effective)


if __name__ == '__main__':
    unittest.main()


class PilotReportTests(VertexCase):
    SEGMENTS = VertexAsrTests.SEGMENTS

    def test_report_separates_layers_and_counts_from_the_database(self):
        import importlib.util
        import sqlite3
        spec = importlib.util.spec_from_file_location('pilot_report', Path(__file__).resolve().parents[2] / 'scripts' / 'pilot_report.py')
        report_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(report_module)
        self.transport(self.SEGMENTS)
        aid = self.add_audio()
        enqueue_transcription(aid, recipe='asr-vertex-gemini')
        worker.process_job(claim_job())
        conn = sqlite3.connect(f'file:{self.settings.db_path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        report = report_module.collect(conn)
        text = report['artifacts'][0]['derived_texts'][0]
        self.assertEqual((text['layer'], text['model'], text['speakers']), ('model output', 'gemini-3.5-flash', ['S1', 'S2']))
        self.assertEqual(report['provider']['calls'], 1)
        self.assertIn('no word timing', ' '.join(report['gaps']))
        self.assertIn('wynik modelu', report_module.markdown(report))

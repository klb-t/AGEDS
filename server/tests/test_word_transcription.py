"""Synthetic adapter checks: no model, network or recording is required."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server.app import db, transcription, worker
from server.app.jobs import claim_job


def fixture():
    return [{'start': 0, 'end': 1, 'text': ' Żółć\n!', 'words': [
        {'start': 0.0004, 'end': .52345, 'word': ' Żółć', 'probability': .123456789},
        {'start': .52345, 'end': .987654, 'word': '\n!', 'probability': None}]}]


class WordTimingCapabilityTests(unittest.TestCase):
    def test_report_preserves_raw_unicode_whitespace_floats_and_probability(self):
        data = fixture()
        before = copy.deepcopy(data)
        report = transcription.word_timing_capabilities(data)
        self.assertEqual(data, before)
        self.assertEqual(report['status'], 'available')
        self.assertEqual(report['source_resolution'], 'as_supplied_by_asr')
        self.assertEqual(report['alignment_verification'], 'not_performed')
        self.assertFalse(report['raw_words_modified'])
        self.assertEqual(report['segments'], [{'segment_index': 0, 'word_selection_available': True, 'word_count': 2}])

    def test_legacy_missing_or_broken_word_data_is_unavailable_not_precise(self):
        for words in (None, [], [{'start': 0, 'end': float('nan'), 'word': 'wrong'}]):
            data = fixture()
            data[0]['words'] = words
            report = transcription.word_timing_capabilities(data)
            self.assertEqual(report['status'], 'unavailable')
            self.assertIn('reason', report['segments'][0])
        for segments in (None, {}, 'x', []):
            self.assertEqual(transcription.word_timing_capabilities(segments)['status'], 'unavailable')

    def test_mixed_capabilities_and_bounded_coverage_are_explicit(self):
        data = fixture() + [{'start': 1, 'end': 2, 'text': 'legacy'}]
        report = transcription.word_timing_capabilities(data)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['available_segments'], 1)
        with patch.object(transcription, 'MAX_TIMING_REPORT_SEGMENTS', 1):
            report = transcription.word_timing_capabilities(fixture() * 2)
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['coverage_complete'])
        self.assertEqual(report['examined_segments'], 1)
        self.assertEqual(report['total_segments'], 2)

    def test_malformed_segments_are_reported_without_repair(self):
        for segment in (None, [], 'x', {}, {'start': False, 'end': 1, 'text': 'word'}):
            report = transcription.word_timing_capabilities([segment])
            self.assertEqual(report['status'], 'unavailable')
            self.assertFalse(report['segments'][0]['word_selection_available'])

    def test_faster_whisper_boundary_preserves_exact_raw_fields(self):
        raw = fixture()[0]
        captured = {}
        class FakeModel:
            def __init__(self, *args, **kwargs):
                pass
            def transcribe(self, path, **kwargs):
                captured.update(kwargs)
                result = SimpleNamespace(**{**raw, 'words': [SimpleNamespace(**word) for word in raw['words']]})
                info = SimpleNamespace(language='pl', language_probability=.9, transcription_options={'synthetic': True})
                return iter([result]), info
        with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=FakeModel)}):
            result = worker.FasterWhisperAdapter().transcribe('synthetic:not-read')
        self.assertEqual(result.segments, [raw])
        self.assertEqual(captured, {'word_timestamps': True, 'vad_filter': True})
        self.assertEqual(result.metadata['transcript_confidence'], 'unknown')

    def test_model_numeric_scalars_keep_word_timing_available_before_and_after_storage(self):
        # Models emit numpy.float64; float subclass reproduces the strict-type
        # boundary without adding NumPy or a model to the offline test suite.
        class ModelFloat(float):
            pass
        raw = fixture()[0]
        segment = SimpleNamespace(start=ModelFloat(raw['start']), end=ModelFloat(raw['end']),
            text=raw['text'], words=[SimpleNamespace(**{**w, 'start': ModelFloat(w['start']),
                'end': ModelFloat(w['end']), 'probability': ModelFloat(w['probability'])
                if w['probability'] is not None else None}) for w in raw['words']])
        class Model:
            def __init__(self, *args, **kwargs): pass
            def transcribe(self, *args, **kwargs):
                return iter([segment]), SimpleNamespace(language='pl', language_probability=ModelFloat(.9))
        with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Model)}):
            result = worker.FasterWhisperAdapter().transcribe('synthetic:not-read')
        self.assertEqual(result.segments, [raw])
        self.assertIs(type(result.segments[0]['start']), float)
        self.assertIs(type(result.segments[0]['words'][0]['start']), float)
        self.assertIs(type(result.metadata['language_probability']), float)
        before = transcription.word_timing_capabilities(result.segments)
        after = transcription.word_timing_capabilities(json.loads(json.dumps(result.segments)))
        self.assertEqual(before['status'], 'available')
        self.assertEqual(before, after)
        self.assertEqual(worker._json_number(True), True)
        self.assertIs(type(worker._json_number(True)), bool)
        self.assertEqual(worker._json_number('0.5'), '0.5')


class WorkerWordTimingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config = replace(db.settings, db_path=self.root / 'evidence.db')
        patcher = patch.object(db, 'settings', config)
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        audio = self.root / 'synthetic.wav'
        audio.write_bytes(b'not a decoded recording')
        with db.session() as conn:
            self.aid = conn.execute('INSERT INTO artifacts(original_name,mime_type,stored_path,sha256) VALUES (?,?,?,?)',
                ('synthetic.wav', 'audio/wav', str(audio), hashlib.sha256(audio.read_bytes()).hexdigest())).lastrowid

    def run_result(self, data):
        class Adapter:
            def describe(self):
                return {'model': 'synthetic', 'provider': 'test'}
            def transcribe(self, path):
                return worker.TranscriptionResult('display text', 'pl', data, {'word_timing': {'status': 'untrusted claim'}})
        transcription.queue_transcription(self.aid)
        tid = worker.process_job(claim_job(), Adapter())
        with db.session() as conn:
            return dict(conn.execute('SELECT * FROM derived_text WHERE id=?', (tid,)).fetchone())

    def test_published_word_metadata_is_computed_from_exact_saved_words(self):
        data = fixture()
        row = self.run_result(data)
        self.assertEqual(json.loads(row['segments_json']), data)
        metadata = json.loads(row['metadata_json'])
        self.assertEqual(metadata['word_timing']['status'], 'available')
        self.assertEqual(metadata['transcript_confidence'], 'unknown')
        self.assertIsNone(row['confidence'])

    def test_legacy_or_conflicting_words_stay_saved_without_fake_precision(self):
        data = fixture()
        data[0]['words'][0]['word'] = 'mismatch'
        row = self.run_result(data)
        self.assertEqual(json.loads(row['segments_json']), data)
        self.assertEqual(json.loads(row['metadata_json'])['word_timing']['status'], 'unavailable')
        row = self.run_result([{'start': 0, 'end': 1, 'text': 'legacy result'}])
        self.assertEqual(json.loads(row['metadata_json'])['word_timing']['status'], 'unavailable')

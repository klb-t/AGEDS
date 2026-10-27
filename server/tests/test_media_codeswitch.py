"""Per-segment language strategy on synthetic tones (NOT speech) with a fake faster-whisper."""
from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import patch

from server.app import citations, jobs, worker
from server.app.media import recipe as recipes
from server.app.media.strategies import (CodeSwitchingAdapter, Window, normalize_regions, plan_windows,
                                         restrict_language_probabilities)
from server.app.transcription import enqueue_transcription, word_timing_capabilities
from server.tests.corpus import audio
from server.tests.test_media_support import MediaCase


class StrategyHelperTests(MediaCase):
    def test_corpus_part_is_labelled_synthetic_tones_not_speech(self):
        from server.tests.corpus import build_corpus
        manifest = build_corpus(self.root / 'registry', ('audio',))['audio']['manifest']
        self.assertIn('NOT speech', manifest['label'])
        planted = manifest['files']['codeswitch.wav']['planted_cases']
        self.assertEqual(['pl', 'nl', 'en'], planted['three_languages'])
        self.assertEqual(6.0, planted['window_straddles_switch']['switch_at'])

    def test_regions_are_clipped_sorted_and_merged_without_inventing_speech(self):
        raw = [{'start': 900, 'end': 2000}, {'start': -5, 'end': 100}, {'start': 50, 'end': 300},
               {'start': 5000, 'end': 5000}, {'start': 1900, 'end': 99999}]
        self.assertEqual([(0, 300), (900, 3000)], normalize_regions(raw, 3000))

    def test_windows_are_bounded_equal_splits(self):
        sr = 16000
        windows = plan_windows([(0, 3 * sr), (4 * sr, 16 * sr + 1)], max_window_s=6.0)
        self.assertEqual([(0, 48000), (64000, 128000), (128000, 192000), (192000, 256001)],
                         [(w.start_sample, w.end_sample) for w in windows])
        self.assertTrue(all(w.end - w.start <= 6.0 for w in windows[:2]))
        self.assertEqual([0, 1, 1, 1], [w.region_index for w in windows])
        self.assertEqual([0, 1, 2, 3], [w.index for w in windows])
        self.assertEqual([], plan_windows([], max_window_s=6.0))
        self.assertEqual(Window(0, 0, 0, 16000).end, 1.0)

    def test_restriction_keeps_raw_report_and_renormalizes_over_allowed(self):
        report = [('de', 0.5), ('nl', 0.3), ('pl', 0.1), ('xx', float('nan')), ('fr', 0.1)]
        lid = restrict_language_probabilities(report, ['pl', 'nl', 'en'], 2)
        self.assertEqual([{'language': 'de', 'probability': 0.5}, {'language': 'nl', 'probability': 0.3}], lid['raw_top_k'])
        self.assertEqual('nl', lid['chosen'])
        self.assertAlmostEqual(0.75, lid['allowed_distribution']['nl'])
        self.assertAlmostEqual(0.25, lid['allowed_distribution']['pl'])
        self.assertEqual(0.0, lid['allowed_distribution']['en'])
        self.assertIsNone(lid['allowed_reported']['en'])
        self.assertIn('not transcript correctness', lid['meaning'])
        tie = restrict_language_probabilities([('en', 0.4), ('pl', 0.4)], ['pl', 'nl', 'en'], 5)
        self.assertEqual('pl', tie['chosen'])
        none = restrict_language_probabilities([('de', 0.9)], ['pl', 'nl'], 5)
        self.assertIsNone(none['chosen'])
        self.assertEqual('no_allowed_language_reported', none['status'])
        self.assertEqual({'pl': None, 'nl': None}, none['allowed_distribution'])


class CodeSwitchAdapterTests(MediaCase):
    def run_adapter(self, overrides=None):
        self.fake = self.fake_whisper()
        record = recipes.effective_recipe('per-segment-pl-nl-en', overrides=overrides)
        with open(self.corpus['audio_codeswitch'], 'rb') as stream:
            return CodeSwitchingAdapter(record).transcribe(stream)

    def test_language_is_forced_per_window_with_word_timestamps_and_no_window_vad(self):
        result = self.run_adapter()
        self.assertEqual({'model': 'small', 'device': 'cpu', 'compute_type': 'int8'}, self.fake.named('WhisperModel')[0])
        vad = self.fake.named('get_speech_timestamps')
        self.assertEqual(1, len(vad))
        self.assertEqual((500, 200), (vad[0]['options']['min_silence_duration_ms'], vad[0]['options']['speech_pad_ms']))
        calls = self.fake.named('transcribe')
        self.assertEqual(['pl', 'nl', 'en'], [c['language'] for c in calls])
        self.assertTrue(all(c['word_timestamps'] is True and c['vad_filter'] is False and c['input'] == 'array'
                            and c['condition_on_previous_text'] is False for c in calls))
        self.assertEqual([67200, 67200, 70400], [c['samples'] for c in calls])
        self.assertEqual(3, len(self.fake.named('detect_language')))
        self.assertEqual('mul', result.language)
        self.assertEqual(['pl', 'nl', 'en'], result.metadata['languages'])
        self.assertEqual([[0.8, 9.2], [10.3, 14.7]], result.metadata['vad']['regions'])
        self.assertEqual({'unit': 'audio_second', 'amount': 18.0, 'basis': 'decoded_samples_at_16000_hz'}, result.work)
        self.assertEqual('complete', result.metadata['coverage']['status'])

    def test_window_straddling_a_switch_gets_one_language_and_both_distributions_are_kept(self):
        result = self.run_adapter()
        straddle = result.metadata['windows'][1]
        self.assertEqual((5.0, 9.2), (straddle['start'], straddle['end']))
        self.assertEqual('nl', straddle['chosen_language'])
        self.assertEqual(['nl', 'pl', 'de'], [item['language'] for item in straddle['raw_top_k']])
        self.assertAlmostEqual(0.1, straddle['raw_top_k'][2]['probability'])
        self.assertNotIn('de', straddle['allowed_distribution'])
        self.assertAlmostEqual(1.0, sum(v for v in straddle['allowed_distribution'].values()))
        self.assertAlmostEqual(0.75, straddle['allowed_distribution']['nl'])
        window_segments = [s for s in result.segments if s['window']['index'] == 1]
        self.assertTrue(all(s['language'] == 'nl' for s in window_segments))
        # The 1 s of the pl tone inside this window was decoded under forced nl:
        # the documented limit, visible rather than corrected.
        self.assertTrue(window_segments[0]['text'].startswith(' nl~pl nl~pl nl'))
        segment = window_segments[0]
        self.assertAlmostEqual(0.675, segment['language_probability'])
        self.assertAlmostEqual(0.75, segment['language_probability_allowed'])
        self.assertEqual('per_segment_language', segment['strategy'])
        self.assertIn('not transcript correctness', segment['language_probability_meaning'])
        self.assertEqual(['nl', 'pl', 'en'], [a['language'] for a in segment['language_alternatives']])

    def test_times_are_file_time_words_validate_and_quiet_speech_is_dropped(self):
        result = self.run_adapter()
        self.assertEqual('available', word_timing_capabilities(result.segments)['status'])
        self.assertEqual(0.8, result.segments[0]['words'][0]['start'])
        last_end = max(s['end'] for s in result.segments)
        self.assertLess(last_end, 15.5)          # the quiet pl segment (15.5-17.0 s) produced nothing
        for left, right in zip(result.segments, result.segments[1:]):
            self.assertLessEqual(left['end'], right['start'])
        for segment in result.segments:
            window = segment['window']
            self.assertGreaterEqual(segment['start'], window['start'])
            self.assertLessEqual(segment['end'], window['end'])
        self.assertTrue(any('not resolved' in item for item in result.metadata['limitations']))
        self.assertTrue(any('VAD can drop' in item for item in result.metadata['limitations']))

    def test_window_limit_is_partial_coverage_not_silent(self):
        result = self.run_adapter({'limits': {'max_windows': 1}})
        self.assertEqual({'status': 'partial', 'windows_planned': 3, 'windows_processed': 1,
                          'reason': 'limits.max_windows reached'}, result.metadata['coverage'])
        self.assertEqual({'pl'}, {s['language'] for s in result.segments})

    def test_no_allowed_language_falls_back_to_model_detection_and_is_marked(self):
        def only_decoy(samples):
            return 'de', 0.9, [('de', 0.9)]
        with patch.object(audio, 'fake_language_report', only_decoy):
            result = self.run_adapter()
        calls = self.fake.named('transcribe')
        self.assertTrue(all(c['language'] is None for c in calls))
        self.assertTrue(all(w['outside_allowed'] and w['chosen_language'] is None for w in result.metadata['windows']))
        self.assertEqual({'model_auto_fallback'}, {s['language_selection'] for s in result.segments})
        self.assertIsNone(result.segments[0]['language_probability_allowed'])


class CodeSwitchWorkerTests(MediaCase):
    def setUp(self):
        super().setUp()
        self.fake = self.fake_whisper()
        self.aid = self.add_audio()

    def test_two_recipes_give_two_jobs_two_runs_two_versions(self):
        legacy = enqueue_transcription(self.aid)
        switched = enqueue_transcription(self.aid, recipe='per-segment-pl-nl-en')
        self.assertNotEqual(legacy['job_id'], switched['job_id'])
        first = worker.process_job(jobs.claim_job())
        second = worker.process_job(jobs.claim_job())
        self.assertIsNone(jobs.claim_job())
        versions = self.rows('derived_text')
        self.assertEqual([first, second], [v['id'] for v in versions])
        self.assertEqual(['transcript', 'transcript'], [v['kind'] for v in versions])
        runs = self.rows('processing_runs')
        self.assertEqual(2, len(runs))
        self.assertEqual([r['id'] for r in runs], [v['run_id'] for v in versions])
        strategies = [json.loads(r['provenance_json'])['recipe']['effective']['strategy'] for r in runs]
        self.assertEqual(['auto_per_file', 'per_segment_language'], strategies)
        self.assertEqual(['pl', 'mul'], [v['language'] for v in versions])
        metadata = json.loads(versions[1]['metadata_json'])
        self.assertEqual('per_segment_language', metadata['strategy'])
        self.assertEqual('available', metadata['word_timing']['status'])
        self.assertEqual('unknown', metadata['transcript_confidence'])

    def test_word_citation_works_on_a_code_switching_version(self):
        enqueue_transcription(self.aid, recipe='per-segment-pl-nl-en')
        did = worker.process_job(jobs.claim_job())
        segments = json.loads(self.rows('derived_text')[0]['segments_json'])
        last_word = len(segments[2]['words']) - 1
        # Crosses a segment boundary inside window 1 (the window straddling the switch).
        refs = [{'segment_index': 2, 'word_index': last_word}, {'segment_index': 3, 'word_index': 0}]
        anchor = citations.create_citation(self.aid, did, word_refs=refs)
        self.assertEqual(segments[2]['words'][-1]['word'] + segments[3]['words'][0]['word'], anchor['quote_text'])
        self.assertEqual('words', anchor['selector']['kind'])
        self.assertEqual(round(segments[2]['words'][-1]['start'] * 1000), anchor['start_ms'])
        # Across windows 0 -> 1 as well.
        refs = [{'segment_index': 1, 'word_index': len(segments[1]['words']) - 1}, {'segment_index': 2, 'word_index': 0}]
        anchor = citations.create_citation(self.aid, did, word_refs=refs)
        self.assertTrue(math.isclose(anchor['end_ms'], round(segments[2]['words'][0]['end'] * 1000)))

    def test_late_code_switching_worker_cannot_publish_and_replacement_uses_the_same_pin(self):
        import time
        from server.app import db
        from server.app.jobs import LeaseLost
        queued = enqueue_transcription(self.aid, recipe='per-segment-pl-nl-en')
        replacement = []
        record = recipes.recipe_from_job(self.rows('jobs')[0] | {'kind': 'transcribe'})

        class Reclaimed(CodeSwitchingAdapter):
            def transcribe(self, audio):
                with db.session() as conn:
                    conn.execute("UPDATE jobs SET lease_expires_at=? WHERE status='running'", (time.time() - 1,))
                replacement.append(jobs.claim_job(worker_id='replacement'))
                return super().transcribe(audio)
        with self.assertRaises(LeaseLost):
            worker.process_job(jobs.claim_job(), Reclaimed(record))
        self.assertEqual([], self.rows('derived_text'))
        worker.process_job(replacement[0])
        runs = self.rows('processing_runs')
        self.assertEqual(['abandoned', 'done'], [r['status'] for r in runs])
        self.assertEqual({queued['recipe']['sha256']},
                         {json.loads(r['provenance_json'])['recipe']['sha256'] for r in runs})
        self.assertEqual('injected_adapter_bound_to_same_recipe',
                         json.loads(runs[0]['provenance_json'])['recipe']['application'])
        self.assertEqual(1, len(self.rows('derived_text')))

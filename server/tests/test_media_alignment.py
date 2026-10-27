"""Word times for Gemini text from a timing reference (generated data; fake engines, no network)."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from server.app import db, worker
from server.app.citations import validated_segment_words
from server.app.jobs import claim_job
from server.app.media import alignment
from server.app.media.recipe import RecipeError, effective_recipe, validate_effective
from server.app.transcription import enqueue_transcription
from server.tests import test_media_vertex as vertex_tests


def ref_words(*items):
    return [{'word': w, 'start': s, 'end': e, 'probability': 0.9} for w, s, e in items]


SEGMENTS = [{'start': 0.0, 'end': 2.0, 'text': 'Goedemiddag, met wie spreek ik?', 'speaker_hypothesis': 'S1',
             'language': 'nl', 'words': [], 'timing_source': 'model_estimate'},
            {'start': 2.0, 'end': 4.0, 'text': ' I am calling about the contract.', 'speaker_hypothesis': 'S2',
             'language': 'en', 'words': [], 'timing_source': 'model_estimate'}]
REFERENCE = ref_words((' Goedemiddag,', 0.4, 1.1), (' met', 1.1, 1.3), (' wie', 1.3, 1.5), (' spreekt', 1.5, 1.8),
                      (' ik?', 1.8, 2.0), (' I', 2.6, 2.7), (' am', 2.7, 2.8), (' colling', 2.8, 3.1),
                      (' the', 3.3, 3.4), (' contract.', 3.4, 3.9))


class AlignTests(unittest.TestCase):
    def test_split_words_join_exactly(self):
        for text in ('Dzień dobry', '  dwa  spacje  ', 'jeden', '', ' x\n y '):
            self.assertEqual(''.join(alignment.split_words(text)), text)

    def test_exact_fuzzy_and_interpolated_words_take_reference_times(self):
        out, report = alignment.align(SEGMENTS, REFERENCE)
        first, second = out
        self.assertEqual((first['start'], first['end'], first['timing_source']), (0.4, 2.0, 'reference_alignment'))
        self.assertEqual(first['model_estimate'], {'start': 0.0, 'end': 2.0})
        self.assertEqual([w['match'] for w in first['words']], ['exact', 'exact', 'exact', 'fuzzy', 'exact'])
        self.assertEqual(first['words'][0]['word'], 'Goedemiddag,')
        about = second['words'][3]
        self.assertEqual((about['word'], about['match'], about['start'], about['end'], about['bound']),
                         (' about', 'interpolated', 3.1, 3.3, {'start': 3.1, 'end': 3.3}))
        self.assertEqual(second['words'][2]['match'], 'fuzzy')  # calling ~ colling
        for segment in out:  # the citation contract accepts them unchanged
            self.assertEqual(''.join(w['word'] for w in validated_segment_words(segment)), segment['text'])
        self.assertEqual((report['segments_aligned'], report['exact'], report['fuzzy'], report['interpolated']), (2, 8, 2, 1))
        self.assertEqual(report['segments'][1]['delta_start'], 0.6)
        self.assertEqual(SEGMENTS[0]['words'], [])  # input untouched

    def test_consecutive_unmatched_words_share_the_gap_in_order(self):
        segment = [{'start': 0.0, 'end': 3.0, 'text': 'een twee drie vier', 'words': []}]
        out, _ = alignment.align(segment, ref_words((' een', 0.0, 0.5), (' x', 0.5, 0.6), (' vier', 2.5, 3.0)))
        self.assertEqual([(w['match'], w['start'], w['end']) for w in out[0]['words']],
                         [('exact', 0.0, 0.5), ('interpolated', 0.5, 1.5), ('interpolated', 1.5, 2.5), ('exact', 2.5, 3.0)])
        validated_segment_words(out[0])

    def test_segment_without_enough_matches_keeps_model_estimate(self):
        segments = SEGMENTS + [{'start': 4.0, 'end': 6.0, 'text': 'Całkiem inny tekst tutaj.', 'words': [],
                                'timing_source': 'model_estimate'}]
        out, report = alignment.align(segments, REFERENCE)
        self.assertEqual((out[2]['words'], out[2]['timing_source'], out[2]['start']), ([], 'model_estimate', 4.0))
        self.assertIn('alignment_note', out[2])
        self.assertEqual(report['segments_model_estimate'], 1)
        self.assertEqual(report['untimed'], 4)

    def test_empty_reference_aligns_nothing(self):
        out, report = alignment.align(SEGMENTS, [])
        self.assertEqual([s['words'] for s in out], [[], []])
        self.assertEqual(report['segments_aligned'], 0)


class AlignedRecipeTests(vertex_tests.VertexCase):
    REPLY = {'segments': [{'start': s['start'], 'end': s['end'], 'speaker': s['speaker_hypothesis'],
                           'language': s['language'], 'text': s['text']} for s in SEGMENTS]}

    def test_aligned_recipe_publishes_citable_words_and_the_comparison(self):
        self.transport(self.REPLY)
        info = {'engine': 'faster-whisper', 'model': 'small', 'revision': 'r', 'language': 'nl', 'segments': []}
        with patch.object(alignment, 'faster_whisper_words', return_value=(REFERENCE, info)) as reference:
            enqueue_transcription(self.add_audio(), recipe='asr-vertex-gemini-aligned')
            worker.process_job(claim_job())
        self.assertEqual(reference.call_args.args[1]['model'], 'large-v3-turbo')
        version = self.rows('derived_text')[0]
        metadata = json.loads(version['metadata_json'])
        self.assertEqual(metadata['timing_alignment']['status'], 'aligned')
        self.assertEqual(metadata['timing_alignment']['reference']['engine'], 'faster-whisper')
        self.assertEqual(metadata['word_timing']['status'], 'available')
        segments = json.loads(version['segments_json'])
        self.assertEqual([s['timing_source'] for s in segments], ['reference_alignment'] * 2)
        run = self.rows('processing_runs')[0]
        self.assertIn('timing_reference', json.loads(run['parameters_json'])['recipe']['model_options'])

    def test_reference_failure_keeps_the_model_text_and_records_why(self):
        self.transport(self.REPLY)
        with patch.object(alignment, 'faster_whisper_words', side_effect=RuntimeError('model not downloaded')):
            enqueue_transcription(self.add_audio(), recipe='asr-vertex-gemini-aligned')
            worker.process_job(claim_job())
        version = self.rows('derived_text')[0]
        metadata = json.loads(version['metadata_json'])
        self.assertEqual(metadata['timing_alignment']['status'], 'unavailable')
        self.assertIn('model not downloaded', metadata['timing_alignment']['reason'])
        self.assertEqual({s['timing_source'] for s in json.loads(version['segments_json'])}, {'model_estimate'})

    def test_chirp_over_sixty_seconds_is_not_sent(self):
        sent = []
        with self.assertRaises(Exception):
            alignment.google_speech_words(b'x', {'model': 'chirp_2', 'location': 'europe-west4', 'language_codes': ['auto'],
                                                 'price_usd_per_minute': 0.016}, duration=61.0, artifact_id=None,
                                          transport=lambda ref, body: sent.append(body))
        self.assertEqual((sent, self.calls()), ([], []))

    def test_chirp_word_offsets_are_parsed_and_booked(self):
        reply = {'results': [{'languageCode': 'nl-nl', 'alternatives': [{'transcript': 'Goedemiddag met', 'words': [
            {'word': 'Goedemiddag', 'startOffset': '0.400s', 'endOffset': '1.100s', 'confidence': 0.9},
            {'word': 'met', 'startOffset': '1.100s', 'endOffset': '1.300s'}]}]}]}
        words, info = alignment.google_speech_words(
            b'audio', {'model': 'chirp_2', 'location': 'europe-west4', 'language_codes': ['auto'], 'price_usd_per_minute': 0.016},
            duration=30.0, artifact_id=None, transport=lambda ref, body: reply)
        self.assertEqual([(w['word'], w['start'], w['end']) for w in words], [('Goedemiddag', 0.4, 1.1), ('met', 1.1, 1.3)])
        self.assertEqual([c['status'] for c in self.calls()], ['reserved', 'ok'])
        with db.session() as conn:
            self.assertAlmostEqual(conn.execute('SELECT SUM(cost_eur) FROM provider_calls').fetchone()[0], 0.008)

    def test_recipe_validation(self):
        base = effective_recipe('asr-vertex-gemini-aligned')['effective']
        validate_effective(base)
        validate_effective(effective_recipe('asr-vertex-gemini-chirp')['effective'])
        bad = [{'model_options': {}},
               {'model_options': {'timing_reference': {'engine': 'whisper.cpp'}}},
               {'model_options': {'timing_reference': {**base['model_options']['timing_reference'], 'beam': 5}}},
               {'model_options': {'other': 1}}, {'word_timestamps': 'yes'}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(RecipeError):
                validate_effective(dict(base, **change))
        validate_effective(dict(base, word_timestamps=False))  # reference configured but switched off


if __name__ == '__main__':
    unittest.main()


class ForcedAlignRecipeTests(vertex_tests.VertexCase):
    REPLY = AlignedRecipeTests.REPLY

    def test_letters_fold_diacritics_and_drop_symbols(self):
        from server.app.media.forced_align import _letters
        alphabet = set("abcdefghijklmnopqrstuvwxyz'")
        self.assertEqual([_letters(w, alphabet) for w in ('Źdźbło,', ' [niezrozumiałe]', '10:42', "I'm")],
                         ['zdzblo', 'niezrozumiale', '', "i'm"])

    def test_forced_preset_publishes_scored_words_and_records_the_reference(self):
        from server.app.media import forced_align
        self.transport(self.REPLY)
        def fake(data, segments, ref):
            out = []
            for i, seg in enumerate(segments):
                raws = alignment.split_words(seg['text'])
                words = [{'word': raw, 'start': i * 2 + k * 0.2, 'end': i * 2 + k * 0.2 + 0.1, 'probability': None,
                          'score': 0.9, 'match': 'forced'} for k, raw in enumerate(raws)]
                out.append({**seg, 'model_estimate': {'start': seg['start'], 'end': seg['end']}, 'words': words,
                            'start': words[0]['start'], 'end': words[-1]['end'], 'timing_source': 'forced_alignment'})
            return out, {'segments_aligned': len(out), 'segments_model_estimate': 0, 'min_score': ref['min_score'],
                         'segments': [], 'reference': {'engine': 'mms-forced-align', 'model': 'torchaudio.pipelines.MMS_FA'}}
        with patch.object(forced_align, 'align', side_effect=fake) as called:
            enqueue_transcription(self.add_audio(), recipe='asr-vertex-gemini-forced')
            worker.process_job(claim_job())
        self.assertEqual(called.call_args.args[2]['min_score'], 0.7)
        version = self.rows('derived_text')[0]
        metadata = json.loads(version['metadata_json'])
        self.assertEqual((metadata['timing_alignment']['status'], metadata['word_timing']['status']), ('aligned', 'available'))
        self.assertEqual({s['timing_source'] for s in json.loads(version['segments_json'])}, {'forced_alignment'})

    def test_missing_torch_keeps_the_text(self):
        from server.app.media import forced_align
        self.transport(self.REPLY)
        with patch.object(forced_align, 'align', side_effect=ImportError('No module named torchaudio')):
            enqueue_transcription(self.add_audio(), recipe='asr-vertex-gemini-forced')
            worker.process_job(claim_job())
        metadata = json.loads(self.rows('derived_text')[0]['metadata_json'])
        self.assertEqual(metadata['timing_alignment']['status'], 'unavailable')
        self.assertIn('torchaudio', metadata['timing_alignment']['reason'])

    def test_forced_recipe_validation(self):
        base = effective_recipe('asr-vertex-gemini-forced')['effective']
        validate_effective(base)
        ref = base['model_options']['timing_reference']
        for change in ({'model': 'wav2vec2'}, {'model_sha256': 'ABC'}, {'min_score': 2}, {'audio_filter': 'amovie=/etc/passwd'}):
            with self.subTest(change=change), self.assertRaises(RecipeError):
                validate_effective(dict(base, model_options={'timing_reference': {**ref, **change}}))


class NotationTests(unittest.TestCase):
    @staticmethod
    def segments(raw, aligned):
        return [{'start': a if a is not None else r, 'end': None, 'text': 'x',
                 'words': [{'word': 'x'}] if a is not None else [],
                 'timing_source': 'reference_alignment' if a is not None else 'model_estimate',
                 'model_estimate': {'start': r, 'end': r}} for r, a in zip(raw, aligned)]

    def test_confirmed_switch_adds_only_a_derived_field(self):
        segs = self.segments([10.0, 30.0, 55.0, 1.02, 1.3, 1.45, 2.05], [10.2, 30.1, 55.3, 62.5, 89.0, 105.5, 125.1])
        result = alignment.notation_hypothesis(segs, reference='faster-whisper')
        self.assertEqual((result['status'], result['first_segment'], result['checked'], result['within_tolerance']),
                         ('confirmed', 3, 4, 4))
        self.assertEqual(segs[4]['model_estimate'], {'start': 1.3, 'end': 1.3})  # Gemini's values stay
        self.assertEqual(segs[4]['model_estimate_reinterpreted']['start'], 90.0)
        self.assertNotIn('model_estimate_reinterpreted', segs[2])

    def test_unconfirmed_switch_is_a_hypothesis_only(self):
        segs = self.segments([10.0, 55.0, 1.02, 1.3, 1.45, 2.05], [10.2, 55.3, 75.0, 89.0, 130.0, 160.0])
        result = alignment.notation_hypothesis(segs, reference='faster-whisper')
        self.assertEqual(result['status'], 'hypothesis_only')
        self.assertTrue(all('model_estimate_reinterpreted' not in s for s in segs))
        self.assertFalse(segs[3]['timing_notation_hypothesis']['confirmed'])

    def test_no_drop_or_impossible_seconds_detect_nothing(self):
        self.assertEqual(alignment.notation_hypothesis(self.segments([1.0, 5.0, 9.0], [1, 5, 9]), reference='r')['status'],
                         'not_detected')
        self.assertEqual(alignment.notation_hypothesis(self.segments([30.0, 1.75, 2.1], [30, 105, 130]), reference='r')['status'],
                         'not_consistent')  # .75 cannot be seconds


class HybridTests(vertex_tests.VertexCase):
    REPLY = {'segments': AlignedRecipeTests.REPLY['segments'] + [
        {'start': 4.0, 'end': 6.0, 'speaker': 'S1', 'language': 'pl', 'text': 'Całkiem inny tekst tutaj.'}]}

    @staticmethod
    def forced(intervals):
        def fake(data, segments, ref):
            out = []
            for seg, interval in zip(segments, intervals):
                raws = alignment.split_words(seg['text'])
                if interval is None:
                    out.append({**seg, 'words': []})
                    continue
                start, end = interval
                step = (end - start) / len(raws)
                words = [{'word': raw, 'start': round(start + k * step, 3), 'end': round(start + (k + 1) * step, 3),
                          'probability': None, 'score': 0.9, 'match': 'forced'} for k, raw in enumerate(raws)]
                out.append({**seg, 'words': words, 'start': start, 'end': end, 'timing_source': 'forced_alignment'})
            return out, {'segments_aligned': sum(1 for i in intervals if i), 'min_score': ref['min_score'],
                         'reference': {'engine': 'mms-forced-align'}}
        return fake

    def run_hybrid(self, intervals):
        from server.app.media import forced_align
        self.transport(self.REPLY)
        info = {'engine': 'faster-whisper', 'model': 'large-v3-turbo', 'segments': []}
        with patch.object(alignment, 'faster_whisper_words', return_value=(REFERENCE, info)), \
                patch.object(forced_align, 'align', side_effect=self.forced(intervals)):
            enqueue_transcription(self.add_audio(), recipe='asr-vertex-gemini-hybrid')
            worker.process_job(claim_job())
        version = self.rows('derived_text')[0]
        return json.loads(version['segments_json']), json.loads(version['metadata_json'])

    def test_unaligned_segment_is_filled_and_marked_unconfirmed(self):
        segments, metadata = self.run_hybrid([(0.4, 2.0), (2.6, 3.9), (4.1, 5.8)])
        self.assertEqual([s['timing_source'] for s in segments],
                         ['reference_alignment', 'reference_alignment', 'forced_alignment_unconfirmed'])
        self.assertIs(segments[2]['independent_confirmation'], False)
        self.assertEqual(metadata['timing_alignment']['fill']['filled_segments'], [2])
        self.assertEqual(metadata['word_timing']['available_segments'], 3)

    def test_overlapping_fill_is_rejected(self):
        segments, metadata = self.run_hybrid([None, None, (3.0, 5.0)])  # would start before the previous end 3.9
        self.assertEqual(segments[2]['words'], [])
        self.assertEqual(metadata['timing_alignment']['fill']['rejected_overlapping'], [2])

    def test_hybrid_validation(self):
        base = effective_recipe('asr-vertex-gemini-hybrid')['effective']
        validate_effective(base)
        ref = base['model_options']['timing_reference']
        for change in ({'primary': ref['fill']}, {'fill': ref['primary']}, {'extra': 1}):
            with self.subTest(change=change), self.assertRaises(RecipeError):
                validate_effective(dict(base, model_options={'timing_reference': {**ref, **change}}))

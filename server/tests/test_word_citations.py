"""Word selectors retain literal ASR tokens and reject invented precision."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import citations, db, evidence


def segments():
    return [
        {'start': 0, 'end': 1, 'text': ' Zażółć  gęślą', 'words': [
            {'start': 0.1234, 'end': 0.5, 'word': ' Zażółć', 'probability': 0.123},
            {'start': 0.5, 'end': 0.9994, 'word': '  gęślą', 'probability': 0.9}]},
        {'start': 1.1, 'end': 2, 'text': '\nJaźń!', 'words': [
            {'start': 1.1001, 'end': 1.8, 'word': '\nJaźń', 'probability': None},
            {'start': 1.8, 'end': 1.8005, 'word': '!', 'probability': 0}]},
    ]


def refs(*pairs):
    return [{'segment_index': s, 'word_index': w} for s, w in pairs]


class WordProjectionTests(unittest.TestCase):
    def test_exact_unicode_spacing_source_seconds_and_nearest_ms(self):
        data = segments()
        before = copy.deepcopy(data)
        chosen = refs((0, 1), (1, 0))
        projection = citations.projection_from_words(data, chosen)
        self.assertEqual(projection['quote_text'], '  gęślą\nJaźń')
        self.assertEqual(projection['quote_sha256'], hashlib.sha256('  gęślą\nJaźń'.encode()).hexdigest())
        self.assertEqual((projection['start_ms'], projection['end_ms']), (500, 1800))
        self.assertEqual(projection['selector']['precision'], 'word_asr')
        self.assertEqual(projection['selector']['source_start'], 0.5)
        self.assertEqual(projection['selector']['alignment_verification'], 'not_performed')
        self.assertEqual(data, before)
        chosen[0]['word_index'] = 999
        self.assertEqual(projection['selector']['word_refs'][0]['word_index'], 1)

    def test_single_zero_duration_word_does_not_invent_duration(self):
        data = [{'start': 1, 'end': 1, 'text': 'x', 'words': [{'start': 1, 'end': 1, 'word': 'x'}]}]
        result = citations.projection_from_words(data, refs((0, 0)))
        self.assertEqual((result['start_ms'], result['end_ms']), (1000, 1000))

    def test_invalid_reference_types_order_duplicates_gaps_and_bounds(self):
        cases = [None, [], {}, (), 'x', [None], [{'segment_index': 0}],
                 [{'segment_index': 0, 'word_index': 0, 'other': 1}],
                 refs((True, 0)), refs((0, False)), refs((0, 0.0)), refs((0, '0')),
                 refs((-1, 0)), refs((0, -1)), refs((10**1000, 0)), refs((0, 10**1000)),
                 refs((0, 0), (0, 0)), refs((0, 1), (0, 0)), refs((0, 0), (1, 0)),
                 refs((0, 1), (1, 1)), refs((1, 1), (0, 0))]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                citations.projection_from_words(segments(), value)

    def test_selection_limit_checked_before_iterating_invalid_refs(self):
        with patch.object(citations, 'MAX_SELECTED_WORDS', 2):
            with self.assertRaisesRegex(ValueError, 'selection limit'):
                citations.projection_from_words(segments(), [None] * 3)

    def test_missing_words_never_fall_back_to_segment_precision(self):
        for value in (None, [], {}, 'words'):
            data = segments()
            data[0]['words'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                citations.projection_from_words(data, refs((0, 0)))
            self.assertEqual(citations.projection_from_segments(data, [0])['selector']['precision'], 'segment')

    def test_text_mismatch_or_normalization_is_not_silently_repaired(self):
        for text in ('Zażółć  gęślą', ' Zażółć gęślą', ' different', ' Zażółć  gęślą'):
            data = segments()
            data[0]['text'] = text
            before = copy.deepcopy(data)
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'exactly match'):
                citations.projection_from_words(data, refs((0, 1)))
            self.assertEqual(data, before)

    def test_invalid_word_text_and_container(self):
        for value in (None, {}, 'text', {'word': 3}, {'word': ''}, {'word': ' \n'}):
            data = segments()
            data[0]['words'][0] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                citations.projection_from_words(data, refs((0, 1)))

    def test_invalid_times_and_segment_boundaries(self):
        cases = [(float('nan'), .5), (0, float('inf')), (-1, .5), (True, .5),
                 ('0', .5), (None, .5), (.6, .5), (0, 10**1000), (0, 1.1)]
        for start, end in cases:
            data = segments()
            data[0]['words'][0].update(start=start, end=end)
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                citations.projection_from_words(data, refs((0, 0)))
        data = segments()
        data[0]['start'] = .2
        with self.assertRaisesRegex(ValueError, 'outside'):
            citations.projection_from_words(data, refs((0, 0)))

    def test_overlap_and_reordered_times_rejected_even_outside_selected_word(self):
        data = segments()
        data[0]['words'][1]['start'] = .49
        with self.assertRaisesRegex(ValueError, 'overlap'):
            citations.projection_from_words(data, refs((0, 0)))
        data = segments()
        data[1]['start'] = .8
        with self.assertRaisesRegex(ValueError, 'segment time ranges overlap'):
            citations.projection_from_words(data, refs((0, 1), (1, 0)))

    def test_unselected_legacy_segments_do_not_block_a_valid_local_selection(self):
        data = segments()
        data[1]['words'] = None
        self.assertEqual(citations.projection_from_words(data, refs((0, 1)))['quote_text'], '  gęślą')

    def test_per_segment_work_limit_is_explicit(self):
        with patch.object(citations, 'MAX_WORDS_PER_SEGMENT', 1):
            with self.assertRaisesRegex(ValueError, 'validation limit'):
                citations.projection_from_words(segments(), refs((0, 0)))

    def test_dispatcher_accepts_legacy_and_words_but_rejects_tampered_semantics(self):
        data = segments()
        for projection in (citations.projection_from_segments(data, [0]),
                           citations.projection_from_words(data, refs((0, 1), (1, 0)))):
            self.assertEqual(citations.projection_from_selector(data, projection['selector']), projection)
            for key, value in [('precision', 'verified_word'), ('rounding', 'floor'), ('extra', 'ignored')]:
                selector = dict(projection['selector'], **{key: value})
                with self.subTest(key=key), self.assertRaises(ValueError):
                    citations.projection_from_selector(data, selector)
        for selector in (None, [], {}, {'kind': 'future'}):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                citations.projection_from_selector(data, selector)


class StoredWordCitationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        settings = replace(db.settings, db_path=self.path / 'evidence.db', data_dir=self.path, store_dir=self.path / 'store')
        patcher = patch.object(db, 'settings', settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        with db.session() as conn:
            self.aid = conn.execute("INSERT INTO artifacts(original_name,mime_type) VALUES ('fake.wav','audio/wav')").lastrowid
        self.tid = evidence.add_derived_text(self.aid, 'transcript', 'non-authoritative display text', segments=segments())

    def counts(self):
        with db.session() as conn:
            return tuple(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                         for table in ('evidence_anchors', 'audit_log'))

    def test_old_version_is_pinned_after_newer_words_are_published(self):
        old = citations.create_citation(self.aid, self.tid, word_refs=refs((0, 1)), quote_text='  gęślą')
        newer = evidence.add_derived_text(self.aid, 'transcript', 'changed', segments=[{'start': 0, 'end': 2, 'text': 'changed'}])
        self.assertNotEqual(old['derived_text_id'], newer)
        again = citations.create_citation(self.aid, self.tid, word_refs=refs((0, 1)))
        self.assertEqual(again['quote_text'], old['quote_text'])
        self.assertEqual(again['validation'], 'matches_stored_transcript_version')
        self.assertEqual(again['audio_verification'], 'not_performed')

    def test_quote_mismatch_and_ambiguous_selector_are_atomic(self):
        before = self.counts()
        attempts = [dict(), dict(segment_indices=[0], word_refs=refs((0, 0))),
                    dict(word_refs=refs((0, 0)), quote_text='Zażółć'),
                    dict(word_refs=refs((0, 0)), quote_text=False),
                    dict(word_refs=refs((0, 0)), quote_text=''),
                    dict(segment_indices=[0], quote_text='incorrect')]
        for kwargs in attempts:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                citations.create_citation(self.aid, self.tid, **kwargs)
        self.assertEqual(self.counts(), before)

    def test_word_citation_rejects_wrong_artifact_and_non_transcript(self):
        ocr = evidence.add_derived_text(self.aid, 'ocr', 'text', segments=segments())
        for aid, tid in [(self.aid + 1, self.tid), (self.aid, ocr), (self.aid, True), (False, self.tid)]:
            with self.subTest(aid=aid, tid=tid), self.assertRaises(ValueError):
                citations.create_citation(aid, tid, word_refs=refs((0, 0)))

    def test_legacy_segment_citation_keeps_same_selector(self):
        anchor = citations.create_citation(self.aid, self.tid, [0])
        self.assertEqual(anchor['selector'], citations.projection_from_segments(segments(), [0])['selector'])

    def test_duplicate_json_keys_at_any_depth_reject_creation_atomically(self):
        raw_values = [
            '[{"start":0,"end":1,"text":"first","text":"second"}]',
            '[{"start":0,"end":1,"text":"x","words":[{"start":0,"start":0.1,"end":1,"word":"x"}]}]',
            '[{"start":0,"end":1,"text":"x","metadata":{"origin":"one","origin":"two"}}]',
        ]
        for raw in raw_values:
            with db.session() as conn:
                tid = conn.execute("INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES (?,'transcript','raw',?)",
                                   (self.aid, raw)).lastrowid
            before = self.counts()
            for kwargs in ({'segment_indices': [0]}, {'word_refs': refs((0, 0))}):
                with self.subTest(raw=raw, kwargs=kwargs), self.assertRaisesRegex(ValueError, 'duplicate JSON keys'):
                    citations.create_citation(self.aid, tid, **kwargs)
            self.assertEqual(self.counts(), before)

    def test_unused_legacy_nan_words_do_not_disable_segment_citation(self):
        raw = '[{"start":0,"end":1,"text":"x","words":[{"start":NaN,"end":1,"word":"x"}]}]'
        with db.session() as conn:
            tid = conn.execute("INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES (?,'transcript','raw',?)",
                               (self.aid, raw)).lastrowid
        anchor = citations.create_citation(self.aid, tid, [0])
        self.assertEqual(anchor['quote_text'], 'x')
        self.assertEqual(anchor['selector']['precision'], 'segment')
        before = self.counts()
        with self.assertRaisesRegex(ValueError, 'finite'):
            citations.create_citation(self.aid, tid, word_refs=refs((0, 0)))
        self.assertEqual(self.counts(), before)
        with db.session() as conn:
            self.assertEqual(conn.execute('SELECT segments_json FROM derived_text WHERE id=?', (tid,)).fetchone()[0], raw)

    def test_loader_rejects_non_array_or_invalid_json_and_retains_order(self):
        for raw in (None, '', '{bad', '{}', 'null', '0', '"text"'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                citations.loads_transcript_segments(raw)
        self.assertEqual(citations.loads_transcript_segments(json.dumps(segments())), segments())

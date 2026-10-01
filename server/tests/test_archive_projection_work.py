"""Count real projection work; elapsed time is not the acceptance oracle."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import archive, citations, packages
from server.tests.test_metadata_archive import fixture, rehash


def word_package(count=3, *, versions=False):
    package = fixture()
    segments = [{'start': 0, 'end': 4, 'text': 'abcd', 'words': [
        {'start': i, 'end': i + 1, 'word': character}
        for i, character in enumerate('abcd')]}]
    text = package['tables']['derived_text'][0]
    text['segments_json'] = json.dumps(segments)
    text['text'] = 'abcd'
    projection = citations.projection_from_words(segments, [{'segment_index': 0, 'word_index': 0}])
    anchor = package['tables']['evidence_anchors'][0]
    anchor.update({key: projection[key] for key in ('quote_text', 'quote_sha256', 'start_ms', 'end_ms')})
    anchor['selector_json'] = json.dumps(projection['selector'])
    package['tables']['evidence_anchors'] = [dict(anchor, id=100 + index) for index in range(count)]
    if versions:
        other = package['tables']['derived_text'][1]
        other.update(kind='transcript', text='abcd', segments_json=json.dumps(segments))
        for index, row in enumerate(package['tables']['evidence_anchors']):
            row['derived_text_id'] = 17 if index % 2 == 0 else 27
    return rehash(package)


class ArchiveProjectionWorkTests(unittest.TestCase):
    # Formula: anchor 1 + reference 1 + segment 1 + words 4 + raw segment
    # characters 4 + raw word characters 4 + selected characters 1 = 16.
    WORD_COST = 16

    def verify(self, package, cap):
        budget = packages.VerificationBudget(max_projection_visits=cap)
        return packages.validate_metadata_package(package, budget=budget), budget

    def codes(self, result):
        return [error['code'] for error in result['errors']]

    def test_decoded_cache_does_not_remove_repeated_word_validation(self):
        package = word_package(3)
        with patch.object(citations, 'validated_segment_words', wraps=citations.validated_segment_words) as words, \
                patch.object(packages, '_strict_stored_json', wraps=packages._strict_stored_json) as decoded:
            result, budget = self.verify(package, 48)
        self.assertTrue(result['valid'], result)
        self.assertEqual(words.call_count, 3)
        self.assertEqual(sum(call.kwargs.get('finite') is False for call in decoded.call_args_list), 1)
        self.assertEqual(budget.projection_visits, 48)

    def test_second_projection_stops_before_expensive_word_validation(self):
        package = word_package(3)
        with patch.object(citations, 'validated_segment_words', wraps=citations.validated_segment_words) as words:
            result, budget = self.verify(package, 31)
        self.assertIn('verification_limit_exceeded', self.codes(result))
        self.assertEqual(words.call_count, 1)
        self.assertTrue(budget.exhausted)
        self.assertEqual(budget.projection_visits, 32)  # includes refused precharge, not performed work

    def test_first_overbudget_projection_does_not_validate_any_words(self):
        package = word_package(1)
        with patch.object(citations, 'validated_segment_words', wraps=citations.validated_segment_words) as words:
            result, _ = self.verify(package, 15)
        self.assertIn('verification_limit_exceeded', self.codes(result))
        self.assertEqual(words.call_count, 0)

    def test_fresh_validation_calls_reset_work(self):
        package = word_package(1)
        with patch.object(citations, 'validated_segment_words', wraps=citations.validated_segment_words) as words:
            for _ in range(3):
                result, budget = self.verify(package, 16)
                self.assertTrue(result['valid'], result)
                self.assertEqual(budget.projection_visits, 16)
        self.assertEqual(words.call_count, 3)

    def test_different_versions_share_one_call_budget(self):
        package = word_package(3, versions=True)
        with patch.object(citations, 'validated_segment_words', wraps=citations.validated_segment_words) as words:
            result, _ = self.verify(package, 32)
        self.assertIn('verification_limit_exceeded', self.codes(result))
        self.assertEqual(words.call_count, 2)

    def test_invalid_canonical_selector_still_consumes_projection_work(self):
        package = word_package(3)
        selector = json.loads(package['tables']['evidence_anchors'][0]['selector_json'])
        selector['precision'] = 'invented'
        for anchor in package['tables']['evidence_anchors']:
            anchor['selector_json'] = json.dumps(selector)
        rehash(package)
        with patch.object(citations, 'validated_segment_words', wraps=citations.validated_segment_words) as words:
            result, _ = self.verify(package, 32)
        self.assertIn('invalid_anchor_selector', self.codes(result))
        self.assertIn('verification_limit_exceeded', self.codes(result))
        self.assertEqual(words.call_count, 2)

    def test_legacy_segment_selection_ignores_broken_unused_words(self):
        package = word_package(1)
        segments = json.loads(package['tables']['derived_text'][0]['segments_json'])
        segments[0]['words'] = [{'word': 'bad', 'start': float('nan'), 'end': False}]
        package['tables']['derived_text'][0]['segments_json'] = json.dumps(segments)
        projection = citations.projection_from_segments(segments, [0])
        anchor = package['tables']['evidence_anchors'][0]
        anchor.update({key: projection[key] for key in ('quote_text', 'quote_sha256', 'start_ms', 'end_ms')})
        anchor['selector_json'] = json.dumps(projection['selector'])
        rehash(package)
        with patch.object(citations, 'validated_segment_words', side_effect=AssertionError('unused words visited')):
            result, budget = self.verify(package, 7)
        self.assertTrue(result['valid'], result)
        self.assertEqual(budget.projection_visits, 7)

    def test_word_selection_does_not_inherit_legacy_segment_acceptance(self):
        package = word_package(1)
        segments = json.loads(package['tables']['derived_text'][0]['segments_json'])
        segments[0]['words'][0]['start'] = False
        package['tables']['derived_text'][0]['segments_json'] = json.dumps(segments)
        result, _ = self.verify(rehash(package), 16)
        self.assertIn('invalid_anchor_selector', self.codes(result))

    def test_archive_import_rejects_before_output_or_second_projection(self):
        package = word_package(3)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'absent' / 'archive.sqlite'
            with patch.object(citations, 'validated_segment_words', wraps=citations.validated_segment_words) as words:
                with self.assertRaisesRegex(ValueError, 'verification_limit_exceeded'):
                    archive.import_metadata_archive(package, target, limits=archive.ArchiveLimits(max_projection_visits=31))
            self.assertEqual(words.call_count, 1)
            self.assertFalse(target.parent.exists())

    def test_archive_import_roundtrip_and_read_each_get_fresh_budget(self):
        package = word_package(2)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'archive.sqlite'
            limits = archive.ArchiveLimits(max_projection_visits=32)
            with patch.object(citations, 'validated_segment_words', wraps=citations.validated_segment_words) as words:
                archive.import_metadata_archive(package, target, limits=limits)
                self.assertEqual(words.call_count, 4)  # prevalidation + inert roundtrip validation
                self.assertEqual(archive.read_metadata_archive(target, limits=limits), package)
                self.assertEqual(words.call_count, 6)

    def test_archive_read_and_export_enforce_projection_limit(self):
        package = word_package(3)
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'archive.sqlite', Path(directory) / 'export.json'
            archive.import_metadata_archive(package, source)
            before = source.read_bytes()
            limits = archive.ArchiveLimits(max_projection_visits=31)
            for operation in (lambda: archive.read_metadata_archive(source, limits=limits),
                              lambda: archive.export_metadata_archive(source, output, limits=limits)):
                with patch.object(citations, 'validated_segment_words', wraps=citations.validated_segment_words) as words:
                    with self.assertRaisesRegex(ValueError, 'verification_limit_exceeded'):
                        operation()
                self.assertEqual(words.call_count, 1)
            self.assertFalse(output.exists())
            self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

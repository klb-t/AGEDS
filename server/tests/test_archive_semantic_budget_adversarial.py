"""Independent public archive limits: hidden JSON shares one verification budget."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import archive, packages
from server.tests.test_metadata_archive import fixture, rehash


def nodes(value):
    """Independent model: containers and values count; object keys do not."""
    if isinstance(value, dict):
        return 1 + sum(nodes(child) for child in value.values())
    if isinstance(value, list):
        return 1 + sum(nodes(child) for child in value)
    return 1


def semantic_nodes(package):
    by_id = {row['id']: row for row in package['tables']['derived_text']}
    anchors = package['tables']['evidence_anchors']
    pinned = {row['derived_text_id'] for row in anchors}
    return (nodes(package) + sum(nodes(json.loads(by_id[key]['segments_json'])) for key in pinned)
            + sum(nodes(json.loads(row['selector_json'])) for row in anchors))


def more_anchors(package, count=3, second_version=False):
    original = package['tables']['evidence_anchors'][0]
    package['tables']['evidence_anchors'] = [dict(original, id=100 + index) for index in range(count)]
    if second_version:
        copied = deepcopy(package['tables']['derived_text'][0])
        copied['id'] = 37
        package['tables']['derived_text'].append(copied)
        package['tables']['evidence_anchors'][-1]['derived_text_id'] = 37
    return rehash(package)


class ArchiveSemanticBudgetAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def reject_import(self, package, limits):
        target = self.root / 'never-created' / 'archive.sqlite'
        with self.assertRaisesRegex(ValueError, 'limit'):
            archive.import_metadata_archive(package, target, limits=limits)
        self.assertFalse(target.parent.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_hidden_segments_depth_bypass_is_rejected_before_publication(self):
        package = fixture()
        segments = json.loads(package['tables']['derived_text'][0]['segments_json'])
        hidden = 0
        for _ in range(12):
            hidden = [hidden]
        segments[0]['unselected'] = hidden
        package['tables']['derived_text'][0]['segments_json'] = json.dumps(segments)
        self.reject_import(rehash(package), archive.ArchiveLimits(max_depth=8))

    def test_hidden_selector_depth_reports_budget_not_only_bad_projection(self):
        package = fixture()
        selector = json.loads(package['tables']['evidence_anchors'][0]['selector_json'])
        hidden = 0
        for _ in range(12):
            hidden = [hidden]
        selector['extra'] = hidden
        package['tables']['evidence_anchors'][0]['selector_json'] = json.dumps(selector)
        self.reject_import(rehash(package), archive.ArchiveLimits(max_depth=8))

    def test_decoded_fields_keep_outer_field_depth_at_exact_boundary(self):
        package = fixture()
        output = self.root / 'depth.sqlite'
        archive.import_metadata_archive(package, output, limits=archive.ArchiveLimits(max_depth=6))
        output.unlink()
        self.reject_import(package, archive.ArchiveLimits(max_depth=5))

    def test_hidden_array_nodes_cannot_use_separate_outer_allowance(self):
        package = fixture()
        segments = json.loads(package['tables']['derived_text'][0]['segments_json'])
        segments[0]['unselected'] = [0] * 500
        package['tables']['derived_text'][0]['segments_json'] = json.dumps(segments)
        rehash(package)
        self.reject_import(package, archive.ArchiveLimits(max_nodes=nodes(package) + 20))

    def test_exact_shared_node_boundary_accepts_then_one_less_rejects(self):
        package = more_anchors(fixture(), count=4)
        count = semantic_nodes(package)
        good = self.root / 'good.sqlite'
        archive.import_metadata_archive(package, good, limits=archive.ArchiveLimits(max_nodes=count))
        self.assertEqual(archive.read_metadata_archive(good, limits=archive.ArchiveLimits(max_nodes=count)), package)
        good.unlink()
        self.reject_import(package, archive.ArchiveLimits(max_nodes=count - 1))

    def test_identical_json_in_distinct_versions_is_charged_per_version(self):
        package = more_anchors(fixture(), count=4, second_version=True)
        count = semantic_nodes(package)
        target = self.root / 'exact.sqlite'
        archive.import_metadata_archive(package, target, limits=archive.ArchiveLimits(max_nodes=count))
        target.unlink()
        self.reject_import(package, archive.ArchiveLimits(max_nodes=count - 1))

    def test_public_read_and_export_enforce_hidden_budget_without_output(self):
        package = fixture()
        segments = json.loads(package['tables']['derived_text'][0]['segments_json'])
        segments[0]['unused'] = [0] * 100
        package['tables']['derived_text'][0]['segments_json'] = json.dumps(segments)
        rehash(package)
        source = self.root / 'source.sqlite'
        archive.import_metadata_archive(package, source)
        before = source.read_bytes()
        limits = archive.ArchiveLimits(max_nodes=semantic_nodes(package) - 1)
        with self.assertRaisesRegex(ValueError, 'limit'):
            archive.read_metadata_archive(source, limits=limits)
        target = self.root / 'not-created' / 'out.json'
        with self.assertRaisesRegex(ValueError, 'limit'):
            archive.export_metadata_archive(source, target, limits=limits)
        self.assertFalse(target.parent.exists())
        self.assertEqual(source.read_bytes(), before)


    def test_successful_pinned_decode_is_cached_only_within_each_verification(self):
        package = more_anchors(fixture(), count=4, second_version=True)
        original = packages._strict_stored_json
        with patch.object(packages, '_strict_stored_json', wraps=original) as decode:
            for iteration in range(2):
                result = packages.validate_metadata_package(package)
                self.assertTrue(result['valid'], result)
                segment_calls = [call for call in decode.call_args_list if call.kwargs.get('finite') is False]
                self.assertEqual(2 * (iteration + 1), len(segment_calls))
                self.assertEqual(6 * (iteration + 1), decode.call_count)

    def test_failed_pinned_decode_is_cached_but_not_across_verifications(self):
        package = more_anchors(fixture(), count=4)
        package['tables']['derived_text'][0]['segments_json'] = '{broken'
        rehash(package)
        original = packages._strict_stored_json
        with patch.object(packages, '_strict_stored_json', wraps=original) as decode:
            for iteration in range(2):
                result = packages.validate_metadata_package(package)
                self.assertFalse(result['valid'])
                self.assertEqual(4, sum(item['code'] == 'invalid_anchor_selector' for item in result['errors']))
                segment_calls = [call for call in decode.call_args_list if call.kwargs.get('finite') is False]
                self.assertEqual(iteration + 1, len(segment_calls))
        with self.assertRaises(ValueError):
            archive.import_metadata_archive(package, self.root / 'absent' / 'bad.sqlite')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_fresh_verification_budget_on_repeated_public_imports(self):
        package = more_anchors(fixture(), count=3)
        limits = archive.ArchiveLimits(max_nodes=semantic_nodes(package))
        for index in range(3):
            output = self.root / f'{index}.sqlite'
            archive.import_metadata_archive(package, output, limits=limits)
            self.assertEqual(archive.read_metadata_archive(output, limits=limits), package)


if __name__ == '__main__':
    unittest.main()

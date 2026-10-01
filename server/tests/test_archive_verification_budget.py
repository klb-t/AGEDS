"""Archive entrypoints share bounded semantic validation; raw roundtrip is exact."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import archive, packages
from server.tests.test_metadata_archive import fixture, rehash


def node_count(value):
    """Independent existing convention: values/containers, not dictionary keys."""
    total, pending = 0, [value]
    while pending:
        item = pending.pop()
        total += 1
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return total


class ArchiveVerificationBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = fixture()

    def repeated_anchors(self):
        anchor = self.package['tables']['evidence_anchors'][0]
        self.package['tables']['evidence_anchors'] = [dict(anchor, id=100 + index) for index in range(8)]
        return rehash(self.package)

    def test_projection_budget_forwarded_to_import_read_and_export_without_output(self):
        package = self.repeated_anchors()
        restrictive = archive.ArchiveLimits(max_projection_visits=1)
        rejected = self.root / 'rejected.sqlite'
        with self.assertRaisesRegex(ValueError, 'verification_limit_exceeded'):
            archive.import_metadata_archive(package, rejected, limits=restrictive)
        self.assertFalse(rejected.exists())
        stored = self.root / 'valid.sqlite'
        archive.import_metadata_archive(package, stored)
        prior = stored.read_bytes()
        with self.assertRaisesRegex(ValueError, 'verification_limit_exceeded'):
            archive.read_metadata_archive(stored, limits=restrictive)
        exported = self.root / 'rejected.json'
        with self.assertRaisesRegex(ValueError, 'verification_limit_exceeded'):
            archive.export_metadata_archive(stored, exported, limits=restrictive)
        self.assertFalse(exported.exists())
        self.assertEqual(prior, stored.read_bytes())
        self.assertFalse(list(self.root.glob('.ageds-archive-*')))

    def test_aggregate_archive_rows_fail_during_intake_before_final_semantic_validation(self):
        self.package['tables']['evidence_anchors'] = []
        rehash(self.package)
        stored = self.root / 'valid.sqlite'
        archive.import_metadata_archive(self.package, stored)
        limits = archive.ArchiveLimits(max_nodes=node_count(self.package) - 1)
        with patch.object(archive, '_verified', side_effect=AssertionError('aggregate intake must reject first')):
            with self.assertRaises(ValueError):
                archive.read_metadata_archive(stored, limits=limits)

    def test_outer_exact_node_boundary_is_not_double_charged_across_sqlite_fragments(self):
        self.package['tables']['evidence_anchors'] = []
        rehash(self.package)
        limits = archive.ArchiveLimits(max_nodes=node_count(self.package))
        stored = self.root / 'exact.sqlite'
        archive.import_metadata_archive(self.package, stored, limits=limits)
        restored = archive.read_metadata_archive(stored, limits=limits)
        self.assertEqual(packages.canonical_json(self.package), packages.canonical_json(restored))

    def test_nested_projection_depth_rejects_while_opaque_legacy_strings_roundtrip(self):
        self.package['tables']['artifacts'][0]['metadata_json'] = '[' * 100 + 'unparsed original' + ']' * 100
        stored = self.root / 'opaque.sqlite'
        archive.import_metadata_archive(rehash(self.package), stored)
        restored = archive.read_metadata_archive(stored)
        self.assertEqual(packages.canonical_json(self.package), packages.canonical_json(restored))
        segments = json.loads(self.package['tables']['derived_text'][0]['segments_json'])
        nested = 0
        for _ in range(70):
            nested = [nested]
        segments[0]['unused_metadata'] = nested
        self.package['tables']['derived_text'][0]['segments_json'] = json.dumps(segments)
        rejected = self.root / 'nested.sqlite'
        with self.assertRaisesRegex(ValueError, 'verification_limit_exceeded'):
            archive.import_metadata_archive(rehash(self.package), rejected)
        self.assertFalse(rejected.exists())

    def test_rejection_does_not_clobber_existing_target_or_mutate_input(self):
        package = self.repeated_anchors()
        before = deepcopy(package)
        target = self.root / 'existing.sqlite'
        target.write_bytes(b'existing inert fixture')
        with self.assertRaises(ValueError):
            archive.import_metadata_archive(package, target, limits=archive.ArchiveLimits(max_projection_visits=1))
        self.assertEqual(b'existing inert fixture', target.read_bytes())
        self.assertEqual(before, package)

    def test_duplicate_outer_key_error_is_bounded_without_rewriting_raw_input(self):
        key = "untrusted" * 100
        raw = json.dumps({key: 1})[:-1] + ',' + json.dumps(key) + ':2}'
        before = raw
        with self.assertRaises(ValueError) as error:
            archive.strict_json(raw)
        self.assertIn('Duplicate JSON key', str(error.exception))
        self.assertIn('[truncated]', str(error.exception))
        self.assertLess(len(str(error.exception)), 300)
        self.assertEqual(before, raw)

    def test_projection_limit_is_additive_and_strictly_positive(self):
        self.assertEqual(2_000_000, archive.ArchiveLimits().max_projection_visits)
        for invalid in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                replace(archive.ArchiveLimits(), max_projection_visits=invalid)


if __name__ == '__main__':
    unittest.main()

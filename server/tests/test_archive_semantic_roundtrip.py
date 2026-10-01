"""Independent semantic preservation across bounded inert archive operations."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import archive, packages


TABLES = ('cases', 'sources', 'artifacts', 'source_observations', 'events', 'processing_runs',
          'derived_text', 'annotations', 'evidence_anchors', 'tags', 'artifact_tags', 'event_tags',
          'links', 'jobs', 'audit_log', 'schema_migrations')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def seal(package):
    payload = {key: value for key, value in package.items() if key != 'integrity'}
    package['integrity'] = {
        'algorithm': 'sha256', 'canonicalization': 'sorted-keys-compact-utf8-no-nan/v1',
        'payload_sha256': hashlib.sha256(canonical(payload)).hexdigest(),
        'table_sha256': {name: hashlib.sha256(canonical(payload['tables'][name])).hexdigest() for name in TABLES},
    }
    return package


def segment_selector(indices):
    return {'kind': 'segments', 'indices': indices, 'text_join': 'concatenate_exact',
            'time_unit': 'seconds', 'stored_time_unit': 'milliseconds', 'rounding': 'nearest_ms', 'precision': 'segment'}


def word_selector(segment_index, word_index, start, end):
    return {'kind': 'words', 'word_refs': [{'segment_index': segment_index, 'word_index': word_index}],
            'text_join': 'concatenate_exact', 'time_unit': 'seconds', 'stored_time_unit': 'milliseconds',
            'rounding': 'nearest_ms', 'precision': 'word_asr', 'source_start': start, 'source_end': end,
            'alignment_verification': 'not_performed'}


def anchor(identity, version, text, start_ms, end_ms, selector):
    return {'id': identity, 'artifact_id': 3, 'derived_text_id': version,
            'quote_text': text, 'quote_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'start_ms': start_ms, 'end_ms': end_ms,
            'selector_json': json.dumps(selector, ensure_ascii=False, indent=2), 'created_at': 'raw anchor time'}


def fixture():
    # Authored independently of citations projection helpers and package exporter.
    tables = {name: [] for name in TABLES}
    tables['cases'] = [{'id': 1, 'name': 'synthetic'}]
    tables['sources'] = [{'id': 2, 'case_id': 1, 'locator': 'https://must-not-fetch.invalid/raw',
                          'metadata_json': '{"duplicate":1,"duplicate":2}'}]
    tables['artifacts'] = [{'id': 3, 'source_id': 2, 'stored_path': '/must/not/open/audio.wav',
                            'source_locator': 'file:///must/not/open/audio.wav', 'sha256': 'a' * 64,
                            'size_bytes': 9, 'metadata_json': '  {broken original\n NaN raw 😀'}]
    tables['source_observations'] = [{'id': 6, 'artifact_id': 3, 'source_id': 2, 'sha256': 'a' * 64,
                                    'size_bytes': 9, 'metadata_json': '[not parsed'}]
    tables['jobs'] = [{'id': 4, 'artifact_id': 3, 'status': 'running', 'lease_token': 'historical only',
                       'payload_json': '{not executable', 'error': 'literal job error\r\n'}]
    tables['processing_runs'] = [{'id': 5, 'job_id': 4, 'artifact_id': 3, 'status': 'failed',
                                  'parameters_json': '{"x":NaN}', 'provenance_json': '{broken',
                                  'metadata_json': 'not JSON', 'error': '\tRAW error <trace>\n'}]
    first_text, second_text = ' A  żółć\n', 'B́ 😀'
    segments = [{'start': 0, 'end': 1, 'text': first_text,
                 'words': [{'start': float('nan'), 'end': 1, 'word': first_text}]},
                {'start': 1.25, 'end': 2, 'text': second_text}]
    words = [{'start': 0, 'end': 1, 'text': ' Y Z', 'words': [
        {'start': 0.125, 'end': 0.5, 'word': ' Y'}, {'start': 0.5, 'end': 0.875, 'word': ' Z'}]}]
    tables['derived_text'] = [
        {'id': 10, 'artifact_id': 3, 'run_id': 5, 'kind': 'transcript', 'text': first_text + second_text,
         'segments_json': json.dumps(segments, ensure_ascii=False, indent=3), 'metadata_json': '{legacy broken'},
        {'id': 11, 'artifact_id': 3, 'run_id': None, 'kind': 'transcript', 'text': ' Y Z',
         'segments_json': json.dumps(words, ensure_ascii=False, separators=(', ', ': '))},
        {'id': 12, 'artifact_id': 3, 'run_id': None, 'kind': 'transcript', 'text': 'unanchored original',
         'segments_json': '[[' * 500 + 'opaque malformed NaN'},
    ]
    tables['evidence_anchors'] = [
        anchor(20, 10, first_text, 0, 1000, segment_selector([0])),
        anchor(21, 10, second_text, 1250, 2000, segment_selector([1])),
        anchor(22, 10, first_text + second_text, 0, 2000, segment_selector([0, 1])),
        anchor(23, 11, ' Z', 500, 875, word_selector(0, 1, 0.5, 0.875)),
        anchor(24, 11, ' Y Z', 0, 1000, segment_selector([0])),
    ]
    tables['annotations'] = [{'id': 30, 'artifact_id': 3, 'derived_text_id': 10, 'body': 'raw annotation\r\n'}]
    tables['audit_log'] = [{'id': 31, 'object_kind': 'artifact', 'object_id': 3, 'details_json': '{not JSON'}]
    return seal({'schema': 'ageds.metadata-package/v1', 'metadata_only': True, 'source_bytes_included': False,
                 'replay_supported': False, 'signed': False, 'exported_at': 'unchanged time without inferred zone',
                 'missing_tables': [], 'tables': tables, 'extension': {'raw': [-0.0, None, True, 'ą\n']}})


def oversized_decoded_fixture():
    value = fixture()
    segment_data = json.loads(value['tables']['derived_text'][0]['segments_json'])
    segment_data[0]['unused_but_decoded'] = [0] * 5000
    value['tables']['derived_text'][0]['segments_json'] = json.dumps(segment_data, ensure_ascii=False)
    return seal(value)


class ArchiveSemanticRoundtripTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def assert_invalid_anchor(self, value):
        result = packages.validate_metadata_package(value)
        self.assertFalse(result['valid'], result)
        self.assertFalse(result['anchors_valid'], result)
        self.assertTrue(result['digest_valid'], result)
        return result

    def test_exact_roundtrip_preserves_opaque_errors_unanchored_segments_and_every_pinned_selector(self):
        original = fixture()
        before = canonical(original)
        self.assertTrue(packages.validate_metadata_package(original)['valid'])
        path = self.root / 'inert.sqlite'
        # All locator strings remain inert; SQLite work uses in-memory archives.
        with patch('builtins.open', side_effect=AssertionError('no implicit source file opens')):
            report = archive.import_metadata_archive(original, path)
            restored = archive.read_metadata_archive(path)
            archive.export_metadata_archive(path, self.root / 'export.json')
        self.assertEqual(canonical(original), before)
        self.assertEqual(canonical(restored), before)
        self.assertEqual((self.root / 'export.json').read_bytes(), before)
        self.assertEqual(report['package_sha256'], hashlib.sha256(before).hexdigest())
        for flag in ('jobs_resumed', 'live_restore_supported', 'source_bytes_included', 'replay_supported', 'signed'):
            self.assertFalse(report[flag])
        self.assertEqual([(row['id'], row['derived_text_id'], row['quote_text']) for row in restored['tables']['evidence_anchors']],
                         [(20, 10, ' A  żółć\n'), (21, 10, 'B́ 😀'), (22, 10, ' A  żółć\nB́ 😀'),
                          (23, 11, ' Z'), (24, 11, ' Y Z')])
        self.assertEqual(restored['tables']['derived_text'][2]['segments_json'], '[[' * 500 + 'opaque malformed NaN')

    def test_unused_historic_nan_allows_segment_citation_but_never_word_precision(self):
        value = fixture()
        raw = value['tables']['derived_text'][0]['segments_json']
        self.assertIn('NaN', raw)
        path = self.root / 'valid-segments.sqlite'
        archive.import_metadata_archive(value, path)
        self.assertEqual(archive.read_metadata_archive(path)['tables']['derived_text'][0]['segments_json'], raw)
        before = path.read_bytes()
        value['tables']['evidence_anchors'].append(anchor(25, 10, ' A  żółć\n', 0, 1000, word_selector(0, 0, 0, 1)))
        seal(value)
        self.assert_invalid_anchor(value)
        with self.assertRaises(ValueError):
            archive.import_metadata_archive(value, self.root / 'invalid-word.sqlite')
        self.assertFalse((self.root / 'invalid-word.sqlite').exists())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(value['tables']['derived_text'][0]['segments_json'], raw)

    def test_newer_version_cannot_silently_replace_old_anchor_after_redigest(self):
        value = fixture()
        value['tables']['evidence_anchors'][0]['derived_text_id'] = 11
        seal(value)
        self.assert_invalid_anchor(value)
        with self.assertRaises(ValueError):
            archive.import_metadata_archive(value, self.root / 'wrong-version.sqlite')
        self.assertFalse((self.root / 'wrong-version.sqlite').exists())

    def test_same_version_different_selector_is_checked_independently_after_redigest(self):
        value = fixture()
        value['tables']['evidence_anchors'][1]['selector_json'] = json.dumps(segment_selector([0]))
        seal(value)
        result = self.assert_invalid_anchor(value)
        self.assertTrue(any('[id=21]' in item['path'] for item in result['errors']), result)

    def test_successful_decoded_version_cache_never_survives_between_validation_calls(self):
        value = fixture()
        original = deepcopy(value)
        self.assertTrue(packages.validate_metadata_package(value)['valid'])
        changed = json.loads(value['tables']['derived_text'][1]['segments_json'])
        changed[0]['text'] = 'different result at same version ID'
        value['tables']['derived_text'][1]['segments_json'] = json.dumps(changed)
        seal(value)
        self.assert_invalid_anchor(value)
        self.assertTrue(packages.validate_metadata_package(original)['valid'])
        self.assertTrue(packages.validate_metadata_package(deepcopy(original))['valid'])

    def test_failed_decoded_version_cache_never_poison_later_repaired_input(self):
        value = fixture()
        original_raw = value['tables']['derived_text'][0]['segments_json']
        value['tables']['derived_text'][0]['segments_json'] = '[broken pinned JSON'
        seal(value)
        self.assert_invalid_anchor(value)
        value['tables']['derived_text'][0]['segments_json'] = original_raw
        seal(value)
        self.assertTrue(packages.validate_metadata_package(value)['valid'])
        archive.import_metadata_archive(value, self.root / 'repaired.sqlite')
        self.assertEqual(canonical(archive.read_metadata_archive(self.root / 'repaired.sqlite')), canonical(value))

    def test_duplicate_selector_keys_rejected_without_parsing_duplicate_opaque_metadata_keys(self):
        value = fixture()
        self.assertTrue(packages.validate_metadata_package(value)['valid'])
        selector = value['tables']['evidence_anchors'][0]['selector_json']
        value['tables']['evidence_anchors'][0]['selector_json'] = selector.replace('"kind": "segments"', '"kind": "segments", "kind": "segments"')
        self.assert_invalid_anchor(seal(value))
        self.assertEqual(value['tables']['sources'][0]['metadata_json'], '{"duplicate":1,"duplicate":2}')

    def low_limit_input(self):
        value = oversized_decoded_fixture()
        limits = archive.ArchiveLimits(max_nodes=1500)
        # Prove this is the decoded-data boundary, not a trivial outer JSON cap.
        self.assertEqual(canonical(archive.strict_json(canonical(value), limits=limits)), canonical(value))
        return value, limits

    def test_import_decoded_limit_creates_no_output_and_preserves_previous_target(self):
        value, limits = self.low_limit_input()
        fresh = self.root / 'never-created' / 'rejected.sqlite'
        prior = self.root / 'prior.sqlite'
        archive.import_metadata_archive(fixture(), prior)
        before = prior.read_bytes()
        for output in (fresh, prior):
            with self.subTest(output=output.name), self.assertRaisesRegex(ValueError, 'verification_limit_exceeded|limit'):
                archive.import_metadata_archive(value, output, limits=limits)
        self.assertFalse(fresh.parent.exists())
        self.assertEqual(prior.read_bytes(), before)

    def test_read_decoded_limit_does_not_rewrite_valid_inert_archive(self):
        value, limits = self.low_limit_input()
        path = self.root / 'larger.sqlite'
        archive.import_metadata_archive(value, path)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'verification_limit_exceeded|limit'):
            archive.read_metadata_archive(path, limits=limits)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(canonical(archive.read_metadata_archive(path)), canonical(value))

    def test_export_decoded_limit_publishes_nothing_and_keeps_prior_export(self):
        value, limits = self.low_limit_input()
        path = self.root / 'larger.sqlite'
        archive.import_metadata_archive(value, path)
        previous = self.root / 'prior.json'
        archive.export_metadata_archive(path, previous)
        before = previous.read_bytes()
        fresh = self.root / 'never-exported' / 'new.json'
        for output in (fresh, previous):
            with self.subTest(output=output.name), self.assertRaisesRegex(ValueError, 'verification_limit_exceeded|limit'):
                archive.export_metadata_archive(path, output, limits=limits)
        self.assertFalse(fresh.parent.exists())
        self.assertEqual(previous.read_bytes(), before)
        self.assertEqual(before, canonical(value))

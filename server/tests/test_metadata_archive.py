"""Adversarial exact roundtrip tests for an inert, non-live SQLite archive."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from server.app import archive, packages
from server.app.citations import projection_from_segments


def fixture():
    tables = {name: [] for name in packages.TABLES}
    tables['cases'] = [{'id': 10, 'name': 'Sprawa ą\nraw'}]
    tables['sources'] = [{'id': 11, 'case_id': 10, 'label': 'źródło'}]
    tables['artifacts'] = [{'id': 12, 'source_id': 11, 'stored_path': '/never/read/source.wav',
                            'source_locator': 'https://never.invalid/no-network', 'metadata_json': '{broken original JSON',
                            'sha256': 'recorded-not-verified', 'size_bytes': 90}]
    tables['source_observations'] = [{'id': 13, 'artifact_id': 12, 'source_id': 11, 'metadata_json': 'unchanged'}]
    tables['events'] = [{'id': 14, 'artifact_id': 12, 'source_id': 11, 'body': 'raw\r\ntext', 'confidence': None}]
    tables['jobs'] = [{'id': 15, 'artifact_id': 12, 'status': 'running', 'lease_token': 'historical-only-token'}]
    tables['processing_runs'] = [{'id': 16, 'job_id': 15, 'artifact_id': 12, 'status': 'failed', 'error': 'RAW error\n traceback'}]
    segments = [{'start': .125, 'end': 1.25, 'text': ' Pierwszy\n'}]
    tables['derived_text'] = [{'id': 17, 'artifact_id': 12, 'run_id': 16, 'kind': 'transcript', 'text': ' Pierwszy\n', 'segments_json': json.dumps(segments)},
                              {'id': 27, 'artifact_id': 12, 'run_id': None, 'kind': 'transcript', 'text': 'changed later'}]
    tables['annotations'] = [{'id': 18, 'artifact_id': 12, 'event_id': 14, 'derived_text_id': 17, 'body': 'unchanged'}]
    projection = projection_from_segments(segments, [0])
    tables['evidence_anchors'] = [{'id': 19, 'artifact_id': 12, 'derived_text_id': 17,
        **{key: projection[key] for key in ('quote_text', 'quote_sha256', 'start_ms', 'end_ms')},
        'selector_json': json.dumps(projection['selector'])}]
    tables['tags'] = [{'id': 20, 'name': 'tag'}]
    tables['artifact_tags'] = [{'artifact_id': 12, 'tag_id': 20}]
    tables['event_tags'] = [{'event_id': 14, 'tag_id': 20}]
    tables['links'] = [{'id': 21, 'left_kind': 'artifact', 'left_id': 12, 'right_kind': 'event', 'right_id': 14}]
    tables['audit_log'] = [{'id': 22, 'object_kind': 'artifact', 'object_id': 12, 'details_json': 'raw not JSON'}]
    tables['schema_migrations'] = [{'version': 1, 'name': 'historic'}]
    package = {'schema': packages.SCHEMA_ID, 'metadata_only': True, 'source_bytes_included': False,
               'replay_supported': False, 'signed': False, 'exported_at': 'original time not inferred',
               'missing_tables': [], 'tables': tables, 'extension': {'kept': [-0.0, 2.5, True, None]}}
    package['integrity'] = packages._integrity(package)
    return package


def rehash(package):
    package['integrity'] = packages._integrity({key: value for key, value in package.items() if key != 'integrity'})
    return package


class MetadataArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output = self.root/'archive.sqlite'
        self.package = fixture()

    def tearDown(self):
        self.tmp.cleanup()

    def create(self):
        return archive.import_metadata_archive(self.package, self.output)

    def test_roundtrip_preserves_all_ids_rows_versions_errors_and_original_digests(self):
        report = self.create()
        before = self.output.read_bytes()
        restored = archive.read_metadata_archive(self.output)
        self.assertEqual(packages.canonical_json(restored), packages.canonical_json(self.package))
        self.assertEqual(report['package_sha256'], hashlib.sha256(packages.canonical_json(self.package)).hexdigest())
        self.assertFalse(report['jobs_resumed'])
        self.assertFalse(report['live_restore_supported'])
        with sqlite3.connect(self.output) as db:
            self.assertEqual({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}, {'archive_envelope', 'archive_records'})
        exported = self.root/'exact.json'
        archive.export_metadata_archive(self.output, exported)
        self.assertEqual(packages.canonical_json(json.loads(exported.read_bytes())), packages.canonical_json(self.package))
        self.assertEqual(self.output.read_bytes(), before)

    def test_invalid_graph_is_rejected_before_any_output_directory_creation(self):
        self.package['tables']['sources'][0]['case_id'] = 999
        rehash(self.package)
        with self.assertRaisesRegex(ValueError, 'broken_reference'):
            archive.import_metadata_archive(self.package, self.root/'never-created'/'bad.sqlite')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rehashed_incorrect_pinned_selector_rejected(self):
        self.package['tables']['evidence_anchors'][0]['derived_text_id'] = 27
        with self.assertRaisesRegex(ValueError, 'anchor'):
            archive.import_metadata_archive(rehash(self.package), self.output)
        self.assertFalse(self.output.exists())

    def test_existing_target_and_symlink_never_clobbered(self):
        self.output.write_bytes(b'valuable database')
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual(self.output.read_bytes(), b'valuable database')
        link = self.root/'link.sqlite'
        link.symlink_to(self.output)
        with self.assertRaises(FileExistsError):
            archive.import_metadata_archive(self.package, link)
        self.assertEqual(self.output.read_bytes(), b'valuable database')

    def test_publication_failure_removes_temporary_and_preserves_existing(self):
        def competing_writer(source, destination):
            Path(destination).write_bytes(b'competitor')
            raise FileExistsError('race')
        with patch.object(archive.os, 'link', side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                self.create()
        self.assertEqual(list(self.root.iterdir()), [self.output])
        self.assertEqual(self.output.read_bytes(), b'competitor')

    def test_size_rows_depth_and_nodes_limits_reject_without_output(self):
        for limits in (archive.ArchiveLimits(max_input_bytes=10), archive.ArchiveLimits(max_archive_bytes=10),
                       archive.ArchiveLimits(max_rows=1), archive.ArchiveLimits(max_depth=2), archive.ArchiveLimits(max_nodes=5)):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                archive.import_metadata_archive(self.package, self.output, limits=limits)
            self.assertFalse(self.output.exists())

    def test_strict_json_rejects_duplicate_keys_nonfinite_numbers_surrogates_and_encoding(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":{"b":1,"b":2}}', b'{"a":NaN}', b'{"a":Infinity}',
                    b'{"a":-Infinity}', b'{"a":1e9999}', b'"\\ud800"', '{"a":1}'.encode('utf-16'),
                    b'[' * 2000 + b'0' + b']' * 2000):
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                archive.strict_json(raw)

    def test_unexpected_table_duplicate_missing_and_nonobject_rows_rejected(self):
        bad = deepcopy(self.package); bad['tables']['unknown'] = []
        bad_missing = deepcopy(self.package); bad_missing['missing_tables'] = ['jobs', 'jobs']
        bad_row = deepcopy(self.package); bad_row['tables']['jobs'] = ['not-row']
        for package in (bad, bad_missing, bad_row):
            with self.subTest(package=package), self.assertRaises(ValueError):
                archive.import_metadata_archive(package, self.output)
        self.assertFalse(self.output.exists())

    def test_source_references_are_literal_and_never_opened(self):
        with patch('builtins.open', side_effect=AssertionError('Referenced source read')):
            self.create()
            restored = archive.read_metadata_archive(self.output)
        self.assertEqual(restored['tables']['artifacts'][0]['stored_path'], '/never/read/source.wav')
        self.assertEqual(restored['tables']['jobs'][0]['status'], 'running')

    def test_read_rejects_symlink_pipe_and_oversized_file(self):
        self.create()
        link = self.root/'link'; link.symlink_to(self.output)
        with self.assertRaises(OSError):
            archive.read_metadata_archive(link)
        fifo = self.root/'fifo'; os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            archive.read_metadata_archive(fifo)
        with self.assertRaises(ValueError):
            archive.read_metadata_archive(self.output, limits=archive.ArchiveLimits(max_archive_bytes=10))

    def test_live_sqlite_and_corrupt_archive_are_rejected(self):
        with sqlite3.connect(self.output) as db:
            db.execute('CREATE TABLE jobs(id INTEGER)')
        with self.assertRaises(ValueError):
            archive.read_metadata_archive(self.output)
        self.output.write_bytes(b'not sqlite')
        with self.assertRaises(ValueError):
            archive.read_metadata_archive(self.output)

    def test_unexpected_view_trigger_or_index_rejected_without_execution(self):
        self.create()
        clean = self.output.read_bytes()
        for sql in ('CREATE VIEW sqlitex_hidden AS SELECT load_extension("/never")',
                    'CREATE TRIGGER malicious AFTER INSERT ON archive_records BEGIN DELETE FROM archive_records; END',
                    'CREATE INDEX extra ON archive_records(ordinal)'):
            self.output.write_bytes(clean)
            with sqlite3.connect(self.output) as db:
                db.execute(sql)
            before = self.output.read_bytes()
            with self.subTest(sql=sql), self.assertRaisesRegex(ValueError, 'schema'):
                archive.read_metadata_archive(self.output)
            self.assertEqual(self.output.read_bytes(), before)

    def test_edited_row_missing_row_gap_or_unknown_table_is_rejected(self):
        self.create()
        clean = self.output.read_bytes()
        mutations = ["UPDATE archive_records SET row_json='{}' WHERE table_name='jobs'",
                     "DELETE FROM archive_records WHERE table_name='cases'",
                     "UPDATE archive_records SET ordinal=20 WHERE table_name='jobs'",
                     "UPDATE archive_records SET table_name='unknown' WHERE table_name='jobs'",
                     "UPDATE archive_envelope SET package_sha256='wrong'",
                     "UPDATE archive_envelope SET envelope_json='{\"schema\":1,\"schema\":2}'"]
        for sql in mutations:
            self.output.write_bytes(clean)
            with sqlite3.connect(self.output) as db:
                db.execute(sql)
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                archive.read_metadata_archive(self.output)

    def test_export_refuses_existing_target_and_corrupt_archive_does_not_publish(self):
        self.create()
        exported = self.root/'old.json'; exported.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            archive.export_metadata_archive(self.output, exported)
        self.assertEqual(exported.read_bytes(), b'keep')
        self.output.write_bytes(b'invalid')
        with self.assertRaises(ValueError):
            archive.export_metadata_archive(self.output, self.root/'never.json')
        self.assertFalse((self.root/'never.json').exists())

    def test_module_and_roundtrip_do_not_load_live_config_or_create_runtime_storage(self):
        package_path = self.root/'source.json'; package_path.write_bytes(packages.canonical_json(self.package))
        isolated = self.root/'cwd'; isolated.mkdir()
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]), PYTHONDONTWRITEBYTECODE='1',
                   EW_DB_PATH=str(isolated/'never.db'), EW_DATA_DIR=str(isolated/'never-data'), EW_STORE_DIR=str(isolated/'never-store'))
        code = 'import sys; from pathlib import Path; from server.app.archive import *; p=load_package(Path(sys.argv[1])); import_metadata_archive(p,Path(sys.argv[2])); read_metadata_archive(Path(sys.argv[2])); assert "server.app.db" not in sys.modules; assert "server.app.config" not in sys.modules'
        process = subprocess.run([sys.executable, '-c', code, str(package_path), str(self.output)], env=env, cwd=isolated, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(list(isolated.iterdir()), [])

    def test_word_selector_roundtrip_and_tampering(self):
        from server.app.citations import projection_from_words
        segments = [{'start': 0, 'end': 1, 'text': ' Ala ma', 'words': [
            {'start': 0.1, 'end': 0.4, 'word': ' Ala'}, {'start': 0.4, 'end': 0.7, 'word': ' ma'}]}]
        text = self.package['tables']['derived_text'][0]
        text['segments_json'] = json.dumps(segments)
        text['text'] = ' Ala ma'
        projection = projection_from_words(segments, [{'segment_index': 0, 'word_index': 1}])
        anchor = self.package['tables']['evidence_anchors'][0]
        for key in ('quote_text', 'quote_sha256', 'start_ms', 'end_ms'):
            anchor[key] = projection[key]
        anchor['selector_json'] = json.dumps(projection['selector'])
        rehash(self.package)
        self.create()
        self.assertEqual(archive.read_metadata_archive(self.output), self.package)
        selector = json.loads(anchor['selector_json'])
        selector['word_refs'][0]['word_index'] = 0
        anchor['selector_json'] = json.dumps(selector)
        with self.assertRaisesRegex(ValueError, 'anchor'):
            archive.import_metadata_archive(rehash(self.package), self.root/'bad.sqlite')

    def test_exact_size_limit_export_can_be_reimported(self):
        self.create()
        size = len(packages.canonical_json(self.package))
        limits = archive.ArchiveLimits(max_input_bytes=size)
        exported = self.root/'exact.json'
        archive.export_metadata_archive(self.output, exported, limits=limits)
        restored = archive.load_package(exported, limits=limits)
        self.assertEqual(restored, self.package)
        archive.import_metadata_archive(restored, self.root/'second.sqlite', limits=limits)

    def test_legacy_missing_tables_remain_unknown_and_empty(self):
        self.package['tables']['processing_runs'] = []
        self.package['tables']['derived_text'][0]['run_id'] = None
        self.package['missing_tables'] = ['processing_runs']
        rehash(self.package)
        self.create()
        self.assertEqual(archive.read_metadata_archive(self.output), self.package)


if __name__ == '__main__':
    unittest.main()

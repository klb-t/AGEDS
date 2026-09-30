"""Metadata export verifies recorded graph state, not source truth or restore."""
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

from server.app import db as database
from server.app import evidence
from server.app import packages
from server.app.citations import create_citation


def rehash(package):
    payload = {key: value for key, value in package.items() if key != 'integrity'}
    package['integrity'] = packages._integrity(payload)
    return package


class MetadataPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = replace(database.settings, data_dir=self.root,
                                db_path=self.root/'evidence.db', store_dir=self.root/'store')
        self.settings.store_dir.mkdir()
        self.patch_db = patch.object(database, 'settings', self.settings)
        self.patch_evidence = patch.object(evidence, 'settings', self.settings)
        self.patch_db.start()
        self.patch_evidence.start()
        database.init_db()
        self.source = evidence.ensure_source('synthetic', 'package fixture')
        self.path = self.root/'sample.wav'
        self.path.write_bytes(b'synthetic source bytes')
        self.artifact = evidence.ingest_file(self.path, source_id=self.source, source_locator='literal:not-a-network-request')
        self.event = evidence.add_event(source_id=self.source, artifact_id=self.artifact,
                                       event_type='call', external_id='row:1', body='historical event')
        with database.session() as connection:
            self.job = connection.execute("INSERT INTO jobs(kind,artifact_id,status,lease_token,lease_expires_at) VALUES('transcribe',?,'done','metadata-only-token',2000000000)", (self.artifact,)).lastrowid
            self.run = connection.execute("INSERT INTO processing_runs(job_id,artifact_id,lease_token,status,started_at) VALUES (?,?,'metadata-only-token','done','synthetic')", (self.job, self.artifact)).lastrowid
        self.text = evidence.add_derived_text(self.artifact, 'transcript', ' Pierwszy. Drugi.', run_id=self.run,
            segments=[{'start': 0.125, 'end': 1.2, 'text': ' Pierwszy.'},
                      {'start': 1.2, 'end': 2.5, 'text': ' Drugi.'}])
        self.anchor = create_citation(self.artifact, self.text, [0,1])
        with database.session() as connection:
            self.annotation = connection.execute('INSERT INTO annotations(artifact_id,event_id,derived_text_id,body) VALUES(?,?,?,?)', (self.artifact,self.event,self.text,'historical note')).lastrowid
            self.tag = connection.execute("INSERT INTO tags(name) VALUES('synthetic')").lastrowid
            connection.execute('INSERT INTO artifact_tags(artifact_id,tag_id) VALUES(?,?)', (self.artifact,self.tag))
            connection.execute('INSERT INTO event_tags(event_id,tag_id) VALUES(?,?)', (self.event,self.tag))
            connection.execute("INSERT INTO links(left_kind,left_id,right_kind,right_id,relation) VALUES('artifact',?,'event',?,'contains')", (self.artifact,self.event))

    def tearDown(self):
        self.patch_evidence.stop()
        self.patch_db.stop()
        self.temporary.cleanup()

    def export(self):
        return packages.export_metadata_package()

    def assert_error(self, result, code):
        self.assertFalse(result['valid'], result)
        self.assertIn(code, [item['code'] for item in result['errors']], result)

    def test_complete_json_roundtrip_keeps_every_core_table_and_flags(self):
        package = self.export()
        restored = json.loads(json.dumps(package, ensure_ascii=False, allow_nan=False))
        self.assertEqual(set(restored['tables']), set(packages.TABLES))
        for name in packages.TABLES:
            self.assertGreater(len(restored['tables'][name]), 0, name)
        self.assertTrue(restored['metadata_only'])
        self.assertFalse(restored['source_bytes_included'])
        self.assertFalse(restored['replay_supported'])
        self.assertFalse(restored['signed'])
        result = packages.validate_metadata_package(restored)
        self.assertTrue(result['valid'], result)
        self.assertTrue(result['anchors_valid'])
        self.assertEqual(restored['tables']['evidence_anchors'][0]['quote_text'], ' Pierwszy. Drugi.')

    def test_tamper_is_detected_by_payload_and_table_digests(self):
        package = self.export()
        package['tables']['events'][0]['body'] = 'changed event'
        result = packages.validate_metadata_package(package)
        self.assert_error(result, 'payload_digest_mismatch')
        self.assert_error(result, 'table_digest_mismatch')
        self.assertFalse(result['digest_valid'])

    def test_recomputed_digest_does_not_hide_missing_run_or_text(self):
        package = self.export()
        package['tables']['processing_runs'] = []
        result = packages.validate_metadata_package(rehash(package))
        self.assertTrue(result['digest_valid'])
        self.assert_error(result, 'broken_reference')
        package = self.export()
        package['tables']['derived_text'] = []
        result = packages.validate_metadata_package(rehash(package))
        self.assert_error(result, 'broken_reference')
        self.assertFalse(result['anchors_valid'])

    def test_later_transcript_does_not_change_pinned_quote(self):
        evidence.add_derived_text(self.artifact, 'transcript', ' New version.',
                                 segments=[{'start':0,'end':1,'text':' New version.'}])
        package = self.export()
        self.assertEqual(package['tables']['evidence_anchors'][0]['derived_text_id'], self.text)
        self.assertTrue(packages.validate_metadata_package(package)['valid'])

    def test_quote_hash_recalculation_cannot_hide_selector_or_version_changes(self):
        package = self.export()
        anchor = package['tables']['evidence_anchors'][0]
        anchor['quote_text'] = anchor['quote_text'].strip()
        anchor['quote_sha256'] = hashlib.sha256(anchor['quote_text'].encode()).hexdigest()
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'anchor_selector_mismatch')
        package = self.export()
        anchor = package['tables']['evidence_anchors'][0]
        selector = json.loads(anchor['selector_json'])
        selector['text_join'] = 'space_trim'
        anchor['selector_json'] = json.dumps(selector)
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'invalid_anchor_selector')
        package = self.export()
        package['tables']['derived_text'][0]['kind'] = 'summary'
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'anchor_version_mismatch')

    def test_case_relationships_cannot_be_reassigned_by_rehashing(self):
        package = self.export()
        case = deepcopy(package['tables']['cases'][0])
        case['id'] += 1000
        package['tables']['cases'].append(case)
        source = deepcopy(package['tables']['sources'][0])
        source['id'] += 1000
        source['case_id'] = case['id']
        package['tables']['sources'].append(source)
        package['tables']['source_observations'][0]['source_id'] = source['id']
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'cross_case_artifact')
        package = self.export()
        package['tables']['sources'][0]['case_id'] = 987654
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'broken_reference')

    def test_observation_content_identity_and_annotation_parent_are_verified(self):
        package = self.export()
        package['tables']['source_observations'][0]['sha256'] = 'different hash'
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'observation_content_mismatch')
        other_path = self.root/'other.wav'
        other_path.write_bytes(b'other artifact')
        other = evidence.ingest_file(other_path, source_id=self.source)
        package = self.export()
        package['tables']['annotations'][0]['artifact_id'] = other
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'annotation_artifact_mismatch')

    def test_parent_lineage_and_unattached_event_still_preserve_case_boundary(self):
        package = self.export()
        case = deepcopy(package['tables']['cases'][0])
        case['id'] += 1000
        package['tables']['cases'].append(case)
        source = deepcopy(package['tables']['sources'][0])
        source['id'] += 1000
        source['case_id'] = case['id']
        package['tables']['sources'].append(source)
        parent = deepcopy(package['tables']['artifacts'][0])
        parent['id'] += 1000
        parent['source_id'] = source['id']
        package['tables']['artifacts'].append(parent)
        package['tables']['artifacts'][0]['parent_artifact_id'] = parent['id']
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'cross_case_parent_artifact')
        package['tables']['artifacts'][0]['parent_artifact_id'] = None
        package['tables']['events'][0]['artifact_id'] = None
        package['tables']['events'][0]['source_id'] = source['id']
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'cross_case_annotation_event')

    def test_literal_paths_are_never_dereferenced_and_jobs_are_never_resumed(self):
        package = self.export()
        package['tables']['artifacts'][0]['stored_path'] = '/nonexistent/do-not-open.wav'
        package['tables']['artifacts'][0]['source_locator'] = 'https://example.invalid/no-request'
        package['tables']['jobs'][0]['status'] = 'running'
        package = rehash(package)
        with patch('builtins.open', side_effect=AssertionError('verifier attempted file access')):
            result = packages.validate_metadata_package(package)
        self.assertTrue(result['valid'], result)
        with database.session() as connection:
            self.assertEqual(connection.execute('SELECT status FROM jobs WHERE id=?', (self.job,)).fetchone()[0], 'done')

    def test_verifier_import_and_pure_projection_have_no_live_config_side_effects(self):
        isolated = self.root/'isolated'
        isolated.mkdir()
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
        environment['EW_DATA_DIR'] = str(isolated/'never-create-data')
        environment['EW_STORE_DIR'] = str(isolated/'never-create-store')
        environment['EW_DB_PATH'] = str(isolated/'never-create-db'/'evidence.db')
        code = "from server.app.packages import validate_metadata_package; from server.app.citations import projection_from_segments; validate_metadata_package({}); projection_from_segments([{'start':0,'end':1,'text':'text'}],[0])"
        subprocess.run([sys.executable, '-c', code], cwd=isolated, env=environment, check=True, capture_output=True)
        self.assertEqual(list(isolated.iterdir()), [])

    def test_duplicate_and_malformed_ids_return_errors_without_exceptions(self):
        package = self.export()
        package['tables']['artifacts'].append(deepcopy(package['tables']['artifacts'][0]))
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'duplicate_identity')
        package = self.export()
        package['tables']['artifacts'][0]['source_id'] = ['not', 'an', 'id']
        package['tables']['annotations'][0]['artifact_id'] = {'bad':'id'}
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'broken_reference')

    def test_nonfinite_json_and_missing_table_are_rejected(self):
        package = self.export()
        package['tables']['events'][0]['confidence'] = float('nan')
        self.assert_error(packages.validate_metadata_package(package), 'noncanonical_json')
        package = self.export()
        del package['tables']['links']
        self.assert_error(packages.validate_metadata_package(package), 'missing_or_invalid_table')

    def test_unknown_or_broken_links_are_not_silently_accepted(self):
        package = self.export()
        package['tables']['links'][0]['right_id'] = 998877
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'broken_object_reference')
        package = self.export()
        package['tables']['links'][0]['right_kind'] = 'external_project_object'
        self.assert_error(packages.validate_metadata_package(rehash(package)), 'unknown_link_target_kind')

    def test_anchors_are_append_only_after_migration(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with database.session() as connection:
                connection.execute("UPDATE evidence_anchors SET quote_text='overwrite'")
        with self.assertRaises(sqlite3.IntegrityError):
            with database.session() as connection:
                connection.execute('DELETE FROM evidence_anchors')

    def test_export_snapshot_stays_consistent_during_concurrent_commit(self):
        original_connect = database.connect
        changed = []
        class SnapshotConnection:
            def __init__(inner_self, *, read_only=False):
                self.assertTrue(read_only, 'export must request a SQLite read-only connection')
                inner_self.inner = original_connect(read_only=read_only)
            def execute(inner_self, sql, *args):
                cursor = inner_self.inner.execute(sql, *args)
                if sql.startswith('SELECT * FROM cases') and not changed:
                    with original_connect() as writer:
                        new_case = writer.execute("INSERT INTO cases(name) VALUES('concurrent new case')").lastrowid
                        writer.execute('UPDATE sources SET case_id=? WHERE id=?', (new_case,self.source))
                        changed.append(new_case)
                return cursor
            def rollback(inner_self):
                return inner_self.inner.rollback()
            def close(inner_self):
                return inner_self.inner.close()
        with patch.object(database, 'connect', SnapshotConnection):
            package = self.export()
        self.assertEqual(package['tables']['sources'][0]['case_id'], package['tables']['cases'][0]['id'])
        self.assertEqual(len(package['tables']['cases']), 1)
        self.assertTrue(packages.validate_metadata_package(package)['valid'])
        with original_connect() as connection:
            self.assertEqual(connection.execute('SELECT case_id FROM sources WHERE id=?', (self.source,)).fetchone()[0], changed[0])

    def test_export_missing_database_fails_without_creating_files(self):
        missing = self.root/'never-created.db'
        with patch.object(database, 'settings', replace(self.settings, db_path=missing)):
            before = {path.relative_to(self.root) for path in self.root.rglob('*')}
            with self.assertRaises(sqlite3.OperationalError):
                self.export()
            after = {path.relative_to(self.root) for path in self.root.rglob('*')}
            self.assertEqual(after, before)
            self.assertFalse(missing.exists())

    def test_read_only_connection_rejects_writes_and_escapes_literal_uri_filename(self):
        unusual = self.root/'literal ?mode=rwc&other=1#%ą.sqlite'
        with database.connect(unusual) as connection:
            connection.execute('CREATE TABLE check_read_only(value TEXT)')
            connection.execute("INSERT INTO check_read_only VALUES('retained')")
        before = unusual.read_bytes()
        connection = database.connect(unusual, read_only=True)
        try:
            self.assertEqual(connection.row_factory, sqlite3.Row)
            self.assertEqual(connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT value FROM check_read_only').fetchone()[0], 'retained')
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO check_read_only VALUES('forbidden')")
        finally:
            connection.close()
        self.assertEqual(unusual.read_bytes(), before)


class LegacyMetadataPackageTests(unittest.TestCase):
    def test_fresh_legacy_base_is_exported_read_only_without_fabricated_tables(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = replace(database.settings, db_path=root/'legacy.db')
            with patch.object(database, 'settings', settings):
                with database.connect() as connection:
                    connection.executescript(database.SCHEMA)
                    connection.execute("INSERT INTO cases(id,name) VALUES(1,'legacy')")
                    connection.execute("INSERT INTO sources(id,case_id,kind,label) VALUES(2,1,'legacy','raw')")
                    connection.execute("INSERT INTO artifacts(id,source_id,original_name,metadata_json) VALUES(3,2,'legacy.txt','not JSON')")
                    connection.commit()
                    before = connection.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
                package = packages.export_metadata_package()
                self.assertIn('source_observations', package['missing_tables'])
                self.assertIn('processing_runs', package['missing_tables'])
                self.assertEqual(package['tables']['artifacts'][0]['metadata_json'], 'not JSON')
                result = packages.validate_metadata_package(package)
                self.assertTrue(result['valid'], result)
                self.assertIn('legacy_incomplete_schema', [item['code'] for item in result['warnings']])
                with database.connect() as connection:
                    after = connection.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
                    self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])


if __name__ == '__main__':
    unittest.main()

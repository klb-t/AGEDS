"""Evidence integrity contracts, including upgrade and failure boundaries.

These tests use synthetic bytes only; a successful SHA comparison makes no
claim about the factual content or authorship of a source.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from server.app import db as database
from server.app import evidence


class IntegrityTests(unittest.TestCase):
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

    def tearDown(self):
        self.patch_evidence.stop()
        self.patch_db.stop()
        self.temporary.cleanup()

    def make_source(self, label='source', **kwargs):
        return evidence.ensure_source('synthetic', label, **kwargs)

    def make_input(self, name='sample.wav', content=b'synthetic\x00evidence'):
        path = self.root/name
        path.write_bytes(content)
        return path

    def count(self, table):
        with database.session() as connection:
            return connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_same_bytes_preserve_sources_and_every_acquisition(self):
        first_source = self.make_source('first')
        second_source = self.make_source('second')
        path = self.make_input()
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
        first = evidence.ingest_file(path, source_id=first_source, source_locator='relative/sample', metadata={'raw_name': 'mistake'})
        second = evidence.ingest_file(path, source_id=second_source, source_locator='relative/sample', original_name='other-name.wav', metadata={'hypothesis': 'unverified'})
        third = evidence.ingest_file(path, source_id=first_source, source_locator='relative/sample')
        self.assertEqual((first, second, third), (first, first, first))
        with database.session() as connection:
            artifact = connection.execute('SELECT * FROM artifacts').fetchone()
            observations = connection.execute('SELECT * FROM source_observations ORDER BY id').fetchall()
            logs = connection.execute("SELECT details_json FROM audit_log WHERE action='ingest' ORDER BY id").fetchall()
            self.assertEqual(artifact['source_id'], first_source)
            self.assertEqual(artifact['metadata_json'], '{"raw_name": "mistake"}')
            self.assertEqual([r['source_id'] for r in observations], [first_source, second_source, first_source])
            self.assertEqual(observations[1]['original_name'], 'other-name.wav')
            self.assertEqual(json.loads(observations[1]['metadata_json'])['source_metadata'], {'hypothesis': 'unverified'})
            self.assertEqual([json.loads(r[0])['deduplicated'] for r in logs], [False, True, True])
            self.assertEqual(connection.execute('SELECT count(*) FROM search_fts').fetchone()[0], 1)
            self.assertEqual(Path(artifact['stored_path']).read_bytes(), before[0])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode), before)

    def test_parallel_ingest_keeps_one_artifact_and_all_observations(self):
        sources = [self.make_source(str(i)) for i in range(8)]
        path = self.make_input(content=b'parallel capture'*10000)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda source: evidence.ingest_file(path, source_id=source, source_locator='shared'), sources))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.count('artifacts'), 1)
        self.assertEqual(self.count('source_observations'), 8)
        self.assertEqual(self.count('audit_log'), 8)

    def test_different_cases_are_not_silently_merged(self):
        source_a = self.make_source('same', account='one')
        with database.session() as connection:
            case_b = connection.execute("INSERT INTO cases(name) VALUES ('other case')").lastrowid
        source_b = self.make_source('same', account='one', case_id=case_b)
        path = self.make_input()
        artifact = evidence.ingest_file(path, source_id=source_a, source_locator='shared')
        with self.assertRaises(evidence.IntegrityError):
            evidence.ingest_file(path, source_id=source_b, source_locator='shared')
        with self.assertRaises(evidence.IntegrityError):
            evidence.add_event(source_id=source_b, artifact_id=artifact, event_type='sms', external_id='sms:row:0')
        self.assertEqual(self.count('source_observations'), 1)
        self.assertEqual(self.count('events'), 0)
        self.assertEqual(self.count('audit_log'), 1)

    def test_source_identity_keeps_case_and_account(self):
        first = self.make_source(account='one')
        self.assertEqual(first, self.make_source(account='one'))
        self.assertNotEqual(first, self.make_source(account='two'))
        with self.assertRaises(ValueError):
            self.make_source(case_id=987654)

    def test_event_reimport_is_idempotent_and_collision_is_visible(self):
        source = self.make_source()
        artifact = evidence.ingest_file(self.make_input(), source_id=source)
        kwargs = dict(source_id=source, artifact_id=artifact, event_type='sms',
                      external_id='sms_backup:row:0', body='original', metadata={'raw': 'unchanged'})
        event = evidence.add_event(**kwargs)
        self.assertEqual(evidence.add_event(**kwargs), event)
        with self.assertRaises(evidence.IntegrityError):
            evidence.add_event(**{**kwargs, 'body': 'reinterpretation'})
        # Two equal raw rows remain distinct when their source occurrences differ.
        second = evidence.add_event(**{**kwargs, 'external_id': 'sms_backup:row:1'})
        self.assertNotEqual(event, second)
        self.assertEqual(self.count('events'), 2)
        with database.session() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM audit_log WHERE action='event_reobserved'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM search_fts WHERE object_kind='event'").fetchone()[0], 2)

    def test_corrupted_store_is_detected_without_replacing_bytes(self):
        source = self.make_source()
        path = self.make_input()
        artifact = evidence.ingest_file(path, source_id=source)
        with database.session() as connection:
            stored = Path(connection.execute('SELECT stored_path FROM artifacts WHERE id=?', (artifact,)).fetchone()[0])
        stored.chmod(0o644)
        stored.write_bytes(b'tampered copy')
        with self.assertRaises(evidence.IntegrityError):
            evidence.ingest_file(path, source_id=source)
        self.assertEqual(stored.read_bytes(), b'tampered copy')
        self.assertEqual(self.count('source_observations'), 1)
        self.assertFalse(list(self.settings.store_dir.glob('.capture-*')))

    def test_existing_legacy_stored_path_cannot_be_a_symlink(self):
        source = self.make_source()
        path = self.make_input()
        artifact = evidence.ingest_file(path, source_id=source)
        link = self.root/'legacy-link'
        link.symlink_to(path)
        with database.session() as connection:
            connection.execute('UPDATE artifacts SET stored_path=? WHERE id=?', (str(link), artifact))
        with self.assertRaises(evidence.IntegrityError):
            evidence.ingest_file(path, source_id=source)
        self.assertEqual(self.count('source_observations'), 1)

    def test_capture_stat_difference_is_disclosed_not_reinterpreted(self):
        source = self.make_source()
        path = self.make_input(content=b'before capture')
        real_store = evidence.store_file
        def source_changes_after_capture(input_path):
            result = real_store(input_path)
            input_path.write_bytes(b'new source revision after the stream was captured')
            return result
        with patch.object(evidence, 'store_file', source_changes_after_capture):
            artifact = evidence.ingest_file(path, source_id=source)
        with database.session() as connection:
            row = connection.execute('SELECT * FROM artifacts WHERE id=?', (artifact,)).fetchone()
            observation = connection.execute('SELECT metadata_json FROM source_observations').fetchone()[0]
            self.assertEqual(row['sha256'], hashlib.sha256(b'before capture').hexdigest())
            self.assertEqual(row['size_bytes'], len(b'before capture'))
            self.assertTrue(json.loads(observation)['filesystem_changed_during_capture'])

    def test_observation_failure_rolls_back_artifact_fts_and_audit(self):
        source = self.make_source()
        with database.session() as connection:
            connection.execute("CREATE TRIGGER fail_observation BEFORE INSERT ON source_observations BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        path = self.make_input()
        with self.assertRaises(sqlite3.IntegrityError):
            evidence.ingest_file(path, source_id=source)
        for table in ('artifacts', 'source_observations', 'audit_log', 'search_fts'):
            self.assertEqual(self.count(table), 0)
        self.assertEqual(path.read_bytes(), b'synthetic\x00evidence')
        # Content store and SQLite are separate durability domains. An immutable,
        # unreferenced copy may remain; rollback never deletes shared evidence.
        self.assertEqual(len(list(self.settings.store_dir.glob('*/*/*'))), 1)

    def test_observations_and_audit_are_append_only(self):
        source = self.make_source()
        evidence.ingest_file(self.make_input(), source_id=source)
        for table in ('source_observations', 'audit_log'):
            with self.assertRaises(sqlite3.IntegrityError):
                with database.session() as connection:
                    connection.execute(f'DELETE FROM {table}')
            with self.assertRaises(sqlite3.IntegrityError):
                with database.session() as connection:
                    connection.execute(f'UPDATE {table} SET id=id+100')

    def test_text_run_requires_matching_artifact_and_caller_rollback(self):
        source = self.make_source()
        first = evidence.ingest_file(self.make_input(), source_id=source)
        other = evidence.ingest_file(self.make_input('other.wav', b'other bytes'), source_id=source)
        with database.session() as connection:
            run = connection.execute("INSERT INTO processing_runs(artifact_id,lease_token,status,started_at) VALUES (?, 'token', 'running', 'synthetic')", (first,)).lastrowid
        with self.assertRaises(evidence.IntegrityError):
            evidence.add_derived_text(other, 'transcript', 'wrong parent', run_id=run)
        with self.assertRaises(RuntimeError):
            with database.session() as connection:
                evidence.add_derived_text(first, 'transcript', 'rollback me', run_id=run, metadata={'test': True}, db=connection)
                raise RuntimeError('synthetic publication failure')
        self.assertEqual(self.count('derived_text'), 0)
        first_version = evidence.add_derived_text(first, 'transcript', 'version one', run_id=run, metadata={'status': 'synthetic'})
        second_version = evidence.add_derived_text(first, 'transcript', 'version two')
        self.assertNotEqual(first_version, second_version)
        with database.session() as connection:
            rows = connection.execute('SELECT * FROM derived_text ORDER BY id').fetchall()
            self.assertEqual(rows[0]['run_id'], run)
            self.assertIsNone(rows[1]['run_id'])


class MigrationTests(unittest.TestCase):
    def test_legacy_rows_and_raw_metadata_survive_idempotent_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            with database.connect(Path(temporary)/'legacy.db') as connection:
                connection.executescript(database.SCHEMA)
                connection.execute("INSERT INTO cases(id,name) VALUES (10,'legacy')")
                connection.execute("INSERT INTO sources(id,case_id,kind,label) VALUES (20,10,'legacy','source')")
                connection.execute("INSERT INTO artifacts(id,source_id,sha256,original_name,source_locator,metadata_json) VALUES(30,20,'legacy-hash','legacy.wav','legacy','{broken legacy JSON')")
                connection.execute("INSERT INTO events(id,source_id,artifact_id,event_type,body) VALUES(40,20,30,'sms','historical')")
                connection.execute("INSERT INTO derived_text(id,artifact_id,kind,text) VALUES(50,30,'transcript','historical words')")
                connection.execute("INSERT INTO annotations(id,artifact_id,derived_text_id,body) VALUES(60,30,50,'historical note')")
                connection.execute("INSERT INTO jobs(id,kind,artifact_id,status) VALUES(70,'transcribe',30,'running')")
                connection.execute("INSERT INTO audit_log(id,action,object_kind,object_id,details_json) VALUES(80,'ingest','artifact',30,'{old invalid details')")
                connection.commit()
                tables = ('cases','sources','artifacts','events','derived_text','annotations','jobs','audit_log')
                old = {table: [dict(row) for row in connection.execute(f'SELECT * FROM {table}')] for table in tables}
                database.migrate_db(connection)
                database.migrate_db(connection)
                for table in tables:
                    new = [dict(row) for row in connection.execute(f'SELECT * FROM {table}')]
                    self.assertEqual(len(new), len(old[table]))
                    for before, after in zip(old[table], new):
                        self.assertEqual(before, {key: after[key] for key in before})
                observations = connection.execute('SELECT * FROM source_observations').fetchall()
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0]['acquisition_kind'], 'legacy_snapshot')
                meta = json.loads(observations[0]['metadata_json'])
                self.assertEqual(meta['legacy_artifact_metadata_json'], '{broken legacy JSON')
                self.assertEqual(meta['historical_acquisition_count'], 'unknown')
                self.assertIsNone(connection.execute('SELECT run_id FROM derived_text').fetchone()[0])
                self.assertIsNone(connection.execute('SELECT lease_token FROM jobs').fetchone()[0])
                self.assertFalse(connection.execute('PRAGMA foreign_key_check').fetchall())

    def test_failed_migration_rolls_back_all_additive_ddl(self):
        with tempfile.TemporaryDirectory() as temporary:
            with database.connect(Path(temporary)/'legacy.db') as connection:
                connection.executescript(database.SCHEMA)
                original_add_column = database._add_column
                calls = []
                def fail_after_first_column(*args):
                    calls.append(args[2])
                    if len(calls) > 1:
                        raise RuntimeError('synthetic migration interruption')
                    original_add_column(*args)
                with patch.object(database, '_add_column', fail_after_first_column):
                    with self.assertRaises(RuntimeError):
                        database.migrate_db(connection)
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn('source_observations', tables)
                self.assertNotIn('processing_runs', tables)
                self.assertNotIn('schema_migrations', tables)
                columns = {row['name'] for row in connection.execute('PRAGMA table_info(jobs)')}
                self.assertNotIn('lease_token', columns)
                database.migrate_db(connection)
                self.assertTrue(connection.execute("SELECT 1 FROM sqlite_master WHERE name='source_observations'").fetchone())


if __name__ == '__main__':
    unittest.main()

"""Package P1 of the advocate model: additive migration and case assignments.

Synthetic data only. An assignment records a meaning in one case; it is not a
claim that the material is true, authored by anyone or evidentially weighty.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from server.app import assignments as model
from server.app import citations
from server.app import db as database
from server.app import evidence
from server.app.packages import export_metadata_package, validate_metadata_package


def row_digest(db, table, row_id):
    row = db.execute(f'SELECT * FROM {table} WHERE id=?', (row_id,)).fetchone()
    return hashlib.sha256(json.dumps(dict(row), sort_keys=True).encode()).hexdigest()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = replace(database.settings, data_dir=self.root,
                                db_path=self.root / 'evidence.db', store_dir=self.root / 'store')
        self.settings.store_dir.mkdir()
        self.patches = [patch.object(database, 'settings', self.settings),
                        patch.object(evidence, 'settings', self.settings)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()


class LegacyMigrationTests(Fixture):
    """P8: migrate_db keeps every pre-existing row and value."""

    def legacy_database(self):
        db = sqlite3.connect(self.settings.db_path)
        db.executescript(database.SCHEMA)
        db.execute("INSERT INTO cases(id,name) VALUES(1,'syntetyczna sprawa ż')")
        db.execute("INSERT INTO sources(id,case_id,kind,label) VALUES(1,1,'synthetic','s')")
        db.execute("""INSERT INTO artifacts(id,source_id,sha256,original_name,source_locator,metadata_json)
                      VALUES(1,1,'ab','nazwa.wav','loc','{"k":1}')""")
        db.execute("INSERT INTO links(id,left_kind,left_id,right_kind,right_id,relation) VALUES(1,'artifact',1,'artifact',1,'same')")
        db.commit()
        return db

    def snapshot(self, db, table, columns):
        return [tuple(row) for row in db.execute(f'SELECT {",".join(columns)} FROM {table} ORDER BY id')]

    def test_legacy_rows_unchanged_and_marked_before_model(self):
        legacy = self.legacy_database()
        columns = {table: [r[1] for r in legacy.execute(f'PRAGMA table_info({table})')]
                   for table in ('cases', 'sources', 'artifacts', 'links')}
        before = {table: self.snapshot(legacy, table, cols) for table, cols in columns.items()}
        legacy.close()
        database.init_db()
        database.init_db()  # idempotent
        with database.session() as db:
            for table, cols in columns.items():
                self.assertEqual(self.snapshot(db, table, cols), before[table], table)
            self.assertIsNone(db.execute('SELECT uid FROM cases WHERE id=1').fetchone()[0])
            self.assertIsNone(db.execute('SELECT uid FROM artifacts WHERE id=1').fetchone()[0])
            link = db.execute('SELECT case_id,author_principal_id,visibility_scope_id,status FROM links').fetchone()
            self.assertEqual(tuple(link), (None, None, None, None))
            self.assertLessEqual({1, 2}, {r[0] for r in db.execute('SELECT version FROM schema_migrations')})
            uid = model.ensure_uid(db, 'artifacts', 1)
            self.assertEqual(model.ensure_uid(db, 'artifacts', 1), uid)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='uid_assigned'").fetchone()[0], 1)

    def test_new_rows_receive_stable_uid(self):
        database.init_db()
        with database.session() as db:
            case_id = db.execute("INSERT INTO cases(name) VALUES('B')").lastrowid
            uid = db.execute('SELECT uid FROM cases WHERE id=?', (case_id,)).fetchone()[0]
            self.assertRegex(uid, '^[0-9a-f]{32}$')
            db.execute("UPDATE cases SET name='renamed' WHERE id=?", (case_id,))
            self.assertEqual(db.execute('SELECT uid FROM cases WHERE id=?', (case_id,)).fetchone()[0], uid)


class AssignmentTests(Fixture):
    def setUp(self):
        super().setUp()
        database.init_db()
        source = evidence.ensure_source('synthetic', 'nagrania')
        sample = self.root / 'rozmowa.wav'
        sample.write_bytes(b'RIFF synthetic bytes')
        self.artifact = evidence.ingest_file(sample, source_id=source, source_locator='synthetic://1')
        self.text = evidence.add_derived_text(self.artifact, 'transcript', 'ala ma kota', model='synthetic',
                                              segments=[{'start': 0.0, 'end': 0.5, 'text': 'ala ma kota'}])
        self.anchor = citations.create_citation(self.artifact, self.text, [0])['id']
        with database.session() as db:
            self.case_a = db.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()[0]
            self.case_b = db.execute("INSERT INTO cases(name) VALUES('sprawa B')").lastrowid
            self.owner = model.create_principal(db, 'właściciel', kind='human')
            self.ai = model.create_principal(db, 'model', kind='agent')

    def test_s1_one_material_in_two_cases(self):
        with database.session() as db:
            count = db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]
            uid = db.execute('SELECT uid FROM artifacts WHERE id=?', (self.artifact,)).fetchone()[0]
            a = model.assign(db, case_id=self.case_a, target_kind='artifact', target_id=self.artifact,
                             meaning_text='potwierdza termin', status='human_reviewed',
                             author_principal_id=self.owner, author_kind='human')
            b = model.assign(db, case_id=self.case_b, target_kind='artifact', target_id=self.artifact,
                             meaning_text='kontekst rozmowy', status='ai_proposed',
                             author_principal_id=self.ai, author_kind='ai')
            same_case = model.assign(db, case_id=self.case_a, target_kind='artifact', target_id=self.artifact,
                                     meaning_text='inne zagadnienie', issue_ref={'issue': 'synthetic-2'})
            self.assertEqual(a['warnings'], [])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0], count)
            self.assertEqual(model.cases_for_target(db, 'artifact', self.artifact), [self.case_a, self.case_b])
            rows = {r['id']: r for r in db.execute('SELECT * FROM assignments')}
            self.assertNotEqual(rows[a['id']]['meaning_text'], rows[b['id']]['meaning_text'])
            self.assertNotEqual(rows[a['id']]['status'], rows[b['id']]['status'])
            self.assertNotEqual(rows[a['id']]['author_principal_id'], rows[b['id']]['author_principal_id'])
            self.assertEqual(len(model.assignments_for_case(db, self.case_a)), 2)
            self.assertEqual({r['target_id'] for r in model.assignments_for_case(db, self.case_b)}, {self.artifact})
            self.assertEqual(db.execute('SELECT uid FROM artifacts WHERE id=?', (self.artifact,)).fetchone()[0], uid)
            self.assertNotIn('meaning', json.dumps([dict(r) for r in db.execute(
                "SELECT details_json FROM audit_log WHERE action='assignment_created'")]))
            self.assertNotEqual(same_case['uid'], a['uid'])

    def test_s2_withdraw_and_delete_leave_source_and_other_assignment(self):
        with database.session() as db:
            fragment = model.create_fragment(db, artifact_id=self.artifact, kind='citation',
                                             selector={'start_ms': 0, 'end_ms': 10},
                                             derived_text_id=self.text, anchor_id=self.anchor)
            a = model.assign(db, case_id=self.case_a, target_kind='fragment', target_id=fragment['id'])
            b = model.assign(db, case_id=self.case_b, target_kind='fragment', target_id=fragment['id'])
            protected = [('artifacts', self.artifact), ('derived_text', self.text),
                         ('evidence_anchors', self.anchor), ('fragments', fragment['id']),
                         ('assignments', b['id'])]
            before = {key: row_digest(db, *key) for key in protected}
            model.withdraw_assignment(db, a['id'], actor='synthetic-owner')
            self.assertEqual({key: row_digest(db, *key) for key in protected}, before)
            self.assertEqual(model.cases_for_target(db, 'fragment', fragment['id']), [self.case_b])
            withdrawn = db.execute('SELECT status,withdrawn_at FROM assignments WHERE id=?', (a['id'],)).fetchone()
            self.assertEqual(withdrawn['status'], 'withdrawn')
            self.assertIsNotNone(withdrawn['withdrawn_at'])
            model.delete_assignment(db, a['id'], actor='synthetic-owner')
            self.assertEqual({key: row_digest(db, *key) for key in protected}, before)
            self.assertIsNone(db.execute('SELECT 1 FROM assignments WHERE id=?', (a['id'],)).fetchone())
            actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE object_kind='assignment' ORDER BY id")]
            self.assertEqual(actions, ['assignment_created', 'assignment_created', 'assignment_status', 'assignment_deleted'])

    def test_reinstating_clears_withdrawal(self):
        with database.session() as db:
            a = model.assign(db, case_id=self.case_a, target_kind='artifact', target_id=self.artifact)
            model.withdraw_assignment(db, a['id'])
            model.set_assignment_status(db, a['id'], 'human_reviewed')
            self.assertEqual(len(model.assignments_for_case(db, self.case_a)), 1)

    def test_identity_and_target_are_fixed(self):
        with database.session() as db:
            a = model.assign(db, case_id=self.case_a, target_kind='artifact', target_id=self.artifact)
            for column, value in (('case_id', self.case_b), ('target_id', 999), ('uid', 'x'), ('author_kind', 'ai')):
                with self.subTest(column=column), self.assertRaisesRegex(sqlite3.IntegrityError, 'fixed'):
                    db.execute(f'UPDATE assignments SET {column}=? WHERE id=?', (value, a['id']))

    def test_open_values_warn_but_are_kept(self):
        with database.session() as db:
            result = model.assign(db, case_id=self.case_a, target_kind='page_range', target_id=3,
                                  status='do_sprawdzenia_przez_adwokata')
            self.assertEqual({w['code'] for w in result['warnings']},
                             {'unverified_target_kind', 'status_not_in_presets'})
            row = db.execute('SELECT target_kind,status FROM assignments WHERE id=?', (result['id'],)).fetchone()
            self.assertEqual(tuple(row), ('page_range', 'do_sprawdzenia_przez_adwokata'))

    def test_missing_known_targets_are_rejected(self):
        with database.session() as db:
            for kind in ('artifact', 'fragment', 'derived_text', 'event', 'evidence_anchor'):
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    model.assign(db, case_id=self.case_a, target_kind=kind, target_id=10_000)
            with self.assertRaises(ValueError):
                model.assign(db, case_id=10_000, target_kind='artifact', target_id=self.artifact)

    def test_fragments_are_append_only_and_bound_to_their_artifact(self):
        other = self.root / 'inne.wav'
        other.write_bytes(b'RIFF other synthetic bytes')
        source = evidence.ensure_source('synthetic', 'nagrania')
        other_artifact = evidence.ingest_file(other, source_id=source, source_locator='synthetic://2')
        with database.session() as db:
            with self.assertRaisesRegex(ValueError, 'another artifact'):
                model.create_fragment(db, artifact_id=other_artifact, kind='range', selector={},
                                      derived_text_id=self.text)
            with self.assertRaisesRegex(ValueError, 'another artifact'):
                model.create_fragment(db, artifact_id=other_artifact, kind='range', selector={},
                                      anchor_id=self.anchor)
            fragment = model.create_fragment(db, artifact_id=self.artifact, kind='page_range',
                                             selector={'pages': [3, 5]})
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                db.execute("UPDATE fragments SET kind='x' WHERE id=?", (fragment['id'],))
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                db.execute('DELETE FROM fragments WHERE id=?', (fragment['id'],))

    def test_artifact_versions_keep_both_versions(self):
        newer = self.root / 'rozmowa_v2.wav'
        newer.write_bytes(b'RIFF synthetic bytes, second version')
        source = evidence.ensure_source('synthetic', 'nagrania')
        second = evidence.ingest_file(newer, source_id=source, source_locator='synthetic://1')
        with database.session() as db:
            before = row_digest(db, 'artifacts', self.artifact)
            model.record_artifact_version(db, second, self.artifact)
            self.assertEqual(row_digest(db, 'artifacts', self.artifact), before)
            with self.assertRaises(ValueError):
                model.record_artifact_version(db, second, 10_000)
            with self.assertRaises(sqlite3.IntegrityError):
                model.record_artifact_version(db, second, second)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                db.execute('DELETE FROM artifact_versions')

    def test_case_relations_and_external_reference(self):
        with database.session() as db:
            model.relate_cases(db, self.case_a, self.case_b, 'same_owner')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM case_relations').fetchone()[0], 1)
            cur = db.execute('''INSERT INTO artifacts(source_id,original_name,stored_path,external_ref_json)
                VALUES(NULL,'zewnętrzny',NULL,?)''', (json.dumps({'url': 'https://example.invalid/doc', 'bytes_available': False}),))
            self.assertIsNotNone(db.execute('SELECT uid FROM artifacts WHERE id=?', (cur.lastrowid,)).fetchone()[0])

    def test_metadata_package_still_validates(self):
        with database.session() as db:
            model.assign(db, case_id=self.case_b, target_kind='artifact', target_id=self.artifact)
        package = export_metadata_package()
        result = validate_metadata_package(package)
        self.assertTrue(result['valid'], result['errors'])
        self.assertIn('uid', package['tables']['artifacts'][0])


if __name__ == '__main__':
    unittest.main()

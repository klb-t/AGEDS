"""Package P5: packages, live/snapshot modes, independence and scoped export.

Synthetic data only. A snapshot freezes object versions, not the truth of them.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings, strategies as st

from server.app import assignments as model
from server.app import authz, citations, db, evidence, sharing
from server.app.packages import validate_metadata_package
from server.tests.test_authz import World, canaries_in, canary, memory_db


class PackageModeTests(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.world = World(self.db)
        self.case_a, self.case_b = self.world.case(), self.world.case()
        self.first = self.world.artifact(self.case_a)
        self.recipient = model.create_principal(self.db, 'advocate')

    def share(self, mode, item=('case', None), item_mode=None):
        package = sharing.create_package(self.db, 'for the advocate', mode=mode)
        kind, object_id = item
        sharing.add_item(self.db, package['id'], kind, object_id if object_id is not None else self.case_a, mode=item_mode)
        authz.grant(self.db, self.recipient, package['scope_id'], ['read_metadata', 'read_text'])
        return package

    def visible(self, kind):
        return {row['id'] for row in authz.ContentGate(self.db, self.recipient).list(kind, 'read_text')}

    def test_s11_snapshot_freezes_and_live_follows(self):
        snapshot = self.share('snapshot')
        self.assertEqual(self.visible('artifact'), set())  # nothing until a snapshot is taken
        sharing.snapshot_package(self.db, snapshot['id'])
        self.assertEqual(self.visible('artifact'), {self.first})
        later = self.world.artifact(self.case_a)
        newer_text = self.db.execute("INSERT INTO derived_text(artifact_id,kind,text) VALUES(?,'transcript','v2')",
                                     (self.first,)).lastrowid
        self.assertEqual(self.visible('artifact'), {self.first})
        self.assertNotIn(newer_text, self.visible('derived_text'))
        live_recipient = self.recipient
        self.recipient = model.create_principal(self.db, 'live reader')
        self.share('live')
        self.assertEqual(self.visible('artifact'), {self.first, later})
        self.assertIn(newer_text, self.visible('derived_text'))
        self.recipient = live_recipient
        sharing.snapshot_package(self.db, snapshot['id'])  # explicit refresh is an audited widening
        self.assertEqual(self.visible('artifact'), {self.first, later})

    def test_withdrawal_narrows_both_modes_and_never_widens(self):
        package = self.share('snapshot')
        item_b = sharing.add_item(self.db, package['id'], 'case', self.case_b)
        sharing.snapshot_package(self.db, package['id'])
        before = self.visible('case')
        self.assertEqual(before, {self.case_a, self.case_b})
        self.world.artifact(self.case_a)  # added after the snapshot
        sharing.withdraw_item(self.db, item_b)
        self.assertEqual(self.visible('case'), {self.case_a})
        self.assertEqual(self.visible('artifact'), {self.first})
        live = self.share('live')
        self.assertIn(self.case_a, self.visible('case'))
        for item in self.db.execute('SELECT id FROM package_items WHERE package_id=?', (live['id'],)).fetchall():
            sharing.withdraw_item(self.db, item['id'])
        self.assertEqual(self.visible('case'), {self.case_a})  # still through the snapshot package

    def test_mixed_item_modes(self):
        package = self.share('snapshot', item=('artifact', self.first), item_mode='live')
        self.assertEqual(self.visible('artifact'), {self.first})

    def test_unknown_mode_is_kept_and_covers_nothing(self):
        package = sharing.create_package(self.db, 'odd', mode='every_tuesday')
        self.assertEqual(package['warnings'][0]['code'], 'mode_not_in_presets')
        sharing.add_item(self.db, package['id'], 'case', self.case_a)
        authz.grant(self.db, self.recipient, package['scope_id'], ['read_metadata'])
        sharing.snapshot_package(self.db, package['id'])
        self.assertEqual(self.visible('case'), set())

    def test_revoking_the_package_grant_removes_access(self):
        package = self.share('live')
        grant_id = self.db.execute('SELECT id FROM grants WHERE scope_id=?', (package['scope_id'],)).fetchone()[0]
        authz.revoke_grant(self.db, grant_id, revoked_by='owner')
        self.assertEqual(self.visible('artifact'), set())

    def test_s10_same_material_in_two_cases_counts_once(self):
        self.world.assign(self.case_a, 'artifact', self.first)
        self.world.assign(self.case_b, 'artifact', self.first)
        package = self.share('live')
        sharing.add_item(self.db, package['id'], 'case', self.case_b)
        summary = sharing.independence_summary(self.db, authz.ContentGate(self.db, self.recipient))
        self.assertEqual(summary['independent_sources'], 1)
        self.assertEqual(summary['groups'][0]['case_occurrences'], 2)

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.lists(st.tuples(st.integers(0, 2), st.integers(0, 3)), max_size=8))
    def test_p6_independent_sources_equal_unique_contents(self, assignments):
        db_ = memory_db()
        world = World(db_)
        cases = [world.case() for _ in range(3)]
        artifacts = [world.artifact(cases[i % 3]) for i in range(4)]
        # Two artifacts share content (same bytes seen at two locators).
        db_.execute('UPDATE artifacts SET sha256=(SELECT sha256 FROM artifacts WHERE id=?) WHERE id=?', (artifacts[0], artifacts[1]))
        for case_index, artifact_index in assignments:
            world.assign(cases[case_index], 'artifact', artifacts[artifact_index])
        owner = authz.bootstrap_owner(db_, 'owner')['principal_id']
        summary = sharing.independence_summary(db_, authz.ContentGate(db_, owner))
        unique = {r['sha256'] for r in db_.execute('SELECT sha256 FROM artifacts')}
        self.assertEqual(summary['independent_sources'], len(unique))
        self.assertEqual(sum(g['case_occurrences'] for g in summary['groups']), len(assignments))


class ScopedExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'store').mkdir()
        settings_ = replace(db.settings, data_dir=root, db_path=root / 'db.sqlite', store_dir=root / 'store')
        self.patches = [patch.object(db, 'settings', settings_), patch.object(evidence, 'settings', settings_)]
        for item in self.patches:
            item.start()
        db.init_db()
        self.root = root

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def material(self, name, case_id, text):
        path = self.root / name
        path.write_bytes(name.encode())
        source = evidence.ensure_source('synthetic', f'{text} source', case_id=case_id)
        artifact = evidence.ingest_file(path, source_id=source, source_locator=f'synthetic://{name}')
        version = evidence.add_derived_text(artifact, 'transcript', text, segments=[{'start': 0, 'end': 1, 'text': text}])
        citations.create_citation(artifact, version, [0])
        return artifact

    def test_p9_scoped_export_validates_and_excludes_hidden_rows(self):
        hidden = 'CANARY-export-hidden'
        with db.session() as conn:
            case_a = conn.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()[0]
            case_b = conn.execute('INSERT INTO cases(name) VALUES(?)', (hidden,)).lastrowid
        shared = self.material('shared.wav', case_b, hidden)  # provenance in the hidden case
        self.material('own.wav', case_a, 'jawne')
        self.material('secret.wav', case_b, hidden)
        with db.session() as conn:
            model.assign(conn, case_id=case_a, target_kind='artifact', target_id=shared, meaning_text='w sprawie A')
            model.assign(conn, case_id=case_b, target_kind='artifact', target_id=shared, meaning_text=hidden)
            recipient = model.create_principal(conn, 'advocate')
            scope = authz.create_scope(conn, 'case', {'case_id': case_a})
            authz.grant(conn, recipient, scope, ['export', 'read_metadata', 'read_text'])
            package = sharing.export_scope_package(conn, recipient)
            metadata_only = model.create_principal(conn, 'metadata reader')
            authz.grant(conn, metadata_only, authz.create_scope(conn, 'case', {'case_id': case_a}), ['export', 'read_metadata'])
            without_text = sharing.export_scope_package(conn, metadata_only)
            export_only = model.create_principal(conn, 'export only')
            authz.grant(conn, export_only, authz.create_scope(conn, 'case', {'case_id': case_a}), ['export'])
            nothing = sharing.export_scope_package(conn, export_only)
        result = validate_metadata_package(package)
        self.assertTrue(result['valid'], result['errors'])
        exported = {name: {r['id'] for r in rows} for name, rows in package['tables'].items() if name != 'schema_migrations'}
        self.assertEqual(len(exported['artifacts']), 2)
        self.assertEqual(package['tables']['audit_log'], [])
        text = json.dumps(package, ensure_ascii=False)
        # The shared material's own transcript is visible; the hidden case's name and meanings are not.
        self.assertNotIn(f'"name": "{hidden}"', text)
        self.assertNotIn('"meaning_text": "CANARY', text)
        self.assertTrue(all(r['stored_path'] is None for r in package['tables']['artifacts']))
        self.assertIn({'table': 'artifacts', 'id': shared, 'field': 'source_id', 'reason': 'outside_scope'},
                      package['redacted_references'])
        self.assertFalse(package['scope']['counts_disclosed'])
        # Export without read_text: rows and identities, no text and no citations; still a valid package.
        self.assertTrue(validate_metadata_package(without_text)['valid'], validate_metadata_package(without_text)['errors'])
        self.assertEqual(len(without_text['tables']['artifacts']), 2)
        self.assertTrue(all(r['text'] is None and r['segments_json'] is None for r in without_text['tables']['derived_text']))
        self.assertEqual(without_text['tables']['evidence_anchors'], [])
        self.assertIn('capability', {r['reason'] for r in without_text['redacted_references']})
        self.assertNotIn('jawne', json.dumps(without_text['tables']['derived_text'], ensure_ascii=False))  # labels are metadata
        self.assertEqual(nothing['tables']['artifacts'], [])  # export alone does not even reveal metadata


if __name__ == '__main__':
    unittest.main()

"""Package P7: a cached visible index equals one computed from scratch.

Generated synthetic operations interleave grants, revocations, assignments,
withdrawals, new documents, settings and package snapshots with searches.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st

from server.app import assignments as model
from server.app import authz, search, sharing
from server.app import db as database
from server.tests.test_authz import World

OPERATIONS = st.lists(st.one_of(
    st.tuples(st.just('grant'), st.integers(0, 2), st.sampled_from(['read_text', 'chat_with_advocate'])),
    st.tuples(st.just('revoke'), st.integers(0, 5)),
    st.tuples(st.just('assign'), st.integers(0, 2), st.integers(0, 5)),
    st.tuples(st.just('withdraw'), st.integers(0, 5)),
    st.tuples(st.just('document'), st.integers(0, 2)),
    st.tuples(st.just('policy'), st.sampled_from(['owner_only', 'treat_as_case'])),
    st.tuples(st.just('package'), st.integers(0, 2)),
    st.tuples(st.just('suspend'), st.booleans()),
), min_size=1, max_size=12)


class SearchCacheTests(unittest.TestCase):
    def file_db(self, directory):
        db = sqlite3.connect(Path(directory) / 'cache.sqlite')
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript(database.SCHEMA)
        database.migrate_db(db)
        return db

    def search_both(self, db, principal):
        gate = authz.ContentGate(db, principal)
        cached = search.scoped_search_page('umowa OR CANARY', 50, gate)
        fresh_docs = search._scan_visible_documents(db, authz.ContentGate(db, principal))
        cached_docs = search._visible_documents(db, authz.ContentGate(db, principal))
        return cached, fresh_docs, cached_docs

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(OPERATIONS)
    def test_p5_cache_equals_no_cache(self, operations):
        search.clear_cache()
        with tempfile.TemporaryDirectory() as directory:
            db = self.file_db(directory)
            world = World(db)
            cases = [world.case() for _ in range(3)]
            for index in range(3):
                aid = world.artifact(cases[index])
                tid = next(t for t, a in world.texts.items() if a == aid)
                db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES('derived_text',?,?,?)",
                           (tid, 't', f'umowa CANARY-{index}'))
            principal = model.create_principal(db, 'recipient')
            grants, assignments = [], []
            db.commit()
            self.search_both(db, principal)  # warm the cache before any change
            for operation in operations:
                kind = operation[0]
                if kind == 'grant':
                    scope = authz.create_scope(db, 'case', {'case_id': cases[operation[1]]})
                    grants.append(authz.grant(db, principal, scope, [operation[2], 'read_text'])['id'])
                elif kind == 'revoke' and grants:
                    authz.revoke_grant(db, grants[operation[1] % len(grants)], revoked_by='t')
                elif kind == 'assign':
                    artifact = list(world.artifacts)[operation[2] % len(world.artifacts)]
                    assignments.append(model.assign(db, case_id=cases[operation[1]], target_kind='artifact',
                                                    target_id=artifact)['id'])
                elif kind == 'withdraw' and assignments:
                    model.withdraw_assignment(db, assignments[operation[1] % len(assignments)])
                elif kind == 'document':
                    aid = world.artifact(cases[operation[1]])
                    tid = next(t for t, a in world.texts.items() if a == aid)
                    db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES('derived_text',?,?,?)",
                               (tid, 't', 'umowa nowa'))
                elif kind == 'policy':
                    authz.set_setting(db, 'legacy_scope_policy',
                                      'owner_only' if operation[1] == 'owner_only' else f'treat_as_case:{cases[0]}')
                elif kind == 'package':
                    package = sharing.create_package(db, 'p', mode='snapshot')
                    sharing.add_item(db, package['id'], 'case', cases[operation[1]])
                    sharing.snapshot_package(db, package['id'])
                    grants.append(authz.grant(db, principal, package['scope_id'], ['read_text'])['id'])
                elif kind == 'suspend':
                    db.execute('UPDATE principals SET status=? WHERE id=?',
                               ('suspended' if operation[1] else 'active', principal))
                db.commit()
                cached, fresh, cached_docs = self.search_both(db, principal)
                self.assertEqual(cached_docs, fresh, operation)
                search.clear_cache()
                uncached = search.scoped_search_page('umowa OR CANARY', 50, authz.ContentGate(db, principal))
                self.assertEqual(json.dumps(cached, sort_keys=True), json.dumps(uncached, sort_keys=True), operation)
            db.close()

    def test_cache_is_used_and_never_shared_between_principals(self):
        search.clear_cache()
        with tempfile.TemporaryDirectory() as directory:
            db = self.file_db(directory)
            world = World(db)
            case = world.case()
            aid = world.artifact(case)
            tid = next(iter(world.texts))
            db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES('derived_text',?,?,'umowa')", (tid, 't'))
            first, second = model.create_principal(db, 'a'), model.create_principal(db, 'b')
            authz.grant(db, first, authz.create_scope(db, 'case', {'case_id': case}), ['read_text'])
            db.commit()
            before = dict(search.cache_stats)
            search.scoped_search_page('umowa', 5, authz.ContentGate(db, first))
            search.scoped_search_page('umowa', 5, authz.ContentGate(db, first))
            self.assertEqual(search.cache_stats['hits'] - before['hits'], 1)
            self.assertEqual(search.scoped_search_page('umowa', 5, authz.ContentGate(db, second))['results'], [])
            db.close()


if __name__ == '__main__':
    unittest.main()

"""Package P4: search sees only what the principal may read.

Generated synthetic documents; hidden ones contain the same words as visible
ones so that leaks through ranking statistics or has_more would show.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings, strategies as st

from server.app import assignments as model
from server.app import authz, db, evidence
from server.app.search import scoped_search_page, search_page

WORDS = ('ala', 'kot', 'umowa', 'termin', 'CANARY')


class ScopedSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = []

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def use(self, name):
        for item in reversed(self.patches):
            item.stop()
        self.counter = getattr(self, 'counter', 0) + 1
        directory = self.root / f'{name}-{self.counter}'
        (directory / 'store').mkdir(parents=True)
        settings_ = replace(db.settings, data_dir=directory, db_path=directory / 'db.sqlite', store_dir=directory / 'store')
        self.patches = [patch.object(db, 'settings', settings_), patch.object(evidence, 'settings', settings_)]
        for item in self.patches:
            item.start()
        db.init_db()

    def populate(self, documents, hidden_documents=()):
        """documents: list of (visible, text). Returns the recipient principal id."""
        with db.session() as conn:
            visible_case = conn.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()[0]
            hidden_case = conn.execute("INSERT INTO cases(name) VALUES('hidden')").lastrowid
        for index, (visible, text) in enumerate(list(documents) + [(False, t) for t in hidden_documents]):
            case = visible_case if visible else hidden_case
            path = self.root / f'doc-{self.counter}-{index}-{case}.bin'
            path.write_bytes(f'{index}:{case}:{text}'.encode())
            source = evidence.ensure_source('synthetic', f's{case}', case_id=case)
            artifact = evidence.ingest_file(path, source_id=source, source_locator=f'synthetic://{index}/{case}')
            evidence.add_derived_text(artifact, 'transcript', text)
        with db.session() as conn:
            principal = model.create_principal(conn, 'recipient')
            scope = authz.create_scope(conn, 'case', {'case_id': visible_case})
            authz.grant(conn, principal, scope, ['read_text', 'read_metadata'])
        return principal

    def run_scoped(self, principal, q, limit):
        with db.session() as conn:
            return scoped_search_page(q, limit, authz.ContentGate(conn, principal))

    sentences = st.lists(st.sampled_from(WORDS), min_size=1, max_size=6).map(' '.join)

    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
    @given(st.lists(st.tuples(st.booleans(), sentences), min_size=1, max_size=6),
           st.lists(sentences, max_size=4), st.sampled_from(WORDS[:4]), st.integers(1, 3))
    def test_equals_index_of_visible_documents_and_ignores_hidden(self, documents, extra_hidden, word, limit):
        self.use('a')
        principal = self.populate(documents)
        scoped = self.run_scoped(principal, word, limit)
        # Oracle: an installation containing only the visible documents.
        self.use('b')
        self.populate([(True, text) for visible, text in documents if visible])
        oracle = search_page(word, limit)
        strip = lambda page: [(r['object_kind'], r['snippet'], r['rank']) for r in page['results']] + [page['has_more']]
        self.assertEqual(strip(scoped), strip(oracle))
        # Non-interference: more hidden documents with the same words change nothing.
        self.use('c')
        principal = self.populate(documents, extra_hidden + [word] * 3)
        self.assertEqual(json.dumps(strip(self.run_scoped(principal, word, limit))), json.dumps(strip(scoped)))

    def test_hidden_canary_never_matches(self):
        self.use('d')
        principal = self.populate([(True, 'ala kot'), (False, 'CANARY umowa')])
        self.assertEqual(self.run_scoped(principal, 'CANARY', 10)['results'], [])
        self.assertEqual(len(self.run_scoped(principal, 'ala', 10)['results']), 1)


if __name__ == '__main__':
    unittest.main()

"""Synthetic FTS syntax, bounded work, coverage and operational error boundaries."""
from dataclasses import replace
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from server.app import db
search = importlib.import_module('server.app.search')


class SearchValidationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'search.sqlite'
        settings = replace(db.settings, db_path=self.path)
        setting_patch = patch.object(db, 'settings', settings)
        setting_patch.start()
        self.addCleanup(setting_patch.stop)
        db.init_db()
        self.raw = 'alpha beta café Zażółć <script>raw</script> & "literal"'
        with db.session() as conn:
            conn.executemany('INSERT INTO search_fts(object_kind,object_id,title,content) VALUES (?,?,?,?)',
                             [('event', 1, 'first', self.raw), ('event', 2, 'second', 'alpha gamma'),
                              ('event', 3, 'third', 'alpha beta')])

    def test_invalid_query_types_and_limit_even_when_query_empty(self):
        for q in (None, False, 1, b'alpha', [], {}):
            with self.subTest(q=q), self.assertRaises(search.SearchQueryError):
                search.search(q)
        for limit in (True, False, 0, -1, 201, 1.0, '1', None, 10**100):
            with self.subTest(limit=limit), self.assertRaises(search.SearchQueryError):
                search.search('', limit)

    def test_malformed_quote_unknown_column_and_operator_syntax(self):
        for q in ('"unterminated', 'missing:alpha', 'alpha OR', 'NEAR(alpha beta, nope)',
                  'alpha AND OR beta', ')alpha(', '((alpha)', 'alpha\x00beta'):
            with self.subTest(q=q), self.assertRaises(search.SearchQueryError):
                search.search(q)

    def test_structural_limits_before_live_database(self):
        bad = ['('*17 + 'alpha' + ')'*17,
               ' OR '.join(['alpha']*65),
               'NEAR(' + ' '.join(['alpha']*129) + ', 10)']
        with patch.object(search, 'connect', side_effect=AssertionError('no live DB for invalid input')):
            for q in bad:
                with self.subTest(q=q[:30]), self.assertRaises(search.SearchQueryError):
                    search.search(q)

    def test_utf8_byte_budget_and_surrogate_validation(self):
        search.validate_query('"' + 'é'*2047 + '"')  # exactly 4096 bytes
        for q in ('"' + 'é'*2048 + '"', ' '*4097, '\ud800', '😀'*1025):
            with self.subTest(q=q[:30]), self.assertRaises(search.SearchQueryError):
                search.search(q)

    def test_quote_aware_depth_and_escaped_quotes(self):
        search.validate_query('"' + '('*25 + ')'*25 + '"')
        search.validate_query('"alpha ""beta"""')
        search.validate_query('('*16 + 'alpha' + ')'*16)
        with patch.object(search, 'MAX_QUERY_TOKENS', 1):
            search.validate_query('"alpha beta gamma"')
            search.validate_query('"alpha ""beta"""')
            with self.assertRaises(search.SearchQueryError):
                search.validate_query('alpha beta')

    def test_valid_fts_operators_preserve_original_query_and_source(self):
        cases = {'alpha NOT gamma': {1, 3}, 'title:first': {1},
                 '"alpha beta"': {1, 3}, 'NEAR(alpha beta, 2)': {1, 3},
                 'alp*': {1, 2, 3}, '(gamma OR café) AND alpha': {1, 2},
                 '{title content}: first': {1}, '^alpha': {1, 2, 3},
                 '  alpha  ': {1, 2, 3}}
        for query, expected in cases.items():
            with self.subTest(query=query):
                page = search.search_page(query)
                self.assertEqual(page['query'], query)
                self.assertEqual({item['object_id'] for item in page['results']}, expected)
        self.assertEqual(search.search('café')[0]['snippet'], self.raw)
        with db.session() as conn:
            self.assertEqual(conn.execute('SELECT content FROM search_fts WHERE object_id=1').fetchone()[0], self.raw)

    def test_limit_plus_one_reports_truncation_and_complete_boundary(self):
        for limit, more in ((1, True), (2, True), (3, False), (4, False)):
            with self.subTest(limit=limit):
                page = search.search_page('alpha', limit)
                self.assertEqual(len(page['results']), min(3, limit))
                self.assertEqual(page['limit'], limit)
                self.assertEqual(page['has_more'], more)
                self.assertEqual(page['complete'], not more)
                self.assertEqual(search.search('alpha', limit), page['results'])
        self.assertEqual(search.search_page('absent')['results'], [])
        self.assertTrue(search.search_page('absent')['complete'])

    def test_empty_query_never_opens_live_database(self):
        with patch.object(search, 'connect', side_effect=AssertionError('no query')):
            page = search.search_page(' \t\n', 200)
        self.assertEqual(page, {'query': ' \t\n', 'results': [], 'limit': 200, 'has_more': False, 'complete': True})

    def test_missing_database_and_schema_remain_operational_errors(self):
        with patch.object(db, 'settings', replace(db.settings, db_path=self.path.parent/'missing.sqlite')):
            with self.assertRaises(sqlite3.OperationalError):
                search.search('alpha')
            self.assertFalse((self.path.parent/'missing.sqlite').exists())
        empty = self.path.parent/'empty.sqlite'
        sqlite3.connect(empty).close()
        with patch.object(db, 'settings', replace(db.settings, db_path=empty)):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'no such table'):
                search.search('alpha')

    def test_live_operational_error_never_becomes_query_error(self):
        # An error text resembling FTS input failure in the live DB is still an
        # operational error: syntax was already parsed in an isolated index.
        class Broken:
            def set_progress_handler(self, *args): pass
            def close(self): pass
            def execute(self, *args): raise sqlite3.OperationalError('no such column: broken_schema')
        with patch.object(search, 'connect', return_value=Broken()):
            with self.assertRaises(sqlite3.OperationalError):
                search.search('alpha')

    def test_deterministic_work_budget_is_not_partial_success(self):
        with patch.object(search, 'PROGRESS_INTERVAL', 1), patch.object(search, 'MAX_VM_STEPS', 1):
            with self.assertRaises(search.SearchWorkLimitError):
                search.search_page('alpha')
        # The interrupted connection is disposed; later valid work succeeds.
        self.assertEqual(len(search.search('alpha')), 3)

    def test_unrelated_sqlite_interrupt_remains_operational(self):
        class Interrupted:
            def set_progress_handler(self, *args): pass
            def close(self): pass
            def execute(self, *args):
                error = sqlite3.OperationalError('interrupted')
                error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
                raise error
        with patch.object(search, 'connect', return_value=Interrupted()):
            with self.assertRaises(sqlite3.OperationalError):
                search.search('alpha')

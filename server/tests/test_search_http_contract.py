"""HTTP search preserves the old list body but exposes bounded coverage/errors."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.app import db, main


class SearchHttpContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        settings = replace(db.settings, db_path=root / 'test.sqlite', data_dir=root, store_dir=root / 'store')
        self.patches = [patch.object(module, 'settings', settings) for module in (db, main)]
        for p in self.patches:
            p.start()
        db.init_db()
        with db.session() as conn:
            conn.executemany('INSERT INTO search_fts(object_kind,object_id,title,content) VALUES (?,?,?,?)',
                             [('event', i, 'raw title', 'needle <script>raw</script>') for i in range(1, 202)])
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_api_list_compatibility_and_explicit_truncation_headers(self):
        response = self.client.get('/api/search', params={'q': 'needle'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 200)
        self.assertEqual(response.headers['x-ageds-search-has-more'], 'true')
        self.assertEqual(response.headers['x-ageds-search-complete'], 'false')
        self.assertEqual(response.headers['x-ageds-search-limit'], '200')
        empty = self.client.get('/api/search', params={'q': 'absent'})
        self.assertEqual(empty.json(), [])
        self.assertEqual(empty.headers['x-ageds-search-complete'], 'true')

    def test_html_discloses_coverage_and_keeps_source_markup_inert(self):
        response = self.client.get('/', params={'q': 'needle'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('pokazano pierwsze 100', response.text)
        self.assertNotIn('<script>raw</script>', response.text)
        self.assertIn('&lt;script&gt;', response.text)

    def test_invalid_syntax_is_explicit_not_empty_results_or_server_error(self):
        for query in ('"unfinished', 'unknowncolumn:needle', 'x ' * 5000):
            with self.subTest(query=query[:30]):
                api = self.client.get('/api/search', params={'q': query})
                self.assertEqual(api.status_code, 422)
                self.assertIsInstance(api.json()['detail'], str)
                html = self.client.get('/', params={'q': query})
                self.assertEqual(html.status_code, 422)
                self.assertIn('role="alert"', html.text)
                self.assertNotIn('<p>Brak.</p>', html.text)

    def test_work_limit_is_503_without_partial_success(self):
        with patch.object(main, 'search_page', side_effect=main.SearchWorkLimitError('private detail')):
            api = self.client.get('/api/search', params={'q': 'needle'})
            self.assertEqual(api.status_code, 503)
            self.assertNotIn('private detail', api.text)
            html = self.client.get('/', params={'q': 'needle'})
            self.assertEqual(html.status_code, 503)
            self.assertIn('role="alert"', html.text)
            self.assertNotIn('private detail', html.text)

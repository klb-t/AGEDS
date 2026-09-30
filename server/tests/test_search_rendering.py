"""FTS results are untrusted source text, never executable HTML."""
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.app import db, evidence, main
from server.app.search import search


class ParsedHtml(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


class SearchRenderingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        settings = replace(db.settings, db_path=self.root/'test.sqlite',
                           data_dir=self.root, store_dir=self.root/'store')
        self.patches = [patch.object(module, 'settings', settings) for module in (db, evidence, main)]
        for item in self.patches:
            item.start()
        db.init_db()
        self.raw = '<script>alert(1)</script> <img src=x onerror=alert(2)> needle & <mark>raw</mark>'
        source = evidence.ensure_source('synthetic', 'search rendering')
        self.event = evidence.add_event(source_id=source, artifact_id=None, event_type='message',
                                       subject='<svg onload=alert(3)>', body=self.raw)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def assert_source_unchanged(self):
        with db.session() as connection:
            self.assertEqual(connection.execute('SELECT body FROM events WHERE id=?', (self.event,)).fetchone()[0], self.raw)
            self.assertEqual(connection.execute("SELECT content FROM search_fts WHERE object_kind='event' AND object_id=?", (self.event,)).fetchone()[0], self.raw)

    def test_fts_snippet_is_plain_source_text_with_no_generated_html(self):
        result = search('needle')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['object_id'], self.event)
        self.assertEqual(result[0]['snippet'], self.raw)
        self.assert_source_unchanged()

    def test_http_search_escapes_source_markup_but_keeps_literal_text_and_api_data(self):
        with TestClient(main.app) as client:
            page = client.get('/', params={'q': 'needle'})
            self.assertEqual(page.status_code, 200)
            parsed = ParsedHtml()
            parsed.feed(page.text)
            self.assertFalse([tag for tag, _ in parsed.tags if tag in ('script', 'img', 'svg', 'mark')])
            self.assertFalse([attrs for _, attrs in parsed.tags if any(name.startswith('on') for name in attrs)])
            self.assertIn(self.raw, parsed.text)
            result = client.get('/api/search', params={'q': 'needle'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()[0]['snippet'], self.raw)
        self.assert_source_unchanged()


if __name__ == '__main__':
    unittest.main()

"""Bounded read-model pages over generated metadata only."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import citations, db, read_pages as pages


class ReadPageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'data?#%.sqlite'
        patcher = patch.object(db, 'settings', replace(db.settings, db_path=self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        with db.session() as conn:
            self.aid = conn.execute("INSERT INTO artifacts(original_name) VALUES ('synthetic.wav')").lastrowid
            self.other = conn.execute("INSERT INTO artifacts(original_name) VALUES ('other.wav')").lastrowid
            for ident in (2, 9, 100):
                conn.execute("INSERT INTO derived_text(id,artifact_id,kind,text,segments_json) VALUES (?,?,'transcript','large unused text','[]')", (ident, self.aid))
            conn.execute("INSERT INTO derived_text(id,artifact_id,kind,text) VALUES (99,?,'transcript','other')", (self.other,))
            conn.execute("INSERT INTO derived_text(id,artifact_id,kind,text) VALUES (101,?,'summary','not transcript')", (self.aid,))

    def test_sparse_order_snapshot_append_and_empty(self):
        first = pages.read_page(self.aid, 'transcripts', limit=2)
        self.assertEqual([item['id'] for item in first['items']], [100, 9])
        self.assertEqual((first['snapshotMaxId'], first['nextBeforeId'], first['hasMore']), (100, 9, True))
        with db.session() as conn:
            conn.execute("INSERT INTO derived_text(id,artifact_id,kind,text) VALUES (200,?,'transcript','new')", (self.aid,))
        second = pages.read_page(self.aid, 'transcripts', limit=2, before_id=9, snapshot_max_id=100)
        self.assertEqual([item['id'] for item in second['items']], [2])
        self.assertFalse(second['hasMore'])
        self.assertIsNone(second['nextBeforeId'])
        self.assertEqual(pages.read_page(self.aid, 'annotations')['snapshotMaxId'], 0)
        empty = pages.read_page(self.aid, 'transcripts', before_id=1, snapshot_max_id=0)
        self.assertEqual(empty['items'], [])

    def test_annotations_and_citations_match_existing_shapes(self):
        selector = citations.projection_from_segments([{'start': 0, 'end': 1, 'text': 'raw'}], [0])
        with db.session() as conn:
            conn.execute("INSERT INTO annotations(artifact_id,body,derived_text_id) VALUES (?,'literal <script>',100)", (self.aid,))
            conn.execute('''INSERT INTO evidence_anchors(artifact_id,derived_text_id,start_ms,end_ms,quote_text,quote_sha256,selector_json)
                VALUES (?,100,?,?,?,?,?)''', (self.aid, 0, 1000, 'raw', selector['quote_sha256'], json.dumps(selector['selector'])))
            expected = citations.anchor_payload(conn.execute('SELECT * FROM evidence_anchors').fetchone())
        annotation = pages.read_page(self.aid, 'annotations')['items'][0]
        self.assertEqual(annotation['body'], 'literal <script>')
        self.assertEqual(annotation['derivedTextId'], 100)
        self.assertEqual(annotation['artifactId'], self.aid)
        self.assertEqual(pages.read_page(self.aid, 'citations')['items'], [expected])

    def test_validation_and_missing_artifact(self):
        invalid = [dict(limit=True), dict(limit=0), dict(limit=101), dict(before_id=1),
                   dict(snapshot_max_id=2), dict(before_id=0, snapshot_max_id=2),
                   dict(before_id=3, snapshot_max_id=2), dict(before_id=2, snapshot_max_id=2**63)]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(pages.PageInputError):
                pages.read_page(self.aid, 'transcripts', **kwargs)
        with self.assertRaises(pages.PageNotFound):
            pages.read_page(999, 'transcripts')
        with self.assertRaises(pages.PageInputError):
            pages.read_page(True, 'transcripts')
        with self.assertRaises(pages.PageInputError):
            pages.read_page(self.aid, 'jobs')

    def test_huge_omitted_transcript_data_is_not_materialized(self):
        with db.session() as conn:
            conn.execute("UPDATE derived_text SET text=?,segments_json=? WHERE artifact_id=?", ('x' * 2_000_000, '{' * 2_000_000, self.aid))
        result = pages.read_page(self.aid, 'transcripts')
        self.assertEqual(len(result['items']), 3)
        self.assertNotIn('text', result['items'][0])

    def test_oversized_next_row_defers_then_fails_without_skip(self):
        with db.session() as conn:
            conn.execute("UPDATE derived_text SET model=? WHERE id=9", ('x' * 500,))
        with patch.object(pages, 'MAX_ROW_BYTES', 400):
            first = pages.read_page(self.aid, 'transcripts')
            self.assertEqual([row['id'] for row in first['items']], [100])
            self.assertTrue(first['hasMore'])
            with self.assertRaises(pages.PageLimitError):
                pages.read_page(self.aid, 'transcripts', before_id=first['nextBeforeId'], snapshot_max_id=first['snapshotMaxId'])

    def test_page_budget_stops_with_usable_cursor(self):
        first_item_page = pages.read_page(self.aid, 'transcripts', limit=1)
        budget = len(pages._json_bytes(first_item_page)) + 2
        with patch.object(pages, 'MAX_PAGE_BYTES', budget):
            first = pages.read_page(self.aid, 'transcripts')
            self.assertEqual(len(first['items']), 1)
            self.assertTrue(first['hasMore'])
            self.assertEqual(first['nextBeforeId'], 100)

    def test_read_only_snapshot_and_sqlite_work_limit(self):
        real = db.connect
        statements = []
        def connect(*args, **kwargs):
            self.assertIs(kwargs.get('read_only'), True)
            conn = real(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        before = self.path.read_bytes()
        with patch.object(db, 'connect', side_effect=connect):
            pages.read_page(self.aid, 'transcripts')
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(any(sql.split()[0].upper() in {'INSERT','UPDATE','DELETE','CREATE','ALTER','DROP'} for sql in statements))
        self.assertFalse(any('OFFSET' in sql.upper() or 'COUNT(' in sql.upper() for sql in statements))
        with patch.object(pages, 'SQLITE_PROGRESS_INTERVAL', 1), patch.object(pages, 'MAX_SQLITE_STEPS', 1), self.assertRaises(pages.PageLimitError):
            pages.read_page(self.aid, 'transcripts')

    def test_malformed_selector_rejected_without_repair(self):
        for raw in ('{broken', '{"kind":"segments","kind":"words"}', '{"x":NaN}', '[]', '{"x":1e999}'):
            with self.subTest(raw=raw), self.assertRaises(pages.PageStoredError):
                pages._selector(raw)
        with self.assertRaises(pages.PageLimitError):
            pages._selector('[' * 33 + ']' * 33)

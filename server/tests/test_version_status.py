"""Superseded text versions: append-only marks, default selection skips them (generated data)."""
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from server.app import db, evidence
from server.app.main import app
from server.app.media.comparison import artifact_versions
from server.app.media.version_status import mark, superseded
from server.tests.test_media_support import MediaCase


class VersionStatusTests(MediaCase):
    def setUp(self):
        super().setUp()
        self.aid = self.add_audio()
        with db.session() as conn:
            self.old = evidence.add_derived_text(self.aid, 'transcript', 'stara wersja', db=conn)
            self.new = evidence.add_derived_text(self.aid, 'transcript', 'nowa wersja', db=conn)
            self.ocr = evidence.add_derived_text(self.aid, 'ocr', 'tekst', db=conn)

    def test_mark_validates_and_is_append_only(self):
        with db.session() as conn:
            for kwargs in ({'superseded_by_id': self.ocr}, {'superseded_by_id': self.old}, {'superseded_by_id': 999999}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    mark(conn, self.old, reason='r', actor='t', **kwargs)
            with self.assertRaises(ValueError):
                mark(conn, self.old, superseded_by_id=self.new, reason='  ', actor='t')
            row = mark(conn, self.old, superseded_by_id=self.new, reason='validator defect', actor='t')
            with self.assertRaises(ValueError):  # a superseded version cannot supersede another
                mark(conn, self.new, superseded_by_id=self.old, reason='r', actor='t')
            for sql in ('UPDATE derived_text_status SET reason=? WHERE id=?', 'DELETE FROM derived_text_status WHERE id=? OR ?'):
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(sql, ('x', row) if sql.startswith('UPDATE') else (row, 0))
            self.assertEqual(superseded(conn)[self.old]['superseded_by_id'], self.new)
            mark(conn, self.old, superseded_by_id=None, reason='reinstated after review', actor='t')
            self.assertEqual(superseded(conn), {})
        self.assertEqual(self.rows('derived_text')[0]['text'], 'stara wersja')  # the version is untouched

    def test_default_transcript_and_lists_skip_superseded_versions(self):
        with db.session() as conn:
            newest = evidence.add_derived_text(self.aid, 'transcript', 'wadliwa najnowsza', db=conn)
            mark(conn, newest, superseded_by_id=self.new, reason='validator defect', actor='t')
        with TestClient(app) as client:
            default = client.get(f'/api/artifacts/{self.aid}/transcript').json()
            self.assertEqual(default['text'], 'nowa wersja')
            explicit = client.get(f'/api/artifacts/{self.aid}/transcript', params={'derived_text_id': newest})
            self.assertEqual(explicit.json()['text'], 'wadliwa najnowsza')  # still addressable
            page = client.get(f'/artifact/{self.aid}').text
            self.assertIn(f'zastąpiona przez #{self.new}', page)
        listed = {v['id']: v['superseded'] for v in artifact_versions(self.aid)}
        self.assertEqual((listed[newest]['reason'], listed[self.new]), ('validator defect', None))

"""Synthetic citations pin exact stored text and one concrete transcript version."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from server.app import citations, db, evidence


class ProjectionTests(unittest.TestCase):
    def segments(self):
        return [
            {'start': 0, 'end': 0.1, 'text': 'outside selection'},
            {'start': 0.1234, 'end': 1.1, 'text': ' słowo  '},
            {'start': 1.1, 'end': 1.2346, 'text': 'Koniec.\n'},
        ]

    def test_projection_preserves_exact_spacing_and_utf8_hash(self):
        segments = self.segments()
        original = json.dumps(segments, ensure_ascii=False)
        projection = citations.projection_from_segments(segments, [1, 2])
        exact = ' słowo  Koniec.\n'
        self.assertEqual(projection['quote_text'], exact)
        self.assertEqual(projection['quote_sha256'], hashlib.sha256(exact.encode('utf-8')).hexdigest())
        self.assertEqual(projection['start_ms'], 123)
        self.assertEqual(projection['end_ms'], 1235)
        self.assertEqual(projection['selector']['precision'], 'segment')
        self.assertEqual(projection['selector']['rounding'], 'nearest_ms')
        self.assertEqual(projection['selector']['time_unit'], 'seconds')
        self.assertEqual(projection['selector']['stored_time_unit'], 'milliseconds')
        self.assertEqual(projection['selector']['text_join'], 'concatenate_exact')
        self.assertEqual(projection['selector']['indices'], [1, 2])
        self.assertEqual(json.dumps(segments, ensure_ascii=False), original)

    def test_adjacent_or_gapped_segments_are_valid_without_interpolating_text(self):
        segments = [{'start': 0, 'end': 1, 'text': 'A'}, {'start': 1, 'end': 2, 'text': 'B'}]
        self.assertEqual(citations.projection_from_segments(segments, [0, 1])['quote_text'], 'AB')
        segments[1]['start'] = 1.5
        self.assertEqual(citations.projection_from_segments(segments, [0, 1])['quote_text'], 'AB')

    def test_zero_duration_is_valid_segment_precision(self):
        projection = citations.projection_from_segments([{'start': 1, 'end': 1, 'text': 'point'}], [0])
        self.assertEqual((projection['start_ms'], projection['end_ms']), (1000, 1000))

    def test_rejects_invalid_segment_index_selections(self):
        selections = [None, (), {}, {'indices': [0]}, '0', False, True, {0}, [], [-1], [True], [False], [0.0], ['0'],
                      [0, 0], [1, 0], [0, 2], [3], [1, 3], [2, 1, 2]]
        for selection in selections:
            with self.subTest(selection=selection):
                with self.assertRaises(ValueError):
                    citations.projection_from_segments(self.segments(), selection)

    def test_huge_out_of_bounds_indices_fail_without_range_allocation(self):
        with self.assertRaises(ValueError):
            citations.projection_from_segments(self.segments(), [0, 10 ** 100])

    def test_rejects_invalid_segments_container(self):
        for segments in (None, {}, 'not segments', (), []):
            with self.subTest(segments=segments):
                with self.assertRaises(ValueError):
                    citations.projection_from_segments(segments, [0])

    def test_rejects_negative_nonfinite_boolean_missing_and_reversed_times(self):
        invalid_times = [
            (-1, 1), (0, -1), (float('nan'), 1), (0, float('nan')),
            (float('inf'), 1), (0, float('inf')), (float('-inf'), 1),
            (True, 1), (0, False), (None, 1), (0, None), ('0', 1), (2, 1),
        ]
        for start, end in invalid_times:
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    citations.projection_from_segments([{'start': start, 'end': end, 'text': 'x'}], [0])
        for segment in ({'end': 1, 'text': 'x'}, {'start': 0, 'text': 'x'}):
            with self.subTest(segment=segment):
                with self.assertRaises(ValueError):
                    citations.projection_from_segments([segment], [0])

    def test_unrepresentable_milliseconds_raise_validation_error(self):
        for huge_time in (1e16, 1e308, 10 ** 1000):
            with self.subTest(time_kind=type(huge_time).__name__, sqlite_overflow=huge_time == 1e16):
                with self.assertRaises(ValueError):
                    citations.projection_from_segments([{'start': 0, 'end': huge_time, 'text': 'x'}], [0])

    def test_rejects_overlapping_or_out_of_order_selected_segments(self):
        for second_start in (0.9, 0):
            segments = [
                {'start': 0, 'end': 1, 'text': 'A'},
                {'start': second_start, 'end': 2, 'text': 'B'},
            ]
            with self.subTest(second_start=second_start):
                with self.assertRaises(ValueError):
                    citations.projection_from_segments(segments, [0, 1])

    def test_rejects_missing_blank_and_nontext_segment_content(self):
        segments = [None, 'x', {}, {'start': 0, 'end': 1},
                    {'start': 0, 'end': 1, 'text': None},
                    {'start': 0, 'end': 1, 'text': 123},
                    {'start': 0, 'end': 1, 'text': ''},
                    {'start': 0, 'end': 1, 'text': ' \t\n'}]
        for segment in segments:
            with self.subTest(segment=segment):
                with self.assertRaises(ValueError):
                    citations.projection_from_segments([segment], [0])


class CitationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        settings = replace(db.settings, data_dir=self.root,
                           db_path=self.root / 'evidence.db', store_dir=self.root / 'store')
        for module in (db, evidence):
            patcher = patch.object(module, 'settings', settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        source_id = evidence.ensure_source('synthetic', 'citations')
        original = self.root / 'synthetic.wav'
        original.write_bytes(b'synthetic audio fixture, not a decoded recording')
        self.artifact_id = evidence.ingest_file(original, source_id=source_id, source_locator='synthetic:original')
        self.segments = [
            {'start': 0.1234, 'end': 1.1, 'text': ' słowo  '},
            {'start': 1.1, 'end': 1.2346, 'text': 'Koniec.\n'},
        ]
        self.version_id = evidence.add_derived_text(
            self.artifact_id, 'transcript', ' słowo  Koniec.\n', segments=self.segments,
            model='synthetic-asr-version-one', metadata={'truth_status': 'unverified'},
        )

    def count(self, table):
        with db.session() as connection:
            return connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_citation_pins_exact_stored_version_after_new_transcript_publication(self):
        anchor = citations.create_citation(self.artifact_id, self.version_id, [0, 1])
        newer = evidence.add_derived_text(
            self.artifact_id, 'transcript', 'new interpretation',
            segments=[{'start': 0, 'end': 2, 'text': 'new interpretation'}],
            model='synthetic-asr-version-two',
        )
        self.assertNotEqual(self.version_id, newer)
        with db.session() as connection:
            stored = dict(connection.execute('SELECT * FROM evidence_anchors WHERE id=?', (anchor['id'],)).fetchone())
        self.assertEqual(stored['derived_text_id'], self.version_id)
        self.assertEqual(stored['artifact_id'], self.artifact_id)
        self.assertEqual(stored['quote_text'], ' słowo  Koniec.\n')
        self.assertEqual(stored['quote_sha256'], hashlib.sha256(' słowo  Koniec.\n'.encode('utf-8')).hexdigest())
        self.assertEqual((stored['start_ms'], stored['end_ms']), (123, 1235))
        self.assertEqual(anchor['selector']['precision'], 'segment')
        self.assertEqual(anchor['validation'], 'matches_stored_transcript_version')
        self.assertEqual(anchor['audio_verification'], 'not_performed')
        # Re-selecting an older concrete version remains possible.
        another = citations.create_citation(self.artifact_id, self.version_id, [0])
        self.assertEqual(another['derived_text_id'], self.version_id)
        self.assertEqual(another['quote_text'], ' słowo  ')

    def test_wrong_artifact_kind_or_absent_version_does_not_create_anchor(self):
        source = evidence.ensure_source('synthetic', 'other')
        other = self.root / 'other.wav'
        other.write_bytes(b'a different synthetic artifact')
        other_id = evidence.ingest_file(other, source_id=source)
        ocr_id = evidence.add_derived_text(self.artifact_id, 'ocr', 'OCR words', segments=self.segments)
        attempts = [(other_id, self.version_id), (self.artifact_id, ocr_id),
                    (self.artifact_id, 987654321), (987654321, self.version_id)]
        before_audit = self.count('audit_log')
        for artifact_id, version_id in attempts:
            with self.subTest(artifact_id=artifact_id, version_id=version_id):
                with self.assertRaises(ValueError):
                    citations.create_citation(artifact_id, version_id, [0])
        self.assertEqual(self.count('evidence_anchors'), 0)
        self.assertEqual(self.count('audit_log'), before_audit)

    def test_invalid_selection_is_atomic_for_anchor_and_audit(self):
        before_audit = self.count('audit_log')
        for indices in ([True], [-1], [], [0, 0], [0, 2], [1, 0], [2]):
            with self.subTest(indices=indices):
                with self.assertRaises(ValueError):
                    citations.create_citation(self.artifact_id, self.version_id, indices)
        self.assertEqual(self.count('evidence_anchors'), 0)
        self.assertEqual(self.count('audit_log'), before_audit)

    def test_concrete_version_ids_reject_bool_string_and_unrepresentable_integers(self):
        before_audit = self.count('audit_log')
        attempts = [(False, self.version_id), (True, self.version_id),
                    (str(self.artifact_id), self.version_id),
                    (self.artifact_id, False), (self.artifact_id, True),
                    (self.artifact_id, str(self.version_id)),
                    (10 ** 1000, self.version_id), (self.artifact_id, 10 ** 1000),
                    ('9' * 1000, self.version_id)]
        for artifact_id, version_id in attempts:
            with self.subTest(artifact_kind=type(artifact_id).__name__, version_kind=type(version_id).__name__):
                with self.assertRaises(ValueError):
                    citations.create_citation(artifact_id, version_id, [0])
        self.assertEqual(self.count('evidence_anchors'), 0)
        self.assertEqual(self.count('audit_log'), before_audit)

    def test_invalid_stored_segments_fail_without_changing_sources(self):
        invalid_values = ['{bad json', 'null', '{}', '"not a list"']
        for raw in invalid_values:
            with self.subTest(raw=raw):
                with db.session() as connection:
                    invalid_id = connection.execute(
                        "INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES (?, 'transcript', 'legacy-invalid', ?)",
                        (self.artifact_id, raw),
                    ).lastrowid
                with self.assertRaises(ValueError):
                    citations.create_citation(self.artifact_id, invalid_id, [0])
        self.assertEqual(self.count('evidence_anchors'), 0)
        with db.session() as connection:
            row = connection.execute('SELECT segments_json FROM derived_text WHERE id=?', (self.version_id,)).fetchone()
        self.assertEqual(json.loads(row['segments_json']), self.segments)

    def test_anchors_are_append_only(self):
        anchor = citations.create_citation(self.artifact_id, self.version_id, [0, 1])
        for sql in ('DELETE FROM evidence_anchors WHERE id=?',
                    "UPDATE evidence_anchors SET quote_text='changed' WHERE id=?"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    with db.session() as connection:
                        connection.execute(sql, (anchor['id'],))
        self.assertEqual(self.count('evidence_anchors'), 1)

    def test_anchor_insert_failure_rolls_back_audit(self):
        before = self.count('audit_log')
        with db.session() as connection:
            connection.execute("CREATE TRIGGER fail_anchor BEFORE INSERT ON evidence_anchors BEGIN SELECT RAISE(ABORT, 'synthetic rejection'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            citations.create_citation(self.artifact_id, self.version_id, [0, 1])
        self.assertEqual(self.count('evidence_anchors'), 0)
        self.assertEqual(self.count('audit_log'), before)

    def test_audit_insert_failure_rolls_back_anchor(self):
        before = self.count('audit_log')
        with db.session() as connection:
            connection.execute("CREATE TRIGGER fail_anchor_audit BEFORE INSERT ON audit_log WHEN NEW.action='anchor_create' BEGIN SELECT RAISE(ABORT, 'synthetic audit rejection'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            citations.create_citation(self.artifact_id, self.version_id, [0, 1])
        self.assertEqual(self.count('evidence_anchors'), 0)
        self.assertEqual(self.count('audit_log'), before)


if __name__ == '__main__':
    unittest.main()

"""Producer boundaries, raw preservation and read-only export on synthetic data."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import citations, db, evidence, exchange


class ExchangeProducerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'literal?#%.sqlite'
        self.settings = replace(db.settings, db_path=self.path)
        patcher = patch.object(db, 'settings', self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        self.locator = 'file:///private/$(touch SHOULD_NOT_EXIST)?#%'
        with db.session() as conn:
            self.aid = conn.execute('''INSERT INTO artifacts(original_name,source_locator,stored_path,
                sha256,size_bytes,metadata_json) VALUES ('synthetic.wav',?,?,?,0,?)''',
                (self.locator, '/private/server.wav', hashlib.sha256(b'').hexdigest(), '{broken NaN')).lastrowid
        self.raw = '[ {"start":0.0005,"end":1,"text":" Zażółć","words":[{"start":NaN}]} ]'
        with db.session() as conn:
            self.tid = conn.execute('''INSERT INTO derived_text(artifact_id,kind,text,segments_json,metadata_json)
                VALUES (?,'transcript','literal full text unrelated to concatenation',?,'{bad')''',
                (self.aid, self.raw)).lastrowid
        self.anchor = citations.create_citation(self.aid, self.tid, [0])

    def export(self, **kwargs):
        return exchange.export_citation_packet(self.aid, self.anchor['id'], **kwargs)

    def test_preserves_pinned_raw_unknowns_and_digest_without_opening_paths(self):
        evidence.add_derived_text(self.aid, 'transcript', 'newer', segments=[{'start': 0, 'end': 1, 'text': 'newer'}])
        # Flush prior writers before taking the byte-level read-only receipt.
        conn = db.connect()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
        before = self.path.read_bytes()
        with patch('builtins.open', side_effect=AssertionError('no source IO')), patch.object(Path, 'open', side_effect=AssertionError('no source IO')):
            packet = self.export()
        self.assertEqual(self.path.read_bytes(), before)
        payload = packet['payload']
        self.assertEqual(payload['derived_text']['id'], self.tid)
        self.assertEqual(payload['derived_text']['segments_json'], self.raw)
        self.assertEqual(payload['derived_text']['metadata_json'], '{bad')
        self.assertEqual(payload['artifact']['metadata_json'], '{broken NaN')
        self.assertEqual(payload['artifact']['source_locator'], self.locator)
        self.assertNotIn('stored_path', payload['artifact'])
        self.assertIn('processing_run_unknown', payload['unknowns'])
        self.assertIn('acquisition_history_unknown', payload['unknowns'])
        self.assertEqual(payload['projection']['start_ms'], 0)
        digest = hashlib.sha256(exchange.DOMAIN.encode() + exchange.canonical_json(payload)).hexdigest()
        self.assertEqual(packet['integrity']['payload_sha256'], digest)
        self.assertEqual(packet, self.export())

    def test_storage_path_option_is_explicit_and_literal(self):
        packet = self.export(include_stored_path=True)
        self.assertEqual(packet['payload']['artifact']['stored_path'], '/private/server.wav')
        self.assertTrue(packet['payload']['scope']['stored_path_included'])
        with self.assertRaises(ValueError):
            self.export(include_stored_path=1)

    def test_invalid_stored_segments_reject_without_repair(self):
        for raw in ('{broken', '[{"start":0,"end":1,"text":"a","text":"b"}]', '{}'):
            with db.session() as conn:
                conn.execute('UPDATE derived_text SET segments_json=? WHERE id=?', (raw, self.tid))
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.export()
            with db.connect(read_only=True) as conn:
                self.assertEqual(conn.execute('SELECT segments_json FROM derived_text WHERE id=?', (self.tid,)).fetchone()[0], raw)

    def test_stored_version_tamper_rejected(self):
        with db.session() as conn:
            conn.execute('UPDATE derived_text SET segments_json=? WHERE id=?',
                         ('[{"start":0,"end":1,"text":"altered"}]', self.tid))
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.export()

    def test_limits_and_missing_ids(self):
        for key, value in (('MAX_PACKET_BYTES', 10), ('MAX_SEGMENTS', 0), ('MAX_NODES', 2), ('MAX_DEPTH', 1)):
            with self.subTest(key=key), patch.object(exchange, key, value), self.assertRaises(exchange.PacketLimitError):
                self.export()
        with self.assertRaises(exchange.PacketNotFound):
            exchange.export_citation_packet(self.aid + 1, self.anchor['id'])
        for value in (True, 0, -1, '1', 2**63):
            with self.subTest(value=value), self.assertRaises(ValueError):
                exchange.export_citation_packet(value, self.anchor['id'])

    def test_read_only_sqlite_connection_requested_and_no_mutating_statements(self):
        real = db.connect
        statements = []
        def connect(*args, **kwargs):
            self.assertIs(kwargs.get('read_only'), True)
            conn = real(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(db, 'connect', side_effect=connect):
            self.export()
        self.assertTrue(statements)
        self.assertFalse(any(s.split()[0].upper() in {'INSERT','UPDATE','DELETE','CREATE','DROP','ALTER','REPLACE'} for s in statements))

    def test_run_raw_error_and_unknown_fields_preserved_without_lease_token(self):
        with db.session() as conn:
            run_id = conn.execute('''INSERT INTO processing_runs(artifact_id,lease_token,status,started_at,
                error,parameters_json) VALUES (?,'private-token','failed','rawtime','raw failure','{NaN')''', (self.aid,)).lastrowid
            conn.execute('UPDATE derived_text SET run_id=? WHERE id=?', (run_id, self.tid))
        run = self.export()['payload']['processing_run']
        self.assertEqual(run['error'], 'raw failure')
        self.assertEqual(run['parameters_json'], '{NaN')
        self.assertNotIn('lease_token', run)

    def test_observation_bound_applied_before_export(self):
        with db.session() as conn:
            for _ in range(2):
                conn.execute('''INSERT INTO source_observations(artifact_id,observed_at,original_name)
                    VALUES (?,'unknown','literal')''', (self.aid,))
        with patch.object(exchange, 'MAX_OBSERVATIONS', 1), self.assertRaises(exchange.PacketLimitError):
            self.export()

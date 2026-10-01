"""Independent N21 exchange acceptance using synthetic, hostile literal metadata.

Digest construction here is deliberately independent of both exchange modules.
No private corpus, network, model, device, restore, or partner integration.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.app import citations, db, evidence

REPO = Path(__file__).resolve().parents[2]
DOMAIN = b'ageds.citation-evidence/v1\n'
RAW_ERROR = '{broken JSON; NaN; błędny numer +48??; e\u0301 ≠ é'
LOCATOR = 'file:///never/read/../../private; $(touch SHOULD_NOT_EXIST); content://fixture/音声'


def redigest(packet):
    raw = json.dumps(packet['payload'], ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    packet['integrity']['payload_sha256'] = hashlib.sha256(DOMAIN + raw).hexdigest()
    return packet


class ExchangeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = replace(db.settings, data_dir=self.root, db_path=self.root/'fixture.sqlite',
                                store_dir=self.root/'store')
        p = patch.object(db, 'settings', self.settings)
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        with db.session() as conn:
            case = conn.execute("SELECT id FROM cases LIMIT 1").fetchone()[0]
            sid = conn.execute("INSERT INTO sources(case_id,kind,label,locator,metadata_json) VALUES (?,'synthetic','N21',?,?)",
                               (case, LOCATOR, RAW_ERROR)).lastrowid
            self.aid = conn.execute("INSERT INTO artifacts(source_id,sha256,original_name,mime_type,source_locator,stored_path,metadata_json) VALUES (?,?,?,'audio/wav',?,?,?)",
                                    (sid, 'a'*64, 'raw wrong +48?? 音声.wav', LOCATOR, '/never/read/source.wav', RAW_ERROR)).lastrowid
            self.run = conn.execute("INSERT INTO processing_runs(artifact_id,lease_token,status,started_at,tool,parameters_json,error) VALUES (?,?,'done','2026-10-01','synthetic',?,?)",
                                    (self.aid, 'must-not-leak-lease', '{"raw":NaN}', RAW_ERROR)).lastrowid
            for locator in (LOCATOR, 'https://example.invalid/do-not-fetch'):
                conn.execute("INSERT INTO source_observations(artifact_id,source_id,source_locator,observed_at,original_name,sha256,metadata_json) VALUES (?,?,?,'unknown','raw-name',?,?)",
                             (self.aid, sid, locator, 'a'*64, RAW_ERROR))
        self.segments = [
            {'start': .125, 'end': 1.25, 'text': ' Zażółć  e\u0301', 'words': [
                {'start': .125, 'end': .5, 'word': ' Zażółć'},
                {'start': .7, 'end': 1.25, 'word': '  e\u0301'}]},
            {'start': 1.5, 'end': 2.0004, 'text': '\n音声!?', 'words': [
                {'start': 1.5004, 'end': 2.0004, 'word': '\n音声!?'}]}]
        self.tid = evidence.add_derived_text(self.aid, 'transcript', 'raw display error preserved',
                                            segments=self.segments, run_id=self.run)
        self.anchor = citations.create_citation(self.aid, self.tid, word_refs=[
            {'segment_index': 0, 'word_index': 1}, {'segment_index': 1, 'word_index': 0}])
        self.newer = evidence.add_derived_text(self.aid, 'transcript', 'NEW VERSION',
                                             segments=[{'start': 0, 'end': 2, 'text': 'NEW VERSION'}])
        self.env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO), os.environ.get('PYTHONPATH', '')]),
                        PYTHONDONTWRITEBYTECODE='1', EW_DATA_DIR=str(self.root),
                        EW_DB_PATH=str(self.settings.db_path), EW_STORE_DIR=str(self.root/'store'))

    def packet(self):
        from server.app.exchange import export_citation_packet
        return export_citation_packet(self.aid, self.anchor['id'])

    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'server.app.cli', *map(str,args)],
                              env=self.env, cwd=self.root, capture_output=True, text=True, timeout=20)

    def test_producer_readonly_exact_raw_and_independent_digest(self):
        before = self.settings.db_path.read_bytes()
        with patch('builtins.open', side_effect=AssertionError('source must not be opened')), \
             patch.object(Path, 'open', side_effect=AssertionError('source must not be opened')):
            packet = self.packet()
        self.assertEqual(self.settings.db_path.read_bytes(), before)
        self.assertEqual(packet['integrity']['payload_sha256'], redigest(deepcopy(packet))['integrity']['payload_sha256'])
        payload = packet['payload']
        self.assertEqual(payload['derived_text']['id'], self.tid)
        self.assertNotEqual(payload['derived_text']['id'], self.newer)
        self.assertEqual(payload['projection']['quote_text'], '  e\u0301\n音声!?')
        self.assertEqual(payload['artifact']['source_locator'], LOCATOR)
        self.assertEqual(payload['artifact']['metadata_json'], RAW_ERROR)
        self.assertEqual(payload['processing_run']['error'], RAW_ERROR)
        self.assertEqual(payload['processing_run']['parameters_json'], '{"raw":NaN}')
        self.assertNotIn('stored_path', payload['artifact'])
        self.assertNotIn('lease_token', payload['processing_run'])
        self.assertEqual(len(payload['source_observations']), 2)
        self.assertFalse(payload['scope']['source_bytes_included'])
        self.assertFalse(payload['scope']['live_restore_supported'])
        self.assertFalse(payload['scope']['tasks_imported'])
        self.assertEqual(payload['scope']['locators'], 'literal_inert_metadata')

    def test_http_and_cli_export_same_version_and_literal_payload(self):
        from server.app.main import app
        with TestClient(app) as client:
            # Startup may legitimately migrate/checkpoint the SQLite store.
            # Measure only export operations within the running-app lifecycle;
            # context teardown/checkpoint timing is not a producer mutation.
            before = self.settings.db_path.read_bytes()
            response = client.get(f'/api/artifacts/{self.aid}/citations/{self.anchor["id"]}/packet')
            self.assertEqual(response.status_code, 200, response.text)
            output = self.root/'packet.json'
            process = self.command('citation-export', self.aid, self.anchor['id'], output)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(output.read_bytes()), response.json())
            self.assertEqual(response.json(), self.packet())
            self.assertEqual(self.settings.db_path.read_bytes(), before)
            inspected = self.command('citation-inspect', output)
            self.assertEqual(inspected.returncode, 0, inspected.stderr + inspected.stdout)
            self.assertEqual(json.loads(inspected.stdout)['verification'], 'matches_included_pinned_transcript')

    def test_cli_export_no_clobber_or_missing_database_creation(self):
        output = self.root/'existing.json'
        output.write_bytes(b'original exact bytes')
        result = self.command('citation-export', self.aid, self.anchor['id'], output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), b'original exact bytes')
        missing = self.root/'absent'/'missing.sqlite'
        self.env['EW_DB_PATH'] = str(missing)
        new = self.root/'never-published.json'
        result = self.command('citation-export', self.aid, self.anchor['id'], new)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(missing.exists())
        self.assertFalse(new.exists())

    def test_http_invalid_ids_and_missing_anchor_are_clean(self):
        from server.app.main import app
        with TestClient(app) as client:
            for aid, anchor, status in [(0, self.anchor['id'], 422), (self.aid, 0, 422),
                                        (2**100, self.anchor['id'], 422),
                                        (self.aid, self.anchor['id']+1000, 404)]:
                with self.subTest(aid=aid, anchor=anchor):
                    response = client.get(f'/api/artifacts/{aid}/citations/{anchor}/packet')
                    self.assertEqual(response.status_code, status, response.text)

    def inspect(self, packet):
        from server.app.exchange_consumer import inspect_packet
        return inspect_packet(json.dumps(packet, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def test_independent_consumer_never_calls_server_helpers_or_opens_locators(self):
        packet = self.packet()
        with patch.object(citations, 'projection_from_selector', side_effect=AssertionError('producer helper called')), \
             patch('builtins.open', side_effect=AssertionError('file access forbidden')), \
             patch.object(Path, 'open', side_effect=AssertionError('file access forbidden')):
            report = self.inspect(packet)
        self.assertEqual(report['quote_text'], '  e\u0301\n音声!?')
        self.assertEqual(report['derived_text_id'], self.tid)
        self.assertEqual(report['origins']['source']['metadata_json'], RAW_ERROR)
        self.assertEqual(report['origins']['artifact']['source_locator'], LOCATOR)

    def test_redigested_semantic_tampering_is_rejected(self):
        good = self.packet()
        mutations = [
            ('quote', lambda p: p['anchor'].__setitem__('quote_text', 'invented')),
            ('version', lambda p: p['anchor'].__setitem__('derived_text_id', self.newer)),
            ('artifact', lambda p: p['derived_text'].__setitem__('artifact_id', self.aid+100)),
            ('projection', lambda p: p['projection'].__setitem__('start_ms', 701)),
            ('run', lambda p: p['processing_run'].__setitem__('artifact_id', self.aid+1)),
            ('acquisition', lambda p: p['source_observations'][0].__setitem__('sha256', 'b'*64)),
            ('restore', lambda p: p['scope'].__setitem__('live_restore_supported', True)),
            ('wordref', lambda p: p['anchor'].__setitem__('selector_json',
                p['anchor']['selector_json'].replace('"word_index": 1', '"word_index": 0'))),
        ]
        for name, mutate in mutations:
            packet = deepcopy(good)
            mutate(packet['payload'])
            self.assertNotEqual(packet, good, name)
            redigest(packet)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.inspect(packet)

    def test_digest_and_unknown_schema_rejected(self):
        packet = self.packet()
        packet['payload']['artifact']['original_name'] = 'changed without redigest'
        with self.assertRaises(ValueError):
            self.inspect(packet)
        packet = redigest(self.packet())
        packet['schema'] = 'ageds.citation-evidence/v999'
        with self.assertRaises(ValueError):
            self.inspect(packet)

    def test_wire_duplicate_nonfinite_depth_nodes_and_utf8_bounds(self):
        from server.app import exchange_consumer as consumer
        raw = json.dumps(self.packet(), ensure_ascii=False)
        duplicate = raw.replace('"schema": ', '"schema":"other", "schema": ', 1)
        nested_duplicate = raw.replace('"original_name": ', '"original_name":"other", "original_name": ', 1)
        for value in (duplicate, nested_duplicate, '{"n":NaN}', '{"n":Infinity}',
                      '{"n":1e9999}', '['*80+'0'+']'*80):
            with self.subTest(raw=value[:100]), self.assertRaises(ValueError):
                consumer.inspect_packet(value.encode())
        with self.assertRaises(ValueError):
            consumer.inspect_packet(b'\xff')
        with patch.object(consumer, 'MAX_NODES', 10), self.assertRaises(ValueError):
            consumer.inspect_packet(raw.encode())
        with patch.object(consumer, 'MAX_BYTES', 100), self.assertRaises(ValueError):
            consumer.inspect_packet(raw.encode())

    def test_raw_invalid_unselected_word_times_keep_segment_exchange_usable(self):
        raw = '[{"start":0,"end":1,"text":" Raw wrong é","words":[{"start":NaN,"end":1,"word":" Raw wrong é"}]}]'
        with db.session() as conn:
            tid = conn.execute("INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES (?,'transcript',' Raw wrong é',?)",
                               (self.aid, raw)).lastrowid
        anchor = citations.create_citation(self.aid, tid, [0])
        from server.app.exchange import export_citation_packet
        packet = export_citation_packet(self.aid, anchor['id'])
        self.assertEqual(packet['payload']['derived_text']['segments_json'], raw)
        with self.assertRaises(ValueError):
            citations.create_citation(self.aid, tid, word_refs=[{'segment_index': 0, 'word_index': 0}])
        self.assertEqual(self.inspect(packet)['quote_text'], ' Raw wrong é')

    def test_huge_db_text_preflight_and_parsed_structure_caps(self):
        from server.app import exchange
        with db.session() as conn:
            conn.execute('UPDATE artifacts SET metadata_json=? WHERE id=?', ('x'*(exchange.MAX_PACKET_BYTES+1), self.aid))
        with self.assertRaises(exchange.PacketLimitError):
            self.packet()
        with db.session() as conn:
            conn.execute('UPDATE artifacts SET metadata_json=? WHERE id=?', (RAW_ERROR, self.aid))
        for name, limit in [('MAX_NODES', 10), ('MAX_DEPTH', 2), ('MAX_SEGMENTS', 1), ('MAX_OBSERVATIONS', 1)]:
            with self.subTest(name=name), patch.object(exchange, name, limit), self.assertRaises(ValueError):
                self.packet()

    def test_standalone_inspect_has_no_server_import_or_live_storage_side_effect(self):
        packet = self.root/'input.json'
        packet.write_text(json.dumps(self.packet(), ensure_ascii=False), encoding='utf-8')
        self.env.update(EW_DATA_DIR=str(self.root/'never-live'), EW_STORE_DIR=str(self.root/'never-live'/'store'),
                        EW_DB_PATH=str(self.root/'never-live'/'database.sqlite'))
        code = ('import sys; from server.app.exchange_consumer import inspect_path; '
                'inspect_path(sys.argv[1]); '
                'assert not any(n in sys.modules for n in '
                '["server.app.exchange","server.app.citations","server.app.db","server.app.config"])')
        result = subprocess.run([sys.executable, '-c', code, str(packet)], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root/'never-live').exists())

    def test_failed_run_literal_error_and_status_remain_interoperable(self):
        with db.session() as conn:
            conn.execute("UPDATE processing_runs SET status='failed' WHERE id=?", (self.run,))
        packet = self.packet()
        self.assertEqual(packet['payload']['processing_run']['status'], 'failed')
        self.assertEqual(self.inspect(packet)['origins']['processing_run']['error'], RAW_ERROR)

    def test_export_and_consumer_share_deep_auxiliary_json_boundary(self):
        from server.app import exchange
        # 31 nested arrays plus segments list+segment object: 33 containers.
        nested = '[]'
        for _ in range(30):
            nested = '[' + nested + ']'
        raw = '[{"start":0,"end":1,"text":"x","aux":' + nested + '}]'
        with db.session() as conn:
            tid = conn.execute("INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES (?,'transcript','x',?)",
                               (self.aid, raw)).lastrowid
        anchor = citations.create_citation(self.aid, tid, [0])
        with self.assertRaises(exchange.PacketLimitError):
            exchange.export_citation_packet(self.aid, anchor['id'])

    def test_cli_inspect_invalid_input_is_clean_and_does_not_create_live_database(self):
        self.env.update(EW_DATA_DIR=str(self.root/'never-live'), EW_STORE_DIR=str(self.root/'never-live'/'store'),
                        EW_DB_PATH=str(self.root/'never-live'/'database.sqlite'))
        path = self.root/'invalid.json'
        for raw in ('{"schema":"future"}', '{"schema":1,"schema":2}', '{"x":NaN}'):
            path.write_text(raw)
            result = self.command('citation-inspect', path)
            with self.subTest(raw=raw):
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse((self.root/'never-live').exists())

    def test_http_rejects_invalid_stored_data_and_size_cap_without_partial_packet(self):
        from server.app.main import app
        from server.app import exchange
        with db.session() as conn:
            conn.execute('UPDATE artifacts SET sha256=? WHERE id=?', ('not-a-digest', self.aid))
        with TestClient(app) as client:
            response = client.get(f'/api/artifacts/{self.aid}/citations/{self.anchor["id"]}/packet')
            self.assertEqual(response.status_code, 409, response.text)
            self.assertNotIn('payload', response.json())
        with db.session() as conn:
            conn.execute('UPDATE artifacts SET sha256=? WHERE id=?', ('a'*64, self.aid))
        with TestClient(app) as client, patch.object(exchange, 'MAX_PACKET_BYTES', 100):
            response = client.get(f'/api/artifacts/{self.aid}/citations/{self.anchor["id"]}/packet')
            self.assertEqual(response.status_code, 413, response.text)
            self.assertNotIn('payload', response.json())

    def test_nested_selector_and_segments_duplicate_keys_rejected_after_redigest(self):
        good = self.packet()
        for record, field, key in [('anchor', 'selector_json', 'precision'),
                                   ('derived_text', 'segments_json', 'text')]:
            packet = deepcopy(good)
            raw = packet['payload'][record][field]
            marker = json.dumps(key)+':'
            self.assertIn(marker, raw)
            packet['payload'][record][field] = raw.replace(marker, marker+' "forged", '+marker, 1)
            redigest(packet)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.inspect(packet)

    def test_consumer_safe_file_boundary_rejects_symlink_and_fifo(self):
        from server.app.exchange_consumer import inspect_path
        packet = self.root/'packet.json'
        packet.write_text(json.dumps(self.packet(), ensure_ascii=False), encoding='utf-8')
        link = self.root/'symlink.json'
        link.symlink_to(packet)
        with self.assertRaises((OSError, ValueError)):
            inspect_path(link)
        fifo = self.root/'fifo'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            inspect_path(fifo)

    def test_node_budget_counts_auxiliary_object_keys_before_export(self):
        from server.app import exchange
        raw = json.dumps([{'start': 0, 'end': 1, 'text': 'x',
                           'aux': {str(index): 0 for index in range(50_000)}}])
        self.assertLess(len(raw), exchange.MAX_PACKET_BYTES)
        with db.session() as conn:
            tid = conn.execute("INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES (?,'transcript','x',?)",
                               (self.aid, raw)).lastrowid
        anchor = citations.create_citation(self.aid, tid, [0])
        with self.assertRaises(exchange.PacketLimitError):
            exchange.export_citation_packet(self.aid, anchor['id'])

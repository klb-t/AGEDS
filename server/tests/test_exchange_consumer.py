"""Independently authored portable packet fixtures (no producer dependency)."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from server.app import exchange_consumer as consumer


def encode(packet):
    return json.dumps(packet, ensure_ascii=False, separators=(',', ':')).encode()


def seal(payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return {'schema': 'ageds.citation-evidence/v1', 'payload': payload,
            'integrity': {'algorithm': 'sha256', 'canonicalization': 'python-json-sorted-keys-compact-utf8-no-nan/v1',
                          'domain': 'ageds.citation-evidence/v1\n',
                          'payload_sha256': hashlib.sha256(b'ageds.citation-evidence/v1\n' + canonical).hexdigest()}}


def fixture(words=False):
    text = ' Zażółć <script>\n😀\u202e'
    segments = [{'start': 0, 'end': 2, 'text': text, 'words': [
        {'start': 0.1005, 'end': 1, 'word': ' Zażółć'},
        {'start': 1.2, 'end': 1.9995, 'word': ' <script>\n😀\u202e'}]}]
    selector = {'kind': 'segments', 'indices': [0], 'text_join': 'concatenate_exact',
                'time_unit': 'seconds', 'stored_time_unit': 'milliseconds', 'rounding': 'nearest_ms', 'precision': 'segment'}
    start, end = 0, 2000
    if words:
        text = segments[0]['words'][1]['word']
        selector = {key: value for key, value in selector.items() if key not in ('indices', 'kind', 'precision')}
        selector.update(kind='words', precision='word_asr', word_refs=[{'segment_index': 0, 'word_index': 1}],
                        source_start=1.2, source_end=1.9995, alignment_verification='not_performed')
        start = 1200
    digest = hashlib.sha256(text.encode()).hexdigest()
    projection = {'quote_text': text, 'quote_sha256': digest, 'start_ms': start, 'end_ms': end, 'selector': selector}
    payload = {'artifact': {'id': 7, 'source_id': 3, 'sha256': 'a'*64, 'size_bytes': 4, 'source_locator': 'https://invalid.example/private'},
               'source': {'id': 3, 'locator': 'file:///secret'},
               'source_observations': [{'id': 2, 'artifact_id': 7, 'source_id': 9, 'sha256': 'a'*64, 'size_bytes': 4}],
               'processing_run': None,
               'derived_text': {'id': 11, 'artifact_id': 7, 'run_id': None, 'kind': 'transcript', 'text': segments[0]['text'],
                                'segments_json': json.dumps(segments, ensure_ascii=False)},
               'anchor': dict(id=13, artifact_id=7, derived_text_id=11, selector_json=json.dumps(selector), **{k:v for k,v in projection.items() if k != 'selector'}),
               'projection': projection, 'unknowns': [],
               'scope': {'source_bytes_included': False, 'media_bytes_included': False, 'live_restore_supported': False,
                         'tasks_imported': False, 'locators': 'literal_inert_metadata', 'stored_path_included': False}}
    return seal(payload)


class PortableConsumerTests(unittest.TestCase):
    def test_literal_segment_and_word_reports(self):
        for words in (False, True):
            packet = fixture(words)
            report = consumer.inspect_packet_bytes(encode(packet))
            self.assertEqual(report['quote_text'], packet['payload']['projection']['quote_text'])
            self.assertIn('processing_run', report['observed_missing_provenance'])
            rendered = consumer.report_json(report)
            self.assertNotIn('<script>', rendered)
            self.assertNotIn('\u202e', rendered)
            self.assertEqual(json.loads(rendered), report)

    def test_digest_tamper(self):
        packet = fixture()
        packet['payload']['artifact']['source_locator'] = '/changed'
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            consumer.inspect_packet_bytes(encode(packet))

    def test_redigested_identity_and_selector_tampering(self):
        edits = [
            lambda p: p['derived_text'].update(artifact_id=8),
            lambda p: p['anchor'].update(derived_text_id=12),
            lambda p: p['source'].update(id=8),
            lambda p: p['source_observations'][0].update(sha256='b'*64),
            lambda p: p['source_observations'].append(copy.deepcopy(p['source_observations'][0])),
            lambda p: p['artifact'].update(id=True),
            lambda p: p['anchor'].update(start_ms=False),
            lambda p: p['projection'].update(quote_text='altered'),
            lambda p: p['anchor'].update(quote_text='altered'),
            lambda p: p['scope'].update(tasks_imported=True),
            lambda p: p['artifact'].update(stored_path='/secret'),
            lambda p: p['derived_text'].update(run_id=9),
            lambda p: p['anchor'].update(selector_json='{"kind":"segments","kind":"words"}'),
            lambda p: p['derived_text'].update(segments_json='[{"text":"x","extra":NaN}]'),
        ]
        for edit in edits:
            payload = fixture()['payload']
            edit(payload)
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                consumer.inspect_packet_bytes(encode(seal(payload)))

    def test_unknown_schema_and_integrity_contract(self):
        for key, value in [('schema', 'ageds.citation-evidence/v2'), ('schema', True)]:
            packet = fixture()
            packet[key] = value
            with self.assertRaises(ValueError):
                consumer.inspect_packet_bytes(encode(packet))
        packet = fixture()
        packet['integrity']['domain'] = 'other\n'
        with self.assertRaises(ValueError):
            consumer.inspect_packet_bytes(encode(packet))

    def test_strict_json_and_bounds(self):
        values = [b'\xff', b'\xef\xbb\xbf{}', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}',
                  b'{"x":"\\ud800"}', b'['*33 + b'0' + b']'*33,
                  b' '*consumer.MAX_BYTES + b'{}']
        for raw in values:
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                consumer.inspect_packet_bytes(raw)
        with patch.object(consumer, 'MAX_NODES', 10), self.assertRaisesRegex(ValueError, 'node limit'):
            consumer.inspect_packet_bytes(encode(fixture()))

    def test_invalid_word_boundaries_and_literal_text(self):
        for change in ('overlap', 'text', 'index', 'precision'):
            payload = fixture(True)['payload']
            segments = json.loads(payload['derived_text']['segments_json'])
            if change == 'overlap':
                segments[0]['words'][1]['start'] = 0.9
            if change == 'text':
                segments[0]['words'][0]['word'] = 'normalized'
            if change in ('index', 'precision'):
                selector = json.loads(payload['anchor']['selector_json'])
                if change == 'index':
                    selector['word_refs'][0]['word_index'] = True
                else:
                    selector['precision'] = 'verified_alignment'
                payload['anchor']['selector_json'] = json.dumps(selector)
            payload['derived_text']['segments_json'] = json.dumps(segments)
            with self.subTest(change=change), self.assertRaises(ValueError):
                consumer.inspect_packet_bytes(encode(seal(payload)))

    def test_opaque_auxiliary_metadata_remains_string(self):
        payload = fixture()['payload']
        payload['artifact']['metadata_json'] = '{"untrusted": NaN, broken'
        self.assertEqual(consumer.inspect_packet_bytes(encode(seal(payload)))['origins']['artifact']['metadata_json'],
                         payload['artifact']['metadata_json'])

    def test_standalone_execution_without_project_import_path_and_no_locator_io(self):
        script = Path(consumer.__file__).resolve()
        with tempfile.TemporaryDirectory() as temp:
            packet_path = Path(temp) / 'packet.json'
            packet_path.write_bytes(encode(fixture()))
            completed = subprocess.run([sys.executable, '-I', str(script), str(packet_path)], cwd=temp,
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)['derived_text_id'], 11)
            with patch('builtins.open', side_effect=AssertionError('no implicit files')):
                consumer.inspect_packet_bytes(packet_path.read_bytes())

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'requires no-follow descriptors')
    def test_explicit_file_only_regular_nofollow_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            packet = Path(temp) / 'packet'
            packet.write_bytes(encode(fixture()))
            self.assertEqual(consumer.inspect_packet_file(packet)['artifact_id'], 7)
            link = Path(temp) / 'link'
            link.symlink_to(packet)
            with self.assertRaises((OSError, ValueError)):
                consumer.inspect_packet_file(link)
            with self.assertRaises((OSError, ValueError)):
                consumer.inspect_packet_file(temp)
            fifo = Path(temp) / 'fifo'
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                consumer.inspect_packet_file(fifo)
            with packet.open('wb') as stream:
                stream.truncate(consumer.MAX_BYTES + 1)
            with self.assertRaisesRegex(ValueError, 'byte limit'):
                consumer.inspect_packet_file(packet)

    def test_legacy_unused_nonfinite_word_data_and_failed_run_are_explicit(self):
        payload = fixture()['payload']
        segments = json.loads(payload['derived_text']['segments_json'])
        segments[0]['words'][0]['start'] = float('nan')
        payload['derived_text']['segments_json'] = json.dumps(segments)
        payload['derived_text']['run_id'] = 4
        payload['processing_run'] = {'id': 4, 'artifact_id': 7, 'status': 'failed', 'error': 'raw error'}
        report = consumer.inspect_packet_bytes(encode(seal(payload)))
        self.assertEqual(report['origins']['processing_run']['error'], 'raw error')
        self.assertTrue(report['provenance_warnings'])
        payload = fixture(True)['payload']
        payload['derived_text']['segments_json'] = json.dumps(segments)
        with self.assertRaises(ValueError):
            consumer.inspect_packet_bytes(encode(seal(payload)))

    def test_real_producer_interoperability_pinned_segments_and_words(self):
        # Only tests import AGEDS; the consumer also runs alone under python -I.
        from dataclasses import replace
        from server.app import citations, db, exchange
        with tempfile.TemporaryDirectory() as temp, patch.object(db, 'settings', replace(db.settings, db_path=Path(temp)/'db.sqlite')):
            db.init_db()
            raw = json.loads(fixture()['payload']['derived_text']['segments_json'])
            with db.session() as conn:
                aid = conn.execute("INSERT INTO artifacts(original_name,stored_path,source_locator) VALUES ('synthetic.wav','/never/open','https://never.fetch')").lastrowid
                tid = conn.execute("INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES (?,'transcript',?,?)",
                                   (aid, raw[0]['text'], json.dumps(raw))).lastrowid
                conn.execute("INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES (?,'transcript','newer','[]')", (aid,))
            anchors = [citations.create_citation(aid, tid, [0]),
                       citations.create_citation(aid, tid, word_refs=[{'segment_index': 0, 'word_index': 1}])]
            for anchor in anchors:
                for include_path in (False, True):
                    packet = exchange.export_citation_packet(aid, anchor['id'], include_stored_path=include_path)
                    report = consumer.inspect_packet_bytes(encode(packet))
                    self.assertEqual(report['derived_text_id'], tid)
                    self.assertEqual(report['quote_text'], anchor['quote_text'])
                    self.assertEqual('stored_path' in report['origins']['artifact'], include_path)

    def test_real_producer_aggregate_node_budget_exact_boundary(self):
        from dataclasses import replace
        from server.app import citations, db, exchange
        def count(value):
            if isinstance(value, dict):
                return 1 + sum(count(k) + count(v) for k, v in value.items())
            if isinstance(value, list):
                return 1 + sum(count(v) for v in value)
            return 1
        with tempfile.TemporaryDirectory() as temp, patch.object(db, 'settings', replace(db.settings, db_path=Path(temp)/'db.sqlite')):
            db.init_db()
            segments = [{'start': 0, 'end': 1, 'text': 'literal'}]
            with db.session() as conn:
                aid = conn.execute("INSERT INTO artifacts(original_name) VALUES ('synthetic.wav')").lastrowid
                tid = conn.execute("INSERT INTO derived_text(artifact_id,kind,text,segments_json) VALUES (?,'transcript','literal',?)",
                                   (aid, json.dumps(segments))).lastrowid
            anchor = citations.create_citation(aid, tid, [0])
            packet = exchange.export_citation_packet(aid, anchor['id'])
            total = count(packet) + count(segments) + count(json.loads(packet['payload']['anchor']['selector_json']))
            segments[0]['unused'] = [0] * (100_000 - total - 2)
            with db.session() as conn:
                conn.execute('UPDATE derived_text SET segments_json=? WHERE id=?', (json.dumps(segments), tid))
            packet = exchange.export_citation_packet(aid, anchor['id'])
            self.assertEqual(consumer.inspect_packet_bytes(encode(packet))['quote_text'], 'literal')
            segments[0]['unused'].append(0)
            with db.session() as conn:
                conn.execute('UPDATE derived_text SET segments_json=? WHERE id=?', (json.dumps(segments), tid))
            with self.assertRaises(exchange.PacketLimitError):
                exchange.export_citation_packet(aid, anchor['id'])
            packet['payload']['derived_text']['segments_json'] = json.dumps(segments)
            with self.assertRaisesRegex(ValueError, 'node limit'):
                consumer.inspect_packet_bytes(encode(seal(packet['payload'])))

"""Loom adapter: typed export with provenance, quarantine import, promotion, real CLI subprocesses."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from server.app import db
from server.app.integrations import loom_graph
from server.tests.test_integrations_support import IntegrationCase

REPO_ROOT = Path(__file__).resolve().parents[2]


class GraphExportTests(IntegrationCase):
    def test_every_node_and_edge_carries_provenance_and_epistemic_status(self):
        graph = loom_graph.export_graph()
        self.assertEqual(graph['schema'], 'ageds.graph-export/v1')
        self.assertEqual(graph['claims'], {'available': False, 'items': [], 'reason': graph['claims']['reason']})
        self.assertIn('no claims', graph['claims']['reason'])
        self.assertFalse(graph['scope']['source_bytes_included'])
        self.assertFalse(graph['scope']['private_seed_included'])
        by_kind = {}
        for node in graph['nodes']:
            prov = node['provenance']
            self.assertIn(prov['epistemic_status'], loom_graph.EPISTEMIC)
            self.assertIn('source_table', prov)
            self.assertIn('loom', prov)
            by_kind.setdefault(node['kind'], []).append(node)
        self.assertEqual(sorted(by_kind), ['annotation', 'artifact', 'citation', 'counterparty', 'event', 'transcript'])
        self.assertEqual(len(by_kind['event']), 5)
        self.assertEqual(len(by_kind['artifact']), 3)
        self.assertEqual(len(by_kind['transcript']), 2)
        self.assertEqual({n['provenance']['epistemic_status'] for n in by_kind['event']}, {'parser_output'})
        self.assertEqual({n['provenance']['epistemic_status'] for n in by_kind['artifact']}, {'observed'})
        self.assertEqual({n['provenance']['epistemic_status'] for n in by_kind['transcript']}, {'model_output'})
        self.assertEqual({n['provenance']['epistemic_status'] for n in by_kind['annotation']}, {'human_annotation'})
        self.assertEqual({n['provenance']['epistemic_status'] for n in by_kind['citation']}, {'human_annotation'})
        self.assertEqual(by_kind['citation'][0]['attrs']['quote_epistemic_status'], 'model_output')
        parties = by_kind['counterparty']
        self.assertEqual({p['provenance']['epistemic_status'] for p in parties}, {'hypothesis'})
        self.assertTrue(all(p['attrs']['identity_claim'] is False for p in parties))
        labels = {p['label'] for p in parties}
        self.assertEqual(labels, {'+48 000 000 001', 'Osoba A', '+31 6 0000 0002', 'Osoba B', '+48 000 000 003'})
        osoba_a = next(p for p in parties if p['label'] == 'Osoba A')
        self.assertEqual(osoba_a['attrs']['observed_in_events'], 3)
        self.assertEqual(osoba_a['id'], loom_graph.counterparty_address('osoba  A'), 'whitespace/case-normalized address, not an identity')
        transcript = by_kind['transcript'][0]
        self.assertEqual(transcript['attrs']['provenance_status'], 'legacy_unknown')
        self.assertIsNone(transcript['attrs']['run'])
        node_ids = {n['id'] for n in graph['nodes']}
        relations = {}
        for edge in graph['edges']:
            self.assertIn(edge['src'], node_ids)
            self.assertIn(edge['dst'], node_ids)
            self.assertIn(edge['provenance']['epistemic_status'], loom_graph.EPISTEMIC)
            relations[edge['relation']] = relations.get(edge['relation'], 0) + 1
        self.assertEqual(relations['derived_from'], 5 + 2)
        self.assertEqual(relations['counterparty_of'], 9)
        self.assertEqual(relations['annotates'], 2)
        self.assertEqual(relations['quotes'], 1)
        self.assertEqual(graph['counts']['nodes'], len(graph['nodes']))
        self.assertEqual(graph['counts']['nodes_hypothesis'], 5)
        self.assertEqual(graph['loom_mapping']['observed']['evidence_class'], 'observed')
        self.assertIn('unknown', graph['loom_packet_compat'])
        self.assertNotIn('RIFF', json.dumps(graph))
        self.assertNotIn(str(self.root / 'store'), json.dumps(graph))

    def test_window_text_toggle_and_truncation_are_explicit(self):
        windowed = loom_graph.export_graph(from_='2026-03-02', to='2026-03-03')
        events = [n for n in windowed['nodes'] if n['kind'] == 'event']
        self.assertEqual([n['provenance']['row_id'] for n in events], [self.events[4]])
        self.assertEqual([n['id'] for n in windowed['nodes'] if n['kind'] == 'artifact'], [f'artifact:{self.artifact_sms}'])
        self.assertEqual(windowed['scope']['from'], '2026-03-02')
        hashed = loom_graph.export_graph(include_text=False)
        event = next(n for n in hashed['nodes'] if n['kind'] == 'event' and n['provenance']['row_id'] == self.events[0])
        self.assertNotIn('body', event['attrs'])
        self.assertEqual(len(event['attrs']['body_sha256']), 64)
        self.assertFalse(hashed['scope']['text_included'])
        small = loom_graph.export_graph(max_nodes=3, max_edges=2)
        self.assertTrue(small['truncation']['nodes'])
        self.assertTrue(small['truncation']['events'])
        self.assertLessEqual(len(small['nodes']), 3)
        self.assertLessEqual(len(small['edges']), 2)
        with self.assertRaises(loom_graph.GraphInputError):
            loom_graph.export_graph(from_='b', to='a')
        token = self.token()
        response = self.get('/api/read/v1/graph/export', token, **{'from': '2026-03-02'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['counts'], windowed['counts'])
        self.assertEqual(self.get('/api/read/v1/graph/export', token, **{'from': 'b', 'to': 'a'}).status_code, 422)


class HypothesisQuarantineTests(IntegrationCase):
    def document(self, items):
        return {'schema': 'ageds.relation-hypotheses/v1', 'origin': 'loom', 'run': {'tool': 'loom', 'version': 'test'}, 'items': items}

    def test_import_quarantines_and_never_writes_links(self):
        items = [
            {'external_id': 'h1', 'left': f'event:{self.events[0]}', 'right': f'event:{self.events[1]}', 'relation': 'same_thread', 'confidence': 0.7, 'rationale': 'same counterparty within 2h'},
            {'left': {'kind': 'transcript', 'id': self.transcript_v1}, 'right': {'kind': 'event', 'id': self.events[1]}, 'relation': 'mentions_same_contract'},
            {'left': f'event:{self.events[0]}', 'right': 'event:999999', 'relation': 'reply_to'},
            {'left': f'event:{self.events[0]}', 'right': loom_graph.counterparty_address('Osoba A'), 'relation': 'is_person', 'confidence': 1},
        ]
        result = loom_graph.import_hypotheses(self.document(items), origin='loom', file_name='h.json')
        self.assertEqual(result['counts'], {'quarantined': 2, 'rejected_unresolved': 1, 'rejected_unsupported_kind': 1})
        self.assertEqual(result['links_written'], 0)
        self.assertEqual(self.count('links'), 0)
        self.assertEqual(self.count('relation_hypotheses'), 4)
        stored = loom_graph.list_hypotheses()
        self.assertEqual([h['status'] for h in stored], ['quarantined', 'quarantined', 'rejected', 'rejected'])
        self.assertIn('unresolved_reference', stored[2]['decision_note'])
        self.assertIn('counterparty', stored[3]['decision_note'])
        self.assertEqual(stored[0]['provenance']['file_sha256'], result['file_sha256'])
        self.assertEqual(stored[0]['provenance']['run'], {'tool': 'loom', 'version': 'test'})
        self.assertEqual(stored[1]['left_kind'], 'derived_text')
        self.assertEqual(stored[0]['epistemic_status'], 'hypothesis')
        self.assertEqual(len(self.audit('hypotheses_import')), 1)
        page = self.client.get('/integrations')
        self.assertIn('same_thread', page.text)
        self.assertIn('kwarantann', page.text)

    def test_promote_writes_exactly_one_link_with_audit_and_reject_keeps_row(self):
        result = loom_graph.import_hypotheses(self.document([
            {'left': f'event:{self.events[0]}', 'right': f'event:{self.events[1]}', 'relation': 'same_thread', 'confidence': 0.5, 'rationale': 'r'},
            {'left': f'artifact:{self.artifact}', 'right': f'artifact:{self.artifact_twin}', 'relation': 'same_content'},
        ]))
        first, second = result['hypothesis_ids']
        promoted = self.client.post(f'/integrations/hypotheses/{first}/decide', data={'decision': 'promoted', 'decided_by': 'reviewer'}, follow_redirects=False)
        self.assertEqual(promoted.status_code, 303)
        with db.session() as conn:
            links = [dict(r) for r in conn.execute('SELECT * FROM links')]
            row = dict(conn.execute('SELECT * FROM relation_hypotheses WHERE id=?', (first,)).fetchone())
        self.assertEqual(len(links), 1)
        self.assertEqual((links[0]['left_kind'], links[0]['left_id'], links[0]['right_kind'], links[0]['right_id'], links[0]['relation'], links[0]['confidence']),
                         ('event', self.events[0], 'event', self.events[1], 'same_thread', 0.5))
        self.assertEqual((row['status'], row['link_id'], row['decided_by']), ('promoted', links[0]['id'], 'reviewer'))
        audit = json.loads(self.audit('link_from_hypothesis')[0]['details_json'])
        self.assertEqual((audit['hypothesis_id'], audit['origin']), (first, 'loom'))
        with self.assertRaises(loom_graph.GraphInputError):
            loom_graph.decide_hypothesis(first, 'promoted', decided_by='again')
        rejected = loom_graph.decide_hypothesis(second, 'rejected', decided_by='cli', note='duplicate bytes are already one content group')
        self.assertEqual((rejected['status'], rejected['link_id']), ('rejected', None))
        self.assertEqual(self.count('links'), 1)
        with self.assertRaises(loom_graph.HypothesisNotFound):
            loom_graph.decide_hypothesis(999, 'rejected', decided_by='cli')
        graph = loom_graph.export_graph()
        link_edges = [e for e in graph['edges'] if e['provenance']['source_table'] == 'links']
        self.assertEqual(len(link_edges), 1)
        self.assertEqual(link_edges[0]['provenance']['epistemic_status'], 'hypothesis')
        self.assertEqual(link_edges[0]['attrs']['promoted_from']['hypothesis_id'], first)

    def test_import_validation_and_http_scope(self):
        for document, fragment in ((['x'], 'schema must be'), ({'schema': 'loom.graph_packet_diff/1', 'items': []}, 'format_compat'),
                                   (self.document('x'), 'items must be'), (self.document([{'left': 'event:1', 'right': 'event:2'}]), 'relation'),
                                   (self.document([{'left': 'event:1', 'right': 'event:2', 'relation': 'r', 'confidence': 2}]), 'confidence'),
                                   (self.document([{'left': 'person:1', 'right': 'event:2', 'relation': 'r'}]), 'reference must look like'),
                                   (self.document([{'left': {'kind': 'event', 'id': 1}, 'right': {'kind': 'event', 'id': 2}, 'relation': 'r', 'bogus': 1}]), 'unknown fields')):
            with self.subTest(fragment=fragment):
                with self.assertRaises(loom_graph.GraphInputError) as ctx:
                    loom_graph.import_hypotheses(document)
                self.assertIn(fragment, str(ctx.exception))
        self.assertEqual(self.count('relation_hypotheses'), 0)
        read_only = self.token(scopes=('read',), client_id='loom')
        response = self.client.post('/api/integrations/v1/hypotheses', headers=self.headers(read_only), json=self.document([]))
        self.assertEqual(response.status_code, 403)
        writer = self.token(scopes=('read', 'propose'), client_id='loom')
        response = self.client.post('/api/integrations/v1/hypotheses', headers=self.headers(writer),
                                    json=self.document([{'left': f'event:{self.events[0]}', 'right': f'event:{self.events[1]}', 'relation': 'r'}]))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['origin'], 'loom')
        listed = self.client.get('/api/integrations/v1/hypotheses', headers=self.headers(writer)).json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(self.count('links'), 0)


class CliSubprocessTests(IntegrationCase):
    def command(self, *args):
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONDONTWRITEBYTECODE='1', EW_DATA_DIR=str(self.root),
                   EW_STORE_DIR=str(self.root / 'store'), EW_DB_PATH=str(self.settings.db_path), EW_REQUIRE_TOKEN='')
        return subprocess.run([sys.executable, '-m', 'server.app.cli', *map(str, args)], cwd=self.root, env=env, capture_output=True, text=True, timeout=60)

    def test_token_create_prints_once_and_cli_module_stays_isolated(self):
        process = self.command('token-create', '--label', 'ChatADHD laptop', '--scope', 'read', '--scope', 'propose', '--client-id', 'chatadhd', '--expires-in-days', '7')
        self.assertEqual(process.returncode, 0, process.stderr)
        printed = json.loads(process.stdout)
        self.assertTrue(printed['token'].startswith('ageds_'))
        self.assertEqual(printed['stored'], 'salted_sha256_only')
        listed = json.loads(self.command('token-list').stdout)['tokens']
        self.assertEqual(len(listed), 1)
        self.assertNotIn('token', listed[0])
        self.assertEqual(listed[0]['scopes'], ['read', 'propose'])
        self.assertEqual(self.get('/api/read/v1/contract', printed).status_code, 200)
        revoked = json.loads(self.command('token-revoke', listed[0]['id'], '--note', 'laptop lost').stdout)
        self.assertEqual(revoked['status'], 'revoked')
        self.assertEqual(self.get('/api/read/v1/contract', printed).status_code, 401)
        self.assertEqual(self.command('token-create', '--label', 'x', '--scope', 'admin').returncode, 2)
        code = 'import server.app.cli,sys,json; print(json.dumps({n:n in sys.modules for n in ["server.app.config","server.app.db","server.app.integrations.tokens"]}))'
        isolated = subprocess.run([sys.executable, '-c', code], cwd=self.root, env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)), capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(isolated.stdout), {'server.app.config': False, 'server.app.db': False, 'server.app.integrations.tokens': False})

    def test_loom_export_and_import_roundtrip_through_files(self):
        output = self.root / 'exports' / 'graph.json'
        process = self.command('loom-export', output, '--from', '2026-03-01', '--to', '2026-03-02')
        self.assertEqual(process.returncode, 0, process.stderr)
        status = json.loads(process.stdout)
        self.assertEqual(status['schema'], 'ageds.graph-export/v1')
        self.assertFalse(status['claims_available'])
        self.assertFalse(status['source_bytes_written'])
        graph = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(graph['counts']['nodes'], status['counts']['nodes'])
        self.assertEqual(self.command('loom-export', output).returncode, 2, 'existing files are never overwritten')
        event_ids = [n['provenance']['row_id'] for n in graph['nodes'] if n['kind'] == 'event']
        hypotheses = self.root / 'from-loom.json'
        hypotheses.write_text(json.dumps({'schema': 'ageds.relation-hypotheses/v1', 'items': [
            {'external_id': 'loom-1', 'left': f'event:{event_ids[0]}', 'right': f'event:{event_ids[1]}', 'relation': 'same_thread', 'confidence': 0.6},
            {'left': f'event:{event_ids[0]}', 'right': 'event:424242', 'relation': 'reply_to'},
        ]}), encoding='utf-8')
        imported = self.command('loom-import-hypotheses', hypotheses, '--origin', 'loom')
        self.assertEqual(imported.returncode, 0, imported.stderr)
        summary = json.loads(imported.stdout)
        self.assertEqual(summary['counts'], {'quarantined': 1, 'rejected_unresolved': 1, 'rejected_unsupported_kind': 0})
        self.assertEqual(summary['links_written'], 0)
        self.assertEqual(self.count('links'), 0)
        listed = json.loads(self.command('hypothesis-list', '--status', 'quarantined').stdout)['hypotheses']
        self.assertEqual(len(listed), 1)
        promoted = json.loads(self.command('hypothesis-promote', listed[0]['id'], '--by', 'reviewer', '--note', 'checked').stdout)
        self.assertEqual(promoted['status'], 'promoted')
        self.assertEqual(self.count('links'), 1)
        self.assertEqual(self.command('hypothesis-promote', listed[0]['id']).returncode, 2)
        bad = self.root / 'bad.json'
        bad.write_text('{"schema": "loom.graph_packet_diff/1"}', encoding='utf-8')
        self.assertEqual(self.command('loom-import-hypotheses', bad).returncode, 2)
        self.assertEqual(self.count('relation_hypotheses'), 2)

    def test_proposal_cli_accept(self):
        from server.app.integrations import proposals
        proposal = proposals.submit_proposal({'artifact_id': self.artifact, 'body': 'z klienta'}, client_id='chatadhd')
        listed = json.loads(self.command('proposal-list', '--status', 'proposed').stdout)['proposals']
        self.assertEqual([p['id'] for p in listed], [proposal['id']])
        accepted = json.loads(self.command('proposal-accept', proposal['id'], '--by', 'reviewer').stdout)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertIsNotNone(accepted['annotation_id'])
        self.assertEqual(self.count('annotations'), 2)
        contract = json.loads(self.command('read-contract').stdout)
        self.assertEqual(contract['schema'], 'ageds.read/v1')

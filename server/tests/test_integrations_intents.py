"""Intents: schema validation, anchored execution, interpretive labelling, proposals; the ChatADHD benefit test."""
from __future__ import annotations

import hashlib
import json

from server.app import db
from server.app.integrations import intents, proposals
from server.tests.test_integrations_support import IntegrationCase, SEGMENTS


class IntentValidationTests(IntegrationCase):
    def test_schema_document_is_well_formed_and_kinds_match(self):
        schema = intents.INTENT_SCHEMA
        self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
        kinds = [branch['properties']['kind']['const'] for branch in schema['oneOf']]
        self.assertEqual(tuple(kinds), intents.INTENT_KINDS)
        json.dumps(schema)

    def test_invalid_intents_are_refused_with_reason(self):
        cases = [
            ('not an object', 'intent must be a JSON object'),
            ({'kind': 'summarize'}, 'kind must be one of'),
            ({'kind': 'search'}, 'requires q'),
            ({'kind': 'search', 'q': ''}, 'nonempty'),
            ({'kind': 'search', 'q': 'x', 'limit': 0}, 'limit'),
            ({'kind': 'search', 'q': 'x', 'limit': 201}, 'limit'),
            ({'kind': 'search', 'q': 'x', 'prose': True}, 'unknown fields'),
            ({'kind': 'search', 'q': 'x' * 5000}, '4096'),
            ({'kind': 'artifact', 'artifact_id': '1'}, 'must be int'),
            ({'kind': 'artifact', 'artifact_id': True}, 'must be int'),
            ({'kind': 'artifact', 'artifact_id': 0}, 'positive'),
            ({'kind': 'citation', 'artifact_id': 1}, 'requires anchor_id'),
            ({'kind': 'quote', 'artifact_id': 1, 'derived_text_id': 1, 'segment_indices': []}, 'segment_indices'),
            ({'kind': 'quote', 'artifact_id': 1, 'derived_text_id': 1, 'segment_indices': [-1]}, 'segment_indices'),
            ({'kind': 'timeline_window', 'limit': 501}, 'limit'),
            ({'kind': 'timeline_window', 'from': 'a\x00'}, 'NUL'),
        ]
        for intent, fragment in cases:
            with self.subTest(intent=intent):
                with self.assertRaises(intents.IntentError) as ctx:
                    intents.validate_intent(intent)
                self.assertIn(fragment, str(ctx.exception))

    def test_query_terms_drop_operators_and_keep_phrases(self):
        self.assertEqual(intents.query_terms('umowa AND "spotkanie w biurze" OR biur* NOT x -y title:z'), ['umowa', 'spotkanie w biurze', 'biur', 'x', 'z'])
        self.assertEqual(intents.query_terms('"doubled ""quote"" inside"'), ['doubled "quote" inside'])


class IntentExecutionTests(IntegrationCase):
    def setUp(self):
        super().setUp()
        self.read = self.token()

    def execute(self, intent, token=None):
        return self.client.post('/api/read/v1/intents/execute', headers=self.headers(token or self.read), json=intent)

    def test_search_returns_anchored_segments_and_events_without_prose(self):
        result = self.execute({'kind': 'search', 'q': 'umowa'}).json()
        self.assertEqual((result['mode'], result['prose']), ('anchored', None))
        self.assertEqual(result['schema'], 'ageds.intent-result/v1')
        kinds = {item['kind'] for item in result['items']}
        self.assertEqual(kinds, {'transcript_segment', 'event'})
        segment = next(item for item in result['items'] if item['kind'] == 'transcript_segment')
        self.assertEqual(segment['item_id'], f'segment:{self.transcript_v1}:2')
        self.assertEqual(segment['quote_text'], ' umowa podpisana')
        self.assertEqual(segment['quote_sha256'], hashlib.sha256(' umowa podpisana'.encode()).hexdigest())
        self.assertEqual(segment['selector'], {'kind': 'segments', 'indices': [2], 'text_join': 'concatenate_exact', 'time_unit': 'seconds',
                                               'stored_time_unit': 'milliseconds', 'rounding': 'nearest_ms', 'precision': 'segment'})
        self.assertEqual((segment['start_ms'], segment['end_ms']), (2500, 3000))
        self.assertEqual(segment['match'], {'rule': intents.MATCH_RULE, 'terms': ['umowa']})
        self.assertEqual(segment['audio_verification'], 'not_performed')
        event = next(item for item in result['items'] if item['kind'] == 'event')
        self.assertEqual(event['quote_text'], 'umowa podpisana wczoraj')
        self.assertEqual(event['quote_sha256'], hashlib.sha256(b'umowa podpisana wczoraj').hexdigest())
        self.assertNotIn('summary', json.dumps(result))

    def test_search_matches_diacritics_insensitively_and_reports_unanchored_hits(self):
        # FTS5 unicode61 remove_diacritics 2 folds ę/ś/ą/ź/ń (but not ż/ć); the locator folds at least as much.
        for q in ('gesla', 'JAŹŃ', 'gęślą'):
            with self.subTest(q=q):
                result = self.execute({'kind': 'search', 'q': q}).json()
                self.assertEqual([item['item_id'] for item in result['items']], [f'segment:{self.transcript_v1}:0'])
                self.assertEqual(result['items'][0]['quote_text'], ' Zażółć gęślą jaźń')
        self.assertEqual(intents._normalize('Łódź gęślą'), 'lodz gesla')
        result = self.execute({'kind': 'search', 'q': 'nowa'}).json()
        self.assertEqual(result['items'][0]['item_id'], f'segment:{self.transcript_v2}:0')
        # A phrase that only matches across segment boundaries yields an explicit reference, never a guessed quote.
        result = self.execute({'kind': 'search', 'q': '"dziewiątej umowa"'}).json()
        self.assertEqual([item['kind'] for item in result['items']], ['derived_text_reference'])
        self.assertEqual(result['items'][0]['reason'], 'no_segment_contains_query_terms')
        self.assertIsNone(result['items'][0]['anchor'])

    def test_quote_citation_timeline_and_artifact_intents(self):
        quote = self.execute({'kind': 'quote', 'artifact_id': self.artifact, 'derived_text_id': self.transcript_v1, 'segment_indices': [1, 2]}).json()
        self.assertEqual(quote['items'][0]['quote_text'], ' spotkanie w biurze o dziewiątej umowa podpisana')
        self.assertEqual(quote['items'][0]['selector']['indices'], [1, 2])
        bad = self.execute({'kind': 'quote', 'artifact_id': self.artifact, 'derived_text_id': self.transcript_v1, 'segment_indices': [1, 3]})
        self.assertEqual((bad.status_code, bad.json()['detail']['code']), (422, 'invalid_input'))
        foreign = self.execute({'kind': 'quote', 'artifact_id': self.artifact_twin, 'derived_text_id': self.transcript_v1, 'segment_indices': [0]})
        self.assertEqual(foreign.status_code, 404)
        citation = self.execute({'kind': 'citation', 'artifact_id': self.artifact, 'anchor_id': self.anchor['id']}).json()
        item = citation['items'][0]
        self.assertEqual(item['quote_sha256'], self.anchor['quote_sha256'])
        self.assertEqual(item['packet_schema'], 'ageds.citation-evidence/v1')
        self.assertEqual(len(item['packet_payload_sha256']), 64)
        self.assertIn('processing_run_unknown', item['unknowns'])
        window = self.execute({'kind': 'timeline_window', 'from': '2026-03-02', 'limit': 10}).json()
        self.assertEqual([i['event_id'] for i in window['items']], [self.events[4]])
        self.assertEqual(window['items'][0]['quote_text'], 'koniec')
        self.assertIn('display_rule', window)
        artifact = self.execute({'kind': 'artifact', 'artifact_id': self.artifact}).json()
        self.assertEqual(artifact['items'][0]['artifact']['original_name'], 'recording.wav')
        self.assertNotIn('stored_path', artifact['items'][0]['artifact'])
        refused = self.execute({'kind': 'summarize', 'q': 'x'})
        self.assertEqual((refused.status_code, refused.json()['detail']['code']), (422, 'invalid_intent'))

    def test_chatadhd_client_benefit_anchored_quote_verifies_against_citation_packet(self):
        """Small benefit test (A09): a client with a token asks a parsed intent and can verify the quote."""
        client_token = self.token(scopes=('read',), client_id='chatadhd')
        headers = self.headers(client_token)
        # 1. The client's model parsed the user's question into an intent; AGEDS executes it.
        result = self.client.post('/api/read/v1/intents/execute', headers=headers, json={'kind': 'search', 'q': 'biurze'}).json()
        self.assertIsNone(result['prose'])
        item = next(i for i in result['items'] if i['kind'] == 'transcript_segment')
        self.assertEqual(item['item_id'], f'segment:{self.transcript_v1}:1')
        # 2. The quote hash is recomputable by the client from the returned text.
        self.assertEqual(hashlib.sha256(item['quote_text'].encode('utf-8')).hexdigest(), item['quote_sha256'])
        # 3. The human pinned the same selection earlier; the packet agrees byte for byte on the quote and selector.
        packet = json.loads(self.client.get(f'/api/read/v1/artifacts/{item["artifact_id"]}/citations/{self.anchor["id"]}/packet', headers=headers).content)
        self.assertEqual(packet['payload']['anchor']['quote_sha256'], item['quote_sha256'])
        self.assertEqual(packet['payload']['anchor']['quote_text'], item['quote_text'])
        self.assertEqual(packet['payload']['projection']['selector'], item['selector'])
        self.assertEqual(packet['payload']['derived_text']['id'], item['derived_text_id'])
        # 4. Nothing the client did wrote to the database beyond token use counters, and no source bytes crossed.
        self.assertEqual(self.count('annotations'), 1)
        self.assertNotIn('RIFF', json.dumps(result))

    def test_interpretive_answers_are_labelled_and_unreferenced_claims_flagged(self):
        result = self.execute({'kind': 'search', 'q': 'umowa'}).json()
        known = [item['item_id'] for item in result['items']]
        answer = {'mode': 'interpretive', 'provider': 'openrouter', 'model': 'example/model-1', 'prose': 'Umowa została podpisana i potwierdzona.',
                  'item_ids_available': known,
                  'claims': [{'text': 'Umowa została podpisana.', 'item_ids': [known[0]]},
                             {'text': 'Spotkanie odwołano.', 'item_ids': []},
                             {'text': 'Ktoś coś powiedział.', 'item_ids': ['segment:999:0']}]}
        labelled = self.client.post('/api/read/v1/intents/interpretive/validate', headers=self.headers(self.read), json=answer).json()
        self.assertEqual(labelled['schema'], 'ageds.interpretive-answer/v1')
        self.assertEqual(labelled['label']['mode'], 'interpretive')
        self.assertEqual((labelled['label']['provider'], labelled['label']['model'], labelled['label']['run']), ('openrouter', 'example/model-1', 'unknown'))
        self.assertEqual([c['support'] for c in labelled['claims']], ['referenced', 'unsupported', 'unsupported'])
        self.assertEqual(labelled['claims'][2]['unknown_item_ids'], ['segment:999:0'])
        self.assertEqual((labelled['unsupported_count'], labelled['supported_count']), (2, 1))
        self.assertEqual(labelled['label']['generated_by'], 'external_client')
        unknown = intents.label_interpretive_answer({'mode': 'interpretive', 'prose': 'x', 'claims': []})
        self.assertEqual(unknown['label']['provider'], 'unknown')
        for bad in ({'mode': 'anchored', 'claims': []}, {'mode': 'interpretive', 'claims': 'x'}, {'mode': 'interpretive', 'claims': [{'text': ''}]},
                    {'mode': 'interpretive', 'item_ids_available': ['bogus id']}, {'mode': 'interpretive', 'prose': 'x' * 70_000}):
            with self.subTest(bad=bad):
                response = self.client.post('/api/read/v1/intents/interpretive/validate', headers=self.headers(self.read), json=bad)
                self.assertEqual((response.status_code, response.json()['detail']['code']), (422, 'invalid_interpretive_answer'))


class ProposalTests(IntegrationCase):
    def setUp(self):
        super().setUp()
        self.propose = self.token(scopes=('read', 'propose'), client_id='chatadhd')

    def submit(self, payload, token=None):
        return self.client.post('/api/integrations/v1/proposals', headers=self.headers(token or self.propose), json=payload)

    def test_proposal_is_quarantined_until_human_decision(self):
        payload = {'artifact_id': self.artifact, 'derived_text_id': self.transcript_v1, 'body': 'Podpisanie umowy', 'label': 'umowa',
                   'start_ms': 2500, 'end_ms': 3000, 'basis': [f'segment:{self.transcript_v1}:2'], 'mode': 'anchored'}
        created = self.submit(payload)
        self.assertEqual(created.status_code, 201, created.text)
        proposal = created.json()
        self.assertEqual((proposal['status'], proposal['client_id'], proposal['token_id']), ('proposed', 'chatadhd', self.propose['id']))
        self.assertEqual(self.count('annotations'), 1, 'nothing applied automatically')
        listed = self.client.get('/api/integrations/v1/proposals', headers=self.headers(self.propose)).json()
        self.assertEqual([p['id'] for p in listed], [proposal['id']])
        other = self.token(scopes=('read',), client_id='other-client')
        self.assertEqual(self.client.get('/api/integrations/v1/proposals', headers=self.headers(other)).json(), [])
        page = self.client.get('/integrations')
        self.assertIn('Podpisanie umowy', page.text)
        decided = self.client.post(f'/integrations/proposals/{proposal["id"]}/decide', data={'decision': 'accepted', 'decided_by': 'reviewer', 'note': 'ok'}, follow_redirects=False)
        self.assertEqual(decided.status_code, 303)
        self.assertEqual(self.count('annotations'), 2)
        with db.session() as conn:
            annotation = dict(conn.execute('SELECT * FROM annotations ORDER BY id DESC LIMIT 1').fetchone())
            stored = dict(conn.execute('SELECT * FROM annotation_proposals WHERE id=?', (proposal['id'],)).fetchone())
        self.assertEqual((annotation['body'], annotation['start_ms'], annotation['end_ms'], annotation['derived_text_id']), ('Podpisanie umowy', 2500, 3000, self.transcript_v1))
        self.assertEqual((stored['status'], stored['decided_by'], stored['annotation_id']), ('accepted', 'reviewer', annotation['id']))
        provenance = json.loads(self.audit('annotation_from_proposal')[0]['details_json'])
        self.assertEqual((provenance['proposal_id'], provenance['client_id'], provenance['token_id'], provenance['mode']), (proposal['id'], 'chatadhd', self.propose['id'], 'anchored'))
        self.assertEqual(provenance['basis'], [f'segment:{self.transcript_v1}:2'])
        again = self.client.post(f'/integrations/proposals/{proposal["id"]}/decide', data={'decision': 'rejected'}, follow_redirects=False)
        self.assertEqual(again.status_code, 422)
        native = self.client.get(f'/api/artifacts/{self.artifact}/annotations').json()
        self.assertEqual(native[0]['id'], annotation['id'])

    def test_reject_keeps_row_and_validation_refuses_bad_proposals(self):
        proposal = self.submit({'artifact_id': self.artifact, 'body': 'x', 'mode': 'interpretive', 'basis': [f'event:{self.events[0]}'],
                                'provider': 'openrouter', 'model': 'm'}).json()
        rejected = proposals.decide_proposal(proposal['id'], 'rejected', decided_by='cli', note='nie')
        self.assertEqual((rejected['status'], rejected['annotation_id'], rejected['decision_note']), ('rejected', None, 'nie'))
        self.assertEqual(self.count('annotations'), 1)
        self.assertEqual(len(self.audit('proposal_rejected')), 1)
        for bad, status in (({'artifact_id': self.artifact, 'body': '   '}, 422), ({'artifact_id': 999999, 'body': 'x'}, 404),
                            ({'artifact_id': self.artifact, 'body': 'x', 'start_ms': 1}, 422),
                            ({'artifact_id': self.artifact, 'body': 'x', 'start_ms': 1, 'end_ms': 2}, 422),
                            ({'artifact_id': self.artifact, 'derived_text_id': self.transcript_v2, 'body': 'x', 'start_ms': 0, 'end_ms': 999999}, 201),
                            ({'artifact_id': self.artifact_twin, 'derived_text_id': self.transcript_v1, 'body': 'x'}, 422),
                            ({'artifact_id': self.artifact, 'body': 'x', 'mode': 'interpretive'}, 422),
                            ({'artifact_id': self.artifact, 'body': 'x', 'basis': ['not an id']}, 422),
                            ({'artifact_id': self.artifact, 'body': 'x', 'extra': 1}, 422)):
            with self.subTest(bad=bad):
                self.assertEqual(self.submit(bad).status_code, status)
        out_of_range = proposals.list_proposals(status='proposed')[-1]
        with self.assertRaises(proposals.ProposalError) as ctx:
            proposals.decide_proposal(out_of_range['id'], 'accepted', decided_by='cli')
        self.assertIn('outside transcript', str(ctx.exception))
        self.assertEqual(self.count('annotations'), 1)
        read_only = self.token(scopes=('read',))
        self.assertEqual(self.submit({'artifact_id': self.artifact, 'body': 'x'}, token=read_only).status_code, 403)

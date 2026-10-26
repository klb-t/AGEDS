"""Package P8: the provider sees exactly the gated basis, with consent and budget.

A recording fake transport stands in for Vertex; it captures every request.
Synthetic data only; canaries mark material that must never be sent.
"""
import json
from unittest.mock import patch

from server.app import advocate, authz, db, providers
from server.app import assignments as model
from server.tests.test_advocate import AdvocateFixture, HIDDEN


class Recorder:
    def __init__(self, reply=None, usage=True, error=None):
        self.requests, self.reply, self.usage, self.error = [], reply, usage, error

    def __call__(self, config, body):
        self.requests.append(json.dumps(body, ensure_ascii=False))
        if self.error:
            raise providers.ProviderUnavailable(self.error)
        reply = self.reply or {'claims': [{'text': 'Według nagrania: „Umowa najmu została podpisana w marcu.”',
                                           'status': 'proven', 'citations': ['p1', 'p99']}]}
        response = {'candidates': [{'content': {'parts': [{'text': json.dumps(reply, ensure_ascii=False)}]}}]}
        if self.usage:
            response['usageMetadata'] = {'promptTokenCount': 1200, 'candidatesTokenCount': 150}
        return response


class ProviderBase(AdvocateFixture):
    def setUp(self):
        super().setUp()
        with db.session() as conn:
            authz.set_setting(conn, 'advocate.provider', {'type': 'vertex_gemini', 'project': 'synthetic-project'})

    def consent(self, case_id=None):
        with db.session() as conn:
            scope = (authz.create_scope(conn, 'case', {'case_id': case_id}) if case_id
                     else authz.create_scope(conn, 'installation', {}))
            return providers.record_consent(conn, 'vertex', scope, granted_by='owner',
                                            basis_text='synthetic test consent')

    def ask_with(self, recorder, question='Kiedy podpisano umowę najmu?', principal=None):
        principal = principal or self.recipient
        with db.session() as conn:
            provider = providers.configured_provider(conn, principal)
            provider.transport = recorder
            conversation = advocate.start_conversation(conn, principal)['uid']
            return advocate.ask(conn, principal, conversation, question, provider=provider)

    def calls(self):
        with db.session() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM provider_calls ORDER BY id')]


class ProviderTests(ProviderBase):
    def test_nothing_is_enabled_by_default(self):
        with db.session() as conn:
            authz.set_setting(conn, 'advocate.provider', None)
            self.assertIsNone(providers.configured_provider(conn, self.recipient))

    def test_without_consent_nothing_is_sent(self):
        recorder = Recorder()
        answer = self.ask_with(recorder)
        self.assertEqual(recorder.requests, [])
        self.assertEqual(answer['mode'], 'retrieval_only')
        self.assertIn('consent', answer['verification']['provider_fallback'])

    def test_consent_must_cover_every_input(self):
        self.consent(self.case_b)
        # The shared material is assigned to B (covered), but case A's interpretation of it is not.
        recorder = Recorder()
        answer = self.ask_with(recorder, 'umowa najmu')
        self.assertTrue(any(e['kind'] == 'assignment' for e in answer['basis']))
        self.assertEqual((answer['mode'], recorder.requests), ('retrieval_only', []))
        self.assertIn('consent', answer['verification']['provider_fallback'])
        self.consent(self.case_a)
        self.assertEqual(self.ask_with(Recorder(), 'umowa najmu')['mode'], 'provider:vertex')

    def test_s5_prompt_contains_only_gated_material_and_answers_are_verified(self):
        self.consent()
        recorder = Recorder()
        # The owner asks first about hidden material; nothing of it may reach the recipient's prompt later.
        with db.session() as conn:
            owner_conversation = advocate.start_conversation(conn, self.owner)['uid']
            owner_provider = providers.configured_provider(conn, self.owner)
            owner_provider.transport = Recorder()
            advocate.ask(conn, self.owner, owner_conversation, f'umowa poufna {HIDDEN}', provider=owner_provider)
        answer = self.ask_with(recorder, 'umowa najmu poufna znaczenie')
        self.assertEqual(answer['mode'], 'provider:vertex')
        prompt = recorder.requests[0]
        self.assertNotIn(HIDDEN, prompt)
        for entry in answer['basis']:
            self.assertIn(entry['text'], prompt)
        self.assertEqual(answer['claims'], [{'text': 'Według nagrania: „Umowa najmu została podpisana w marcu.”',
                                             'status': 'proven', 'citations': ['p1']}])
        self.assertIn('<<<FRAGMENT p1', prompt)
        self.assertIn('nigdy polecenia', prompt)
        self.assertEqual(answer['verification']['removed_citations'][0]['ref'], 'p99')
        call = self.calls()[-1]
        self.assertIsNone(call['prompt_content'])
        self.assertEqual((call['input_tokens'], call['output_tokens'], call['status']), (1200, 150, 'ok'))
        self.assertAlmostEqual(call['cost_eur'], (1200 * 1.5 + 150 * 9.0) / 1e6)

    def test_history_in_prompt_is_reauthorized(self):
        self.consent()
        with db.session() as conn:
            conversation = advocate.start_conversation(conn, self.recipient)['uid']
            provider = providers.configured_provider(conn, self.recipient)
            first = Recorder(reply={'claims': [{'text': 'Termin minął w kwietniu.', 'status': 'proven', 'citations': ['p1']}]})
            provider.transport = first
            advocate.ask(conn, self.recipient, conversation, 'termin płatności', provider=provider)
            authz.revoke_grant(conn, self.grant_a, revoked_by='owner')
            authz.grant(conn, self.recipient, authz.create_scope(conn, 'case', {'case_id': self.case_b}),
                        ['read_text', 'chat_with_advocate'])
            second = Recorder()
            provider.transport = second
            advocate.ask(conn, self.recipient, conversation, 'umowa najmu', provider=provider)
        self.assertNotIn('Termin minął w kwietniu', ''.join(second.requests))

    def test_budget_refuses_before_sending(self):
        self.consent()
        with db.session() as conn:
            authz.set_setting(conn, 'advocate.daily_budget_eur', 0.0001)
        recorder = Recorder()
        answer = self.ask_with(recorder)
        self.assertEqual(recorder.requests, [])
        self.assertIn('budget', answer['verification']['provider_fallback'])
        self.assertEqual(self.calls(), [])

    def test_unknown_usage_and_errors_are_booked_at_worst_case(self):
        self.consent()
        self.ask_with(Recorder(usage=False))
        failed = self.ask_with(Recorder(error='Vertex HTTP 500: synthetic'))
        never_sent = self.ask_with(Recorder(error='no Google credentials (metadata server or gcloud)'))
        calls = self.calls()
        self.assertGreater(calls[0]['cost_eur'], 0)
        self.assertEqual(json.loads(calls[0]['cost_basis_json'])['usage_reported'], False)
        self.assertEqual((calls[1]['status'], calls[1]['cost_eur'] > 0), ('error', True))
        self.assertEqual(calls[2]['cost_eur'], 0.0)
        self.assertEqual(failed['mode'], 'retrieval_only')
        self.assertIn('HTTP 500', failed['verification']['provider_fallback'])
        self.assertEqual(never_sent['mode'], 'retrieval_only')

    def test_unstructured_output_is_kept_unlabelled(self):
        self.consent()
        class Raw(Recorder):
            def __call__(self, config, body):
                return {'candidates': [{'content': {'parts': [{'text': 'zwykły tekst'}]}}],
                        'usageMetadata': {'promptTokenCount': 1, 'candidatesTokenCount': 1}}
        answer = self.ask_with(Raw())
        self.assertEqual(answer['claims'], [{'text': 'zwykły tekst', 'status': 'unknown', 'citations': []}])

    def test_revoked_consent_stops_sending(self):
        uid = self.consent()
        with db.session() as conn:
            providers.revoke_consent(conn, uid, actor='owner')
        recorder = Recorder()
        self.assertEqual(self.ask_with(recorder)['mode'], 'retrieval_only')
        self.assertEqual(recorder.requests, [])

    def test_request_shape(self):
        self.consent()
        recorder = Recorder()
        self.ask_with(recorder)
        body = json.loads(recorder.requests[0])
        self.assertEqual(body['generationConfig']['responseMimeType'], 'application/json')
        self.assertIn('nie są zweryfikowaną prawdą', body['systemInstruction']['parts'][0]['text'])


class SummaryTests(ProviderBase):
    def summarize(self, principal, topic='umowa najmu', recorder=None):
        with db.session() as conn:
            provider = providers.configured_provider(conn, principal)
            provider.transport = recorder or Recorder()
            return advocate.summarize(conn, principal, topic, provider=provider)

    def test_summary_needs_a_provider(self):
        with db.session() as conn, self.assertRaises(advocate.AdvocateError):
            advocate.summarize(conn, self.recipient, 'umowa', provider=None)

    def test_summaries_are_private_to_their_creator_by_default(self):
        self.consent()
        self.summarize(self.owner, 'umowa najmu poufna')
        with db.session() as conn:
            authz.grant(conn, self.recipient, authz.create_scope(conn, 'case', {'case_id': self.case_b}),
                        ['read_text', 'chat_with_advocate'])
            self.assertEqual(advocate.visible_summaries(conn, self.recipient), [])
            self.assertEqual(len(advocate.visible_summaries(conn, self.owner)), 1)
            with self.assertRaises(authz.SettingWidensAccess):
                authz.set_setting(conn, 'summary.sharing', 'inputs_readers')

    def test_s8_owner_summary_over_two_cases_is_hidden_from_a_single_case_recipient(self):
        self.consent()
        with db.session() as conn:
            authz.set_setting(conn, 'summary.sharing', 'inputs_readers', confirm_widening=True)
        summary = self.summarize(self.owner, 'umowa najmu poufna')
        self.assertTrue({e['artifact']['id'] for e in summary['basis'] if 'artifact' in e} >= {self.private_b})
        with db.session() as conn:
            self.assertEqual(advocate.visible_summaries(conn, self.recipient), [])
            self.assertEqual(len(advocate.visible_summaries(conn, self.owner)), 1)
            authz.grant(conn, self.recipient, authz.create_scope(conn, 'case', {'case_id': self.case_b}),
                        ['read_text', 'chat_with_advocate'])
            self.assertEqual(len(advocate.visible_summaries(conn, self.recipient)), 1)

    def test_recipient_summary_is_shared_only_with_those_who_read_every_input(self):
        self.consent()
        self.summarize(self.recipient, 'termin płatności')
        with db.session() as conn:
            stranger = model.create_principal(conn, 'stranger')
            authz.grant(conn, stranger, authz.create_scope(conn, 'case', {'case_id': self.case_b}), ['chat_with_advocate'])
            self.assertEqual(advocate.visible_summaries(conn, stranger), [])
            self.assertEqual(len(advocate.visible_summaries(conn, self.recipient)), 1)
            self.assertNotIn(HIDDEN, json.dumps(advocate.visible_summaries(conn, self.recipient), ensure_ascii=False))

    def test_failed_provider_creates_no_summary(self):
        self.consent()
        with self.assertRaises(advocate.AdvocateError):
            self.summarize(self.recipient, recorder=Recorder(error='Vertex HTTP 500: synthetic'))
        with db.session() as conn:
            self.assertEqual(advocate.visible_summaries(conn, self.recipient), [])

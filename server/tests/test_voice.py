"""Package P12: voice is a layer over the same gate, consent and budget.

Recording fake transports stand in for Vertex STT and Cloud TTS; synthetic data only.
"""
import base64
import json

from fastapi.testclient import TestClient

from server.app import advocate, auth, authz, db, main, providers
from server.app import assignments as model
from server.tests.test_advocate import AdvocateFixture, HIDDEN


class FakeSpeech:
    def __init__(self):
        self.stt_requests, self.tts_requests = [], []

    def stt(self, config, body):
        self.stt_requests.append(body)
        return {'candidates': [{'content': {'parts': [{'text': 'Kiedy podpisano umowę najmu?'}]}}],
                'usageMetadata': {'promptTokenCount': 300, 'candidatesTokenCount': 12}}

    def tts(self, config, body):
        self.tts_requests.append(json.dumps(body, ensure_ascii=False))
        return {'audioContent': base64.b64encode(b'ID3 synthetic mp3').decode()}


class VoiceTests(AdvocateFixture):
    def setUp(self):
        super().setUp()
        self.fake = FakeSpeech()
        with db.session() as conn:
            authz.set_setting(conn, 'advocate.provider', {'type': 'vertex_gemini', 'project': 'synthetic-project'})
            authz.set_setting(conn, 'advocate.voice', {'enabled': True})
            for provider in ('vertex', 'google_tts'):  # text-to-speech is a separate service with its own consent
                providers.record_consent(conn, provider, authz.create_scope(conn, 'installation', {}),
                                         granted_by='owner', basis_text='synthetic test consent')

    def voice(self, conn, principal=None):
        voice = providers.configured_voice(conn, principal or self.recipient)
        voice.stt, voice.tts = self.fake.stt, self.fake.tts
        return voice

    def calls(self, purpose):
        with db.session() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM provider_calls WHERE purpose=? AND status<>'reserved'", (purpose,))]

    def answer(self, principal=None, question='umowa najmu'):
        principal = principal or self.recipient
        with db.session() as conn:
            conversation = advocate.start_conversation(conn, principal)['uid']
            return advocate.ask(conn, principal, conversation, question)

    def test_voice_is_off_by_default_and_needs_a_text_provider(self):
        with db.session() as conn:
            authz.set_setting(conn, 'advocate.voice', None)
            self.assertIsNone(providers.configured_voice(conn, self.recipient))
            authz.set_setting(conn, 'advocate.voice', {'enabled': True})
            authz.set_setting(conn, 'advocate.provider', None)
            self.assertIsNone(providers.configured_voice(conn, self.recipient))

    def test_recording_is_sent_only_with_the_speakers_consent(self):
        with db.session() as conn:
            voice = self.voice(conn)
            with self.assertRaises(providers.ProviderUnavailable):
                voice.transcribe(conn, b'webm bytes', 'audio/webm;codecs=opus', speaker_consent=False)
            self.assertEqual(self.fake.stt_requests, [])
            for mime, audio in (('text/html', b'x'), ('audio/webm', b''), ('audio/webm', b'x' * (10 * 1024 * 1024 + 1))):
                with self.subTest(mime=mime, size=len(audio)), self.assertRaises(providers.ProviderUnavailable):
                    voice.transcribe(conn, audio, mime, speaker_consent=True)
            result = voice.transcribe(conn, b'webm bytes', 'audio/webm;codecs=opus', speaker_consent=True)
        self.assertEqual(result['text'], 'Kiedy podpisano umowę najmu?')
        self.assertEqual(len(self.fake.stt_requests), 1)
        call = self.calls('speech_to_text')[0]
        self.assertIsNone(call['prompt_content'])
        self.assertTrue(json.loads(call['cost_basis_json'])['speaker_consent'])

    def test_answer_is_read_aloud_only_under_gate_and_consent(self):
        answer = self.answer()
        with db.session() as conn:
            audio = self.voice(conn).synthesize(conn, answer['answer_uid'])
        self.assertEqual(audio, b'ID3 synthetic mp3')
        self.assertNotIn(HIDDEN, ''.join(self.fake.tts_requests))
        self.assertIn('Umowa najmu', self.fake.tts_requests[0])
        call = self.calls('text_to_speech')[0]
        self.assertGreater(json.loads(call['cost_basis_json'])['actual_eur'], 0)
        with db.session() as conn:
            stranger = model.create_principal(conn, 'obcy')
            authz.grant(conn, stranger, authz.create_scope(conn, 'case', {'case_id': self.case_b}), ['chat_with_advocate'])
            with self.assertRaises(authz.Denied):
                self.voice(conn, stranger).synthesize(conn, answer['answer_uid'])
            authz.revoke_grant(conn, self.grant_a, revoked_by='owner')
            authz.grant(conn, self.recipient, authz.create_scope(conn, 'case', {'case_id': self.case_b}), ['chat_with_advocate'])
            with self.assertRaises(authz.Denied):
                self.voice(conn).synthesize(conn, answer['answer_uid'])

    def test_a_replay_is_served_from_the_cache_without_a_second_call(self):
        answer = self.answer()
        with db.session() as conn:
            first = self.voice(conn).synthesize(conn, answer['answer_uid'])
            spent = providers.spent_today_eur(conn)
            second = self.voice(conn).synthesize(conn, answer['answer_uid'])
            self.assertEqual(providers.spent_today_eur(conn), spent)
            [cached] = [dict(r) for r in conn.execute('SELECT * FROM advocate_speech')]
        self.assertEqual((first, second), (b'ID3 synthetic mp3', b'ID3 synthetic mp3'))
        self.assertEqual(len(self.fake.tts_requests), 1)
        [call] = self.calls('text_to_speech')
        self.assertEqual(cached['provider_call_uid'], json.loads(call['cost_basis_json'])['settles_reservation'])
        self.assertEqual((cached['voice'], bytes(cached['audio'])), (providers.VOICE_PRESET['tts_voice'], b'ID3 synthetic mp3'))
        with db.session() as conn:  # another voice is another text-to-speech call
            voice = self.voice(conn)
            voice.config = {**voice.config, 'tts_voice': 'pl-PL-Standard-B'}
            voice.synthesize(conn, answer['answer_uid'])
        self.assertEqual(len(self.fake.tts_requests), 2)
        self.assertEqual(len(self.calls('text_to_speech')), 2)

    def test_reading_aloud_needs_owner_consent_for_every_input_and_budget(self):
        answer = self.answer()
        with db.session() as conn:
            for row in conn.execute('SELECT uid FROM provider_consents').fetchall():
                providers.revoke_consent(conn, row['uid'], actor='owner')
            with self.assertRaises(providers.ProviderUnavailable):
                self.voice(conn).synthesize(conn, answer['answer_uid'])
            for provider in ('vertex', 'google_tts'):
                providers.record_consent(conn, provider, authz.create_scope(conn, 'installation', {}),
                                         granted_by='owner', basis_text='again')
            authz.set_setting(conn, 'advocate.daily_budget_eur', 0.0)
            with self.assertRaises(providers.ProviderUnavailable):
                self.voice(conn).synthesize(conn, answer['answer_uid'])
        self.assertEqual(self.fake.tts_requests, [])

    def test_voice_over_http(self):
        answer = self.answer()
        with db.session() as conn:
            token = auth.issue_token(conn, self.recipient)
            authz.set_setting(conn, 'auth.mode', 'tokens')
        original = providers.configured_voice
        def configured(conn, principal):
            voice = original(conn, principal)
            if voice is not None:
                voice.stt, voice.tts = self.fake.stt, self.fake.tts
            return voice
        providers.configured_voice = configured
        try:
            with TestClient(main.app) as client:
                headers = {'X-AGEDS-Token': token}
                status = client.get('/api/advocate/status', headers=headers).json()
                self.assertEqual(status['voice']['stt_model'], 'gemini-3.5-flash')
                refused = client.post('/api/advocate/voice/transcribe', headers=headers,
                                      files={'file': ('q.webm', b'audio', 'audio/webm')})
                self.assertEqual(refused.status_code, 422)
                heard = client.post('/api/advocate/voice/transcribe', headers=headers, data={'speaker_consent': 'yes'},
                                    files={'file': ('q.webm', b'audio', 'audio/webm')})
                self.assertEqual(heard.json()['text'], 'Kiedy podpisano umowę najmu?')
                speech = client.get(f"/api/advocate/answers/{answer['answer_uid']}/speech", headers=headers)
                self.assertEqual((speech.status_code, speech.headers['content-type'], speech.content),
                                 (200, 'audio/mpeg', b'ID3 synthetic mp3'))
                missing = client.get('/api/advocate/answers/nope/speech', headers=headers)
                self.assertEqual(missing.status_code, 404)
        finally:
            providers.configured_voice = original

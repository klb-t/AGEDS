"""Isolated synthetic advocate fixture in tokens mode; never points at an existing database."""
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import wave

HIDDEN = 'CANARY-browser-hidden'
LITERAL = ' <img src=x onerror="window.__sourceExecuted=1">'


def main():
    with tempfile.TemporaryDirectory(prefix='ageds-advocate-browser-') as directory:
        root = Path(directory)
        os.environ.update(EW_DATA_DIR=str(root), EW_DB_PATH=str(root / 'evidence.sqlite'), EW_STORE_DIR=str(root / 'store'))
        from server.app import auth, authz, db, evidence
        from server.app import assignments as model
        import uvicorn
        db.init_db()
        path = root / 'rozmowa-syntetyczna.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b''.join(struct.pack('<h', int(800 * math.sin(2 * math.pi * 330 * i / 16000))) for i in range(96000)))
        with db.session() as conn:
            case_a = conn.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()[0]
            case_b = conn.execute('INSERT INTO cases(name) VALUES(?)', (HIDDEN,)).lastrowid
        shared = evidence.ingest_file(path, source_id=evidence.ensure_source('synthetic', 'browser A', case_id=case_a),
                                      source_locator='synthetic:browser-a', mime_type='audio/wav')
        segments = [{'start': 0.5, 'end': 2.0, 'text': 'Umowa najmu została podpisana piątego marca.' + LITERAL},
                    {'start': 2.5, 'end': 4.0, 'text': 'Termin płatności czynszu minął w kwietniu.'}]
        evidence.add_derived_text(shared, 'transcript', ' '.join(s['text'] for s in segments), model='synthetic', segments=segments)
        hidden = root / 'ukryte.wav'
        hidden.write_bytes(path.read_bytes()[:44] + b'\x00' * 3200)
        secret = evidence.ingest_file(hidden, source_id=evidence.ensure_source('synthetic', 'browser B', case_id=case_b),
                                      source_locator='synthetic:browser-b', mime_type='audio/wav')
        evidence.add_derived_text(secret, 'transcript', f'Umowa najmu {HIDDEN}', model='synthetic',
                                  segments=[{'start': 0, 'end': 1, 'text': f'Umowa najmu {HIDDEN}'}])
        with db.session() as conn:
            model.assign(conn, case_id=case_b, target_kind='artifact', target_id=shared, meaning_text=f'{HIDDEN} meaning')
            authz.bootstrap_owner(conn, 'właściciel (syntetyczny)')
            recipient = model.create_principal(conn, 'adwokat (syntetyczny)', kind='human')
            authz.grant(conn, recipient, authz.create_scope(conn, 'case', {'case_id': case_a}),
                        ['read_metadata', 'read_text', 'read_bytes', 'chat_with_advocate'])
            token = auth.issue_token(conn, recipient)
            authz.set_setting(conn, 'auth.mode', 'tokens')
            if os.environ.get('AGEDS_BROWSER_TEST_PROVIDER') == '1':
                # Test-only transport: deterministic replies, never a network call.
                from server.app import providers
                authz.set_setting(conn, 'advocate.provider', {'type': 'vertex_gemini', 'project': 'synthetic-test'})
                for provider in ('vertex', 'google_tts'):
                    providers.record_consent(conn, provider, authz.create_scope(conn, 'installation', {}),
                                             granted_by='browser fixture', basis_text='synthetic browser test')
                def transport(config, body):
                    text = json.dumps({'claims': [{'text': 'Według transkrypcji: „Umowa najmu została podpisana piątego marca.”',
                                                   'status': 'proven', 'citations': ['p1']},
                                                  {'text': 'Materiał nie mówi, kto był obecny.', 'status': 'unknown',
                                                   'citations': []}]}, ensure_ascii=False)
                    return {'candidates': [{'content': {'parts': [{'text': text}]}}],
                            'usageMetadata': {'promptTokenCount': 900, 'candidatesTokenCount': 60}}
                original = providers.configured_provider
                def configured(db_, principal):
                    provider = original(db_, principal)
                    if provider is not None:
                        provider.transport = transport
                    return provider
                providers.configured_provider = configured
                authz.set_setting(conn, 'advocate.voice', {'enabled': True})
                import base64
                wav = base64.b64encode(path.read_bytes()[:16044]).decode()
                def stt(config, body):
                    return {'candidates': [{'content': {'parts': [{'text': 'Kiedy minął termin płatności?'}]}}],
                            'usageMetadata': {'promptTokenCount': 200, 'candidatesTokenCount': 8}}
                original_voice = providers.configured_voice
                def configured_voice(db_, principal):
                    voice = original_voice(db_, principal)
                    if voice is not None:
                        voice.stt, voice.tts = stt, (lambda config, body: {'audioContent': wav})
                    return voice
                providers.configured_voice = configured_voice
        print(json.dumps({'token': token, 'hidden': HIDDEN}), flush=True)
        from server.app.main import app
        uvicorn.run(app, host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    main()

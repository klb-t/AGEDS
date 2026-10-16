"""Review findings #3 (HTTP recipe overrides), #6 (token coverage + CSRF), #8 (token audit/write load)."""
from __future__ import annotations

import pytest

from server.app import evidence
from server.app.media.queue import enqueue_media_job
from server.tests.test_review_fixes_support import env, wav_bytes  # noqa: F401 (fixture)


def _audio(env) -> int:
    path = env.root / 'call.wav'
    path.write_bytes(wav_bytes())
    source = evidence.ensure_source('synthetic', 'media overrides')
    return evidence.ingest_file(path, source_id=source, source_locator='synthetic:call.wav', mime_type='audio/wav')


# --------------------------------------------------------------------- finding 3

OPERATOR_ONLY = [
    {'kind': 'diarize', 'overrides': {'pipeline': 'someone-else/arbitrary-repo'}},
    {'kind': 'diarize', 'overrides': {'engine': 'pyannote.audio'}},
    {'kind': 'diarize', 'overrides': {'limits': {'max_turns': 10}}},
    {'kind': 'transcribe', 'overrides': {'model': '/etc'}},
    {'kind': 'transcribe', 'overrides': {'model': 'someone-else/any-ct2-model'}},
    {'kind': 'transcribe', 'overrides': {'model_options': {'revision': 'deadbeef'}}},
    {'kind': 'transcribe', 'overrides': {'model_options': {'download_root': '/tmp/x'}}},
    {'kind': 'transcribe', 'overrides': {'device': 'cuda'}},
    {'kind': 'transcribe', 'overrides': {'compute_type': 'float32'}},
    {'kind': 'transcribe', 'overrides': {'engine': 'faster-whisper'}},
    {'kind': 'transcribe', 'recipe': 'per-segment-pl-nl-en', 'overrides': {'limits': {'max_windows': 1, 'max_regions_recorded': 0}}},
    {'kind': 'ocr', 'overrides': {'engine': 'tesseract-cli'}},
    {'kind': 'ocr', 'overrides': {'limits': {'timeout_s': 86400}}},
    {'kind': 'ocr', 'overrides': {'pdf': {'rasterizer': 'pdftoppm', 'dpi': 72, 'max_pages': 2}}},
]

CLIENT_ALLOWED = [
    {'kind': 'transcribe', 'overrides': {'transcribe_options': {'beam_size': 3}}},
    {'kind': 'transcribe', 'overrides': {'language': 'pl', 'word_timestamps': False}},
    {'kind': 'transcribe', 'overrides': {'vad': {'enabled': True, 'parameters': {'min_silence_duration_ms': 300}}}},
    {'kind': 'transcribe', 'recipe': 'per-segment-pl-nl-en',
     'overrides': {'allowed_languages': ['pl', 'en'], 'windows': {'max_window_s': 10.0, 'min_window_s': 1.0}}},
    {'kind': 'diarize', 'overrides': {'num_speakers': 2}},
    {'kind': 'ocr', 'overrides': {'psm': 6, 'languages': ['eng'], 'pdf': {'dpi': 150}}},
]


@pytest.mark.parametrize('body', OPERATOR_ONLY)
def test_finding3_http_cannot_choose_model_pipeline_or_operator_options(env, body):
    artifact = _audio(env)
    with env.client() as http:
        response = http.post(f'/api/media/artifacts/{artifact}/jobs', json=body)
    assert response.status_code == 422, response.text
    assert 'preset' in response.json()['detail'] and 'CLI' in response.json()['detail']
    assert env.one('SELECT COUNT(*) FROM jobs') == 0


@pytest.mark.parametrize('body', CLIENT_ALLOWED)
def test_finding3_http_may_override_decode_and_strategy_parameters(env, body):
    artifact = _audio(env)
    with env.client() as http:
        response = http.post(f'/api/media/artifacts/{artifact}/jobs', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['created'] is True


def test_finding3_operator_paths_keep_full_overrides(env):
    artifact = _audio(env)
    job = enqueue_media_job('transcribe', artifact, overrides={'model': 'large-v3', 'model_options': {'revision': 'abc'}})
    assert job['created'] is True
    pipeline = enqueue_media_job('diarize', artifact, overrides={'pipeline': '/opt/private/pipeline'})
    assert pipeline['created'] is True


# --------------------------------------------------------------------- finding 8

def test_finding8_rejected_tokens_are_aggregated_not_one_row_each(env, monkeypatch):
    from server.app.integrations import tokens
    monkeypatch.setenv('EW_REQUIRE_TOKEN', '1')
    with env.client() as http:
        for i in range(60):
            assert http.get('/api/timeline', headers={'Authorization': f'Bearer ageds_{i:012x}.junk'}).status_code == 401
        for _ in range(30):
            assert http.get('/api/timeline', headers={'Authorization': 'Bearer ageds_aaaaaaaaaaaa.junk'}).status_code == 401
    audit_rows = env.one("SELECT COUNT(*) FROM audit_log WHERE action='token_rejected'")
    assert audit_rows <= tokens.MAX_REJECTION_KEYS_PER_BUCKET + 1
    counters = env.rows('SELECT token_key, reason, count FROM api_token_rejections')
    assert sum(r['count'] for r in counters) == 90
    assert len(counters) <= tokens.MAX_REJECTION_KEYS_PER_BUCKET + 1
    assert any(r['token_key'] == '*' for r in counters)  # keys beyond the per-hour cap are pooled


def test_finding8_valid_token_checks_are_read_only_and_touch_lazily(env, monkeypatch):
    import sqlite3
    from server.app.integrations import tokens
    created = tokens.create_token('synthetic', ['read'])
    first = tokens.verify_token(created['token'])
    assert first['use_count'] == 1 and first['last_used_at']
    blocker = sqlite3.connect(str(env.settings.db_path), timeout=0.1)
    blocker.execute('BEGIN IMMEDIATE')  # e.g. a long write elsewhere: verification must not need the lock
    try:
        for _ in range(10):
            assert tokens.verify_token(created['token'])['id'] == created['id']
    finally:
        blocker.rollback()
        blocker.close()
    assert env.one('SELECT use_count FROM api_tokens WHERE id=?', (created['id'],)) == 1  # uses pending in memory
    monkeypatch.setattr(tokens, 'TOUCH_INTERVAL_SECONDS', 0)
    assert tokens.verify_token(created['token'])['use_count'] == 12  # 1 + 10 pending + this one
    assert env.one("SELECT COUNT(*) FROM audit_log WHERE action='token_rejected'") == 0


# --------------------------------------------------------------------- finding 6

def _proposal(env) -> tuple[int, dict]:
    from server.app.integrations import proposals, tokens
    artifact = _audio(env)
    token = tokens.create_token('synthetic client', ['propose'], client_id='chatadhd')
    proposal = proposals.submit_proposal({'artifact_id': artifact, 'body': 'model says X'}, client_id='chatadhd',
                                         token_id=token['id'])
    return proposal['id'], token


def test_finding6_require_token_covers_exports_and_every_state_change(env, monkeypatch):
    from server.app.integrations import tokens
    proposal_id, client_token = _proposal(env)
    operator = tokens.create_token('operator browser', ['read'])['token']
    monkeypatch.setenv('EW_REQUIRE_TOKEN', '1')
    with env.client() as http:
        assert http.get('/api/timeline').status_code == 401
        for path in ('/export/events.csv', '/export/manifest.json'):
            assert http.get(path).status_code == 401, path
        assert http.post('/upload', files={'file': ('a.txt', b'abc', 'text/plain')}).status_code == 401
        decided = http.post(f'/integrations/proposals/{proposal_id}/decide', data={'decision': 'accepted'},
                            follow_redirects=False)
        assert decided.status_code == 401
        assert http.post(f"/integrations/tokens/{client_token['id']}/revoke", follow_redirects=False).status_code == 401
        assert env.one('SELECT COUNT(*) FROM annotations') == 0
        assert env.one('SELECT revoked_at FROM api_tokens WHERE id=?', (client_token['id'],)) is None
        assert http.get('/integrations').status_code == 200  # read-only pages stay open (documented)
        bearer = {'Authorization': f'Bearer {operator}'}
        assert http.get('/export/events.csv', headers=bearer).status_code == 200
        assert http.post('/upload', files={'file': ('a.txt', b'abc', 'text/plain')}, headers=bearer).status_code == 200
        # Browser forms: a same-origin session cookie issued by the login form.
        assert http.post('/integrations/session', data={'token': 'ageds_000000000000.nope'},
                         follow_redirects=False).status_code == 401
        login = http.post('/integrations/session', data={'token': operator}, follow_redirects=False)
        assert login.status_code == 303
        cookie = login.headers['set-cookie']
        assert 'ageds_token=' in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie.replace('Strict', 'strict')
        assert http.post(f'/integrations/proposals/{proposal_id}/decide', data={'decision': 'accepted'},
                         follow_redirects=False).status_code == 303
        assert env.one('SELECT COUNT(*) FROM annotations') == 1
        assert http.get('/export/events.csv').status_code == 200
        assert http.get('/api/read/v1/contract').status_code == 401  # scoped routes still need a bearer header
        assert http.post('/integrations/session/logout', follow_redirects=False).status_code == 303
        assert http.get('/export/events.csv').status_code == 401


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://evil.example'},
    {'Origin': 'null'},
    {'Origin': 'http://testserver.evil.example'},
    {'Referer': 'https://evil.example/page'},
    {'Sec-Fetch-Site': 'cross-site'},
    {'Origin': 'http://testserver', 'Sec-Fetch-Site': 'cross-site'},
])
def test_finding6_cross_site_state_changes_are_refused(env, headers):
    proposal_id, client_token = _proposal(env)
    with env.client() as http:
        response = http.post(f'/integrations/proposals/{proposal_id}/decide', data={'decision': 'accepted'},
                             headers=headers, follow_redirects=False)
        assert response.status_code == 403, response.text
        assert http.post(f"/integrations/tokens/{client_token['id']}/revoke", headers=headers,
                         follow_redirects=False).status_code == 403
        assert http.post('/upload', files={'file': ('a.txt', b'abc', 'text/plain')}, headers=headers).status_code == 403
        assert http.get('/integrations', headers=headers).status_code == 200  # reads are not state changes
    assert env.one('SELECT COUNT(*) FROM annotations') == 0
    assert env.one('SELECT revoked_at FROM api_tokens WHERE id=?', (client_token['id'],)) is None


@pytest.mark.parametrize('headers', [
    {},  # CLI, curl, scripts
    {'Origin': 'http://testserver'},
    {'Origin': 'http://testserver', 'Sec-Fetch-Site': 'same-origin'},
    {'Referer': 'http://testserver/integrations'},
])
def test_finding6_same_origin_and_headerless_state_changes_work(env, headers):
    proposal_id, _ = _proposal(env)
    with env.client() as http:
        response = http.post(f'/integrations/proposals/{proposal_id}/decide', data={'decision': 'accepted'},
                             headers=headers, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert env.one('SELECT COUNT(*) FROM annotations') == 1


def test_finding6_trusted_origins_and_bearer_clients(env, monkeypatch):
    from server.app.integrations import tokens
    proposal_id, _ = _proposal(env)
    client = tokens.create_token('synthetic proposer', ['propose'], client_id='chatadhd')['token']
    monkeypatch.setenv('EW_TRUSTED_ORIGINS', 'https://evidence.example.org')
    with env.client() as http:
        # A bearer client cannot be forged cross-site (custom headers need a CORS preflight).
        sent = http.post('/api/integrations/v1/proposals', json={'artifact_id': 1, 'body': 'x'},
                         headers={'Authorization': f'Bearer {client}', 'Origin': 'https://elsewhere.example'})
        assert sent.status_code == 201, sent.text
        proxied = http.post(f'/integrations/proposals/{proposal_id}/decide', data={'decision': 'rejected'},
                            headers={'Origin': 'https://evidence.example.org'}, follow_redirects=False)
        assert proxied.status_code == 303

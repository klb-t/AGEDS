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

"""Review 2 (AGEDS-TASK-20261016-R2): job-side fixes — import plan heartbeat (#10), bounded HTTP
decoding options (#11). Temporary synthetic databases only."""
from __future__ import annotations

import sqlite3
import time

import pytest

from server.app.importplan import execute
from server.app.media import recipe
from server.tests.test_review_fixes_security import _audio
from server.tests.test_review_fixes_support import env  # noqa: F401 (fixture)


# --------------------------------------------------------------------- finding 10

def _wait_for(calls: list, count: int, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while len(calls) < count and time.monotonic() < deadline:
        time.sleep(0.01)


def test_review2_10_heartbeat_retries_after_a_failed_renewal(monkeypatch):
    monkeypatch.setattr(execute, 'HEARTBEAT_MIN_INTERVAL_S', 0.05)
    monkeypatch.setattr(execute, 'HEARTBEAT_RETRY_BASE_S', 0.01)
    calls = []

    def flaky(attempt_id, token, lease_seconds):
        calls.append(time.monotonic())
        if len(calls) <= 3:  # e.g. another request holds the write lock longer than the busy timeout
            raise sqlite3.OperationalError('database is locked')
        return True
    monkeypatch.setattr(execute, '_renew', flaky)
    with execute._heartbeat(1, 'token', lease_seconds=0):
        _wait_for(calls, 6)
    assert len(calls) >= 6, calls  # pre-fix: the thread returned after the first exception (one call)
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert max(gaps[:3]) < 0.05 + 0.04  # retries come sooner than (at most) the regular interval


def test_review2_10_heartbeat_stops_when_the_lease_was_taken_over(monkeypatch):
    monkeypatch.setattr(execute, 'HEARTBEAT_MIN_INTERVAL_S', 0.02)
    calls = []

    def lost(attempt_id, token, lease_seconds):
        calls.append(1)
        return False
    monkeypatch.setattr(execute, '_renew', lost)
    with execute._heartbeat(1, 'token', lease_seconds=0):
        _wait_for(calls, 3, seconds=0.5)
    assert calls == [1]


# --------------------------------------------------------------------- finding 11

REFUSED = [
    {'transcribe_options': {'beam_size': 1000}},
    {'transcribe_options': {'best_of': 500}},
    {'transcribe_options': {'patience': 1e6}},
    {'transcribe_options': {'temperature': [0.1] * 64}},
    {'transcribe_options': {'temperature': 7}},
    {'transcribe_options': {'max_new_tokens': 10 ** 9}},
    {'transcribe_options': {'no_repeat_ngram_size': 10 ** 6}},
    {'transcribe_options': {'chunk_length': 3600}},
    {'transcribe_options': {'suppress_tokens': list(range(65))}},
    {'transcribe_options': {'initial_prompt': 'x' * 5000}},
    {'transcribe_options': {'beam_size': True}},
    {'vad': {'enabled': True, 'parameters': {'min_silence_duration_ms': 10 ** 12}}},
]
ALLOWED = [
    {'transcribe_options': {'beam_size': 10, 'best_of': 10, 'patience': 2.0}},
    {'transcribe_options': {'temperature': [0.0, 0.2, 0.4, 0.6, 0.8, 1.0], 'condition_on_previous_text': False}},
    {'transcribe_options': {'max_new_tokens': 224, 'chunk_length': 30, 'initial_prompt': 'Rozmowa telefoniczna.'}},
    {'transcribe_options': {'log_prob_threshold': None, 'no_speech_threshold': 0.6}},
]


@pytest.mark.parametrize('overrides', REFUSED)
def test_review2_11_http_decoding_options_are_bounded(env, overrides):  # noqa: F811
    artifact = _audio(env)
    with env.client() as http:
        response = http.post(f'/api/media/artifacts/{artifact}/jobs', json={'kind': 'transcribe', 'overrides': overrides})
    assert response.status_code == 422, response.text
    assert env.one('SELECT COUNT(*) FROM jobs') == 0


@pytest.mark.parametrize('overrides', ALLOWED)
def test_review2_11_reasonable_http_decoding_options_are_accepted(env, overrides):  # noqa: F811
    artifact = _audio(env)
    with env.client() as http:
        response = http.post(f'/api/media/artifacts/{artifact}/jobs', json={'kind': 'transcribe', 'overrides': overrides})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('kind,overrides', [('ocr', {'pdf': {'dpi': 1200}}), ('ocr', {'pdf': {'max_pages': 10_000}}),
                                            ('diarization', {'num_speakers': 1000})])
def test_review2_11_other_http_resource_values_are_bounded(kind, overrides):
    with pytest.raises(recipe.RecipeError):
        recipe.check_client_overrides(kind, overrides)


def test_review2_11_bounds_cover_every_decoding_option():
    assert set(recipe.CLIENT_TRANSCRIBE_BOUNDS) == recipe.TRANSCRIBE_OPTION_KEYS
    assert set(recipe.CLIENT_VAD_BOUNDS) == recipe.VAD_PARAMETER_KEYS

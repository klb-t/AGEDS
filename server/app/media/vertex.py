"""Vertex AI (Gemini) engines for ASR and OCR behind the existing recipe and run contracts.

Owner consent R100 (2026-10-25): the owner's material may go to Google Vertex, not
public, not for training, provider cache on. A call still needs a recorded provider
consent covering the artifact (``providers.consent_coverage``) and fits the daily
budget through the same reservation as the advocate.

What the model returns is model output: timestamps are the model's estimates (no word
timing, so citations are limited to segment selectors), speaker labels are hypotheses,
and OCR lines have no pixel boxes. Original bytes are never changed; a recording in a
format Gemini does not accept is transcoded in memory for the call only, and the
transcoding is recorded with its own hash.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager

from .. import authz, providers
from ..db import session

ENGINE = 'vertex-gemini'
GEMINI_AUDIO = {'audio/wav', 'audio/x-wav', 'audio/mpeg', 'audio/mp3', 'audio/aiff', 'audio/aac', 'audio/ogg',
                'audio/flac'}
GEMINI_DOCUMENT = {'image/png', 'image/jpeg', 'image/webp', 'image/heic', 'image/heif', 'application/pdf'}
CHARS_PER_TOKEN = 3.0
AUDIO_TOKENS_PER_SECOND = 32  # Gemini's documented audio rate; used only for the worst-case reservation
ASR_PROMPT = (
    'Przepisz dosłownie całe nagranie w języku, w którym mówiono (polski, niderlandzki, angielski lub inny), bez '
    'tłumaczenia, poprawiania ani streszczania. Podziel na segmenty po zmianie mówcy lub co najwyżej 30 sekund. '
    'Dla każdego segmentu podaj początek i koniec w sekundach od początku nagrania, etykietę mówcy S1, S2, … '
    '(hipoteza, nie tożsamość), kod języka (pl, nl, en, …) i dosłowny tekst. Niezrozumiałe miejsca oznacz '
    '[niezrozumiałe]. Ciszę i dźwięki pomiń. Zwróć wyłącznie JSON zgodny ze schematem.')
ASR_SCHEMA = {'type': 'OBJECT', 'required': ['segments'], 'properties': {'segments': {'type': 'ARRAY', 'items': {
    'type': 'OBJECT', 'required': ['start', 'end', 'speaker', 'language', 'text'],
    'properties': {'start': {'type': 'NUMBER'}, 'end': {'type': 'NUMBER'}, 'speaker': {'type': 'STRING'},
                   'language': {'type': 'STRING'}, 'text': {'type': 'STRING'}}}}}}
OCR_PROMPT = (
    'Odczytaj dosłownie cały widoczny tekst, strona po stronie, linia po linii, w oryginalnym języku i pisowni, '
    'bez tłumaczenia i poprawek. {layout}Zwróć wyłącznie JSON zgodny ze schematem.')
OCR_CHAT = ('To może być zrzut ekranu rozmowy: dodatkowo podaj listę wiadomości z nadawcą (dokładnie tak, jak '
            'widać), czasem (dokładnie tak, jak widać, bez przeliczania) i treścią; po której stronie ekranu jest '
            'dymek (left/right/unknown). ')
OCR_SCHEMA = {'type': 'OBJECT', 'required': ['pages'], 'properties': {
    'pages': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'required': ['page', 'lines'], 'properties': {
        'page': {'type': 'INTEGER'}, 'lines': {'type': 'ARRAY', 'items': {'type': 'STRING'}}}}},
    'messages': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'required': ['sender', 'time', 'text', 'side'],
                 'properties': {'page': {'type': 'INTEGER'}, 'sender': {'type': 'STRING'}, 'time': {'type': 'STRING'},
                                'text': {'type': 'STRING'}, 'side': {'type': 'STRING'}}}}}}


class VertexUnavailable(RuntimeError):
    """The engine cannot run (no consent, budget, credentials or format); nothing was published."""


def parse_engine(engine: str) -> tuple[str, str]:
    """'vertex-gemini:<model>@<location>' -> (model, location)."""
    if not isinstance(engine, str) or not engine.startswith(ENGINE + ':') or '@' not in engine:
        raise ValueError('vertex engine must be "vertex-gemini:<model>@<location>"')
    model, location = engine[len(ENGINE) + 1:].split('@', 1)
    if not model or not location:
        raise ValueError('vertex engine needs a model and a location')
    return model, location


def _config(model: str, location: str, max_output_tokens: int) -> dict:
    with session() as db:
        stored = authz.get_setting(db, 'advocate.provider') or {}
    config = {**providers.VERTEX_PRESET, **(stored if isinstance(stored, dict) else {})}
    config.update({'model': model, 'location': location, 'max_output_tokens': max_output_tokens, 'temperature': 0.0,
                   'timeout_seconds': max(int(config.get('timeout_seconds', 120)), 600)})
    return config


def require_consent(artifact_id: int) -> None:
    with session() as db:
        consent = providers.consent_coverage(db, 'vertex')
        if not authz.covered_by(db, consent, 'artifact', artifact_id):
            raise VertexUnavailable('no recorded provider consent for vertex covers this artifact '
                                    '(advocate-consent --scope installation --basis "R100 …")')


def call(config: dict, parts: list, schema: dict, *, purpose: str, input_tokens_estimate: float,
         refs, transport=None) -> tuple[dict, dict]:
    """One generateContent call with reservation and settlement in provider_calls; returns (json, usage)."""
    transport = transport or providers.vertex_transport
    body = {'contents': [{'role': 'user', 'parts': parts}],
            'generationConfig': {'temperature': config['temperature'], 'maxOutputTokens': config['max_output_tokens'],
                                 'responseMimeType': 'application/json', 'responseSchema': schema}}
    worst = providers._cost(config, input_tokens_estimate, config['max_output_tokens'])
    digest_source = json.dumps(body, sort_keys=True)[:200_000]
    with session() as db:
        reservation = providers.reserve(db, None, worst, provider='vertex', model=config['model'], purpose=purpose,
                                        digest_source=digest_source, refs=refs)
    status, error, usage, decoded = 'ok', None, {}, None
    try:
        response = transport(config, body)
        usage = response.get('usageMetadata', {})
        text = ''.join(part.get('text', '') for part in response['candidates'][0]['content']['parts'])
        decoded = json.loads(text)
    except providers.ProviderUnavailable as exc:
        status, error = 'error', str(exc)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        status, error = 'error', f'unexpected response: {type(exc).__name__}'
    known = 'promptTokenCount' in usage and 'candidatesTokenCount' in usage
    never_sent = status == 'error' and error.startswith(providers.NOT_SENT)
    actual = (providers._cost(config, usage['promptTokenCount'], usage['candidatesTokenCount']) if known
              else 0.0 if never_sent else worst)
    with session() as db:
        providers._book(db, 'vertex', config['model'], None, purpose, digest_source, refs, status, error, usage,
                        actual - worst, {'usage_reported': known, 'actual_eur': actual, 'worst_case_eur': worst,
                                         'settles_reservation': reservation['uid'],
                                         'price': config['price_usd_per_million'], 'eur_per_usd': config['eur_per_usd']},
                        reservation['started'])
    if status == 'error':
        raise VertexUnavailable(error)
    return decoded, dict(usage, actual_eur=actual)


def _sniff_audio(data: bytes) -> str:
    if data[:6] == b'#!AMR\n':
        return 'audio/amr'
    if data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        return 'audio/wav'
    if data[:4] == b'fLaC':
        return 'audio/flac'
    if data[:4] == b'OggS':
        return 'audio/ogg'
    if data[:3] == b'ID3' or data[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):
        return 'audio/mpeg'
    if data[4:8] == b'ftyp':
        return 'audio/mp4'
    return 'application/octet-stream'


@contextmanager
def _private_copy(data: bytes):
    """A 0600 temporary file for tools that need seekable input (MP4 indexes); removed at once."""
    handle, path = tempfile.mkstemp(prefix='ageds-media-')
    try:
        with os.fdopen(handle, 'wb') as out:
            out.write(data)
        yield path
    finally:
        os.unlink(path)


def _transcode_flac(data: bytes) -> tuple[bytes, dict]:
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        raise VertexUnavailable('this recording format needs transcoding and ffmpeg is not available')
    version = subprocess.run([ffmpeg, '-version'], capture_output=True, timeout=30).stdout.decode('utf-8', 'replace').split('\n')[0]
    with _private_copy(data) as path:
        result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', path, '-f', 'flac', 'pipe:1'],
                                capture_output=True, timeout=600)
    if result.returncode != 0 or not result.stdout:
        raise VertexUnavailable('ffmpeg could not decode the recording: ' + result.stderr.decode('utf-8', 'replace')[:300])
    return result.stdout, {'tool': version, 'to': 'audio/flac', 'sha256': hashlib.sha256(result.stdout).hexdigest(),
                           'bytes': len(result.stdout), 'scope': 'in-memory copy for the model call; original unchanged'}


def _duration_seconds(data: bytes) -> float | None:
    ffprobe = shutil.which('ffprobe')
    if ffprobe is None:
        return None
    with _private_copy(data) as path:
        result = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', '-i', path],
                                capture_output=True, timeout=120)
    try:
        return float(json.loads(result.stdout)['format']['duration'])
    except (ValueError, KeyError, TypeError):
        return None


class VertexGeminiASR:
    """Strategy ``vertex_gemini``: one whole-file call returning timed, speaker-labelled segments."""

    def __init__(self, recipe: dict, transport=None):
        self.recipe = recipe
        self.transport = transport
        effective = recipe['effective']
        self.model, self.location = effective['model'], effective['device'].split(':', 1)[1]
        self.limits = effective['limits']
        self.reference = effective['model_options'].get('timing_reference') if effective['word_timestamps'] else None
        self.artifact_id = None
        self.prepared = {}

    def bind(self, job: dict) -> None:
        self.artifact_id = job['artifact_id']
        require_consent(self.artifact_id)

    def describe(self) -> dict:
        effective = self.recipe['effective']
        return {'tool': 'vertex-ai-generate-content', 'tool_version': 'v1', 'provider': 'google-vertex',
                'model': self.model, 'model_version': 'unknown',
                'parameters': {'strategy': 'vertex_gemini', 'location': self.location, 'prompt': ASR_PROMPT,
                               'response_schema': ASR_SCHEMA, 'temperature': 0.0, 'recipe_id': self.recipe['id'],
                               'recipe_sha256': self.recipe['sha256'], 'recipe': effective},
                'metadata': {'consent': 'R100 recorded as provider consent; checked before the call',
                             'timestamps': ('word times from the timing reference, aligned to the model text'
                                            if self.reference else 'model estimates; no word timing'),
                             'timing_reference': self.reference, 'speakers': 'labels are hypotheses'}}

    def transcribe(self, audio):
        from ..worker import TranscriptionResult  # same result contract as faster-whisper
        # The verified reader bounds each read; collect chunks up to the inline limit.
        chunks, total = [], 0
        while True:
            chunk = audio.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > self.limits['max_inline_bytes']:
                break
        data = b''.join(chunks)
        if len(data) > self.limits['max_inline_bytes']:
            raise VertexUnavailable(f'recording exceeds the inline limit {self.limits["max_inline_bytes"]} B '
                                    '(Cloud Storage upload is not implemented)')
        mime = _sniff_audio(data)
        sent, transcoding = data, None
        if mime not in GEMINI_AUDIO:
            sent, transcoding = _transcode_flac(data)
        duration = _duration_seconds(data)
        tokens = (duration or len(sent) / 4000) * AUDIO_TOKENS_PER_SECOND + len(ASR_PROMPT) / CHARS_PER_TOKEN
        config = _config(self.model, self.location, self.limits['max_output_tokens'])
        parts = [{'inlineData': {'mimeType': 'audio/flac' if transcoding else mime,
                                 'data': base64.b64encode(sent).decode()}}, {'text': ASR_PROMPT}]
        decoded, usage = call(config, parts, ASR_SCHEMA, purpose='asr', input_tokens_estimate=tokens,
                              refs=[('artifact', self.artifact_id)] if self.artifact_id else [], transport=self.transport)
        segments, texts, languages = [], [], {}
        for item in decoded.get('segments', []) if isinstance(decoded, dict) else []:
            if not isinstance(item, dict) or not isinstance(item.get('text'), str):
                continue
            start, end = item.get('start'), item.get('end')
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end < start or start < 0:
                start = end = None
            language = item.get('language') if isinstance(item.get('language'), str) else None
            languages[language] = languages.get(language, 0) + 1
            segments.append({'start': start, 'end': end, 'text': item['text'], 'words': [],
                             'speaker_hypothesis': item.get('speaker'), 'language': language,
                             'timing_source': 'model_estimate'})
            texts.append(item['text'].strip())
        if duration is not None:
            for segment in segments:
                if segment['end'] is not None and segment['end'] > duration + 1.0:
                    segment['timing_warning'] = 'end beyond recording duration'
        timed = [seg['end'] for seg in segments if seg['end'] is not None]
        timing_quality = 'unknown'
        if duration and timed:
            covered = max(timed) / duration
            timing_quality = ('suspect: segment times cover only {:.0%} of the recording'.format(covered) if covered < 0.8
                              else 'suspect: segment times run past the recording end' if covered > 1.02 else 'plausible')
        if any(seg['start'] is None for seg in segments):
            timing_quality += '; some segments have no usable time'
        dominant = max(languages, key=languages.get) if languages else None
        metadata = {'language_meaning': 'most frequent segment language reported by the model',
                    'segment_languages': {str(k): v for k, v in languages.items()},
                    'transcript_confidence': 'unknown', 'speaker_labels': 'model hypotheses, not identities',
                    'timing': 'model estimates in seconds; word timing unavailable', 'timing_quality': timing_quality,
                    'input_mime_detected': mime,
                    'transcoding': transcoding, 'duration_seconds_ffprobe': duration,
                    'usage': {k: usage.get(k) for k in ('promptTokenCount', 'candidatesTokenCount', 'actual_eur')}}
        if self.reference is not None:
            segments = self._align(segments, sent, duration, metadata)
        work = {'unit': 'audio_second', 'amount': duration if duration is not None else 'unknown',
                'basis': 'ffprobe duration of the original bytes'}
        return TranscriptionResult(' '.join(texts), dominant, segments, metadata, work)


    def _align(self, segments: list, sent: bytes, duration, metadata: dict) -> list:
        """Word times from the reference engine; on failure the model estimates stay and the reason is stored."""
        from . import alignment
        ref = self.reference
        try:
            if ref['engine'] == 'faster-whisper':
                words, info = alignment.faster_whisper_words(sent, ref)
            else:
                config = _config(self.model, self.location, 256)
                words, info = alignment.google_speech_words(
                    sent, {**ref, 'project': ref.get('project') or config.get('project')}, duration=duration,
                    artifact_id=self.artifact_id)
        except Exception as exc:  # the paid model text is kept; only the time source is missing
            metadata['timing_alignment'] = {'status': 'unavailable', 'engine': ref['engine'],
                                            'reason': f'{type(exc).__name__}: {str(exc)[:300]}'}
            return segments
        aligned, report = alignment.align(segments, words, min_matched_ratio=ref.get('min_matched_ratio', 0.5))
        metadata['timing_alignment'] = {'status': 'aligned', 'reference': info, **report}
        metadata['timing'] = ('word times from {} aligned to the model text in {} of {} segments; the rest are model '
                              'estimates'.format(ref['engine'], report['segments_aligned'], len(aligned)))
        return aligned


class VertexGeminiOCR:
    """OCR engine for images and PDFs; lines per page without pixel boxes, optional chat messages."""

    def __init__(self, engine: str, transport=None):
        self.model, self.location = parse_engine(engine)
        self.transport = transport

    def describe(self) -> dict:
        return {'tool': 'vertex-ai-generate-content', 'tool_version': 'v1', 'provider': 'google-vertex',
                'model': self.model, 'location': self.location}

    def availability(self, languages) -> dict:
        return {'status': 'available', 'engine': ENGINE, 'model': self.model,
                'note': 'remote model; language list is a hint in the prompt'}

    def recognize_document(self, data: bytes, mime: str, *, layout: str, languages, max_output_tokens: int,
                           artifact_id: int | None) -> dict:
        if mime not in GEMINI_DOCUMENT:
            raise VertexUnavailable(f'unsupported document type for Vertex OCR: {mime}')
        prompt = OCR_PROMPT.format(layout=OCR_CHAT if layout == 'chat_screenshot' else '') + \
            f' Spodziewane języki: {", ".join(languages)}.'
        config = _config(self.model, self.location, max_output_tokens)
        parts = [{'inlineData': {'mimeType': mime, 'data': base64.b64encode(data).decode()}}, {'text': prompt}]
        estimate = 1300 * max(1, data.count(b'/Type /Page') if mime == 'application/pdf' else 1) + len(prompt) / CHARS_PER_TOKEN
        decoded, usage = call(config, parts, OCR_SCHEMA, purpose='ocr', input_tokens_estimate=estimate,
                              refs=[('artifact', artifact_id)] if artifact_id else [], transport=self.transport)
        return {'decoded': decoded if isinstance(decoded, dict) else {}, 'prompt': prompt, 'usage': usage}

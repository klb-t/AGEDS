"""Per-segment language strategy for code-switched recordings (separate adapter/run).

Pipeline, written against the faster-whisper 1.2.0 API:

1. ``faster_whisper.decode_audio(stream, sampling_rate=16000)`` decodes the
   verified stream (the same file-like object the legacy path passes).
2. ``faster_whisper.vad.get_speech_timestamps(audio, VadOptions(**params))``
   returns speech regions in samples (Silero VAD).
3. Each region is split into equal windows no longer than ``max_window_s``.
4. ``WhisperModel.detect_language(audio=window)`` reports language
   probabilities; they are restricted to ``allowed_languages`` and renormalized.
   Both the raw top-k as reported and the renormalized distribution are kept;
   they identify language only and say nothing about transcript correctness.
5. ``WhisperModel.transcribe(window, language=chosen, word_timestamps=...,
   vad_filter=False)`` transcribes each window with the forced language.
6. Segment and word times are offset by the window start back to file time.
   Raw model times are otherwise unchanged; nothing is repaired.

Known limits: a switch inside a window is not resolved (the whole window gets
one language); VAD can drop quiet or overlapped speech; windows shorter than a
few seconds give unstable identification; probabilities are not quality.
"""
from __future__ import annotations

import importlib
import math
import platform
from dataclasses import dataclass

from ..worker import TranscriptionResult, _json_number, _options_dict, _version
from .recipe import LANGUAGE_PROBABILITY_MEANING

SAMPLE_RATE = 16000
STRATEGY = 'per_segment_language'
LIMITATIONS = (
    'language switches shorter than one window are not resolved; a window gets one forced language',
    'VAD can drop quiet, distant or overlapped speech; dropped audio is not transcribed',
    'language identification probability is not transcript correctness or acoustic alignment',
    'windows shorter than a few seconds give unstable language identification',
    'window boundaries can split words; Whisper context is not carried across windows by default',
)


@dataclass(frozen=True)
class Window:
    index: int
    region_index: int
    start_sample: int
    end_sample: int
    sample_rate: int = SAMPLE_RATE

    @property
    def start(self) -> float:
        return self.start_sample / self.sample_rate

    @property
    def end(self) -> float:
        return self.end_sample / self.sample_rate


def normalize_regions(raw_regions, total_samples: int) -> list[tuple[int, int]]:
    """Clip to the audio, sort and merge overlaps; never invent speech."""
    regions = []
    for item in raw_regions:
        start, end = int(item['start']), int(item['end'])
        start, end = max(0, start), min(total_samples, end)
        if end > start:
            regions.append((start, end))
    regions.sort()
    merged: list[tuple[int, int]] = []
    for start, end in regions:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def plan_windows(regions: list[tuple[int, int]], *, max_window_s: float, sample_rate: int = SAMPLE_RATE) -> list[Window]:
    """Split every region into the fewest equal windows not longer than max_window_s."""
    max_samples = max(1, int(max_window_s * sample_rate))
    windows: list[Window] = []
    for region_index, (start, end) in enumerate(regions):
        length = end - start
        count = max(1, math.ceil(length / max_samples))
        bounds = [start + (length * k) // count for k in range(count + 1)]
        for left, right in zip(bounds, bounds[1:]):
            if right > left:
                windows.append(Window(len(windows), region_index, left, right, sample_rate))
    return windows


def restrict_language_probabilities(reported, allowed: list[str], top_k: int) -> dict:
    """Keep the raw report and renormalize over the allowed languages.

    Ties are broken by the order of ``allowed`` so the result is deterministic.
    """
    raw = []
    for item in reported or []:
        language, probability = item[0], _json_number(item[1])
        if not isinstance(language, str) or type(probability) not in (int, float) or not math.isfinite(probability):
            continue
        raw.append((language, float(probability)))
    allowed_raw = {code: None for code in allowed}
    for language, probability in raw:
        if language in allowed_raw and allowed_raw[language] is None:
            allowed_raw[language] = probability
    total = sum(p for p in allowed_raw.values() if p is not None and p > 0)
    distribution = {code: (p / total if total > 0 and p is not None and p > 0 else (0.0 if total > 0 else None))
                    for code, p in allowed_raw.items()}
    order = {code: index for index, code in enumerate(allowed)}
    ranked = sorted(allowed, key=lambda code: (-(distribution[code] or 0.0), order[code]))
    chosen = ranked[0] if total > 0 else None
    return {
        'raw_top_k': [{'language': language, 'probability': probability} for language, probability in raw[:top_k]],
        'raw_reported_count': len(raw),
        'allowed_reported': allowed_raw,
        'allowed_distribution': distribution,
        'chosen': chosen,
        'alternatives': [{'language': code, 'probability_reported': allowed_raw[code],
                          'probability_allowed': distribution[code]} for code in ranked],
        'status': 'restricted_to_allowed' if chosen else 'no_allowed_language_reported',
        'meaning': LANGUAGE_PROBABILITY_MEANING,
    }


def _runtime():
    try:
        whisper = importlib.import_module('faster_whisper')
        vad = importlib.import_module('faster_whisper.vad')
    except ImportError as error:
        raise RuntimeError('Install requirements-whisper.txt to enable transcription') from error
    return whisper, vad


def _word(word, offset: float) -> dict:
    start, end = _json_number(word.start), _json_number(word.end)
    return {'start': offset + start if type(start) in (int, float) else start,
            'end': offset + end if type(end) in (int, float) else end,
            'word': word.word, 'probability': _json_number(word.probability)}


class CodeSwitchingAdapter:
    """ASR adapter for strategy ``per_segment_language``; one run per attempt."""

    def __init__(self, recipe: dict):
        effective = recipe['effective']
        if effective.get('strategy') != STRATEGY:
            raise ValueError('CodeSwitchingAdapter runs only the per_segment_language strategy')
        self.recipe = recipe
        self.effective = effective

    def describe(self) -> dict:
        e = self.effective
        return {
            'tool': 'faster-whisper', 'tool_version': _version('faster-whisper'), 'provider': 'local',
            'model': e['model'], 'model_version': 'unknown',
            'parameters': {'strategy': STRATEGY, 'device': e['device'], 'compute_type': e['compute_type'],
                           'model_options': e['model_options'], 'word_timestamps': e['word_timestamps'],
                           'vad': e['vad'], 'allowed_languages': e['allowed_languages'], 'windows': e['windows'],
                           'detection': e['detection'], 'transcribe_options': e['transcribe_options'],
                           'window_vad_filter': False, 'limits': e['limits'],
                           'recipe_id': self.recipe['id'], 'recipe_sha256': self.recipe['sha256'], 'recipe': e},
            'metadata': {'python_version': platform.python_version(), 'ctranslate2_version': _version('ctranslate2'),
                         'model_revision': e['model_options'].get('revision', 'unknown'), 'seed': 'unknown',
                         'vad_engine': 'faster_whisper.vad.get_speech_timestamps (Silero)'},
        }

    def transcribe(self, audio) -> TranscriptionResult:
        whisper, vad = _runtime()
        e = self.effective
        model = whisper.WhisperModel(e['model'], device=e['device'], compute_type=e['compute_type'], **e['model_options'])
        samples = whisper.decode_audio(audio, sampling_rate=SAMPLE_RATE)
        total = len(samples)
        vad_options = vad.VadOptions(**(e['vad']['parameters'] or {}))
        regions = normalize_regions(vad.get_speech_timestamps(samples, vad_options, sampling_rate=SAMPLE_RATE), total)
        windows = plan_windows(regions, max_window_s=e['windows']['max_window_s'])
        limit = e['limits']['max_windows']
        processed = windows[:limit]
        detection = e['detection']
        segments, texts, window_records, languages, options_seen = [], [], [], [], None
        for window in processed:
            clip = samples[window.start_sample:window.end_sample]
            reported_language, reported_probability, reported = model.detect_language(
                audio=clip, language_detection_segments=detection['language_detection_segments'],
                language_detection_threshold=detection['language_detection_threshold'])
            lid = restrict_language_probabilities(reported, e['allowed_languages'], detection['top_k_reported'])
            forced = lid['chosen']
            call = {'language': forced, 'word_timestamps': e['word_timestamps'], 'vad_filter': False}
            call.update(e['transcribe_options'])
            raw_segments, info = model.transcribe(clip, **call)
            used = forced if forced is not None else getattr(info, 'language', None)
            if options_seen is None:
                options_seen = _options_dict(getattr(info, 'transcription_options', None))
            count = 0
            window_ref = {'index': window.index, 'start': window.start, 'end': window.end}
            for raw in raw_segments:
                start, end = _json_number(raw.start), _json_number(raw.end)
                segment = {
                    'start': window.start + start if type(start) in (int, float) else start,
                    'end': window.start + end if type(end) in (int, float) else end,
                    'text': raw.text,
                    'words': [_word(word, window.start) for word in (raw.words or [])],
                    'language': used,
                    'language_probability': lid['allowed_reported'].get(used) if forced else
                        _json_number(getattr(info, 'language_probability', None)),
                    'language_probability_allowed': lid['allowed_distribution'].get(used) if forced else None,
                    'language_probability_meaning': LANGUAGE_PROBABILITY_MEANING,
                    'language_alternatives': lid['alternatives'],
                    'language_selection': 'forced_from_restricted_identification' if forced else 'model_auto_fallback',
                    'window': window_ref,
                    'strategy': STRATEGY,
                }
                segments.append(segment)
                texts.append(raw.text.strip())
                count += 1
            if used and used not in languages:
                languages.append(used)
            window_records.append({
                'index': window.index, 'region_index': window.region_index, 'start': window.start, 'end': window.end,
                'duration': window.end - window.start,
                'short_window': (window.end - window.start) < e['windows']['min_window_s'],
                'reported_language': reported_language, 'reported_probability': _json_number(reported_probability),
                'raw_top_k': lid['raw_top_k'], 'raw_reported_count': lid['raw_reported_count'],
                'allowed_distribution': lid['allowed_distribution'], 'chosen_language': forced,
                'language_used': used, 'language_selection_status': lid['status'],
                'outside_allowed': used is not None and used not in e['allowed_languages'],
                'segments': count,
            })
        duration = total / SAMPLE_RATE
        record_limit = e['limits']['max_regions_recorded']
        metadata = {
            'strategy': STRATEGY, 'recipe_id': self.recipe['id'], 'recipe_sha256': self.recipe['sha256'],
            'allowed_languages': e['allowed_languages'], 'languages': languages,
            'language_probability_meaning': LANGUAGE_PROBABILITY_MEANING,
            'transcript_confidence': 'unknown',
            'media_duration_seconds': duration,
            'vad': {'engine': 'faster_whisper.vad (Silero)', 'parameters': e['vad']['parameters'],
                    'regions_total': len(regions),
                    'regions': [[s / SAMPLE_RATE, t / SAMPLE_RATE] for s, t in regions[:record_limit]],
                    'regions_recorded_complete': len(regions) <= record_limit,
                    'speech_seconds': sum(t - s for s, t in regions) / SAMPLE_RATE},
            'windows': window_records,
            'coverage': {'status': 'complete' if len(processed) == len(windows) else 'partial',
                         'windows_planned': len(windows), 'windows_processed': len(processed),
                         **({} if len(processed) == len(windows) else {'reason': 'limits.max_windows reached'})},
            'time_base': 'file seconds = window start (VAD sample index / 16000) + model time within window',
            'effective_transcription_options': options_seen if options_seen is not None else 'unknown',
            'limitations': list(LIMITATIONS),
        }
        language = languages[0] if len(languages) == 1 else ('mul' if languages else None)
        return TranscriptionResult(' '.join(texts), language, segments, metadata,
                                   {'unit': 'audio_second', 'amount': duration,
                                    'basis': 'decoded_samples_at_16000_hz'})

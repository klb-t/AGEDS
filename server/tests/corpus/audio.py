"""Synthetic audio for ASR strategy tests: TONES, NOT SPEECH.

``build(root)`` writes WAV files made of sine tones and silence with the
stdlib ``wave`` module plus a labelled manifest. Each tone frequency stands in
for one "language" so that a fake ASR model can derive deterministic output
from the actual samples. Nothing here is a recording, a voice or real speech;
results on these files say nothing about recognition quality.

``FakeFasterWhisper`` mimics the parts of the faster-whisper 1.2.0 module API
used by AGEDS (``WhisperModel``, ``decode_audio``, ``vad.VadOptions``,
``vad.get_speech_timestamps``) and records every call's parameters.
"""
from __future__ import annotations

import io
import json
import math
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

SAMPLE_RATE = 16000
LABEL = 'synthetic sine tones and silence; NOT speech, not a recording'
# Tone frequency (Hz) standing in for each declared "language".
TONE_LANGUAGE = {400.0: 'pl', 800.0: 'nl', 1600.0: 'en'}
LANGUAGE_TONE = {language: frequency for frequency, language in TONE_LANGUAGE.items()}
LOUD, QUIET = 0.5, 0.01
# Planted layout of codeswitch.wav, seconds. pl and nl are contiguous, so the
# VAD region 1.0-9.0 s is split into two 4 s windows (max_window_s 6) and the
# second window straddles the pl->nl switch at 6.0 s (1 s pl + 3 s nl).
CODESWITCH_LAYOUT = (
    (0.0, 1.0, None, 0.0),
    (1.0, 6.0, 'pl', LOUD),
    (6.0, 9.0, 'nl', LOUD),
    (9.0, 10.5, None, 0.0),
    (10.5, 14.5, 'en', LOUD),
    (14.5, 15.5, None, 0.0),
    (15.5, 17.0, 'pl', QUIET),   # below the fake VAD threshold: dropped like quiet speech
    (17.0, 18.0, None, 0.0),
)


def tone_samples(layout, sample_rate: int = SAMPLE_RATE) -> list[int]:
    samples: list[int] = []
    for start, end, language, amplitude in layout:
        count = round((end - start) * sample_rate)
        frequency = LANGUAGE_TONE.get(language, 0.0)
        offset = len(samples)
        for n in range(count):
            value = amplitude * math.sin(2 * math.pi * frequency * (offset + n) / sample_rate) if frequency else 0.0
            samples.append(int(round(value * 32767)))
    return samples


def wav_bytes(samples: list[int], sample_rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(struct.pack('<%dh' % len(samples), *samples))
    return buffer.getvalue()


def build(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'codeswitch.wav'
    path.write_bytes(wav_bytes(tone_samples(CODESWITCH_LAYOUT)))
    single = root / 'single_en.wav'
    single.write_bytes(wav_bytes(tone_samples(((0.0, 0.5, None, 0.0), (0.5, 3.5, 'en', LOUD), (3.5, 4.0, None, 0.0)))))
    manifest = {
        'label': LABEL,
        'sample_rate': SAMPLE_RATE,
        'tone_language': {str(int(k)): v for k, v in TONE_LANGUAGE.items()},
        'files': {
            'codeswitch.wav': {
                'segments': [{'start': s, 'end': e, 'language': lang, 'amplitude': a}
                             for s, e, lang, a in CODESWITCH_LAYOUT],
                'planted_cases': {
                    'three_languages': ['pl', 'nl', 'en'],
                    'window_straddles_switch': {'window_seconds': [5.0, 9.0], 'switch_at': 6.0},
                    'quiet_segment_dropped_by_vad': [15.5, 17.0],
                    'silence_between_languages': [9.0, 10.5],
                },
            },
            'single_en.wav': {'segments': [{'start': 0.5, 'end': 3.5, 'language': 'en', 'amplitude': LOUD}]},
        },
    }
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return {'audio_codeswitch': str(path), 'audio_single_en': str(single), 'manifest': manifest}


# --------------------------------------------------------------------------- fake runtime

BLOCK_SECONDS = 0.25          # language identification block
WORD_SECONDS = 0.5            # one fake "word" per loud half second
ENERGY_THRESHOLD = 0.05       # fake VAD/word energy threshold (RMS, full scale 1.0)
DECOY = ('de', 0.1)           # a language outside every allowed list, always reported


def _read_wav(source) -> tuple[list[float], int]:
    if isinstance(source, (str, Path)):
        handle = open(source, 'rb')
        close = True
    else:
        handle, close = source, False
    try:
        with wave.open(handle, 'rb') as reader:
            if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
                raise ValueError('fake decoder supports mono 16-bit WAV only')
            rate = reader.getframerate()
            frames = bytearray()
            while True:
                chunk = reader.readframes(65536)   # 128 KiB, below VerifiedReader's read cap
                if not chunk:
                    break
                frames.extend(chunk)
    finally:
        if close:
            handle.close()
    values = struct.unpack('<%dh' % (len(frames) // 2), bytes(frames))
    return [v / 32768.0 for v in values], rate


def _rms(block) -> float:
    return math.sqrt(sum(v * v for v in block) / len(block)) if len(block) else 0.0


def _frequency(block, sample_rate: int) -> float:
    """Zero-crossing frequency over the block's non-silent span (silence edges trimmed)."""
    loud = [i for i, v in enumerate(block) if abs(v) > 0.02]
    if len(loud) < 2:
        return 0.0
    span = block[loud[0]:loud[-1] + 1]
    crossings = sum(1 for a, b in zip(span, span[1:]) if (a < 0 <= b) or (a >= 0 > b))
    return crossings / (2 * len(span) / sample_rate)


def _tone_language(block, sample_rate: int) -> str | None:
    if _rms(block) < ENERGY_THRESHOLD:
        return None
    frequency = _frequency(block, sample_rate)
    nearest = min(TONE_LANGUAGE, key=lambda f: abs(f - frequency))
    return TONE_LANGUAGE[nearest]


def fake_speech_regions(samples, *, sample_rate=SAMPLE_RATE, min_silence_duration_ms=2000, speech_pad_ms=400,
                        min_speech_duration_ms=0, **_ignored) -> list[dict]:
    frame = int(0.02 * sample_rate)
    speech = [_rms(samples[i:i + frame]) >= ENERGY_THRESHOLD for i in range(0, len(samples), frame)]
    regions, start = [], None
    for index, active in enumerate(speech + [False]):
        if active and start is None:
            start = index
        elif not active and start is not None:
            regions.append([start * frame, min(len(samples), index * frame)])
            start = None
    merged = []
    gap = sample_rate * min_silence_duration_ms / 1000
    for region in regions:
        if merged and region[0] - merged[-1][1] < gap:
            merged[-1][1] = region[1]
        else:
            merged.append(region)
    pad = int(sample_rate * speech_pad_ms / 1000)
    minimum = sample_rate * min_speech_duration_ms / 1000
    return [{'start': max(0, s - pad), 'end': min(len(samples), e + pad)} for s, e in merged if e - s >= minimum]


@dataclass
class FakeFasterWhisper:
    """A stand-in module object; install with ``patch.dict(sys.modules, fake.modules())``."""
    calls: list = field(default_factory=list)

    def modules(self) -> dict:
        fake = self

        class VadOptions:
            def __init__(self, threshold=0.5, neg_threshold=None, min_speech_duration_ms=0,
                         max_speech_duration_s=float('inf'), min_silence_duration_ms=2000, speech_pad_ms=400):
                self.threshold, self.neg_threshold = threshold, neg_threshold
                self.min_speech_duration_ms, self.max_speech_duration_s = min_speech_duration_ms, max_speech_duration_s
                self.min_silence_duration_ms, self.speech_pad_ms = min_silence_duration_ms, speech_pad_ms

        def get_speech_timestamps(audio, vad_options=None, sampling_rate=SAMPLE_RATE, **kwargs):
            options = vad_options or VadOptions(**kwargs)
            fake.calls.append(('get_speech_timestamps', {'samples': len(audio), 'sampling_rate': sampling_rate,
                                                         'options': dict(vars(options))}))
            return fake_speech_regions(audio, sample_rate=sampling_rate,
                                       min_silence_duration_ms=options.min_silence_duration_ms,
                                       speech_pad_ms=options.speech_pad_ms,
                                       min_speech_duration_ms=options.min_speech_duration_ms)

        def decode_audio(input_file, sampling_rate=SAMPLE_RATE, split_stereo=False):
            samples, rate = _read_wav(input_file)
            if rate != sampling_rate:
                raise ValueError('fake decoder does not resample')
            fake.calls.append(('decode_audio', {'sampling_rate': sampling_rate, 'samples': len(samples),
                                                'input': 'path' if isinstance(input_file, (str, Path)) else 'stream'}))
            return samples

        class WhisperModel:
            def __init__(self, model_size_or_path, device='auto', compute_type='default', **kwargs):
                fake.calls.append(('WhisperModel', {'model': model_size_or_path, 'device': device,
                                                    'compute_type': compute_type, **kwargs}))

            def detect_language(self, audio=None, features=None, vad_filter=False, vad_parameters=None,
                                language_detection_segments=1, language_detection_threshold=0.5):
                fake.calls.append(('detect_language', {'samples': len(audio), 'vad_filter': vad_filter,
                                                       'language_detection_segments': language_detection_segments,
                                                       'language_detection_threshold': language_detection_threshold}))
                return fake_language_report(audio)

            def transcribe(self, audio, **kwargs):
                is_stream = hasattr(audio, 'read') or isinstance(audio, (str, Path))
                fake.calls.append(('transcribe', {'input': 'stream' if hasattr(audio, 'read') else
                                                  ('path' if is_stream else 'array'),
                                                  'samples': None if is_stream else len(audio), **kwargs}))
                samples = _read_wav(audio)[0] if is_stream else audio
                regions = [(0, len(samples))]
                if kwargs.get('vad_filter'):
                    params = kwargs.get('vad_parameters') or {}
                    regions = [(r['start'], r['end']) for r in fake_speech_regions(samples, **params)]
                language = kwargs.get('language')
                probability = 1
                if language is None:
                    speech = [v for s, e in regions for v in samples[s:e]][:30 * SAMPLE_RATE]
                    language, probability, _ = fake_language_report(speech)
                segments = fake_segments(samples, regions, language)
                info = SimpleNamespace(language=language, language_probability=probability,
                                       duration=len(samples) / SAMPLE_RATE, all_language_probs=None,
                                       transcription_options={'synthetic_fake': True, 'word_timestamps':
                                                              kwargs.get('word_timestamps', False)})
                return iter(segments), info

        whisper = SimpleNamespace(WhisperModel=WhisperModel, decode_audio=decode_audio,
                                  __version__='fake-for-tests')
        vad = SimpleNamespace(VadOptions=VadOptions, get_speech_timestamps=get_speech_timestamps)
        whisper.vad = vad
        return {'faster_whisper': whisper, 'faster_whisper.vad': vad}

    def named(self, name: str) -> list[dict]:
        return [params for call, params in self.calls if call == name]


def fake_language_report(samples):
    """Language shares from tone blocks, scaled to 0.9, plus a disallowed decoy at 0.1."""
    block = int(BLOCK_SECONDS * SAMPLE_RATE)
    counts: dict[str, int] = {}
    for i in range(0, len(samples) - block + 1, block):
        language = _tone_language(samples[i:i + block], SAMPLE_RATE)
        if language:
            counts[language] = counts.get(language, 0) + 1
    total = sum(counts.values())
    report = [(language, 0.9 * count / total) for language, count in counts.items()] if total else []
    report.append(DECOY)
    report.sort(key=lambda item: (-item[1], item[0]))
    return report[0][0], report[0][1], report


def fake_segments(samples, regions, used_language):
    """One fake word per loud half second; word text names the forced language and the tone's language."""
    word = int(WORD_SECONDS * SAMPLE_RATE)
    segments = []
    for start, end in regions:
        words = []
        for i in range(start, end - word + 1, word):
            tone = _tone_language(samples[i:i + word], SAMPLE_RATE)
            if tone is None:
                if words:
                    segments.append(words)
                    words = []
                continue
            token = f' {used_language}' if tone == used_language else f' {used_language}~{tone}'
            # Times are relative to the supplied audio (a window clip or the whole file).
            words.append(SimpleNamespace(start=i / SAMPLE_RATE, end=None, word=token, probability=0.5))
            if len(words) == 6:
                segments.append(words)
                words = []
        if words:
            segments.append(words)
    result = []
    for words in segments:
        for w in words:
            w.end = w.start + 0.45
        result.append(SimpleNamespace(start=words[0].start, end=words[-1].end,
                                      text=''.join(w.word for w in words), words=words))
    return result

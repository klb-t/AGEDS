"""Forced alignment of the model text to the audio (MMS_FA, CTC), local and without network once cached.

Unlike matching against a second recogniser, every Gemini word is placed on the audio by the
acoustic model, with a score (mean probability of its characters). The text is not changed:
characters outside the aligner's alphabet are folded (diacritics) or skipped, and a word with no
alignable letters (``[niezrozumiałe]``, digits) is aligned as a wildcard (``*``) that absorbs
the audio it covers. A wildcard also sits before, between and after segments, so speech the
model did not transcribe does not drag the neighbouring words. A segment becomes word-citable
only when its mean word score reaches ``min_score``; the rest keep the model estimate.

A forced alignment always returns times, also for text that was never said: the score, not the
existence of a time, is the signal. It is not verification of what was said.
"""
from __future__ import annotations

import hashlib
import subprocess
import unicodedata
from pathlib import Path

from .alignment import split_words

SAMPLE_RATE = 16000


def _letters(raw: str, alphabet: set[str]) -> str:
    folded = unicodedata.normalize('NFKD', raw.casefold())
    folded = folded.replace('ł', 'l').replace('ø', 'o').replace('æ', 'ae').replace('ß', 'ss')
    return ''.join(ch for ch in folded if ch in alphabet and ch != '*')


def _decode(data: bytes):
    import torch
    from .vertex import _private_copy
    with _private_copy(data) as path:
        result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(path), '-ac', '1', '-ar',
                                 str(SAMPLE_RATE), '-f', 'f32le', '-'], capture_output=True, check=True, timeout=600)
    return torch.frombuffer(bytearray(result.stdout), dtype=torch.float32)


def _model_file_sha256() -> str | None:
    import torch
    path = Path(torch.hub.get_dir()) / 'checkpoints' / 'model.pt'
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _emission(model, waveform, window: float, context: float):
    """Emissions over overlapping windows; each window keeps only its central frames."""
    import torch
    step, pad = int(window * SAMPLE_RATE), int(context * SAMPLE_RATE)
    parts = []
    with torch.inference_mode():
        for start in range(0, waveform.numel(), step):
            lo, hi = max(0, start - pad), min(waveform.numel(), start + step + pad)
            emission, _ = model(waveform[lo:hi].unsqueeze(0))
            frames = emission.shape[1]
            per_sample = frames / (hi - lo)
            keep_lo = round((start - lo) * per_sample)
            keep_hi = round((min(start + step, waveform.numel()) - lo) * per_sample)
            parts.append(emission[0, keep_lo:keep_hi])
    return torch.cat(parts)


def align(data: bytes, segments: list[dict], ref: dict) -> tuple[list[dict], dict]:
    import torch
    import torchaudio
    from torchaudio.pipelines import MMS_FA
    model = MMS_FA.get_model(with_star=True)  # downloads once into the torch hub cache
    model_sha = _model_file_sha256() if ref.get('model_sha256') else None
    if model_sha is not None and model_sha != ref['model_sha256']:
        raise RuntimeError('MMS_FA weights differ from the pinned SHA-256')
    model.eval()
    labels = MMS_FA.get_labels(star='*')
    index = {label: i for i, label in enumerate(labels)}
    alphabet = set(labels) - {'-'}
    waveform = _decode(data)
    if waveform.numel() == 0:
        raise RuntimeError('no decodable audio')
    emission = _emission(model, waveform, ref.get('window_seconds', 20.0), ref.get('context_seconds', 2.0))
    frame_seconds = waveform.numel() / SAMPLE_RATE / emission.shape[0]
    # Token plan: (segment, word index or None for a separator star, letters or '*').
    plan, targets = [], []
    for si, seg in enumerate(segments):
        plan.append((si, None, '*'))
        for wi, raw in enumerate(split_words(seg['text'])):
            letters = _letters(raw, alphabet)
            plan.append((si, wi, letters or '*'))
    plan.append((len(segments), None, '*'))
    for _, _, letters in plan:
        targets += [index[ch] for ch in letters]
    if len(targets) > emission.shape[0]:
        raise RuntimeError('more characters than audio frames: the text cannot have been spoken in this recording')
    path, scores = torchaudio.functional.forced_align(emission.unsqueeze(0), torch.tensor([targets], dtype=torch.int32),
                                                      blank=0)
    spans = torchaudio.functional.merge_tokens(path[0], scores[0].exp())
    if len(spans) != len(targets):
        raise RuntimeError('alignment returned an unexpected number of token spans')
    words_by_segment: dict[int, list] = {}
    cursor = 0
    for si, wi, letters in plan:
        mine = spans[cursor:cursor + len(letters)]
        cursor += len(letters)
        if wi is None:
            continue
        start, end = round(mine[0].start * frame_seconds, 3), round(mine[-1].end * frame_seconds, 3)
        score = sum(s.score * (s.end - s.start) for s in mine) / max(1, sum(s.end - s.start for s in mine))
        words_by_segment.setdefault(si, []).append({'start': start, 'end': end, 'score': round(float(score), 4),
                                                    'wildcard': letters == '*'})
    min_score = ref.get('min_score', 0.5)
    out, per_segment, citable = [], [], 0
    for si, seg in enumerate(segments):
        raws = split_words(seg['text'])
        placed = words_by_segment.get(si, [])
        scored = [w['score'] for w in placed if not w['wildcard']]
        mean = round(sum(scored) / len(scored), 4) if scored else None
        estimate = {'start': seg.get('start'), 'end': seg.get('end')}
        new = {**seg, 'model_estimate': estimate}
        words = [{'word': raw, 'start': w['start'], 'end': w['end'], 'probability': None, 'score': w['score'],
                  'match': 'forced_wildcard' if w['wildcard'] else 'forced'} for raw, w in zip(raws, placed)]
        if words and mean is not None and mean >= min_score:
            citable += 1
            new.update({'start': words[0]['start'], 'end': words[-1]['end'], 'words': words,
                        'timing_source': 'forced_alignment'})
        else:
            new.update({'words': [], 'timing_source': seg.get('timing_source', 'model_estimate'),
                        'alignment_note': 'forced-alignment score below the threshold; times are model estimates',
                        'forced_alignment': {'start': words[0]['start'], 'end': words[-1]['end']} if words else None})
        per_segment.append({'index': si, 'words': len(words), 'mean_score': mean, 'model_estimate': estimate,
                            'aligned': {'start': words[0]['start'], 'end': words[-1]['end']} if words else None,
                            'citable': bool(new['words'])})
        out.append(new)
    report = {'model_words': sum(len(split_words(s['text'])) for s in segments), 'segments_aligned': citable,
              'segments_model_estimate': len(segments) - citable, 'min_score': min_score,
              'wildcard_words': sum(1 for w in sum(words_by_segment.values(), []) if w['wildcard']),
              'frame_seconds': round(frame_seconds, 5), 'segments': per_segment,
              'meaning': 'every word is placed by the acoustic model; the score (mean character probability) is the '
                         'signal, a time alone is not; not verification of what was said'}
    info = {'engine': 'mms-forced-align', 'model': 'torchaudio.pipelines.MMS_FA', 'torch_version': torch.__version__,
            'torchaudio_version': torchaudio.__version__, 'model_sha256': model_sha or 'not checked'}
    return out, {**report, 'reference': info}

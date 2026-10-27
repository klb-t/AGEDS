"""Word times for a model transcript from a separate timing reference (faster-whisper or Speech-to-Text).

Gemini returns the text and speaker/language hypotheses but only estimated segment times.
A reference engine that reports word offsets supplies the time axis: Gemini's words are
matched to the reference words in order (difflib on normalized words). A matched word
takes the reference word's interval; a word between two matched words gets the interval
between them (``interpolated``, a bound, not a measurement). A segment receives words
only when enough of its words matched; otherwise it keeps the model estimate and says so.
The text is never changed: the raw words join exactly to the stored segment text.

The comparison is stored with the transcript: the reference transcript itself, counts of
exact/fuzzy/interpolated words, text agreement and, per segment, the model estimate next
to the aligned interval. Alignment is not verification of what was said.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import re
import unicodedata

WORD = re.compile(r'\s*\S+')
TIMING_SOURCE = 'reference_alignment'


def split_words(text: str) -> list[str]:
    """Raw words whose concatenation is exactly ``text`` (leading whitespace kept, trailing on the last)."""
    words = WORD.findall(text)
    if words:
        words[-1] += text[len(''.join(words)):]
    return words


def normalize(word: str) -> str:
    folded = unicodedata.normalize('NFKD', word.casefold())
    return ''.join(ch for ch in folded if ch.isalnum())


def align(segments: list[dict], reference: list[dict], *, min_matched_ratio: float = 0.5,
          fuzzy_ratio: float = 0.8) -> tuple[list[dict], dict]:
    """Return new segments with aligned words and a comparison report; ``segments`` are not modified."""
    reference = [dict(w, start=float(w['start']), end=float(w['end'])) for w in reference
                 if normalize(w['word']) and w.get('start') is not None and w.get('end') is not None and w['end'] >= w['start']]
    pieces = [(si, raw) for si, seg in enumerate(segments) for raw in split_words(seg['text'])]
    model_norm = [normalize(raw) for _, raw in pieces]
    ref_norm = [normalize(w['word']) for w in reference]
    matched: dict[int, tuple[int, str]] = {}
    matcher = difflib.SequenceMatcher(None, model_norm, ref_norm, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            matched.update({i1 + k: (j1 + k, 'exact') for k in range(i2 - i1)})
        elif tag == 'replace':  # in order: each model word takes the next similar reference word of the block
            j = j1
            for i in range(i1, i2):
                for candidate in range(j, j2):
                    if model_norm[i] and difflib.SequenceMatcher(None, model_norm[i], ref_norm[candidate]).ratio() >= fuzzy_ratio:
                        matched[i], j = (candidate, 'fuzzy'), candidate + 1
                        break
    # A run of unmatched words between two matched ones shares that gap in equal parts, in order;
    # each word keeps the whole gap as its bound.
    interpolated, i = {}, 0
    while i < len(pieces):
        if i in matched:
            i += 1
            continue
        run_end = i
        while run_end < len(pieces) and run_end not in matched:
            run_end += 1
        if i > 0 and run_end < len(pieces):
            low, high = reference[matched[i - 1][0]]['end'], reference[matched[run_end][0]]['start']
            if high >= low:
                step = (high - low) / (run_end - i)
                for k in range(i, run_end):
                    interpolated[k] = (round(low + step * (k - i), 3), round(low + step * (k - i + 1), 3), low, high)
        i = run_end
    out, per_segment, position = [], [], 0
    counts = {'exact': 0, 'fuzzy': 0, 'interpolated': 0, 'untimed': 0}
    for si, seg in enumerate(segments):
        count = len(split_words(seg['text']))
        indices = range(position, position + count)
        position += count
        words = []
        for i in indices:
            raw = pieces[i][1]
            if i in matched:
                ref = reference[matched[i][0]]
                words.append({'word': raw, 'start': ref['start'], 'end': ref['end'], 'probability': ref.get('probability'),
                              'match': matched[i][1], 'reference_word': ref['word']})
            elif i in interpolated:
                start, end, low, high = interpolated[i]
                words.append({'word': raw, 'start': start, 'end': end, 'probability': None, 'match': 'interpolated',
                              'bound': {'start': low, 'end': high}})
            else:
                words.append({'word': raw, 'start': None, 'end': None, 'probability': None, 'match': None})
        hits = sum(1 for w in words if w['match'] in ('exact', 'fuzzy'))
        ratio = hits / len(words) if words else 0.0
        estimate = {'start': seg.get('start'), 'end': seg.get('end')}
        new = {**seg, 'model_estimate': estimate}
        if words and ratio >= min_matched_ratio and all(w['start'] is not None for w in words) and _ordered(words):
            for w in words:
                counts[w['match']] += 1
            new.update({'start': words[0]['start'], 'end': words[-1]['end'], 'words': words, 'timing_source': TIMING_SOURCE})
        else:
            counts['untimed'] += len(words)
            new.update({'words': [], 'timing_source': seg.get('timing_source', 'model_estimate'),
                        'alignment_note': 'too few words matched the timing reference; times are model estimates'})
        per_segment.append({'index': si, 'words': len(words), 'matched_ratio': round(ratio, 3), 'model_estimate': estimate,
                            'aligned': {'start': new['start'], 'end': new['end']} if new['words'] else None,
                            'delta_start': _delta(new['start'], estimate['start']) if new['words'] else None,
                            'delta_end': _delta(new['end'], estimate['end']) if new['words'] else None})
        out.append(new)
    _fence(out)
    report = {'model_words': len(pieces), 'reference_words': len(reference), **counts,
              'text_agreement_ratio': round(matcher.ratio(), 3), 'segments_aligned': sum(1 for s in out if s['words']),
              'segments_model_estimate': sum(1 for s in out if not s['words']), 'segments': per_segment,
              'min_matched_ratio': min_matched_ratio, 'fuzzy_ratio': fuzzy_ratio,
              'meaning': 'times of matched words come from the reference engine; interpolated words share the gap '
                         'between their matched neighbours (bound kept); agreement of two engines is not proof of what was said'}
    return out, report


def _ordered(words: list[dict]) -> bool:
    previous = words[0]['start']
    for w in words:
        if w['end'] < w['start'] or w['start'] < previous:
            return False
        previous = w['end']
    return True


def _fence(segments: list[dict]) -> None:
    """A model-estimate segment that overlaps an aligned neighbour keeps its times but is flagged."""
    aligned = [(s['start'], s['end']) for s in segments if s['words']]
    for seg in segments:
        if not seg['words'] and seg.get('start') is not None and any(
                seg['start'] < end and seg['end'] > start for start, end in aligned):
            seg['timing_warning'] = 'model estimate overlaps aligned segments'


def _delta(a, b):
    return round(a - b, 3) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None


# ---------------------------------------------------------------- reference engines

def faster_whisper_words(data: bytes, ref: dict) -> tuple[list[dict], dict]:
    """Local faster-whisper with word timestamps on the bytes sent to the model (never the network)."""
    from faster_whisper import WhisperModel
    model = WhisperModel(ref['model'], device=ref['device'], compute_type=ref['compute_type'],
                         **{k: ref[k] for k in ('revision', 'download_root', 'local_files_only', 'cpu_threads') if k in ref})
    segments, info = model.transcribe(io.BytesIO(data), word_timestamps=True, vad_filter=ref.get('vad_filter', True),
                                      vad_parameters=ref.get('vad_parameters'),
                                      language=ref.get('language'), multilingual=ref.get('multilingual', False),
                                      condition_on_previous_text=ref.get('condition_on_previous_text', True))
    words, texts = [], []
    for segment in segments:
        texts.append({'start': _seconds(segment.start), 'end': _seconds(segment.end), 'text': segment.text})
        words += [{'word': w.word, 'start': _seconds(w.start), 'end': _seconds(w.end),
                   'probability': round(float(w.probability), 4)} for w in segment.words or []]
    from ..worker import _version
    return words, {'engine': 'faster-whisper', 'tool_version': _version('faster-whisper'), 'model': ref['model'],
                   'revision': ref.get('revision', 'unknown'), 'language': getattr(info, 'language', None),
                   'language_probability': round(float(getattr(info, 'language_probability', 0.0) or 0.0), 4),
                   'segments': texts}


def _seconds(value) -> float:
    """Plain float: faster-whisper returns numpy scalars, which the citation validator rightly refuses."""
    return round(float(value), 3)


SPEECH_SYNC_SECONDS = 60


def google_speech_words(data: bytes, ref: dict, *, duration: float | None, artifact_id, transport=None) -> tuple[list[dict], dict]:
    """Speech-to-Text v2 recognize (Chirp) with word offsets; synchronous, so at most 60 s per call."""
    from .. import providers
    from ..db import session
    from .vertex import VertexUnavailable
    if duration is None or duration > SPEECH_SYNC_SECONDS:
        raise VertexUnavailable(f'Speech-to-Text synchronous recognition takes at most {SPEECH_SYNC_SECONDS} s '
                                '(batch recognition through Cloud Storage is not implemented)')
    transport = transport or speech_transport
    body = {'config': {'autoDecodingConfig': {}, 'model': ref['model'], 'languageCodes': ref['language_codes'],
                       'features': {'enableWordTimeOffsets': True, 'enableAutomaticPunctuation': True}},
            'content': base64.b64encode(data).decode()}
    worst = (duration / 60.0) * ref['price_usd_per_minute'] * ref.get('eur_per_usd', 1.0)
    refs = [('artifact', artifact_id)] if artifact_id else []
    digest_source = json.dumps({**body, 'content': hashlib.sha256(data).hexdigest()}, sort_keys=True)
    with session() as db:
        reservation = providers.reserve(db, None, worst, provider='vertex', model=ref['model'], purpose='asr_timing',
                                        digest_source=digest_source, refs=refs)
    status, error, response = 'ok', None, None
    try:
        response = transport(ref, body)
    except providers.ProviderUnavailable as exc:
        status, error = 'error', str(exc)
    actual = 0.0 if status == 'error' and error.startswith(providers.NOT_SENT) else worst
    with session() as db:
        providers._book(db, 'vertex', ref['model'], None, 'asr_timing', digest_source, refs, status, error, {},
                        actual - worst, {'actual_eur': actual, 'worst_case_eur': worst, 'settles_reservation': reservation['uid'],
                                         'price_usd_per_minute': ref['price_usd_per_minute'],
                                         'basis': 'billed per audio second; duration from ffprobe'}, reservation['started'])
    if status == 'error':
        raise VertexUnavailable(error)
    words, texts = [], []
    for result in response.get('results', []):
        alternative = (result.get('alternatives') or [{}])[0]
        texts.append({'text': alternative.get('transcript', ''), 'language': result.get('languageCode')})
        for w in alternative.get('words', []):
            words.append({'word': w.get('word', ''), 'start': _offset(w.get('startOffset')), 'end': _offset(w.get('endOffset')),
                          'probability': w.get('confidence')})
    words = [w for w in words if w['start'] is not None and w['end'] is not None]
    return words, {'engine': 'google-speech-v2', 'model': ref['model'], 'location': ref['location'],
                   'language_codes': ref['language_codes'], 'segments': texts, 'cost_eur': actual}


def _offset(value) -> float | None:
    if isinstance(value, str) and value.endswith('s'):
        try:
            return round(float(value[:-1]), 3)
        except ValueError:
            return None
    return None


def speech_transport(ref: dict, body: dict) -> dict:
    import urllib.error
    import urllib.request
    from .. import providers
    location = ref['location']
    url = (f'https://{location}-speech.googleapis.com/v2/projects/{providers._project(ref)}/locations/{location}'
           '/recognizers/_:recognize')
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers={
        'Authorization': f'Bearer {providers._access_token()}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise providers.ProviderUnavailable(f'Speech HTTP {exc.code}: {exc.read()[:500].decode("utf-8", "replace")}') from exc
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise providers.ProviderUnavailable(f'Speech call failed: {type(exc).__name__}') from exc


# ---------------------------------------------------------------- time notation of the model estimates

def _mmss(value) -> float | None:
    """1.26 read as 1 min 26 s; None when the hundredths cannot be seconds (>= 60)."""
    if not isinstance(value, (int, float)) or value < 0:
        return None
    minutes = int(value)
    seconds = round((value - minutes) * 100, 6)
    return minutes * 60 + seconds if seconds < 60 else None


def notation_hypothesis(segments: list[dict], *, reference: str, tolerance: float = 2.0, min_share: float = 0.8,
                        min_checked: int = 3) -> dict:
    """Detect model estimates that switch to minutes.seconds written as a decimal; never edits them.

    From the first drop in the raw estimates onward, if the minutes.seconds readings run forward
    from the time reached before the drop, those segments get ``timing_notation_hypothesis``. Only
    when the independent reference confirms it (at least ``min_share`` of the checked segments
    within ``tolerance`` seconds of their aligned start) does each of them also get the derived
    ``model_estimate_reinterpreted``. The original values stay in ``model_estimate``.
    """
    raw = [(s.get('model_estimate') or {}).get('start', s.get('start')) for s in segments]
    timed = [(i, v) for i, v in enumerate(raw) if isinstance(v, (int, float))]
    change = next((k for (j, a), (k, b) in zip(timed, timed[1:]) if b < a), None)
    result = {'status': 'not_detected', 'notation': 'minutes.seconds', 'reference': reference,
              'tolerance_seconds': tolerance, 'min_share': min_share}
    if change is None:
        return result
    before = max(v for i, v in timed if i < change)
    candidates = [i for i, v in timed if i >= change]
    readings = {i: _mmss(raw[i]) for i in candidates}
    forward = [readings[i] for i in candidates]
    backward = sum(1 for a, b in zip(forward, forward[1:]) if a is not None and b is not None and b < a)
    if (any(r is None for r in forward) or forward[0] < before - tolerance
            or backward > max(1, len(forward) // 10)):  # the model's own estimates are not strictly ordered
        result.update(status='not_consistent', first_segment=change, backward_steps=backward)
        return result
    checked = [(i, readings[i], segments[i]['start']) for i in candidates
               if segments[i].get('words') and segments[i].get('timing_source') != 'model_estimate']
    within = sum(1 for _, r, aligned in checked if abs(r - aligned) <= tolerance)
    confirmed = len(checked) >= min_checked and within / len(checked) >= min_share
    for i in candidates:
        segments[i]['timing_notation_hypothesis'] = {'notation': 'minutes.seconds', 'confirmed': confirmed,
                                                     'basis': 'raw estimates drop here and run forward as minutes.seconds'}
        if confirmed:
            estimate = segments[i].get('model_estimate') or {}
            segments[i]['model_estimate_reinterpreted'] = {
                'start': readings[i], 'end': _mmss(estimate.get('end')), 'notation': 'minutes.seconds',
                'confirmed_by': reference, 'derived': True}
    result.update(status='confirmed' if confirmed else 'hypothesis_only', first_segment=change,
                  segments=len(candidates), checked=len(checked), within_tolerance=within, backward_steps=backward)
    return result


def fill_from_forced(aligned: list[dict], forced: list[dict]) -> dict:
    """Fill segments the reference did not align with forced-alignment words; mark them unconfirmed.

    Only segments that passed the forced-alignment score gate, and only when their interval stays
    between the neighbouring timed segments (so multi-segment selections remain ordered).
    """
    filled, overlapping = [], []
    for i, (seg, alt) in enumerate(zip(aligned, forced)):
        if seg['words'] or not alt.get('words'):
            continue
        before = max((s['end'] for s in aligned[:i] if s['words']), default=0.0)
        after = min((s['start'] for s in aligned[i + 1:] if s['words']), default=float('inf'))
        if alt['start'] < before or alt['end'] > after:
            overlapping.append(i)
            continue
        aligned[i] = {**seg, 'start': alt['start'], 'end': alt['end'], 'words': alt['words'],
                      'timing_source': 'forced_alignment_unconfirmed', 'independent_confirmation': False,
                      'alignment_note': 'times from forced alignment only; no independent confirmation by the reference'}
        aligned[i].pop('timing_warning', None)
        filled.append(i)
    return {'filled_segments': filled, 'rejected_overlapping': overlapping,
            'meaning': 'filled segments are word-citable on the acoustic score alone, without independent confirmation'}

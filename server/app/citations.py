"""Deterministic references to stored transcript versions, not ASR truth claims."""
from __future__ import annotations

import hashlib
import json
import math

MAX_MILLISECONDS = 2**63 - 1
MAX_SELECTED_WORDS = 10_000
MAX_WORDS_PER_SEGMENT = 100_000

def milliseconds(seconds) -> int:
    if type(seconds) not in (int, float) or seconds < 0 or seconds > MAX_MILLISECONDS / 1000:
        raise ValueError('time must be nonnegative seconds representable as SQLite milliseconds')
    if not math.isfinite(seconds):
        raise ValueError('time must be finite')
    result = round(seconds * 1000)
    if result > MAX_MILLISECONDS:
        raise ValueError('time exceeds SQLite milliseconds')
    return result


def projection_from_segments(segments: list, indices: list[int]) -> dict:
    if not isinstance(segments, list) or not isinstance(indices, list) or not indices:
        raise ValueError('select at least one transcript segment')
    if any(type(i) is not int or i < 0 for i in indices):
        raise ValueError('segment indices must be nonnegative integers')
    if any(right != left + 1 for left, right in zip(indices, indices[1:])):
        raise ValueError('segment indices must be contiguous, unique and ascending')
    if indices[-1] >= len(segments):
        raise ValueError('segment index outside stored transcript')
    selected = [segments[i] for i in indices]
    previous_end = None
    for segment in selected:
        if not isinstance(segment, dict) or not isinstance(segment.get('text'), str) or not segment['text'].strip():
            raise ValueError('selected segment has no nonblank text')
        start, end = segment.get('start'), segment.get('end')
        milliseconds(start)
        milliseconds(end)
        if end < start:
            raise ValueError('segment times must be finite, nonnegative seconds with end >= start')
        if previous_end is not None and start < previous_end:
            raise ValueError('selected segment time ranges overlap or are out of order')
        previous_end = end
    text = ''.join(segment['text'] for segment in selected)
    return {
        'quote_text': text,
        'quote_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'start_ms': milliseconds(selected[0]['start']),
        'end_ms': milliseconds(selected[-1]['end']),
        'selector': {'kind': 'segments', 'indices': indices, 'text_join': 'concatenate_exact',
                     'time_unit': 'seconds', 'stored_time_unit': 'milliseconds',
                     'rounding': 'nearest_ms', 'precision': 'segment'},
    }


def validated_segment_words(segment: dict) -> list:
    """Validate an exact word projection without repairing the stored ASR result.

    Timestamp resolution is that supplied by ASR; validation is not alignment
    against the recording. Missing or conflicting word data stays stored and
    the existing segment selector remains usable independently.
    """
    projection_from_segments([segment], [0])
    words = segment.get('words')
    if not isinstance(words, list) or not words:
        raise ValueError('word timestamps are unavailable for this segment')
    if len(words) > MAX_WORDS_PER_SEGMENT:
        raise ValueError('segment exceeds the word validation limit')
    previous_end = segment['start']
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get('word'), str) or not word['word'].strip():
            raise ValueError('word must contain nonblank raw text')
        start, end = word.get('start'), word.get('end')
        milliseconds(start)
        milliseconds(end)
        if end < start or start < previous_end or end > segment['end']:
            raise ValueError('word times overlap, are out of order or outside the segment')
        previous_end = end
    if ''.join(word['word'] for word in words) != segment['text']:
        raise ValueError('raw word text does not exactly match the stored segment text')
    return words


def projection_from_words(segments: list, word_refs: list[dict]) -> dict:
    """Select consecutive stored words by segment/word indices, never by a guess."""
    if not isinstance(segments, list) or not isinstance(word_refs, list) or not word_refs:
        raise ValueError('select at least one stored word')
    if len(word_refs) > MAX_SELECTED_WORDS:
        raise ValueError('word selection exceeds the selection limit')
    validated = {}
    selected = []
    previous = None
    for ref in word_refs:
        if not isinstance(ref, dict) or set(ref) != {'segment_index', 'word_index'}:
            raise ValueError('word references require exactly segment_index and word_index')
        segment_index, word_index = ref['segment_index'], ref['word_index']
        if any(type(value) is not int or value < 0 for value in (segment_index, word_index)):
            raise ValueError('word reference indices must be nonnegative integers')
        if segment_index >= len(segments):
            raise ValueError('segment index outside stored transcript')
        if segment_index not in validated:
            validated[segment_index] = validated_segment_words(segments[segment_index])
        words = validated[segment_index]
        if word_index >= len(words):
            raise ValueError('word index outside stored segment')
        if previous is not None:
            ps, pw = previous
            contiguous = (segment_index == ps and word_index == pw + 1) or (
                segment_index == ps + 1 and pw == len(validated[ps]) - 1 and word_index == 0)
            if not contiguous:
                raise ValueError('word references must be contiguous, unique and ascending')
            if segments[segment_index]['start'] < segments[ps]['end'] and segment_index != ps:
                raise ValueError('selected segment time ranges overlap or are out of order')
        selected.append(words[word_index])
        previous = (segment_index, word_index)
    text = ''.join(word['word'] for word in selected)
    return {
        'quote_text': text,
        'quote_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'start_ms': milliseconds(selected[0]['start']),
        'end_ms': milliseconds(selected[-1]['end']),
        'selector': {
            'kind': 'words', 'word_refs': [dict(ref) for ref in word_refs],
            'text_join': 'concatenate_exact', 'time_unit': 'seconds',
            'stored_time_unit': 'milliseconds', 'rounding': 'nearest_ms',
            'precision': 'word_asr', 'source_start': selected[0]['start'],
            'source_end': selected[-1]['end'], 'alignment_verification': 'not_performed',
        },
    }


def projection_from_selector(segments: list, selector: dict) -> dict:
    """Rebuild a canonical selector for package validation, including legacy v1."""
    if not isinstance(selector, dict):
        raise ValueError('selector must be an object')
    if selector.get('kind') == 'segments':
        projection = projection_from_segments(segments, selector.get('indices'))
    elif selector.get('kind') == 'words':
        projection = projection_from_words(segments, selector.get('word_refs'))
    else:
        raise ValueError('unsupported selector kind')
    # JSON comparison distinguishes false/0 and keeps the exact numeric payload.
    canonical = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if canonical(selector) != canonical(projection['selector']):
        raise ValueError('selector differs from the canonical stored transcript projection')
    return projection


def anchor_payload(row) -> dict:
    result = dict(row)
    result['selector'] = json.loads(result.pop('selector_json'))
    result['validation'] = 'matches_stored_transcript_version'
    result['audio_verification'] = 'not_performed'
    return result


def create_citation(artifact_id: int, derived_text_id: int, segment_indices: list[int] | None = None,
                    *, word_refs: list[dict] | None = None, quote_text: str | None = None) -> dict:
    if any(type(value) is not int or not 0 < value <= MAX_MILLISECONDS for value in (artifact_id, derived_text_id)):
        raise ValueError('artifact and transcript IDs must be positive SQLite integers')
    if (segment_indices is None) == (word_refs is None):
        raise ValueError('provide exactly one of segment indices or word references')
    if quote_text is not None and not isinstance(quote_text, str):
        raise ValueError('expected quote text must be a string')
    from .db import session
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        transcript = db.execute("SELECT * FROM derived_text WHERE id=? AND artifact_id=? AND kind='transcript'",
                                (derived_text_id, artifact_id)).fetchone()
        if not transcript:
            raise ValueError('concrete transcript version does not belong to artifact')
        try:
            segments = json.loads(transcript['segments_json'])
        except (ValueError, TypeError) as exc:
            raise ValueError('stored transcript segments are invalid') from exc
        projection = (projection_from_words(segments, word_refs) if word_refs is not None
                      else projection_from_segments(segments, segment_indices))
        if quote_text is not None and quote_text != projection['quote_text']:
            raise ValueError('expected quote text differs from the pinned transcript selection')
        cur = db.execute('''INSERT INTO evidence_anchors(artifact_id,derived_text_id,start_ms,end_ms,
                            quote_text,quote_sha256,selector_json) VALUES(?,?,?,?,?,?,?)''',
                         (artifact_id, derived_text_id, projection['start_ms'], projection['end_ms'],
                          projection['quote_text'], projection['quote_sha256'],
                          json.dumps(projection['selector'], sort_keys=True)))
        anchor_id = int(cur.lastrowid)
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES('anchor_create','evidence_anchor',?,?)",
                   (anchor_id, json.dumps({'artifact_id': artifact_id, 'derived_text_id': derived_text_id})))
        return anchor_payload(db.execute('SELECT * FROM evidence_anchors WHERE id=?', (anchor_id,)).fetchone())

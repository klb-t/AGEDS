"""Deterministic references to stored transcript versions, not ASR truth claims."""
from __future__ import annotations

import hashlib
import json
import math

MAX_MILLISECONDS = 2**63 - 1

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


def anchor_payload(row) -> dict:
    result = dict(row)
    result['selector'] = json.loads(result.pop('selector_json'))
    result['validation'] = 'matches_stored_transcript_version'
    result['audio_verification'] = 'not_performed'
    return result


def create_citation(artifact_id: int, derived_text_id: int, segment_indices: list[int]) -> dict:
    if any(type(value) is not int or not 0 < value <= MAX_MILLISECONDS for value in (artifact_id, derived_text_id)):
        raise ValueError('artifact and transcript IDs must be positive SQLite integers')
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
        projection = projection_from_segments(segments, segment_indices)
        cur = db.execute('''INSERT INTO evidence_anchors(artifact_id,derived_text_id,start_ms,end_ms,
                            quote_text,quote_sha256,selector_json) VALUES(?,?,?,?,?,?,?)''',
                         (artifact_id, derived_text_id, projection['start_ms'], projection['end_ms'],
                          projection['quote_text'], projection['quote_sha256'],
                          json.dumps(projection['selector'], sort_keys=True)))
        anchor_id = int(cur.lastrowid)
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES('anchor_create','evidence_anchor',?,?)",
                   (anchor_id, json.dumps({'artifact_id': artifact_id, 'derived_text_id': derived_text_id})))
        return anchor_payload(db.execute('SELECT * FROM evidence_anchors WHERE id=?', (anchor_id,)).fetchone())

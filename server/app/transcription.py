from __future__ import annotations
import json
from .db import session

MAX_TIMING_REPORT_SEGMENTS = 10_000


def word_timing_capabilities(segments) -> dict:
    """Describe selectable ASR timing without altering or inventing raw words.

    Invalid/missing word projections do not invalidate the original segment
    result. Availability is scoped to each examined segment; a multi-segment
    selector additionally validates ordering across segment boundaries.
    """
    from .citations import validated_segment_words
    report = {
        'schema_version': 1, 'kind': 'asr_word_timing',
        'source_time_unit': 'seconds', 'anchor_time_unit': 'milliseconds',
        'anchor_rounding': 'nearest_ms', 'source_resolution': 'as_supplied_by_asr',
        'alignment_verification': 'not_performed', 'audio_verification': 'not_performed',
        'raw_words_modified': False, 'segments': [],
        'status': 'unavailable', 'coverage_complete': isinstance(segments, list),
    }
    if not isinstance(segments, list):
        report['reason'] = 'segments are not an array'
        return report
    report['total_segments'] = len(segments)
    report['coverage_complete'] = len(segments) <= MAX_TIMING_REPORT_SEGMENTS
    for index, segment in enumerate(segments[:MAX_TIMING_REPORT_SEGMENTS]):
        item = {'segment_index': index, 'word_selection_available': False}
        try:
            words = validated_segment_words(segment)
        except ValueError as error:
            item['reason'] = str(error)
        else:
            item['word_selection_available'] = True
            item['word_count'] = len(words)
        report['segments'].append(item)
    available = sum(item['word_selection_available'] for item in report['segments'])
    report['examined_segments'] = len(report['segments'])
    report['available_segments'] = available
    if available:
        report['status'] = ('available' if available == len(segments) and report['coverage_complete'] else 'partial')
    if not report['coverage_complete']:
        report['coverage_reason'] = 'segment reporting limit reached'
    return report


def queue_transcription(artifact_id: int, priority: int | None = None) -> int:
    with session() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM artifacts WHERE id=?", (artifact_id,)).fetchone():
            raise ValueError(f"artifact {artifact_id} missing")
        existing=db.execute("SELECT id FROM jobs WHERE kind='transcribe' AND artifact_id=? AND status IN ('queued','running')",(artifact_id,)).fetchone()
        if existing:
            job_id = int(existing['id'])
            previous = db.execute("SELECT priority FROM jobs WHERE id=?", (job_id,)).fetchone()['priority']
            if priority is not None and previous != priority:
                db.execute("UPDATE jobs SET priority=? WHERE id=?", (priority, job_id))
                db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('job_reprioritized','job',?,?)", (job_id,json.dumps({'previous_priority':previous,'priority':priority})))
            return job_id
        effective_priority = 0 if priority is None else priority
        cur=db.execute("INSERT INTO jobs(kind,artifact_id,priority) VALUES ('transcribe',?,?)",(artifact_id,effective_priority))
        job_id=int(cur.lastrowid)
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('job_enqueued','job',?,?)", (job_id,json.dumps({'artifact_id':artifact_id,'kind':'transcribe','priority':effective_priority})))
        return job_id

def queue_all_audio() -> int:
    with session() as db:
        ids=[r['id'] for r in db.execute("SELECT id FROM artifacts WHERE mime_type LIKE 'audio/%' OR mime_type LIKE 'video/%'")]
    for aid in ids: queue_transcription(int(aid))
    return len(ids)

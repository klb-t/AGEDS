from __future__ import annotations
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


def queue_transcription(artifact_id: int, priority: int | None = None, *, recipe: str | None = None,
                        overrides: dict | None = None, dedup: str | None = None) -> int:
    """Queue ASR with a pinned recipe (default ``EW_ASR_RECIPE`` or ``legacy``).

    Returns the job ID; see ``media.queue`` for the dedup policy and
    ``enqueue_transcription`` for the full enqueue record.
    """
    return enqueue_transcription(artifact_id, priority, recipe=recipe, overrides=overrides, dedup=dedup)['job_id']


def enqueue_transcription(artifact_id: int, priority: int | None = None, *, recipe: str | None = None,
                          overrides: dict | None = None, dedup: str | None = None) -> dict:
    from .media.queue import enqueue_media_job
    return enqueue_media_job('transcribe', artifact_id, recipe_id=recipe, overrides=overrides,
                             priority=priority, dedup=dedup)

def queue_all_audio() -> int:
    with session() as db:
        ids=[r['id'] for r in db.execute("SELECT id FROM artifacts WHERE mime_type LIKE 'audio/%' OR mime_type LIKE 'video/%'")]
    for aid in ids: queue_transcription(int(aid))
    return len(ids)

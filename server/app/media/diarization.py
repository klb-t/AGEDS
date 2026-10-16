"""Optional speaker diarization post-pass and human speaker assignments.

The adapter uses a locally available ``pyannote.audio`` pipeline when the
package is importable; otherwise it reports ``unavailable`` with the reason and
the job fails explicitly. Reported turns are stored in ``speaker_turns`` as
hypotheses linked to their processing run. They are never merged into
transcript text: the speaker view aligns them to a chosen transcript version
by time overlap at display time.

Human assignments (``speaker_assignments``) are a separate append-only
annotation layer: per transcript segment, or naming a diarization label of one
run. In display a segment assignment takes precedence over a label mapping,
which takes precedence over the model hypothesis; all three stay visible.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import math

from .. import worker as _worker
from ..citations import loads_transcript_segments, milliseconds
from ..db import session
from ..jobs import publish_result, update_run_metadata
from .common import AdapterUnavailable, finite_seconds, input_provenance, load_artifact, open_verified, verification_record
from .recipe import recipe_from_job, run_recipe_record

HYPOTHESIS_MEANING = 'speaker label hypothesis from a diarization model; not identity'
MAX_ASSIGNMENT_TEXT = 200


class PyannoteDiarizer:
    def __init__(self, recipe: dict):
        self.recipe = recipe
        self.effective = recipe['effective']

    def availability(self) -> dict:
        try:
            spec = importlib.util.find_spec('pyannote.audio')
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            return {'status': 'unavailable', 'engine': 'pyannote.audio',
                    'reason': 'pyannote.audio is not importable (optional dependency not installed)'}
        return {'status': 'available', 'engine': 'pyannote.audio', 'pipeline': self.effective['pipeline'],
                'note': 'pipeline weights must be available locally; gated downloads are not attempted by AGEDS tests'}

    def describe(self) -> dict:
        try:
            version = importlib.import_module('importlib.metadata').version('pyannote.audio')
        except Exception:
            version = 'unknown'
        return {'tool': 'pyannote.audio', 'tool_version': version, 'provider': 'local',
                'model': self.effective['pipeline']}

    def diarize(self, audio) -> list[dict]:
        """pyannote.audio 3.x API: Pipeline.from_pretrained(...)(file) -> Annotation.

        Not executed in this repository's tests (dependency and weights absent).
        """
        from pyannote.audio import Pipeline
        pipeline = Pipeline.from_pretrained(self.effective['pipeline'])
        if pipeline is None:
            raise AdapterUnavailable('pyannote pipeline could not be loaded locally')
        options = {key: self.effective[key] for key in ('num_speakers', 'min_speakers', 'max_speakers')
                   if self.effective[key] is not None}
        output = pipeline({'audio': audio, 'uri': 'ageds-artifact'}, **options)
        annotation = getattr(output, 'speaker_diarization', output)
        return [{'start': turn.start, 'end': turn.end, 'speaker': str(label)}
                for turn, _, label in annotation.itertracks(yield_label=True)]


def validate_turns(raw_turns, limit: int) -> tuple[list[dict], list[dict], dict]:
    accepted, rejected = [], []
    for index, turn in enumerate(raw_turns):
        if len(accepted) >= limit:
            return accepted, rejected, {'status': 'partial', 'reason': 'limits.max_turns reached',
                                        'turns_accepted': len(accepted)}
        reason = None
        start = finite_seconds(turn.get('start')) if isinstance(turn, dict) else None
        end = finite_seconds(turn.get('end')) if isinstance(turn, dict) else None
        label = turn.get('speaker') if isinstance(turn, dict) else None
        if start is None or end is None or end < start:
            reason = 'turn times must be finite, nonnegative and ordered'
        elif not isinstance(label, str) or not label.strip():
            reason = 'speaker label must be nonblank text'
        if reason:
            rejected.append({'index': index, 'reason': reason, 'raw': repr(turn)[:200]})
            continue
        try:
            accepted.append({'start': start, 'end': end, 'speaker': label,
                             'start_ms': milliseconds(start), 'end_ms': milliseconds(end)})
        except ValueError as error:
            rejected.append({'index': index, 'reason': str(error), 'raw': repr(turn)[:200]})
    return accepted, rejected, {'status': 'complete'}


def run_diarize(job: dict, diarizer=None) -> int:
    record = recipe_from_job(job)
    built = diarizer is None
    diarizer = diarizer or PyannoteDiarizer(record)
    artifact = load_artifact(job['artifact_id'])
    verification = verification_record()
    described = diarizer.describe()
    metadata = {'tool': described.get('tool', 'unknown'), 'tool_version': described.get('tool_version', 'unknown'),
                'provider': described.get('provider', 'local'), 'model': described.get('model', 'unknown'),
                'model_version': 'unknown',
                'parameters': {'recipe_id': record['id'], 'recipe_sha256': record['sha256'], 'recipe': record['effective']},
                'metadata': {},
                'provenance': {'recipe': run_recipe_record(record, 'adapter_built_from_pinned_recipe' if built else
                                                           'injected_diarizer_with_pinned_recipe_parameters'),
                               **input_provenance(artifact, verification)}}
    update_run_metadata(job, metadata)
    availability = diarizer.availability()
    metadata['metadata']['availability'] = availability
    update_run_metadata(job, metadata)
    if availability.get('status') != 'available':
        raise AdapterUnavailable(f'diarization unavailable: {availability.get("reason", "unknown")}')
    with open_verified(artifact) as audio:
        verification['initial_full_sha256'] = 'passed'
        raw_turns = list(diarizer.diarize(audio))
        audio.verify_unchanged()
        verification['final_same_descriptor'] = 'passed'
        work = _worker._wav_work(audio) or {'unit': 'audio_second', 'amount': 'unknown',
                                            'basis': 'not_reported_by_diarizer_or_header'}
    turns, rejected, coverage = validate_turns(raw_turns, record['effective']['limits']['max_turns'])
    metadata['metadata'].update({'turns_reported': len(raw_turns), 'turns_stored': len(turns),
                                 'rejected_turns': rejected[:100], 'rejected_count': len(rejected),
                                 'coverage': coverage, 'meaning': HYPOTHESIS_MEANING,
                                 'work': work})
    update_run_metadata(job, metadata)

    def write(db, current):
        for index, turn in enumerate(turns):
            db.execute('''INSERT INTO speaker_turns(run_id,artifact_id,turn_index,start_ms,end_ms,speaker_label,
                          source_start,source_end,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)''',
                       (job['run_id'], job['artifact_id'], index, turn['start_ms'], turn['end_ms'], turn['speaker'],
                        turn['start'], turn['end'], json.dumps({'meaning': HYPOTHESIS_MEANING})))
        return job['run_id']
    return publish_result(job, write, summary={'speaker_turns': len(turns)})


def _clean(value, name: str, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_ASSIGNMENT_TEXT:
        raise ValueError(f'{name} must be nonblank text of at most {MAX_ASSIGNMENT_TEXT} characters')
    return value


def add_speaker_assignment(artifact_id: int, *, speaker: str, derived_text_id: int | None = None,
                           segment_index: int | None = None, diarization_run_id: int | None = None,
                           speaker_label: str | None = None, note: str | None = None, actor: str = 'user') -> dict:
    """Append a human assignment; validated against the stored version or run, never against model text."""
    speaker = _clean(speaker, 'speaker', required=True)
    if note is not None and (not isinstance(note, str) or len(note) > 4000):
        raise ValueError('note must be text of at most 4000 characters')
    segment_scope = derived_text_id is not None or segment_index is not None
    label_scope = diarization_run_id is not None or speaker_label is not None
    if segment_scope == label_scope:
        raise ValueError('assign either a transcript segment (derived_text_id + segment_index) '
                         'or a diarization label (diarization_run_id + speaker_label)')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        if segment_scope:
            if type(derived_text_id) is not int or type(segment_index) is not int or segment_index < 0:
                raise ValueError('derived_text_id and a nonnegative segment_index are required')
            version = db.execute("SELECT segments_json FROM derived_text WHERE id=? AND artifact_id=? AND kind='transcript'",
                                 (derived_text_id, artifact_id)).fetchone()
            if version is None:
                raise ValueError('transcript version does not belong to artifact')
            if segment_index >= len(loads_transcript_segments(version['segments_json'])):
                raise ValueError('segment index outside stored transcript')
            scope = 'segment'
        else:
            _clean(speaker_label, 'speaker_label', required=True)
            if type(diarization_run_id) is not int:
                raise ValueError('diarization_run_id must be an integer')
            if not db.execute('SELECT 1 FROM speaker_turns WHERE run_id=? AND artifact_id=? AND speaker_label=? LIMIT 1',
                              (diarization_run_id, artifact_id, speaker_label)).fetchone():
                raise ValueError('diarization run has no such speaker label for this artifact')
            scope = 'diarization_label'
        cur = db.execute('''INSERT INTO speaker_assignments(artifact_id,scope,derived_text_id,segment_index,
                            diarization_run_id,speaker_label,speaker,note,actor) VALUES (?,?,?,?,?,?,?,?,?)''',
                         (artifact_id, scope, derived_text_id, segment_index, diarization_run_id, speaker_label,
                          speaker, note, actor))
        assignment_id = int(cur.lastrowid)
        db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES (?,'speaker_assigned','speaker_assignment',?,?)",
                   (actor, assignment_id, json.dumps({'artifact_id': artifact_id, 'scope': scope}, sort_keys=True)))
        return dict(db.execute('SELECT * FROM speaker_assignments WHERE id=?', (assignment_id,)).fetchone())


def speaker_view(artifact_id: int, derived_text_id: int, diarization_run_id: int | None = None,
                 *, max_segments: int = 5000) -> dict:
    """Transcript segments with hypothesis labels and human assignments side by side."""
    with session() as db:
        version = db.execute("SELECT id,segments_json FROM derived_text WHERE id=? AND artifact_id=? AND kind='transcript'",
                             (derived_text_id, artifact_id)).fetchone()
        if version is None:
            raise ValueError('transcript version does not belong to artifact')
        if diarization_run_id is None:
            latest = db.execute("""SELECT r.id FROM processing_runs r JOIN jobs j ON j.id=r.job_id
                                   WHERE r.artifact_id=? AND j.kind='diarize' AND r.status='done' ORDER BY r.id DESC LIMIT 1""",
                                (artifact_id,)).fetchone()
            diarization_run_id = int(latest['id']) if latest else None
        turns = [dict(r) for r in db.execute('SELECT * FROM speaker_turns WHERE run_id=? AND artifact_id=? ORDER BY start_ms,turn_index',
                                             (diarization_run_id, artifact_id))] if diarization_run_id else []
        assignments = [dict(r) for r in db.execute('SELECT * FROM speaker_assignments WHERE artifact_id=? ORDER BY id', (artifact_id,))]
    segments = loads_transcript_segments(version['segments_json'])
    by_segment, by_label = {}, {}
    for item in assignments:
        if item['scope'] == 'segment' and item['derived_text_id'] == derived_text_id:
            by_segment.setdefault(item['segment_index'], []).append(item)
        elif item['scope'] == 'diarization_label' and item['diarization_run_id'] == diarization_run_id:
            by_label.setdefault(item['speaker_label'], []).append(item)
    rows = []
    for index, segment in enumerate(segments[:max_segments]):
        start, end = segment.get('start') if isinstance(segment, dict) else None, segment.get('end') if isinstance(segment, dict) else None
        overlaps: dict[str, float] = {}
        if type(start) in (int, float) and type(end) in (int, float) and math.isfinite(start) and math.isfinite(end):
            for turn in turns:
                overlap = min(end * 1000, turn['end_ms']) - max(start * 1000, turn['start_ms'])
                if overlap > 0:
                    overlaps[turn['speaker_label']] = overlaps.get(turn['speaker_label'], 0.0) + overlap / 1000
        hypotheses = [{'speaker_label': label, 'overlap_seconds': seconds}
                      for label, seconds in sorted(overlaps.items(), key=lambda item: (-item[1], item[0]))]
        hypothesis = hypotheses[0]['speaker_label'] if hypotheses else None
        manual = by_segment.get(index, [])
        mapping = by_label.get(hypothesis, []) if hypothesis else []
        if manual:
            display, source = manual[-1]['speaker'], 'manual_segment'
        elif mapping:
            display, source = mapping[-1]['speaker'], 'manual_label_mapping'
        elif hypothesis:
            display, source = hypothesis, 'diarization_hypothesis'
        else:
            display, source = None, 'none'
        rows.append({'segment_index': index, 'start': start, 'end': end,
                     'text': segment.get('text') if isinstance(segment, dict) else None,
                     'hypotheses': hypotheses, 'hypothesis_label': hypothesis,
                     'manual_segment': manual[-1] if manual else None, 'manual_segment_history': len(manual),
                     'manual_label_mapping': mapping[-1] if mapping else None,
                     'display_speaker': display, 'display_source': source})
    return {'artifact_id': artifact_id, 'derived_text_id': derived_text_id, 'diarization_run_id': diarization_run_id,
            'hypothesis_meaning': HYPOTHESIS_MEANING,
            'precedence': ['manual_segment', 'manual_label_mapping', 'diarization_hypothesis'],
            'transcript_text_modified': False, 'segments': rows,
            'coverage': {'status': 'complete' if len(segments) <= max_segments else 'partial',
                         'segments_total': len(segments), 'segments_listed': len(rows)}}

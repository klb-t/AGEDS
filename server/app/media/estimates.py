"""Queue listing with processing-time estimates measured from past runs.

The rate of a recipe is ``run wall seconds / work units`` (for ASR the
real-time factor: processing seconds per audio second; for OCR seconds per
page) over completed runs with the same job kind and recipe hash. Wall time is
``started_at..finished_at`` of the run, so it includes model loading and input
verification. Without at least one measured run of that hash, or without a
known work amount for the queued input, the estimate is ``unknown`` — never a
guess. Spread is reported as min/median/max with the sample size.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from ..db import session
from ..wav_header import MAX_PREFIX_BYTES, probe_wav_header
from .recipe import RecipeError, parse_payload

MAX_RUNS_SAMPLED = 2000
UNIT_FOR_KIND = {'transcribe': 'audio_second', 'diarize': 'audio_second', 'ocr': 'page'}


def _seconds(started, finished) -> float | None:
    try:
        value = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _json(raw) -> dict:
    try:
        value = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _work(metadata: dict, unit: str) -> float | None:
    work = metadata.get('work')
    if not isinstance(work, dict) or work.get('unit') != unit:
        return None
    amount = work.get('amount')
    if type(amount) in (int, float) and math.isfinite(amount) and amount > 0:
        return float(amount)
    return None


def measured_rates(db, kind: str) -> dict[str, dict]:
    """Per recipe hash: sample size and min/median/max/mean of seconds per work unit."""
    unit = UNIT_FOR_KIND.get(kind)
    rows = db.execute("""SELECT r.started_at,r.finished_at,r.metadata_json,r.provenance_json FROM processing_runs r
                         JOIN jobs j ON j.id=r.job_id WHERE j.kind=? AND r.status='done' ORDER BY r.id DESC LIMIT ?""",
                      (kind, MAX_RUNS_SAMPLED)).fetchall()
    samples: dict[str, list[float]] = {}
    skipped = 0
    for row in rows:
        recipe = _json(row['provenance_json']).get('recipe')
        digest = recipe.get('sha256') if isinstance(recipe, dict) else None
        seconds = _seconds(row['started_at'], row['finished_at'])
        amount = _work(_json(row['metadata_json']), unit)
        if not digest or seconds is None or amount is None:
            skipped += 1
            continue
        samples.setdefault(digest, []).append(seconds / amount)
    result = {}
    for digest, rates in samples.items():
        result[digest] = {'n': len(rates), 'min': min(rates), 'median': statistics.median(rates),
                          'max': max(rates), 'mean': statistics.fmean(rates),
                          'unit': f'wall seconds per {unit}'}
    result['_skipped_runs_without_measurement'] = skipped
    return result


def _known_work(db, artifact: dict, kind: str) -> dict | None:
    unit = UNIT_FOR_KIND.get(kind)
    rows = db.execute("""SELECT r.metadata_json FROM processing_runs r JOIN artifacts a ON a.id=r.artifact_id
                         WHERE a.sha256 IS ? AND r.status='done' ORDER BY r.id DESC LIMIT 50""", (artifact['sha256'],)).fetchall()
    for row in rows:
        amount = _work(_json(row['metadata_json']), unit)
        if amount is not None:
            return {'unit': unit, 'amount': amount, 'basis': 'measured_by_earlier_run_on_same_content_sha256'}
    if unit == 'audio_second' and artifact.get('stored_path'):
        path = Path(artifact['stored_path'])
        try:
            if path.is_file() and not path.is_symlink():
                with path.open('rb') as handle:
                    probe = probe_wav_header(handle.read(MAX_PREFIX_BYTES), artifact.get('size_bytes'))
                duration = probe.get('declared_duration_sec')
                if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
                    return {'unit': unit, 'amount': float(duration), 'basis': 'wav_header_declared_duration'}
        except OSError:
            return None
    if unit == 'page' and (artifact.get('mime_type') or '').startswith('image/'):
        return {'unit': unit, 'amount': 1.0, 'basis': 'single_image'}
    return None


def queue_listing(*, statuses: tuple[str, ...] = ('running', 'queued'), limit: int = 100) -> dict:
    if not statuses or any(s not in ('queued', 'running', 'done', 'failed') for s in statuses):
        raise ValueError('statuses must be among queued, running, done, failed')
    if not 1 <= limit <= 1000:
        raise ValueError('limit must be 1..1000')
    with session() as db:
        jobs = [dict(r) for r in db.execute(
            """SELECT j.id,j.kind,j.artifact_id,j.status,j.priority,j.attempts,j.payload_json,j.created_at,j.started_at,
                      j.finished_at,j.error,a.original_name,a.mime_type,a.sha256,a.size_bytes,a.stored_path
               FROM jobs j LEFT JOIN artifacts a ON a.id=j.artifact_id WHERE j.status IN (%s)
               ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, j.priority DESC, j.id LIMIT ?"""
            % ','.join('?' * len(statuses)), (*statuses, limit))]
        rates = {kind: measured_rates(db, kind) for kind in {job['kind'] for job in jobs}}
        items, cumulative = [], 0.0
        for position, job in enumerate(jobs):
            try:
                payload = parse_payload(job['payload_json'])
                recipe = ({'id': payload['recipe']['id'], 'sha256': payload['recipe']['sha256'], 'pin_status': 'pinned_at_enqueue'}
                          if payload else {'id': 'legacy', 'sha256': None, 'pin_status': 'unpinned_pre_recipe_job'})
            except (RecipeError, KeyError, TypeError):
                recipe = {'id': 'unknown', 'sha256': None, 'pin_status': 'invalid_payload'}
            stats = rates.get(job['kind'], {}).get(recipe['sha256']) if recipe['sha256'] else None
            work = _known_work(db, job, job['kind']) if job['artifact_id'] else None
            if stats and work:
                estimate = {'status': 'measured', 'seconds_median': work['amount'] * stats['median'],
                            'seconds_min': work['amount'] * stats['min'], 'seconds_max': work['amount'] * stats['max'],
                            'sample_size': stats['n'], 'rate': stats, 'work': work}
            else:
                reasons = []
                if not recipe['sha256']:
                    reasons.append('job has no pinned recipe hash')
                elif not stats:
                    reasons.append('no completed run with this recipe hash has a measured work amount')
                if not work:
                    reasons.append('work amount of this input is unknown')
                estimate = {'status': 'unknown', 'reasons': reasons, 'work': work or 'unknown',
                            'rate': stats or 'unknown'}
            cumulative_value = None
            if job['status'] == 'queued' and cumulative is not None:
                if estimate['status'] == 'measured':
                    cumulative += estimate['seconds_median']
                    cumulative_value = cumulative
                else:
                    cumulative = None
            items.append({
                'job_id': job['id'], 'kind': job['kind'], 'status': job['status'], 'priority': job['priority'],
                'attempts': job['attempts'], 'nominal_position': position, 'artifact_id': job['artifact_id'],
                'artifact_name': job['original_name'], 'created_at': job['created_at'], 'started_at': job['started_at'],
                'recipe': recipe, 'estimate': estimate,
                'queued_seconds_median_through_this_job': cumulative_value if job['status'] == 'queued' else None,
            })
    return {'jobs': items, 'limit': limit, 'statuses': list(statuses),
            'ordering': 'running first, then priority descending and job id (claim order; a job waits while '
                        'another job of the same kind and artifact runs)',
            'estimate_model': 'work amount x measured wall seconds per unit for the same job kind and recipe hash; '
                              'min/median/max over completed runs; unknown without data',
            'measured_rates': {kind: {k: v for k, v in value.items()} for kind, value in rates.items()}}


def set_job_priority(job_id: int, priority: int, *, actor: str = 'user') -> dict:
    """Manual choice: change the priority of a queued job (audited)."""
    if type(priority) is not int or not -2**63 <= priority <= 2**63 - 1:
        raise ValueError('priority must be a signed 64-bit integer')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT id,status,priority FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise LookupError('job not found')
        if row['status'] != 'queued':
            raise ValueError(f'only queued jobs can be reprioritized (job is {row["status"]})')
        if row['priority'] != priority:
            db.execute('UPDATE jobs SET priority=? WHERE id=?', (priority, job_id))
            db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES (?,'job_reprioritized','job',?,?)",
                       (actor, job_id, json.dumps({'previous_priority': row['priority'], 'priority': priority})))
        return {'job_id': job_id, 'previous_priority': row['priority'], 'priority': priority}

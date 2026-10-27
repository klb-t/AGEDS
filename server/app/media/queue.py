"""Enqueue media jobs with a pinned recipe and an explicit dedup policy.

Dedup policies (``EW_MEDIA_DEDUP_POLICY`` sets the default, ``reuse``):

* ``reuse`` — an active (queued/running) job of the same kind, artifact and
  recipe hash is returned; a requested priority is applied to it in place.
  Completed jobs never block a new attempt (behaviour before recipes).
* ``reuse_completed`` — additionally returns a completed job with the same
  recipe hash instead of running the identical recipe again.
* ``duplicate`` — always creates a new job.

A different recipe hash is always a separate job. A pre-recipe job with an
empty payload is matched only by a request for the ``legacy`` preset while it
is active, because the worker executes such a job with ``legacy`` resolved at
run time; its historical recipe stays ``unknown``.
"""
from __future__ import annotations

import json

from ..db import session
from .recipe import (DEDUP_POLICIES, JOB_KIND_FOR_RECIPE, RECIPE_KIND_FOR_JOB, RecipeError,
                     default_dedup_policy, effective_recipe, job_payload, parse_payload)


def _pinned_sha(raw) -> str | None:
    try:
        payload = parse_payload(raw)
    except RecipeError:
        return 'invalid'
    if payload is None:
        return None
    recipe = payload.get('recipe')
    return recipe.get('sha256') if isinstance(recipe, dict) else 'invalid'


def enqueue_media_job(job_kind: str, artifact_id: int, *, recipe_id: str | None = None,
                      overrides: dict | None = None, priority: int | None = None,
                      dedup: str | None = None, settings=None) -> dict:
    recipe_kind = RECIPE_KIND_FOR_JOB.get(job_kind)
    if recipe_kind is None:
        raise ValueError(f'unsupported media job kind {job_kind!r}; expected one of {", ".join(JOB_KIND_FOR_RECIPE.values())}')
    policy = dedup or default_dedup_policy()
    if policy not in DEDUP_POLICIES:
        raise RecipeError(f'dedup policy must be one of {", ".join(DEDUP_POLICIES)}')
    if priority is not None and (type(priority) is not int or not -2**63 <= priority <= 2**63 - 1):
        raise ValueError('priority must be a signed 64-bit integer')
    record = effective_recipe(recipe_id, kind=recipe_kind, overrides=overrides, settings=settings)
    recipe_summary = {'id': record['id'], 'sha256': record['sha256'], 'kind': recipe_kind}
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM artifacts WHERE id=?', (artifact_id,)).fetchone():
            raise ValueError(f'artifact {artifact_id} missing')
        if policy != 'duplicate':
            statuses = ('queued', 'running', 'done') if policy == 'reuse_completed' else ('queued', 'running')
            rows = db.execute('SELECT id,status,priority,payload_json FROM jobs WHERE kind=? AND artifact_id=? AND status IN (%s) ORDER BY id'
                              % ','.join('?' * len(statuses)), (job_kind, artifact_id, *statuses)).fetchall()
            active = completed = None
            for row in rows:
                pinned = _pinned_sha(row['payload_json'])
                same = pinned == record['sha256'] or (
                    pinned is None and job_kind == 'transcribe' and record['id'] == 'legacy' and row['status'] != 'done')
                if not same:
                    continue
                if row['status'] in ('queued', 'running') and active is None:
                    active = row
                elif row['status'] == 'done' and completed is None:
                    completed = row
            if active is not None:
                job_id = int(active['id'])
                if priority is not None and active['priority'] != priority:
                    db.execute('UPDATE jobs SET priority=? WHERE id=?', (priority, job_id))
                    db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('job_reprioritized','job',?,?)",
                               (job_id, json.dumps({'previous_priority': active['priority'], 'priority': priority})))
                return {'job_id': job_id, 'created': False, 'reused': 'active', 'dedup_policy': policy,
                        'recipe': recipe_summary, 'pinned': _pinned_sha(active['payload_json']) is not None}
            if completed is not None:
                job_id = int(completed['id'])
                db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('job_enqueue_reused_completed','job',?,?)",
                           (job_id, json.dumps({'artifact_id': artifact_id, 'kind': job_kind, 'recipe_sha256': record['sha256']})))
                return {'job_id': job_id, 'created': False, 'reused': 'completed', 'dedup_policy': policy,
                        'recipe': recipe_summary, 'pinned': True}
        effective_priority = 0 if priority is None else priority
        payload = job_payload(record, policy)
        cur = db.execute('INSERT INTO jobs(kind,artifact_id,priority,payload_json) VALUES (?,?,?,?)',
                         (job_kind, artifact_id, effective_priority,
                          json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)))
        job_id = int(cur.lastrowid)
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('job_enqueued','job',?,?)",
                   (job_id, json.dumps({'artifact_id': artifact_id, 'kind': job_kind, 'priority': effective_priority,
                                        'recipe_id': record['id'], 'recipe_sha256': record['sha256'],
                                        'dedup_policy': policy})))
        return {'job_id': job_id, 'created': True, 'reused': None, 'dedup_policy': policy,
                'recipe': recipe_summary, 'pinned': True}

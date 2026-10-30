"""HTTP routes owned by the media area: recipes, jobs, comparison, speakers, OCR candidates."""
from __future__ import annotations

import json
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..db import session
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/media', 'Transkrypcje'), ('/media/queue', 'Kolejka')]
MAX_SQL_INTEGER = 2**63 - 1
SqlId = Annotated[int, PathParameter(ge=1, le=MAX_SQL_INTEGER)]
StrictPriority = Annotated[StrictInt, Field(ge=-MAX_SQL_INTEGER - 1, le=MAX_SQL_INTEGER)]


class JobIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['transcribe', 'ocr', 'diarize'] = 'transcribe'
    recipe: str | None = None
    overrides: dict | None = None
    priority: StrictPriority | None = None
    dedup: Literal['reuse', 'reuse_completed', 'duplicate'] | None = None


class PriorityIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    priority: StrictPriority


class SpeakerIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    speaker: str
    derivedTextId: StrictInt | None = None
    segmentIndex: StrictInt | None = None
    diarizationRunId: StrictInt | None = None
    speakerLabel: str | None = None
    note: str | None = None


def _unprocessable(error: Exception) -> HTTPException:
    return HTTPException(422, str(error))


def _artifact(artifact_id: int) -> dict:
    with session() as db:
        row = db.execute('SELECT id,original_name,mime_type,size_bytes,sha256 FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'artifact not found')
    return dict(row)


def _recipes(kind: str | None = None) -> list[dict]:
    from ..media.recipe import effective_recipe, load_preset, preset_ids
    items = []
    for recipe_id in preset_ids(kind):
        preset_kind = load_preset(recipe_id)[0]['kind']
        record = effective_recipe(recipe_id, kind=preset_kind)
        items.append({'id': record['id'], 'kind': preset_kind, 'sha256': record['sha256'],
                      'effective': record['effective'], 'provenance': record['provenance']})
    return items


def _enqueue(artifact_id: int, body: JobIn) -> dict:
    """HTTP enqueue: only decode/strategy overrides (``recipe.CLIENT_OVERRIDE_KEYS``).

    Model, engine, device, compute type, model options, diarization pipeline and
    limits are operator decisions (recipe presets or the CLI); review finding 3.
    """
    from ..media.queue import enqueue_media_job
    from ..media.recipe import RECIPE_KIND_FOR_JOB, RecipeError, check_client_overrides
    _artifact(artifact_id)
    try:
        check_client_overrides(RECIPE_KIND_FOR_JOB[body.kind], body.overrides)
    except RecipeError as error:
        raise _unprocessable(error) from error
    try:
        return enqueue_media_job(body.kind, artifact_id, recipe_id=body.recipe, overrides=body.overrides,
                                 priority=body.priority, dedup=body.dedup)
    except ValueError as error:
        raise _unprocessable(error) from error


def _compare(a: int, b: int, normalize: bool) -> dict:
    from ..media.comparison import compare_versions
    try:
        return compare_versions(a, b, normalize=normalize)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise _unprocessable(error) from error


# ----------------------------------------------------------------------------- JSON API

@router.get('/api/media/capabilities')
def api_capabilities():
    from ..media.capabilities import media_capabilities
    return media_capabilities()


@router.get('/api/media/recipes')
def api_recipes(kind: Literal['asr', 'ocr', 'diarization'] | None = None):
    return {'recipes': _recipes(kind)}


@router.post('/api/media/artifacts/{artifact_id}/jobs')
def api_enqueue(artifact_id: SqlId, body: JobIn):
    return _enqueue(artifact_id, body)


@router.get('/api/media/queue')
def api_queue(status: Annotated[list[Literal['queued', 'running', 'done', 'failed', 'cancelled']] | None, Query()] = None,
              limit: int = Query(100, ge=1, le=1000)):
    from ..media.estimates import queue_listing
    return queue_listing(statuses=tuple(status) if status else ('running', 'queued'), limit=limit)


@router.post('/api/media/jobs/{job_id}/priority')
def api_job_priority(job_id: SqlId, body: PriorityIn):
    from ..media.estimates import set_job_priority
    try:
        return set_job_priority(job_id, body.priority)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post('/api/media/jobs/{job_id}/cancel')
def api_job_cancel(job_id: SqlId):
    from ..media.estimates import cancel_job
    try:
        return cancel_job(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.get('/api/media/artifacts/{artifact_id}/versions')
def api_versions(artifact_id: SqlId):
    from ..media.comparison import artifact_versions
    _artifact(artifact_id)
    return {'artifact_id': artifact_id, 'versions': artifact_versions(artifact_id)}


@router.get('/api/media/compare')
def api_compare(a: int = Query(..., ge=1, le=MAX_SQL_INTEGER), b: int = Query(..., ge=1, le=MAX_SQL_INTEGER),
                normalize: bool = False):
    return _compare(a, b, normalize)


@router.get('/api/media/runs/{run_id}/recipe')
def api_run_recipe(run_id: SqlId):
    from ..media.recipe import verify_run_recipe
    with session() as db:
        row = db.execute('SELECT id,status,provenance_json FROM processing_runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'run not found')
    try:
        provenance = json.loads(row['provenance_json'])
    except (TypeError, ValueError):
        provenance = {}
    recipe = provenance.get('recipe') if isinstance(provenance, dict) else None
    return {'run_id': run_id, 'run_status': row['status'], 'verification': verify_run_recipe(provenance),
            'recipe': recipe if isinstance(recipe, dict) else 'unknown'}


@router.get('/api/media/artifacts/{artifact_id}/speakers')
def api_speakers(artifact_id: SqlId, derived_text_id: int = Query(..., ge=1, le=MAX_SQL_INTEGER),
                 diarization_run_id: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER)):
    from ..media.diarization import speaker_view
    _artifact(artifact_id)
    try:
        return speaker_view(artifact_id, derived_text_id, diarization_run_id)
    except ValueError as error:
        raise _unprocessable(error) from error


@router.post('/api/media/artifacts/{artifact_id}/speaker-assignments')
def api_speaker_assignment(artifact_id: SqlId, body: SpeakerIn):
    from ..media.diarization import add_speaker_assignment
    _artifact(artifact_id)
    try:
        return add_speaker_assignment(artifact_id, speaker=body.speaker, derived_text_id=body.derivedTextId,
                                      segment_index=body.segmentIndex, diarization_run_id=body.diarizationRunId,
                                      speaker_label=body.speakerLabel, note=body.note)
    except ValueError as error:
        raise _unprocessable(error) from error


@router.get('/api/media/artifacts/{artifact_id}/event-candidates')
def api_event_candidates(artifact_id: SqlId, limit: int = Query(500, ge=1, le=5000)):
    _artifact(artifact_id)
    with session() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM event_candidates WHERE artifact_id=? ORDER BY id LIMIT ?',
                                            (artifact_id, limit))]
    for row in rows:
        row['locators'] = json.loads(row.pop('locators_json'))
        row['metadata'] = json.loads(row.pop('metadata_json'))
    return {'artifact_id': artifact_id, 'candidates': rows,
            'note': 'OCR layout candidates; not events and not promoted automatically'}


# ----------------------------------------------------------------------------- HTML

@router.get('/media', response_class=HTMLResponse)
def media_index(request: Request):
    with session() as db:
        artifacts = [dict(r) for r in db.execute(
            """SELECT a.id,a.original_name,a.mime_type,
                      (SELECT count(*) FROM derived_text d WHERE d.artifact_id=a.id AND d.kind='transcript') transcripts,
                      (SELECT count(*) FROM derived_text d WHERE d.artifact_id=a.id AND d.kind='ocr') ocr,
                      (SELECT count(*) FROM jobs j WHERE j.artifact_id=a.id AND j.status IN ('queued','running')) active_jobs
               FROM artifacts a
               WHERE a.mime_type LIKE 'audio/%' OR a.mime_type LIKE 'video/%' OR a.mime_type LIKE 'image/%'
                     OR a.mime_type='application/pdf' OR EXISTS(SELECT 1 FROM derived_text d WHERE d.artifact_id=a.id AND d.kind IN ('transcript','ocr'))
               ORDER BY a.id DESC LIMIT 501""")]
    from ..media.capabilities import media_capabilities
    return templates.TemplateResponse(request, 'media_index.html', {
        'artifacts': artifacts[:500], 'more': len(artifacts) > 500, 'capabilities': media_capabilities(),
        'recipes': _recipes()})


@router.get('/media/artifact/{artifact_id}', response_class=HTMLResponse)
def media_artifact(request: Request, artifact_id: SqlId, message: str = ''):
    from ..media.comparison import artifact_versions
    artifact = _artifact(artifact_id)
    versions = artifact_versions(artifact_id)
    with session() as db:
        jobs = [dict(r) for r in db.execute('SELECT id,kind,status,priority,attempts,payload_json,error,created_at FROM jobs '
                                            'WHERE artifact_id=? ORDER BY id DESC LIMIT 50', (artifact_id,))]
        candidates = [dict(r) for r in db.execute('SELECT * FROM event_candidates WHERE artifact_id=? ORDER BY id LIMIT 200', (artifact_id,))]
        diarization_runs = [dict(r) for r in db.execute(
            "SELECT run_id, count(*) turns, count(DISTINCT speaker_label) speakers FROM speaker_turns WHERE artifact_id=? GROUP BY run_id ORDER BY run_id DESC",
            (artifact_id,))]
    for job in jobs:
        try:
            payload = json.loads(job.pop('payload_json') or '{}')
            job['recipe'] = (payload.get('recipe') or {}).get('id', 'legacy (unpinned)') if payload else 'legacy (unpinned)'
        except (TypeError, ValueError, AttributeError):
            job['recipe'] = 'invalid payload'
        job['error'] = (job['error'] or '').splitlines()[0][:300] if job['error'] else ''
    return templates.TemplateResponse(request, 'media_artifact.html', {
        'a': artifact, 'versions': versions, 'jobs': jobs, 'candidates': candidates, 'message': message[:300],
        'diarization_runs': diarization_runs, 'recipes': _recipes()})


@router.post('/media/artifact/{artifact_id}/jobs')
def media_enqueue_form(artifact_id: SqlId, kind: str = Form('transcribe'), recipe: str = Form(''),
                       priority: str = Form(''), dedup: str = Form('')):
    try:
        body = JobIn(kind=kind, recipe=recipe or None, priority=int(priority) if priority.strip() else None,
                     dedup=dedup or None)
    except ValueError as error:
        raise _unprocessable(error) from error
    result = _enqueue(artifact_id, body)
    state = 'utworzono' if result['created'] else f'użyto istniejącego ({result["reused"]})'
    return RedirectResponse(f'/media/artifact/{artifact_id}?message=' + quote(f'Zadanie #{result["job_id"]}: {state}'),
                            status_code=303)


@router.get('/media/compare', response_class=HTMLResponse)
def media_compare(request: Request, a: int = Query(..., ge=1, le=MAX_SQL_INTEGER),
                  b: int = Query(..., ge=1, le=MAX_SQL_INTEGER), normalize: bool = False):
    comparison = _compare(a, b, normalize)
    return templates.TemplateResponse(request, 'media_compare.html', {'c': comparison, 'normalize': normalize})


@router.get('/media/queue', response_class=HTMLResponse)
def media_queue(request: Request):
    from ..media.estimates import queue_listing
    return templates.TemplateResponse(request, 'media_queue.html', {'q': queue_listing()})


@router.post('/media/jobs/{job_id}/priority')
def media_priority_form(job_id: SqlId, priority: str = Form(...)):
    from ..media.estimates import set_job_priority
    try:
        set_job_priority(job_id, int(priority))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return RedirectResponse('/media/queue', status_code=303)


@router.post('/media/jobs/{job_id}/cancel')
def media_cancel_form(job_id: SqlId):
    api_job_cancel(job_id)
    return RedirectResponse('/media/queue', status_code=303)


@router.get('/media/artifact/{artifact_id}/speakers', response_class=HTMLResponse)
def media_speakers(request: Request, artifact_id: SqlId, derived_text_id: int = Query(..., ge=1, le=MAX_SQL_INTEGER),
                   diarization_run_id: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER)):
    view = api_speakers(artifact_id, derived_text_id, diarization_run_id)
    return templates.TemplateResponse(request, 'media_speakers.html', {'a': _artifact(artifact_id), 'v': view})


@router.post('/media/artifact/{artifact_id}/speakers')
def media_speaker_form(artifact_id: SqlId, speaker: str = Form(...), derived_text_id: int = Form(...),
                       segment_index: str = Form(''), diarization_run_id: str = Form(''), speaker_label: str = Form('')):
    from ..media.diarization import add_speaker_assignment
    try:
        if segment_index.strip():
            add_speaker_assignment(artifact_id, speaker=speaker, derived_text_id=derived_text_id,
                                   segment_index=int(segment_index))
        else:
            add_speaker_assignment(artifact_id, speaker=speaker, diarization_run_id=int(diarization_run_id),
                                   speaker_label=speaker_label or None)
    except ValueError as error:
        raise _unprocessable(error) from error
    return RedirectResponse(f'/media/artifact/{artifact_id}/speakers?derived_text_id={derived_text_id}', status_code=303)

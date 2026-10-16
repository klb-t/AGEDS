"""HTTP routes of the import planner (AGEDS-TASK-20261016-I).

JSON API under ``/api/import-plans`` and the review page ``/import``. Plan
generation reads catalogued sources with bounded probes and is synchronous;
``mode='sync'`` runs block until the approved items are imported (use
``mode='queue'`` plus a worker for long runs). The catalog follow-up
``POST /api/catalog/scans/{id}/abandon`` closes a scan left ``running`` by a
dead process. API authentication is not implemented (see HANDOFF): run the
service on a trusted/private network.
"""
from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ..catalog import store
from ..evidence import IntegrityError
from ..importplan import abandon, cross, execute, plan
from ..importplan.adapters import IMPORTERS
from ..importplan.detect import LIMIT_NAMES as PLAN_LIMITS, PlanLimits
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/import', 'Import')]
Id = Annotated[int, Query(ge=1, le=2**63 - 1)]
BodyId = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
Actor = Annotated[str, Field(min_length=1, max_length=200)]


class PlanOptionsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    billingProfileId: str | None = Field(default=None, max_length=200)


class PlanIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    collectionIds: list[BodyId] = Field(default_factory=list, max_length=500)
    scanIds: list[BodyId] = Field(default_factory=list, max_length=5000)
    options: PlanOptionsIn = Field(default_factory=PlanOptionsIn)
    limits: dict[str, StrictInt] = Field(default_factory=dict)
    actor: Actor = 'web'


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    itemId: BodyId
    status: Literal['proposed', 'approved', 'skipped'] | None = None
    importer: str | None = Field(default=None, max_length=100)
    parameters: dict[str, str | StrictInt | None] = Field(default_factory=dict)
    optIns: dict[str, StrictBool] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=2000)


class BulkFilterIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    importer: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=30)
    ambiguous: StrictBool = False
    collectionId: BodyId | None = None
    q: str | None = Field(default=None, max_length=500)


class BulkIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['approve', 'skip', 'propose']
    itemIds: list[BodyId] | None = Field(default=None, max_length=100_000)
    filter: BulkFilterIn = Field(default_factory=BulkFilterIn)
    reason: str | None = Field(default=None, max_length=2000)


class DecisionsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    baseVersion: StrictInt = Field(ge=1)
    actor: Actor
    note: str | None = Field(default=None, max_length=2000)
    decisions: list[DecisionIn] = Field(default_factory=list, max_length=100_000)
    bulk: BulkIn | None = None


class RunIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: StrictInt | None = Field(default=None, ge=1)
    mode: Literal['sync', 'queue'] = 'sync'
    recoverRunning: StrictBool = False
    rerunDone: StrictBool = False
    itemIds: list[BodyId] | None = Field(default=None, max_length=100_000)
    actor: Actor = 'web'


class AbandonIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actor: Actor
    reason: str = Field(min_length=1, max_length=2000)
    minIdleSeconds: StrictInt = Field(default=abandon.DEFAULT_MIN_IDLE_SECONDS, ge=0, le=10 ** 9)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except store.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except plan.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (IntegrityError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/api/import-plans')
def api_plans(limit: int = Query(50, ge=1, le=plan.MAX_PAGE)):
    return {'plans': plan.list_plans(limit), 'importers': [i.describe() for i in IMPORTERS.values()]}


@router.post('/api/import-plans')
def api_plan_create(body: PlanIn):
    unknown = set(body.limits) - set(PLAN_LIMITS)
    if unknown:
        raise HTTPException(422, f'unknown limits: {sorted(unknown)}')
    limits = _call(PlanLimits, **body.limits)
    options = {'billing_profile_id': body.options.billingProfileId} if body.options.billingProfileId else {}
    return _call(plan.create_plan, collection_ids=list(body.collectionIds), scan_ids=list(body.scanIds),
                 options=options, limits=limits, actor=body.actor)


def _filters(importer, status, ambiguous, blocked, collectionId, q) -> dict:
    return {'importer': importer or None, 'status': status or None, 'ambiguous': ambiguous, 'blocked': blocked,
            'collection_id': collectionId, 'q': q or None}


@router.get('/api/import-plans/{plan_id}')
def api_plan(plan_id: int, version: Id | None = None, importer: str | None = Query(None, max_length=100),
             status: str | None = Query(None, max_length=30), ambiguous: bool = False, blocked: bool = False,
             collectionId: Id | None = None, q: str | None = Query(None, max_length=500),
             afterOrdinal: int | None = Query(None, ge=0), limit: int = Query(200, ge=1, le=plan.MAX_PAGE),
             full: bool = False):
    return _call(plan.get_plan, plan_id, version=version, filters=_filters(importer, status, ambiguous, blocked, collectionId, q),
                 after_ordinal=afterOrdinal, limit=limit, full=full)


@router.get('/api/import-plans/{plan_id}/summary')
def api_summary(plan_id: int, version: Id | None = None):
    return _call(plan.summary, plan_id, version)


@router.get('/api/import-plans/{plan_id}/items/{item_id}')
def api_item(plan_id: int, item_id: int, version: Id | None = None):
    return _call(plan.get_item, plan_id, item_id, version=version)


@router.post('/api/import-plans/{plan_id}/decisions')
def api_decide(plan_id: int, body: DecisionsIn):
    decisions = []
    for d in body.decisions:
        decision = {'item_id': d.itemId}
        for field in d.model_fields_set:
            if field == 'status':
                decision['status'] = d.status
            elif field == 'importer':
                decision['importer'] = d.importer
            elif field == 'parameters':
                decision['parameters'] = d.parameters
            elif field == 'optIns':
                decision['opt_ins'] = d.optIns
            elif field == 'reason':
                decision['reason'] = d.reason
        decisions.append(decision)
    bulk = None
    if body.bulk is not None:
        f = body.bulk.filter
        bulk = {'action': body.bulk.action, 'reason': body.bulk.reason,
                'filter': {'importer': f.importer, 'status': f.status, 'ambiguous': f.ambiguous,
                           'collection_id': f.collectionId, 'q': f.q},
                'item_ids': list(body.bulk.itemIds) if body.bulk.itemIds is not None else None}
    return _call(plan.decide, plan_id, base_version=body.baseVersion, decisions=decisions, bulk=bulk,
                 actor=body.actor, note=body.note)


@router.post('/api/import-plans/{plan_id}/runs')
def api_run(plan_id: int, body: RunIn):
    return _call(execute.execute_plan, plan_id, version=body.version, mode=body.mode, actor=body.actor,
                 recover_running=body.recoverRunning, rerun_done=body.rerunDone,
                 item_ids=list(body.itemIds) if body.itemIds is not None else None)


@router.get('/api/import-plans/{plan_id}/runs')
def api_runs(plan_id: int, limit: int = Query(50, ge=1, le=500)):
    return {'runs': _call(execute.list_runs, plan_id, limit)}


@router.get('/api/import-plan-runs/{run_id}')
def api_run_detail(run_id: int):
    return _call(execute.get_run, run_id)


@router.get('/api/import-plans/{plan_id}/cross-exports')
def api_cross(plan_id: int, limit: int = Query(500, ge=1, le=5000)):
    header = _call(plan.get_plan, plan_id, limit=1)['plan']
    return {'before_import': header['cross_export'], 'after_import': cross.post_import(plan_id, limit=limit)}


@router.post('/api/catalog/scans/{scan_id}/abandon')
def api_abandon(scan_id: int, body: AbandonIn):
    return _call(abandon.abandon_scan, scan_id, actor=body.actor, reason=body.reason, min_idle_seconds=body.minIdleSeconds)


# ------------------------------------------------------------------- page

STATUS_LABELS = {'proposed': 'zaproponowany', 'approved': 'zatwierdzony', 'skipped': 'pominięty', 'running': 'w toku',
                 'done': 'zaimportowany', 'failed': 'błąd', 'refused': 'odmowa'}


def _billing_profiles() -> list[str]:
    from ..importers.billing import load_profiles
    try:
        return [p['id'] for p in load_profiles()['profiles']]
    except (OSError, ValueError):
        return []


@router.get('/import', response_class=HTMLResponse)
def import_page(request: Request, plan_id: Id | None = Query(None, alias='plan'), version: Id | None = None,
                importer: str | None = Query(None, max_length=100), status: str | None = Query(None, max_length=30),
                ambiguous: bool = False, blocked: bool = False, collection: Id | None = None,
                q: str | None = Query(None, max_length=500), after: int | None = Query(None, ge=0),
                limit: int = Query(200, ge=1, le=plan.MAX_PAGE)):
    if status is not None and status not in plan.DISPLAY_STATUSES:
        status = None
    collections = store.list_collections()
    selected = None
    post = None
    if plan_id is not None:
        selected = _call(plan.get_plan, plan_id, version=version,
                         filters=_filters(importer, status, ambiguous, blocked, collection, q), after_ordinal=after,
                         limit=limit)
        post = cross.post_import(plan_id, limit=50)
    base = {'plan': plan_id, 'version': version, 'importer': importer, 'status': status,
            'ambiguous': 'true' if ambiguous else None, 'blocked': 'true' if blocked else None,
            'collection': collection, 'q': q, 'limit': limit if limit != 200 else None}

    def query(**overrides) -> str:
        values = {**base, **overrides}
        return '/import?' + urlencode({k: v for k, v in values.items() if v not in (None, '', False)})

    labels = {c['id']: c['label'] for c in collections}
    context = {
        'collections': collections, 'labels': labels, 'plans': plan.list_plans(20), 'selected': selected, 'post': post,
        'importers': IMPORTERS, 'statuses': plan.DISPLAY_STATUSES, 'status_labels': STATUS_LABELS,
        'billing_profiles': _billing_profiles(),
        'filters': {'importer': importer or '', 'status': status or '', 'ambiguous': ambiguous, 'blocked': blocked,
                    'collection': collection, 'q': q or '', 'limit': limit},
        'query': query,
        'next_url': query(after=selected['next_after_ordinal']) if selected and selected['has_more'] else None,
    }
    return templates.TemplateResponse(request, 'importplan.html', context)

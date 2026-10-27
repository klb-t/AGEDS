"""HTTP routes of the hypothesis analyzer (interpretive layer): JSON API and the /hypotheses page.

There is no authentication in this service (see HANDOFF); ``actor`` is a
self-declared label stored with each append-only record.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ..analysis import dossier as dossier_mod
from ..analysis import eventstudy, preregistration, theses
from ..analysis.hypotheses import DISCLAIMER, HypothesisError, HypothesisNotFound, disclaimer
from ..db import connect
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/hypotheses', 'Hipotezy')]
MAX_SQL_INTEGER = 2**63 - 1
SqlId = Annotated[int, PathParameter(ge=1, le=MAX_SQL_INTEGER)]
PositiveId = Annotated[StrictInt, Field(ge=1, le=MAX_SQL_INTEGER)]


class DefinitionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    definition: dict[str, Any]
    actor: str | None = None
    change_note: str | None = None


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    definition: dict[str, Any]


class EventStudyIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    overrides: dict[str, Any] | None = None
    category_config: dict[str, Any] | None = None  # inline private configuration; never a server path
    actor: str | None = None


class ItemIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_kind: Literal['event', 'artifact', 'derived_text', 'anchor', 'annotation']
    target_id: PositiveId
    observation_id: PositiveId | None = None
    ledger_entry_id: PositiveId | None = None
    health_sample_id: PositiveId | None = None
    classification: Literal['candidate', 'supporting', 'contradicting', 'context', 'rejected'] | None = None
    rationale: str | None = None
    version_id: PositiveId | None = None
    note: str | None = None
    actor: str | None = None


class ClassifyIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    classification: Literal['candidate', 'supporting', 'contradicting', 'context', 'rejected']
    rationale: str | None = None
    version_id: PositiveId | None = None
    actor: str | None = None


class CandidatesIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    queries: list[str] | None = None
    use_rules: StrictBool = True
    provider: str = 'none'
    opt_in: StrictBool = False
    limit: Annotated[StrictInt, Field(ge=1, le=dossier_mod.MAX_CANDIDATES)] = 50
    version_id: PositiveId | None = None
    category_config: dict[str, Any] | None = None
    direction: Literal['definition', 'disconfirming', 'both'] | None = None
    actor: str | None = None


class ThesisIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str
    status_type: Literal[theses.STATUS_TYPES]  # type: ignore[valid-type]
    evidence_item_ids: list[PositiveId] = []
    previous_thesis_id: PositiveId | None = None
    actor: str | None = None


class DisclosureIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    quadrant: Literal[theses.QUADRANT_IDS]  # type: ignore[valid-type]
    text: str
    actor: str | None = None


def _call(fn, *args, **kwargs):
    with closing(connect()) as db:
        try:
            return fn(db, *args, **kwargs)
        except HypothesisNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except HypothesisError as exc:
            raise HTTPException(422, str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            if type(exc).__name__ != 'ProjectionNotFresh':
                raise
            raise HTTPException(503, f'{exc}; retry (review 2 finding 4: the projection was evicted concurrently)') from exc


def _detail(db: sqlite3.Connection, hypothesis_id: int) -> dict:
    return {'disclaimer': disclaimer(), 'hypothesis_id': hypothesis_id,
            'versions': preregistration.list_versions(db, hypothesis_id),
            'runs': eventstudy.list_runs(db, hypothesis_id),
            'dossier': dossier_mod.list_dossier(db, hypothesis_id),
            'theses': theses.list_theses(db, hypothesis_id),
            'disclosure': theses.disclosure_view(db, hypothesis_id)}


@router.get('/api/hypotheses')
def api_list(limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)):
    return {'disclaimer': disclaimer(), **_call(preregistration.list_hypotheses, limit=limit, offset=offset)}


@router.post('/api/hypotheses/preview')
def api_preview(body: PreviewIn):
    try:
        return {'disclaimer': disclaimer(), **preregistration.preview(body.definition)}
    except HypothesisError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/api/hypotheses')
def api_register(body: DefinitionIn):
    return {'disclaimer': disclaimer(), **_call(preregistration.register_hypothesis, body.definition, actor=body.actor,
                                               change_note=body.change_note)}


@router.get('/api/hypotheses/versions/{version_id}')
def api_version(version_id: SqlId):
    return {'disclaimer': disclaimer(), **_call(preregistration.get_version, version_id)}


@router.post('/api/hypotheses/versions/{version_id}/event-study')
def api_event_study(version_id: SqlId, body: EventStudyIn):
    return _call(eventstudy.run_event_study, version_id, overrides=body.overrides, actor=body.actor,
                 category_config=body.category_config)


@router.get('/api/hypotheses/runs/{run_id}')
def api_run(run_id: SqlId):
    return _call(eventstudy.get_run, run_id)


@router.post('/api/hypotheses/dossier/{item_id}/classify')
def api_classify(item_id: SqlId, body: ClassifyIn):
    return {'disclaimer': disclaimer(), **_call(dossier_mod.classify, item_id, body.classification, rationale=body.rationale,
                                               version_id=body.version_id, actor=body.actor)}


@router.get('/api/hypotheses/{hypothesis_id}')
def api_detail(hypothesis_id: SqlId):
    return _call(_detail, hypothesis_id)


@router.post('/api/hypotheses/{hypothesis_id}/versions')
def api_new_version(hypothesis_id: SqlId, body: DefinitionIn):
    return {'disclaimer': disclaimer(), **_call(preregistration.register_version, hypothesis_id, body.definition,
                                               actor=body.actor, change_note=body.change_note)}


@router.post('/api/hypotheses/{hypothesis_id}/dossier')
def api_add_item(hypothesis_id: SqlId, body: ItemIn):
    return {'disclaimer': disclaimer(), **_call(dossier_mod.add_item, hypothesis_id, body.target_kind, body.target_id,
                                               observation_id=body.observation_id, classification=body.classification,
                                               rationale=body.rationale, version_id=body.version_id, note=body.note,
                                               actor=body.actor, ledger_entry_id=body.ledger_entry_id,
                                               health_sample_id=body.health_sample_id)}


@router.post('/api/hypotheses/{hypothesis_id}/candidates')
def api_candidates(hypothesis_id: SqlId, body: CandidatesIn):
    return _call(dossier_mod.candidate_search, hypothesis_id, version_id=body.version_id, queries=body.queries,
                 use_rules=body.use_rules, provider=body.provider, opt_in=body.opt_in, limit=body.limit, actor=body.actor,
                 category_config=body.category_config, direction=body.direction)


@router.post('/api/hypotheses/{hypothesis_id}/theses')
def api_thesis(hypothesis_id: SqlId, body: ThesisIn):
    return {'disclaimer': disclaimer(), **_call(theses.add_thesis, hypothesis_id, body.text, body.status_type,
                                               list(body.evidence_item_ids), previous_thesis_id=body.previous_thesis_id,
                                               actor=body.actor)}


@router.post('/api/hypotheses/{hypothesis_id}/disclosure')
def api_disclosure(hypothesis_id: SqlId, body: DisclosureIn):
    return _call(theses.add_disclosure, hypothesis_id, body.quadrant, body.text, actor=body.actor)


EXAMPLE_DEFINITION = {
    'title': 'Pismo urzędu → wpisy zdrowotne',
    'text': 'Po zdarzeniu typu „pismo” w ciągu 3 dni pojawia się więcej wpisów zdrowotnych niż przy losowym położeniu pism.',
    'stimulus': {'event_types': ['letter']},
    'outcome': {'event_types': ['health']},
    'windows': [{'label': 'przed 3 dni', 'start_s': -259200, 'end_s': 0},
                {'label': 'po 3 dni', 'start_s': 0, 'end_s': 259200}],
    'lags': {'min_s': -604800, 'max_s': 604800, 'bin_s': 43200},
    'direction': 'increase',
    'analysis': {'permutations': 999, 'seed': 20261016, 'null_model': 'circular_shift'},
    'dossier': {'queries': ['wizyta']},
}


@router.get('/hypotheses', response_class=HTMLResponse)
def page(request: Request, id: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER),
         run: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER)):
    with closing(connect()) as db:
        listing = preregistration.list_hypotheses(db, limit=1000)
        selected = None
        run_view = None
        try:
            if id is None and listing['hypotheses']:
                id = listing['hypotheses'][-1]['hypothesis_id']
            if id is not None:
                selected = _detail(db, id)
                studies = [r for r in selected['runs'] if r['kind'] == 'event_study']
                wanted = run if run is not None else (studies[-1]['id'] if studies else None)
                if wanted is not None:
                    run_view = eventstudy.get_run(db, wanted)
                    if run_view['kind'] != 'event_study' or run_view['hypothesis_version_id'] not in \
                            {v['version_id'] for v in selected['versions']}:
                        raise HTTPException(404, 'run does not belong to this hypothesis')
        except HypothesisNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
    return templates.TemplateResponse(request, 'hypotheses.html', {
        'listing': listing['hypotheses'], 'selected': selected, 'run': run_view, 'disclaimer': DISCLAIMER,
        'example': EXAMPLE_DEFINITION, 'typology': theses.TYPOLOGY, 'quadrants': theses.QUADRANTS,
        'classifications': dossier_mod.CLASSIFICATIONS, 'null_models': preregistration.NULL_MODELS,
        'providers': sorted(dossier_mod.PROVIDERS),
    })

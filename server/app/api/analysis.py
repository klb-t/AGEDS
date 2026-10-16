"""HTTP routes owned by the analysis area: time budget, feature series, labels.

The category configuration is private user data. Over HTTP it comes either
inline in the request body (``config``) or from the operator's
``AGEDS_ACTIVITY_CATEGORIES`` file; a filesystem path is never accepted from a
request (the API is unauthenticated, see HANDOFF). Every result is an
interpretive artifact stored append-only.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()
NAV: list[tuple[str, str]] = [('/analysis/budget', 'Budżet czasu')]
RunId = Annotated[int, PathParameter(ge=1, le=2 ** 63 - 1)]
MAX_CANDLE_EVENTS_PAGE = 500


class _Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class WindowRequest(_Body):
    window_from: str = Field(alias='from')
    window_to: str = Field(alias='to')
    zone: str
    params: dict[str, Any] | None = None
    assume_zone: str | None = None
    assumption_rule: str | None = None
    config: dict[str, Any] | None = None


class CorrectionRequest(_Body):
    event_id: int = Field(ge=1, le=2 ** 63 - 1)
    categories: dict[str, float] | None = None
    note: str = ''
    author: str = 'unknown'


class ModelLabelRequest(WindowRequest):
    provider: str = 'none'
    opt_in: bool = False


def _config(inline: dict | None, *, required: bool = True):
    from ..analysis import categories
    if inline is not None:
        return categories.parse_config(inline, origin='inline_http')
    path = categories.resolve_config_path()
    if path is None:
        if required:
            raise HTTPException(422, f'no category configuration: send "config" inline or set {categories.ENV_VAR} '
                                     'for the server (paths are not accepted over HTTP)')
        return None
    return categories.load_config(path)


def _call(fn):
    try:
        return fn()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(422, f'category configuration unreadable: {exc}') from exc


def _session():
    from ..db import session
    return session()


@router.get('/analysis/budget', response_class=HTMLResponse)
def budget_page(request: Request):
    from ..web import templates
    return templates.TemplateResponse(request, 'analysis_budget.html', {})


@router.get('/api/analysis/config')
def config_info():
    from ..analysis import categories
    config = _call(lambda: _config(None, required=False))
    if config is None:
        return {'configured': False, 'env_var': categories.ENV_VAR}
    return {'configured': True, 'origin': config.origin, 'sha256': config.sha256, 'version': config.version,
            'categories': categories.category_palette(config), 'rules': config.describe()['rules']}


@router.get('/api/analysis/defaults')
def defaults():
    from ..analysis import budget, features
    return {'budget': budget.DEFAULTS, 'features': features.DEFAULTS, 'grid_keys': list(budget.GRID_KEYS),
            'overlap_rules': list(budget.OVERLAP_RULES)}


@router.get('/api/analysis/runs')
def list_runs(kind: str | None = Query(None, pattern='^(time_budget|feature_series)$'),
              limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    from ..analysis import runs
    with _session() as db:
        return {'runs': runs.list_runs(db, kind=kind, limit=limit, offset=offset)}


@router.post('/api/analysis/budget/runs')
def create_budget_run(body: WindowRequest):
    from ..analysis import budget
    from ..commands.analysis import budget_summary

    def run():
        config = _config(body.config)
        with _session() as db:
            out = budget.run_budget(db, config, window_from=body.window_from, window_to=body.window_to, zone=body.zone,
                                    params=body.params, assume_zone=body.assume_zone,
                                    assumption_rule=body.assumption_rule)
        return budget_summary(out['run'], out['result'])
    return _call(run)


def _get(run_id: int, kind: str) -> dict:
    from ..analysis import runs
    with _session() as db:
        run = runs.get_run(db, run_id, kind=kind)
    if run is None:
        raise HTTPException(404, f'{kind} run {run_id} not found')
    return run


@router.get('/api/analysis/budget/runs/{run_id}')
def get_budget_run(run_id: RunId):
    return _get(run_id, 'time_budget')


@router.get('/api/analysis/budget/runs/{run_id}/candle')
def candle_events(run_id: RunId, granularity: str = Query(..., pattern='^(day|week|month)$'),
                  bucket: str = Query(..., max_length=32), category: str = Query(..., max_length=64),
                  offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=MAX_CANDLE_EVENTS_PAGE)):
    """Events that build one candle (stored ids, bounded), with their current label and links."""
    from ..analysis import categories
    from ..analysis.labels_view import event_summary
    from ..analysis.source import event_item
    result = _get(run_id, 'time_budget')['result']
    candles = result['candles'].get(granularity)
    if candles is None:
        raise HTTPException(404, f'granularity {granularity} not in this run')
    candle = next((c for c in candles if c['bucket'] == bucket), None)
    if candle is None:
        raise HTTPException(404, f'bucket {bucket} not in this run')
    if category == categories.UNOBSERVED:
        return {'run_id': run_id, 'granularity': granularity, 'bucket': bucket, 'category': category,
                'values': candle['unobserved'], 'events': [], 'event_count': 0,
                'note': 'nieobserwowane: no observation covers this time; there are no events to list and it is not idleness'}
    values = candle['categories'].get(category)
    if values is None:
        raise HTTPException(404, f'category {category} has no candle in bucket {bucket}')
    ids = values['event_ids'][offset:offset + limit]
    events = []
    with _session() as db:
        stored = {r[0]: r for r in db.execute(
            'SELECT event_id, path, rules, annotation_id, model_run_id, weight FROM analysis_event_labels '
            'WHERE label_run_id=? AND category=? AND event_id IN (%s)' % ','.join('?' * len(ids)),
            (result['labels']['label_run_id'], category, *ids))} if ids else {}
        for eid in ids:
            try:
                summary = event_summary(event_item(db, eid))
            except LookupError:
                summary = {'event_id': eid, 'missing': True}
            label = stored.get(eid)
            summary['label_in_run'] = {'category': category, 'weight': label[5], 'path': label[1], 'rules': label[2],
                                       'annotation_id': label[3], 'model_run_id': label[4]} if label else None
            summary['links']['timeline'] = f'/api/timeline/items/event:{eid}'
            events.append(summary)
    return {'run_id': run_id, 'granularity': granularity, 'bucket': bucket, 'category': category, 'values': values,
            'event_count': values['event_count'], 'stored_ids': len(values['event_ids']),
            'stored_ids_truncated': values['event_ids_truncated'], 'offset': offset, 'limit': limit, 'events': events}


@router.post('/api/analysis/features/runs')
def create_feature_run(body: WindowRequest):
    from ..analysis import features

    def run():
        config = _config(body.config)
        with _session() as db:
            out = features.run_features(db, config, window_from=body.window_from, window_to=body.window_to,
                                        zone=body.zone, params=body.params, assume_zone=body.assume_zone,
                                        assumption_rule=body.assumption_rule)
        return {'run': out['run'], 'series': [{'id': s['id'], 'kind': s['kind']} for s in out['result']['series']],
                'exclusions': out['result']['exclusions']}
    return _call(run)


@router.get('/api/analysis/features/runs/{run_id}')
def get_feature_run(run_id: RunId):
    return _get(run_id, 'feature_series')


@router.get('/api/analysis/events/{event_id}')
def event_detail(event_id: RunId):
    from ..analysis.labels_view import event_labels

    def run():
        config = _config(None, required=False)
        with _session() as db:
            return event_labels(db, event_id, config)
    return _call(run)


@router.post('/api/analysis/labels/corrections')
def add_correction(body: CorrectionRequest):
    from ..analysis import categories

    def run():
        with _session() as db:
            annotation_id = categories.add_annotation(db, body.event_id, body.categories, note=body.note,
                                                      author=body.author)
        return {'annotation_id': annotation_id, 'event_id': body.event_id, 'categories': body.categories or {},
                'path': 'annotation', 'append_only': True,
                'note': 'takes precedence in the next run; earlier runs keep their labels'}
    return _call(run)


@router.post('/api/analysis/model-label-runs')
def model_label_run(body: ModelLabelRequest):
    from ..analysis import budget, model_runs

    def run():
        config = _config(body.config)
        with _session() as db:
            prepared = budget.prepare_inputs(db, config, window_from=body.window_from, window_to=body.window_to,
                                             zone=body.zone, params=body.params, assume_zone=body.assume_zone,
                                             assumption_rule=body.assumption_rule, persist=False)
            return model_runs.run_model(db, prepared['items'], purpose='labels', provider=body.provider,
                                        opt_in=body.opt_in, config=config, parameters={'window': prepared['window']})
    return _call(run)

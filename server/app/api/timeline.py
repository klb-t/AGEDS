"""HTTP routes owned by the timeline area (projection, windows, density, correlations, page)."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..db import session
from ..timeline.correlation import (CorrelationError, TransitionError, hypothesis_detail, list_hypotheses, list_runs,
                                    review, run_detail, run_rule)
from ..timeline.projection import ensure_projection
from ..timeline.query import (Filters, QueryInputError, QueryLimitError, density, export_window, facets, item_detail,
                              legacy_events, make_filters, undated, window)
from ..timeline.ruleset import RuleError, display_params, load_rule, released_rules
from ..timeline.timeutil import TimeInputError, parse_bound
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/timeline', 'Oś czasu')]
MAX_SQL_INTEGER = 2**63 - 1
SqlId = Annotated[int, PathParameter(ge=1, le=MAX_SQL_INTEGER)]


def _errors(fn):
    try:
        return fn()
    except (RuleError, QueryInputError, TimeInputError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except QueryLimitError as exc:
        raise HTTPException(413, str(exc)) from exc
    except TransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except CorrelationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise HTTPException(503, 'Evidence database is busy or unavailable for the timeline projection.') from exc


class ProjectionParams:
    def __init__(self, rule: str | None = Query(None, max_length=80, description='display rule, e.g. display_time@1'),
                 assume_zone: str | None = Query(None, max_length=64),
                 assumption_rule: str | None = Query(None, max_length=200)):
        self.rule_ref, self.assume_zone, self.assumption_rule = rule, assume_zone, assumption_rule

    def open(self, db):
        rule = load_rule(self.rule_ref, kind='display_rule')
        params = display_params(self.assume_zone, self.assumption_rule)
        return rule, ensure_projection(db, rule, params)


class FilterParams:
    def __init__(self, event_type: list[str] | None = Query(None), kind: list[str] | None = Query(None),
                 source: list[str] | None = Query(None), collection: list[str] | None = Query(None),
                 counterparty: list[str] | None = Query(None), chatbot: list[str] | None = Query(None),
                 thread: list[str] | None = Query(None), tag: list[str] | None = Query(None),
                 q: str | None = Query(None, max_length=4096),
                 transcript_versions: str = Query('latest', max_length=200)):
        self.kwargs = dict(event_types=event_type, kinds=kind, sources=source, collections=collection,
                           counterparties=counterparty, chatbots=chatbot, threads=thread, tags=tag, q=q,
                           transcript_versions=transcript_versions)

    def build(self) -> Filters:
        return make_filters(**self.kwargs)


class WindowParams:
    def __init__(self, from_: str | None = Query(None, alias='from', max_length=40), to: str | None = Query(None, max_length=40),
                 match: Literal['display', 'overlap'] = 'display', order: Literal['asc', 'desc'] = 'asc'):
        self.from_, self.to, self.match, self.order = from_, to, match, order

    def bounds(self):
        start, end = parse_bound(self.from_), parse_bound(self.to, end=True)
        if start is not None and end is not None and end <= start:
            raise QueryInputError('to must be later than from')
        return start, end


@router.get('/api/timeline')
def timeline(limit: int = Query(500, ge=1, le=10000), phone: str | None = None, event_type: str | None = None):
    """Compatibility list of event rows (unchanged shape).

    Ordering: newest projected display time first (rule display_time, no assumed
    zone); events without a usable time follow, ordered by event id. Import time
    (events.created_at) is never used as a time.
    """
    def run():
        with session() as db:
            rule = load_rule(kind='display_rule')
            projection = ensure_projection(db, rule, {})
            rows = legacy_events(db, projection, limit=limit, phone=phone, event_type=event_type)
            return rows, projection
    rows, projection = _errors(run)
    return JSONResponse(rows, headers={
        'X-AGEDS-Timeline-Rule': f"{projection['rule']['rule_id']}@{projection['rule']['version']}",
        'X-AGEDS-Timeline-Rule-SHA256': projection['rule']['sha256'],
        'X-AGEDS-Timeline-Order': 'projected-time-desc; undated-last-by-event-id',
    })


@router.get('/api/timeline/rules')
def timeline_rules():
    return _errors(lambda: [{**r.describe(), 'data': r.data} for r in released_rules()])


@router.get('/api/timeline/projection')
def timeline_projection(p: ProjectionParams = Depends()):
    def run():
        with session() as db:
            return p.open(db)[1]
    return _errors(run)


@router.get('/api/timeline/facets')
def timeline_facets(p: ProjectionParams = Depends()):
    def run():
        with session() as db:
            rule, projection = p.open(db)
            return facets(db, projection)
    return _errors(run)


@router.get('/api/timeline/window')
def timeline_window(p: ProjectionParams = Depends(), f: FilterParams = Depends(), w: WindowParams = Depends(),
                    cursor: str | None = Query(None, max_length=512), limit: int = Query(200, ge=1, le=1000),
                    group: str | None = Query('source', max_length=20), candidates: bool = True,
                    lanes: bool | None = None, max_lanes: int = Query(50, ge=1, le=200)):
    def run():
        filters = f.build()
        start, end = w.bounds()
        with session() as db:
            rule, projection = p.open(db)
            return window(db, projection, rule, start_us=start, end_us=end, filters=filters, cursor=cursor, limit=limit,
                          order=w.order, match=w.match, group=group or None, candidates=candidates,
                          include_lanes=lanes, max_lanes=max_lanes)
    return _errors(run)


@router.get('/api/timeline/density')
def timeline_density(p: ProjectionParams = Depends(), f: FilterParams = Depends(), w: WindowParams = Depends(),
                     bucket: str = Query('auto', max_length=10), group: str | None = Query('source', max_length=20),
                     max_lanes: int = Query(50, ge=1, le=200), max_buckets: int = Query(400, ge=1, le=5000)):
    def run():
        filters = f.build()
        start, end = w.bounds()
        with session() as db:
            rule, projection = p.open(db)
            return density(db, projection, start_us=start, end_us=end, bucket=bucket, filters=filters,
                           group=group or None, max_lanes=max_lanes, max_buckets=max_buckets)
    return _errors(run)


@router.get('/api/timeline/undated')
def timeline_undated(p: ProjectionParams = Depends(), f: FilterParams = Depends(),
                     after: str | None = Query(None, max_length=512), limit: int = Query(100, ge=1, le=1000)):
    def run():
        filters = f.build()
        with session() as db:
            rule, projection = p.open(db)
            return undated(db, projection, rule, filters=filters, after=after, limit=limit)
    return _errors(run)


@router.get('/api/timeline/items/{key}')
def timeline_item(key: str = PathParameter(..., max_length=80), p: ProjectionParams = Depends()):
    def run():
        with session() as db:
            rule, projection = p.open(db)
            return item_detail(db, projection, rule, key)
    result = _errors(run)
    if result is None:
        raise HTTPException(404, 'timeline item not found in this projection')
    return result


def _filename_part(text: str | None) -> str:
    return re.sub(r'[^0-9A-Za-z]+', '', text or 'all')[:20] or 'all'


@router.get('/api/timeline/export')
def timeline_export(p: ProjectionParams = Depends(), f: FilterParams = Depends(), w: WindowParams = Depends(),
                    format: Literal['csv', 'json'] = 'csv', max_rows: int = Query(50_000, ge=1, le=200_000)):
    def run():
        filters = f.build()
        start, end = w.bounds()
        with session() as db:
            rule, projection = p.open(db)
            return export_window(db, projection, rule, fmt=format, max_rows=max_rows, start_us=start, end_us=end,
                                 filters=filters, match=w.match, order=w.order, group=None)
    payload, meta = _errors(run)
    name = f'ageds-timeline-{_filename_part(w.from_)}-{_filename_part(w.to)}.{format}'
    return Response(content=payload, media_type='text/csv; charset=utf-8' if format == 'csv' else 'application/json',
                    headers={'Content-Disposition': f'attachment; filename="{name}"', 'Cache-Control': 'no-store',
                             'X-AGEDS-Timeline-Complete': str(meta['complete']).lower(),
                             'X-AGEDS-Timeline-Rows': str(meta['row_count']),
                             'X-AGEDS-Timeline-Rule': f"{meta['rule']['rule_id']}@{meta['rule']['version']}"})


class RunIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rule: str = Field(..., max_length=80)
    params: dict | None = None
    actor: str = Field('local-operator', max_length=200)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['accept', 'reject', 'revoke']
    reason: str = Field(..., max_length=2000)
    actor: str = Field('local-operator', max_length=200)
    create_link: bool = True


@router.post('/api/timeline/correlations/runs')
def correlation_run(body: RunIn):
    def run():
        with session() as db:
            return run_rule(db, body.rule, body.params, actor=body.actor)
    return _errors(run)


@router.get('/api/timeline/correlations/runs')
def correlation_runs(limit: int = Query(50, ge=1, le=200), before_id: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER)):
    def run():
        with session() as db:
            return list_runs(db, limit=limit, before_id=before_id)
    return _errors(run)


@router.get('/api/timeline/correlations/runs/{run_id}')
def correlation_run_detail(run_id: SqlId):
    def run():
        with session() as db:
            return run_detail(db, run_id)
    result = _errors(run)
    if result is None:
        raise HTTPException(404, 'correlation run not found')
    return result


@router.get('/api/timeline/correlations')
def correlations(run_id: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER), rule_id: str | None = Query(None, max_length=80),
                 status: str | None = Query(None, max_length=20), item_kind: str | None = Query(None, max_length=20),
                 item_id: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER),
                 after_id: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER), limit: int = Query(100, ge=1, le=500)):
    def run():
        with session() as db:
            return list_hypotheses(db, run_id=run_id, rule_id=rule_id, status=status, item_kind=item_kind,
                                   item_id=item_id, after_id=after_id, limit=limit)
    return _errors(run)


@router.get('/api/timeline/correlations/{hypothesis_id}')
def correlation_detail(hypothesis_id: SqlId):
    def run():
        with session() as db:
            return hypothesis_detail(db, hypothesis_id)
    result = _errors(run)
    if result is None:
        raise HTTPException(404, 'hypothesis not found')
    return result


@router.post('/api/timeline/correlations/{hypothesis_id}/review')
def correlation_review(hypothesis_id: SqlId, body: ReviewIn):
    def run():
        with session() as db:
            return review(db, hypothesis_id, body.action, reason=body.reason, actor=body.actor, create_link=body.create_link)
    return _errors(run)


def _reports_route(request: Request) -> bool:
    return any(getattr(route, 'path', None) == '/reports/new' for route in request.app.routes)


@router.get('/timeline', response_class=HTMLResponse)
def timeline_page(request: Request):
    rule = _errors(lambda: load_rule(kind='display_rule'))
    return templates.TemplateResponse(request, 'timeline.html', {
        'rule': rule.describe(), 'reports_available': _reports_route(request),
        'config_json': json.dumps({'rule': rule.ref, 'reportsAvailable': _reports_route(request)}),
    })

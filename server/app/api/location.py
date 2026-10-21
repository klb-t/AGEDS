"""HTTP routes owned by the location area (task L1): map page, imports, places, presence, correlations.

Places over HTTP are **inline JSON only** (or the operator's ``AGEDS_LOCATION_PLACES``
file when ``use_server_places`` is set); a request never names a filesystem path.
Uploads go to a private temporary file and are acquired as temporary copies.
The page ``/location`` is served with a Content-Security-Policy that allows images
from this server only; a map tile server is allowed only for a page opened with a
recorded tile opt-in (``tile_opt_in=<id>``), because every tile request discloses
the viewed area to that server. Opt-in refusals are 403, conflicts 409, invalid
input 422, unavailable adapters 501.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..db import session
from ..evidence import IntegrityError, temporary_copy_acquisitions
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/location', 'Lokalizacja')]
PREFIX = '/api/location'
TILE_WARNING = ('Kafelki mapy pobierane z zewnętrznego serwera ujawniają temu serwerowi oglądany obszar (współrzędne '
                'kafelków), adres IP i czas. Map tiles from an external server disclose the viewed area, your IP '
                'address and the time to that server.')
_TILE = re.compile(r'^https://[A-Za-z0-9.-]{1,253}(?::\d{1,5})?/[A-Za-z0-9._~/{}-]*$')


def _us(value: str | None, name: str) -> int | None:
    if value is None or value == '':
        return None
    from ..location import store
    from ..timeinfo import from_iso
    try:
        candidate = from_iso('other', value, f'http:{name}')
    except ValueError as exc:
        raise HTTPException(422, f'{name}: {exc}') from exc
    if candidate.utc is None:
        raise HTTPException(422, f'{name} needs an explicit offset (e.g. 2024-01-15T00:00:00Z)')
    return store.utc_us(candidate)


def _errors(call):
    from ..location.correlate import RuleError
    from ..location.ingest import FormatUnavailable
    from ..location.places import PlacesError
    try:
        return call()
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FormatUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (PlacesError, RuleError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _place_set(inline: dict | None, use_server_places: bool):
    from ..location import places
    if inline is not None and use_server_places:
        raise HTTPException(422, 'give inline places or use_server_places, not both')
    if inline is not None:
        return places.parse_places(inline, origin='inline')
    if use_server_places:
        found = places.resolve_places(None)
        if found is None:
            raise HTTPException(422, 'the operator has not set AGEDS_LOCATION_PLACES')
        return found
    raise HTTPException(422, 'places are required: inline JSON object (a filesystem path is never accepted)')


class PlacesIn(BaseModel):
    places: dict | None = None
    use_server_places: bool = False


class PresenceIn(BaseModel):
    places: dict | None = None
    use_server_places: bool = False
    from_: str | None = Field(default=None, alias='from', max_length=64)
    to: str | None = Field(default=None, max_length=64)
    params: dict = Field(default_factory=dict)
    publish: bool = False
    actor: str = Field(default='web', min_length=1, max_length=200)


class CorrelationIn(BaseModel):
    rule: str = Field(min_length=1, max_length=80)
    presence_run_id: int = Field(ge=1)
    params: dict = Field(default_factory=dict)
    actor: str = Field(default='web', min_length=1, max_length=200)


class ReviewIn(BaseModel):
    action: str = Field(pattern='^(accept|reject|revoke)$')
    reason: str = Field(min_length=1, max_length=10000)
    actor: str = Field(default='web', min_length=1, max_length=200)


class TileOptIn(BaseModel):
    template: str = Field(min_length=10, max_length=500)
    acknowledge_disclosure: bool = False
    actor: str = Field(default='web', min_length=1, max_length=200)


def tile_template_ok(template: str) -> bool:
    if not _TILE.match(template) or any(token not in ('{z}', '{x}', '{y}') for token in re.findall(r'\{[^}]*\}', template)):
        return False
    return all(token in template for token in ('{z}', '{x}', '{y}'))


def _tile_opt_in(opt_in_id: int | None) -> dict | None:
    if opt_in_id is None:
        return None
    with session() as db:
        row = db.execute("SELECT id, ts, details_json FROM audit_log WHERE id=? AND action='location_tile_opt_in'",
                         (opt_in_id,)).fetchone()
    if row is None:
        return None
    details = json.loads(row['details_json'] or '{}')
    template = details.get('template')
    if not isinstance(template, str) or not tile_template_ok(template):
        return None
    parts = urlsplit(template)
    return {'id': row['id'], 'recorded_at': row['ts'], 'template': template,
            'origin': f'{parts.scheme}://{parts.netloc}', 'warning': TILE_WARNING}


@router.get('/location', response_class=HTMLResponse)
def location_page(request: Request, tile_opt_in: int | None = Query(None, ge=1)):
    from ..location import query
    tiles = _tile_opt_in(tile_opt_in)
    with session() as db:
        summary = query.summary(db)
    context = {'summary': summary, 'tiles': tiles, 'tile_warning': TILE_WARNING,
               'config_json': json.dumps({'tiles': tiles, 'extent': summary['extent'],
                                          'latest_presence_run': (summary['presence_runs'] or [{}])[0].get('run_id')},
                                         ensure_ascii=False).replace('<', '\\u003c')}
    response = templates.TemplateResponse(request, 'location.html', context)
    img = "'self' data:" + (f" {tiles['origin']}" if tiles else '')
    response.headers['Content-Security-Policy'] = (
        f"default-src 'self'; img-src {img}; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@router.get(PREFIX + '/summary')
def summary():
    from ..importers import fit
    from ..location import places, query
    with session() as db:
        out = query.summary(db)
    return {**out, 'adapters': {'fit': fit.status()}, 'geotag_sources': places.geotag_sources()}


@router.get(PREFIX + '/window')
def window(from_: str | None = Query(None, alias='from'), to: str | None = None, stream: list[str] | None = Query(None),
           max_points: int = Query(5000, ge=1, le=20000), display_decimals: int | None = Query(None, ge=0, le=9),
           presence_run: int | None = Query(None, ge=1), basis: list[str] | None = Query(None)):
    from ..location import query
    start, end = _us(from_, 'from'), _us(to, 'to')
    with session() as db:
        return _errors(lambda: query.window(db, start_us=start, end_us=end, streams=stream, max_points=max_points,
                                            display_decimals=display_decimals, presence_run_id=presence_run,
                                            bases=basis))


@router.get(PREFIX + '/lanes')
def lanes(from_: str | None = Query(None, alias='from'), to: str | None = None, limit: int = Query(2000, ge=1, le=20000)):
    from ..location import query
    _us(from_, 'from'), _us(to, 'to')
    with session() as db:
        return _errors(lambda: query.lanes(db, start_utc=from_, end_utc=to, limit=limit))


@router.post(PREFIX + '/places/check')
def places_check(body: PlacesIn):
    from ..location import places
    return _errors(lambda: places.summary(_place_set(body.places, body.use_server_places)))


@router.post(PREFIX + '/presence/runs')
def presence_create(body: PresenceIn):
    from ..location import presence
    return _errors(lambda: presence.run_presence(_place_set(body.places, body.use_server_places), params=body.params,
                                                 start_utc=body.from_, end_utc=body.to, publish=body.publish,
                                                 actor=body.actor))


@router.get(PREFIX + '/presence/runs')
def presence_list(limit: int = Query(50, ge=1, le=500)):
    from ..location import presence
    return {'runs': presence.list_runs(limit)}


@router.get(PREFIX + '/presence/runs/{run_id}')
def presence_get(run_id: int, from_: str | None = Query(None, alias='from'), to: str | None = None,
                 limit: int = Query(10000, ge=1, le=100000)):
    from ..location import presence
    return _errors(lambda: presence.get_run(run_id, start_us=_us(from_, 'from'), end_us=_us(to, 'to'), limit=limit))


@router.post(PREFIX + '/correlations/runs')
def correlation_create(body: CorrelationIn):
    from ..location import correlate
    return _errors(lambda: correlate.run_correlation(body.rule, body.presence_run_id, params=body.params,
                                                     actor=body.actor))


@router.get(PREFIX + '/correlations/runs')
def correlation_list(limit: int = Query(50, ge=1, le=500)):
    from ..location import correlate
    return {'runs': correlate.list_correlation_runs(limit), 'rules': [
        {k: r[k] for k in ('rule_id', 'version', 'sha256', 'relation', 'parameters', 'criteria')}
        for r in correlate.released_rules()]}


@router.get(PREFIX + '/correlations/runs/{run_id}')
def correlation_get(run_id: int):
    from ..location import correlate
    return _errors(lambda: correlate.get_correlation_run(run_id))


@router.post(PREFIX + '/correlations/hypotheses/{hypothesis_id}/review')
def correlation_review(hypothesis_id: int, body: ReviewIn):
    from ..location import correlate
    return _errors(lambda: correlate.review(hypothesis_id, body.action, reason=body.reason, actor=body.actor))


@router.post(PREFIX + '/tiles/opt-in')
def tiles_opt_in(body: TileOptIn):
    if not body.acknowledge_disclosure:
        raise HTTPException(422, 'acknowledge_disclosure must be true: ' + TILE_WARNING)
    if not tile_template_ok(body.template):
        raise HTTPException(422, 'template must be https://host/path with {z}, {x} and {y} and no other placeholders')
    details = {'template': body.template, 'actor': body.actor, 'actor_verified': False,
               'acknowledged_warning': TILE_WARNING, 'scope': 'page sessions opened with this opt-in id'}
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        cur = db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) "
                         "VALUES (?,'location_tile_opt_in','location_view',NULL,?)",
                         (body.actor, json.dumps(details, ensure_ascii=False, sort_keys=True)))
        opt_in_id = int(cur.lastrowid)
    return {'opt_in_id': opt_in_id, 'page': f'/location?tile_opt_in={opt_in_id}', **_tile_opt_in(opt_in_id)}


@router.get(PREFIX + '/tiles/opt-ins')
def tiles_opt_ins():
    with session() as db:
        rows = db.execute("SELECT id, ts, actor, details_json FROM audit_log WHERE action='location_tile_opt_in' "
                          "ORDER BY id DESC LIMIT 100").fetchall()
    return {'opt_ins': [{'id': r['id'], 'recorded_at': r['ts'], 'actor': r['actor'],
                         'template': json.loads(r['details_json'] or '{}').get('template')} for r in rows],
            'warning': TILE_WARNING}


@router.post(PREFIX + '/import')
def upload(file: UploadFile = File(...), enable_location: bool = Form(False), format: str = Form('auto'),
           store_decimals: int | None = Form(None), stream: str | None = Form(None),
           provider_place_text: str = Form('keep'), gpx_naive_time: str = Form('unknown'),
           geojson_options: str | None = Form(None), label: str | None = Form(None),
           collection_id: int | None = Form(None)):
    from ..location import formats
    if format not in ('auto', *formats.FORMATS):
        raise HTTPException(422, 'unknown format')
    if not enable_location:
        raise HTTPException(403, 'location import is disabled; set enable_location=true to opt in (recorded)')
    options = {'provider_place_text': provider_place_text, 'gpx_naive_time': gpx_naive_time}
    if geojson_options:
        try:
            options['geojson'] = json.loads(geojson_options)
        except ValueError as exc:
            raise HTTPException(422, f'geojson_options is not JSON: {exc}') from exc
    name = Path(file.filename or 'upload').name
    with tempfile.TemporaryDirectory(prefix='ageds-location-upload-') as directory:
        path = Path(directory) / ('upload' + ''.join(Path(name).suffixes)[-20:])
        with path.open('wb') as handle:
            shutil.copyfileobj(file.file, handle)
        with temporary_copy_acquisitions():
            return _errors(lambda: formats.import_file(
                path, fmt=format, enable_location=True, store_decimals=store_decimals, stream=stream or None,
                source_locator=f'upload:location:{name}', original_name=name, collection_id=collection_id,
                label=label, format_options=options))

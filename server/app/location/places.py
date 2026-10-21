"""Private places (geofences) and local, accuracy-aware matching.

The places file is user data and lives outside the repository: ``--places PATH``
(CLI), the operator's ``AGEDS_LOCATION_PLACES`` environment variable, or an
inline JSON object over HTTP (a request never names a filesystem path). Schema
``ageds.location-places.v1``::

    {"schema": "ageds.location-places.v1", "version": "2026-10-17",
     "places": [{"id": "p1", "label": "...", "lat": 0.001, "lon": 0.002,
                 "radius_m": 150, "kind": "optional free text"}]}

Every result records the canonical SHA-256 of the parsed set (and the file's byte
hash) and the declared version. Matching is local and runs on the stored
coordinates of each point. Classification of one point against one place
(rule ``place_match.v1``):

* ``d`` = haversine distance point -> place centre, ``R`` = ``radius_m``;
* ``u`` = ``accuracy_m x accuracy_multiplier`` + rounding error of the stored
  coordinate (0 at source precision); missing accuracy uses
  ``missing_accuracy_m`` when given, else ``u`` = rounding error only and the
  match says ``accuracy_basis: not_stated``;
* ``inside`` when ``d + u <= R``; ``outside`` when ``d - u > R``; otherwise
  ``uncertain`` (the accuracy circle overlaps the boundary).

No reverse geocoding: a place is only what the file says. ``match_points`` is the
documented entry point for other areas (e.g. photo geotags of the traces area).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import geo

SCHEMA = 'ageds.location-places.v1'
ENV_VAR = 'AGEDS_LOCATION_PLACES'
MATCH_RULE = 'place_match.v1'
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_PLACES = 10_000
MAX_RADIUS_M = 1_000_000.0
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
STATUSES = ('inside', 'uncertain', 'outside')


class PlacesError(ValueError):
    pass


@dataclass(frozen=True)
class Place:
    id: str
    label: str
    lat: float
    lon: float
    radius_m: float
    kind: str | None = None

    def as_dict(self) -> dict:
        return {'id': self.id, 'label': self.label, 'lat': self.lat, 'lon': self.lon, 'radius_m': self.radius_m,
                'kind': self.kind}


@dataclass(frozen=True)
class PlaceSet:
    places: tuple[Place, ...]
    version: str
    sha256: str
    file_sha256: str | None
    origin: str  # file | env_file | inline

    def describe(self) -> dict:
        """What a result records (no file path)."""
        return {'schema': SCHEMA, 'version': self.version, 'sha256': self.sha256, 'file_sha256': self.file_sha256,
                'origin': self.origin, 'places': len(self.places), 'match_rule': MATCH_RULE,
                'distance_model': geo.DISTANCE_MODEL}

    def by_id(self) -> dict[str, Place]:
        return {p.id: p for p in self.places}


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def parse_places(data, *, origin: str = 'inline', file_sha256: str | None = None) -> PlaceSet:
    if not isinstance(data, dict):
        raise PlacesError('places must be a JSON object')
    if data.get('schema') != SCHEMA:
        raise PlacesError(f'places schema must be {SCHEMA!r}')
    unknown = sorted(set(data) - {'schema', 'version', 'places', 'notes'})
    if unknown:
        raise PlacesError('places has unknown keys: ' + ', '.join(unknown))
    version = data.get('version', 'unversioned')
    if not isinstance(version, str) or not version.strip() or len(version) > 200:
        raise PlacesError('version must be a non-blank string up to 200 characters')
    raw = data.get('places')
    if not isinstance(raw, list) or not raw or len(raw) > MAX_PLACES:
        raise PlacesError(f'places must be a non-empty list of at most {MAX_PLACES} places')
    places, seen = [], set()
    for index, item in enumerate(raw):
        where = f'places[{index}]'
        if not isinstance(item, dict):
            raise PlacesError(f'{where}: must be an object')
        extra = sorted(set(item) - {'id', 'label', 'lat', 'lon', 'radius_m', 'kind'})
        if extra:
            raise PlacesError(f'{where}: unknown keys ' + ', '.join(extra))
        pid = item.get('id')
        if not isinstance(pid, str) or not _ID.match(pid):
            raise PlacesError(f'{where}: id must match {_ID.pattern}')
        if pid in seen:
            raise PlacesError(f'{where}: duplicate id {pid!r}')
        seen.add(pid)
        label = item.get('label', pid)
        if not isinstance(label, str) or len(label) > 500:
            raise PlacesError(f'{where}: label must be a string up to 500 characters')
        lat, lon, radius = item.get('lat'), item.get('lon'), item.get('radius_m')
        for name, value in (('lat', lat), ('lon', lon), ('radius_m', radius)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise PlacesError(f'{where}: {name} must be a finite number')
        try:
            geo.check_range(float(lat), float(lon), where)
        except geo.CoordinateError as exc:
            raise PlacesError(str(exc)) from exc
        if not 0 < radius <= MAX_RADIUS_M:
            raise PlacesError(f'{where}: radius_m must be in (0, {MAX_RADIUS_M:g}]')
        kind = item.get('kind')
        if kind is not None and (not isinstance(kind, str) or len(kind) > 100):
            raise PlacesError(f'{where}: kind must be a string up to 100 characters')
        places.append(Place(pid, label, float(lat), float(lon), float(radius), kind))
    normalized = {'schema': SCHEMA, 'version': version, 'places': [p.as_dict() for p in places]}
    digest = hashlib.sha256(canonical_json(normalized).encode('utf-8')).hexdigest()
    return PlaceSet(tuple(places), version, digest, file_sha256, origin)


def load_places(path: str | os.PathLike, *, origin: str = 'file') -> PlaceSet:
    p = Path(path)
    if p.stat().st_size > MAX_FILE_BYTES:
        raise PlacesError(f'places file exceeds {MAX_FILE_BYTES} bytes')
    raw = p.read_bytes()
    try:
        data = json.loads(raw.decode('utf-8-sig'), parse_constant=lambda c: (_ for _ in ()).throw(PlacesError(c)))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlacesError(f'places file is not valid UTF-8 JSON: {exc}') from exc
    return parse_places(data, origin=origin, file_sha256=hashlib.sha256(raw).hexdigest())


def resolve_places(explicit: str | os.PathLike | None = None) -> PlaceSet | None:
    """CLI argument first, then ``AGEDS_LOCATION_PLACES``; None when neither is set."""
    if explicit:
        return load_places(explicit, origin='file')
    env = os.environ.get(ENV_VAR)
    return load_places(env, origin='env_file') if env else None


def summary(place_set: PlaceSet) -> dict:
    """Validation report for ``places-check``: counts, radii and pairwise overlaps (no labels)."""
    radii = sorted(p.radius_m for p in place_set.places)
    overlaps = []
    places = list(place_set.places)
    for i, a in enumerate(places):
        for b in places[i + 1:i + 1 + 2000]:
            d = geo.haversine_m(a.lat, a.lon, b.lat, b.lon)
            if d < a.radius_m + b.radius_m:
                overlaps.append({'a': a.id, 'b': b.id, 'centre_distance_m': round(d, 3)})
                if len(overlaps) >= 200:
                    break
    return {**place_set.describe(), 'radius_m': {'min': radii[0], 'max': radii[-1]},
            'kinds': sorted({p.kind for p in places if p.kind}), 'overlapping_pairs': overlaps,
            'overlapping_pairs_truncated': len(overlaps) >= 200,
            'note': 'overlapping places are matched independently; a point may be inside several places'}


# ---------------------------------------------------------------- matching

@dataclass(frozen=True)
class MatchParams:
    accuracy_multiplier: float = 1.0
    missing_accuracy_m: float | None = None

    def as_dict(self) -> dict:
        return {'accuracy_multiplier': self.accuracy_multiplier, 'missing_accuracy_m': self.missing_accuracy_m}


def classify(place: Place, lat: float, lon: float, *, accuracy_m: float | None = None, rounding_m: float = 0.0,
             params: MatchParams = MatchParams()) -> dict:
    """One point against one place; see the module docstring for the rule."""
    distance = geo.haversine_m(place.lat, place.lon, lat, lon)
    if accuracy_m is not None and math.isfinite(accuracy_m) and accuracy_m >= 0:
        uncertainty, basis = accuracy_m * params.accuracy_multiplier, 'stated'
    elif params.missing_accuracy_m is not None:
        uncertainty, basis = params.missing_accuracy_m, 'missing_accuracy_parameter'
    else:
        uncertainty, basis = 0.0, 'not_stated'
    uncertainty += rounding_m
    if distance + uncertainty <= place.radius_m:
        status = 'inside'
    elif distance - uncertainty > place.radius_m:
        status = 'outside'
    else:
        status = 'uncertain'
    return {'place_id': place.id, 'status': status, 'distance_m': distance, 'uncertainty_m': uncertainty,
            'accuracy_basis': basis, 'rounding_m': rounding_m, 'margin_m': place.radius_m - distance}


def match_points(points, place_set: PlaceSet, *, params: MatchParams = MatchParams(),
                 include_outside: bool = False) -> list[dict]:
    """Match points against every place of ``place_set`` (stable API for other areas).

    ``points`` is an iterable of dicts with ``lat``, ``lon`` and optionally ``id``,
    ``accuracy_m``, ``decimals`` (stored precision) and ``source_decimals``. Returns one
    entry per point: ``{'id', 'matches': [classification, ...]}`` with ``inside`` and
    ``uncertain`` places (and ``outside`` ones when ``include_outside``), each with
    distance, uncertainty and margin in metres. Points with unusable coordinates get
    ``{'id', 'error': code}``. Nothing is written; the caller records the place-set
    hash (``place_set.describe()``) with any stored result.
    """
    out = []
    for point in points:
        pid = point.get('id')
        try:
            lat, lon = float(point['lat']), float(point['lon'])
            geo.check_range(lat, lon)
        except (KeyError, TypeError, ValueError) as exc:
            out.append({'id': pid, 'error': getattr(exc, 'code', 'coordinate_missing')})
            continue
        decimals = point.get('decimals')
        rounding = geo.rounding_error_m(lat, decimals, point.get('source_decimals')) if isinstance(decimals, int) else 0.0
        accuracy = geo.finite(point.get('accuracy_m'))
        matches = []
        for place in place_set.places:
            result = classify(place, lat, lon, accuracy_m=accuracy, rounding_m=rounding, params=params)
            if include_outside or result['status'] != 'outside':
                matches.append(result)
        out.append({'id': pid, 'matches': matches})
    return out


# ---------------------------------------------------------------- geotag sources

GEOTAG_SOURCES: dict[str, dict] = {}


def register_geotag_source(key: str, *, label: str, signal_class: str, basis: str, loader) -> None:
    """Register a provider of positioned points for later presence matching.

    ``loader(db, start_us, end_us, limit)`` yields dicts like ``match_points`` takes,
    plus ``t_us`` (UTC microseconds) and ``ref`` (a locator in the provider's area).
    The location area ships ``location_fixes``; other areas (e.g. photo geotags of the
    traces area) register here so the coordinator can wire them into presence runs
    without this module importing them. Registration is in-process and explicit.
    """
    if not re.fullmatch(r'[a-z][a-z0-9_.:-]{0,63}', key):
        raise ValueError('geotag source key must be a lowercase identifier')
    if signal_class not in ('passive', 'active', 'declared'):
        raise ValueError('signal_class must be passive, active or declared')
    if key in GEOTAG_SOURCES:
        raise ValueError(f'geotag source {key} already registered')
    GEOTAG_SOURCES[key] = {'key': key, 'label': label, 'signal_class': signal_class, 'basis': basis, 'loader': loader}


def geotag_sources() -> list[dict]:
    return [{k: v for k, v in source.items() if k != 'loader'} for source in GEOTAG_SOURCES.values()]

"""Point sources from other areas, matched against private places on demand.

The location area owns ``location_fixes`` (registered in ``presence.py``). This
module registers the traces area's photo geotags (EXIF GPS and Google Photos
sidecar ``geoData``/``geoDataExif``) through ``places.register_geotag_source`` and
offers ``match_source``: a read-only projection that classifies each point of a
registered source against a place set. Nothing is written; the result carries
the place-set hash and the source key so it can be reproduced. A photo geotag is
a claim of a file or provider about a camera position at an instant, never proof
of a person's presence; sidecar ``geoData`` may be a provider inference or a
user edit and keeps that basis per point.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import places as places_mod
from . import presence as _presence  # noqa: F401  (registers location_fixes)

MATCH_RULE = 'place_match_source.v1'
PHOTO_SOURCE = 'traces.photo_geotags'


def _iso(us: int | None) -> str | None:
    if us is None:
        return None
    return datetime.fromtimestamp(us / 1_000_000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _us(text: str) -> int:
    moment = datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(timezone.utc)
    return int(moment.timestamp()) * 1_000_000 + moment.microsecond


def photo_geotags_loader(db, start_us, end_us, limit):
    """Loader for the registry: only tags with a UTC time and stored coordinates."""
    from ..traces.geotags import iter_geotags
    for tag in iter_geotags(since=_iso(start_us), until=_iso(end_us), limit=limit):
        if tag['time_utc'] is None or tag['lat'] is None or tag['lon'] is None:
            continue
        decimals = None
        if isinstance(tag['stored_precision'], str) and tag['stored_precision'].startswith('decimals:'):
            decimals = int(tag['stored_precision'].split(':', 1)[1])
        yield {'id': f"geotag:{tag['geotag_id']}", 't_us': _us(tag['time_utc']), 'lat': tag['lat'], 'lon': tag['lon'],
               'accuracy_m': tag['accuracy_m'], 'decimals': decimals, 'ref': f"trace_geotags/{tag['geotag_id']}",
               'basis': tag['basis'], 'source_kind': tag['source_kind'], 'time_basis': tag['time_basis']}


if PHOTO_SOURCE not in places_mod.GEOTAG_SOURCES:
    places_mod.register_geotag_source(PHOTO_SOURCE, label='Geotagi zdjęć (EXIF / Google Photos)',
                                      signal_class='active', basis='raw_sample', loader=photo_geotags_loader)


def match_source(key: str, place_set: places_mod.PlaceSet, *, start_us: int | None = None, end_us: int | None = None,
                 limit: int = 100_000, include_outside: bool = False) -> dict:
    """Classify every point of one registered source against ``place_set`` (read-only)."""
    from ..db import session
    if key not in places_mod.GEOTAG_SOURCES:
        raise ValueError(f'unknown point source: {key}; known: {sorted(places_mod.GEOTAG_SOURCES)}')
    if not 1 <= int(limit) <= 1_000_000:
        raise ValueError('limit must be from 1 to 1000000')
    source = places_mod.GEOTAG_SOURCES[key]
    with session() as db:
        points = list(source['loader'](db, start_us, end_us, int(limit) + 1))
    truncated = len(points) > limit
    points = points[:limit]
    by_id = {p['id']: p for p in points}
    matched = places_mod.match_points(points, place_set, include_outside=include_outside)
    items = []
    for entry in matched:
        point = by_id.get(entry['id'], {})
        items.append({'id': entry['id'], 'ref': point.get('ref'), 'time_utc': _iso(point.get('t_us')),
                      'basis': point.get('basis', source['basis']), 'source_kind': point.get('source_kind'),
                      **({'error': entry['error']} if 'error' in entry else {'matches': entry['matches']})})
    return {'rule': MATCH_RULE, 'source': {k: v for k, v in source.items() if k != 'loader'},
            'place_set': place_set.describe(), 'window': {'from': _iso(start_us), 'to': _iso(end_us)},
            'points': len(points), 'truncated': truncated,
            'with_place': sum(1 for i in items if any(m['status'] in ('inside', 'uncertain') for m in i.get('matches', ()))),
            'items': items,
            'note': 'device/file position claims at an instant; not proof of a person\'s presence; nothing stored'}

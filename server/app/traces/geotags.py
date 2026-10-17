"""Photo geotags: EXIF GPS and Google Photos sidecar ``geoData``/``geoDataExif``.

Storage rule: coordinates are written only when the run carries the explicit
``enable_location`` opt-in (recorded in ``opt_in_json``). Without it a row says
that a geotag is present (``status='present_not_stored'``) with no coordinates and
no raw values. Processing is local; no reverse geocoding, no map tiles.

Read contract for the location area (place matching, AGEDS-TASK-20261017-L1)::

    from server.app.traces.geotags import iter_geotags
    for tag in iter_geotags(since='2020-01-01T00:00:00Z', until=None, source_kinds=None,
                            include_provider=True, collection_id=None, limit=None):
        ...

Each yielded dict has exactly these keys:

``geotag_id``        row id in ``trace_geotags`` (stable, append-only)
``source_kind``      ``exif_gps`` | ``takeout_geoData`` | ``takeout_geoDataExif``
``basis``            ``raw_sample`` (embedded EXIF, or the sidecar's copy of it)
                     | ``provider_inference_or_user_edit`` (sidecar ``geoData``:
                     Google may have inferred or the user may have edited it)
``lat``/``lon``      decimal degrees (WGS 84 as the source states; EXIF
                     ``GPSMapDatum`` is in ``raw``), at ``stored_precision``
``alt``              metres or None
``accuracy_m``       EXIF ``GPSHPositioningError`` in metres or None (no other
                     source states an accuracy; ``latitudeSpan`` is not one)
``stored_precision`` ``source`` or ``decimals:N``
``time_utc``         UTC instant of the tag or None: EXIF GPS receiver time
                     (``GPSDateStamp`` + ``GPSTimeStamp``) or the sidecar
                     ``photoTakenTime``; ``time_basis`` says which
``time_basis``       ``exif_gps_receiver_utc`` | ``google_photos_photoTakenTime`` | None
``artifact_id``      the artifact the tag was read from (media file or sidecar)
``media_sha256``     content identity of the media when known (EXIF: the artifact;
                     sidecar: only when exactly one media candidate matched)
``sidecar_id``/``event_id``  provenance rows (may be None)
``collection_ids``   collections of the artifact's acquisition observations
``parser``/``parser_version``/``opt_in``  how and under which opt-in it was stored

Only rows with stored coordinates are yielded; rows are ordered by
``(time_utc IS NULL, time_utc, geotag_id)``. A geotag is an observation of a
file or account claim, not of a person's presence.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Iterator

from ..db import session
from ..timeinfo import TimeCandidate, format_utc
from .common import append_row, dumps

SOURCE_KINDS = ('exif_gps', 'takeout_geoData', 'takeout_geoDataExif')
BASIS = {'exif_gps': 'raw_sample', 'takeout_geoDataExif': 'raw_sample',
         'takeout_geoData': 'provider_inference_or_user_edit'}
_DATESTAMP = re.compile(r'^(\d{4}):(\d{2}):(\d{2})$')


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(h)))


def _rational(pair) -> Fraction | None:
    if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, int) for x in pair)) or pair[1] == 0:
        return None
    return Fraction(pair[0], pair[1])


def _dms(values, issues: list[str], name: str) -> Fraction | None:
    if not isinstance(values, list) or len(values) != 3:
        issues.append(f'gps_{name}_not_three_rationals')
        return None
    parts = [_rational(v) for v in values]
    if any(p is None for p in parts):
        issues.append(f'gps_{name}_zero_denominator')
        return None
    degrees, minutes, seconds = parts
    if degrees < 0 or minutes < 0 or seconds < 0 or minutes >= 60 or seconds >= 60:
        issues.append(f'gps_{name}_component_out_of_range')
        return None
    return degrees + minutes / 60 + seconds / 3600


def exif_position(gps: dict) -> tuple[str, float | None, float | None, float | None, float | None, list[str]]:
    """(status, lat, lon, alt, accuracy_m, issues) from the raw GPS IFD; never guesses a hemisphere."""
    issues: list[str] = []
    if 'GPSLatitude' not in gps and 'GPSLongitude' not in gps:
        return 'invalid', None, None, None, None, ['gps_ifd_without_position']
    lat = _dms(gps.get('GPSLatitude'), issues, 'latitude')
    lon = _dms(gps.get('GPSLongitude'), issues, 'longitude')
    lat_ref = (gps.get('GPSLatitudeRef') or '').upper()
    lon_ref = (gps.get('GPSLongitudeRef') or '').upper()
    if lat is not None and lat_ref not in ('N', 'S'):
        issues.append('gps_latitude_ref_missing_or_invalid')
        lat = None
    if lon is not None and lon_ref not in ('E', 'W'):
        issues.append('gps_longitude_ref_missing_or_invalid')
        lon = None
    if lat is None or lon is None:
        return 'invalid', None, None, None, None, issues
    lat = -lat if lat_ref == 'S' else lat
    lon = -lon if lon_ref == 'W' else lon
    if abs(lat) > 90 or abs(lon) > 180:
        return 'invalid', None, None, None, None, issues + ['gps_position_out_of_range']
    alt = None
    if 'GPSAltitude' in gps:
        value = _rational((gps.get('GPSAltitude') or [None])[0])
        if value is None:
            issues.append('gps_altitude_invalid')
        else:
            ref = (gps.get('GPSAltitudeRef') or [0])[0]
            alt = float(-value if ref == 1 else value)
    accuracy = None
    if 'GPSHPositioningError' in gps:
        value = _rational((gps.get('GPSHPositioningError') or [None])[0])
        accuracy = float(value) if value is not None and value >= 0 else None
    if lat == 0 and lon == 0:
        return 'zero_placeholder', None, None, alt, accuracy, issues
    return 'coordinates', float(lat), float(lon), alt, accuracy, issues


def exif_gps_time(gps: dict, source: str) -> tuple[TimeCandidate | None, list[str]]:
    """``GPSDateStamp`` + ``GPSTimeStamp`` → a UTC candidate (EXIF defines both as UTC)."""
    date_raw, stamp = gps.get('GPSDateStamp'), gps.get('GPSTimeStamp')
    if date_raw is None and stamp is None:
        return None, []
    if date_raw is None or stamp is None:
        return None, ['gps_time_incomplete']
    match = _DATESTAMP.match(date_raw)
    parts = [_rational(v) for v in stamp] if isinstance(stamp, list) and len(stamp) == 3 else None
    if not match or not parts or any(p is None for p in parts):
        return None, ['gps_time_unparsed']
    hours, minutes, seconds = parts
    if not (0 <= hours < 24 and hours.denominator == 1 and 0 <= minutes < 60 and minutes.denominator == 1
            and 0 <= seconds < 61):
        return None, ['gps_time_component_out_of_range']
    try:
        day = datetime(*map(int, match.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None, ['gps_date_invalid_calendar_value']
    micro = (seconds * 1_000_000)
    moment = day + timedelta(hours=int(hours), minutes=int(minutes), microseconds=int(micro))
    precision = 'second' if seconds.denominator == 1 else 'millisecond' if (seconds * 1000).denominator == 1 else 'microsecond'
    raw = f'{date_raw} {":".join(f"{p.numerator}/{p.denominator}" for p in parts)}'
    return TimeCandidate('exif', raw, source, utc=format_utc(moment), tz_status='explicit_offset', tz='UTC',
                         precision=precision,
                         metadata={'tags': ['GPSDateStamp', 'GPSTimeStamp'],
                                   'semantics': 'GPS receiver UTC time of the fix; separate from the camera clock '
                                                'and possibly stale (last fix)'}), []


def sidecar_position(value) -> tuple[str, float | None, float | None, float | None]:
    """Takeout ``geoData``/``geoDataExif``: ``0,0`` is Google's "no location" placeholder."""
    if not isinstance(value, dict):
        return 'invalid', None, None, None
    lat, lon, alt = value.get('latitude'), value.get('longitude'), value.get('altitude')
    numbers = [x for x in (lat, lon) if isinstance(x, (int, float)) and not isinstance(x, bool)]
    if len(numbers) != 2 or any(x != x or abs(x) == float('inf') for x in numbers):
        return 'invalid', None, None, None
    alt = float(alt) if isinstance(alt, (int, float)) and not isinstance(alt, bool) and alt == alt else None
    if lat == 0 and lon == 0:
        return 'zero_placeholder', None, None, alt
    if abs(lat) > 90 or abs(lon) > 180:
        return 'invalid', None, None, None
    return 'coordinates', float(lat), float(lon), alt


def store(db: sqlite3.Connection, *, artifact_id: int, source_kind: str, status: str, lat, lon, alt,
          parser: str, parser_version: str, enable_location: bool, coordinate_decimals: int | None,
          media_sha256: str | None = None, sidecar_id: int | None = None, event_id: int | None = None,
          time_utc: str | None = None, time_raw: str | None = None, time_basis: str | None = None,
          raw: dict | None = None, accuracy_m: float | None = None, issues: list[str] | None = None) -> int:
    """Append one geotag row under the run's opt-in (see the module docstring)."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f'unknown geotag source kind {source_kind}')
    stored = status == 'coordinates' and bool(enable_location)
    if status == 'coordinates' and not stored:
        status = 'present_not_stored'
    elif stored:
        status = 'stored'
    if stored and coordinate_decimals is not None:
        lat, lon = round(lat, coordinate_decimals), round(lon, coordinate_decimals)
        alt = None if alt is None else round(alt, 1)
    raw_payload = dict(raw or {}) if stored else {}
    if stored and accuracy_m is not None:
        raw_payload['accuracy_m'] = accuracy_m
    # A row that holds no coordinates must not depend on the run's opt-in (re-imports stay
    # idempotent); the run's options are in its import_run audit entry.
    if stored:
        opt_in = {'enable_location': True, 'processing': 'local_only', 'coordinate_decimals': coordinate_decimals}
    elif status == 'present_not_stored':
        opt_in = {'enable_location': False, 'processing': 'local_only'}
    else:
        opt_in = {'processing': 'local_only', 'coordinates': 'none_to_store'}
    return append_row(db, 'trace_geotags',
                      {'artifact_id': artifact_id, 'source_kind': source_kind, 'parser': parser,
                       'parser_version': parser_version,
                       'stored_precision': ('source' if coordinate_decimals is None else f'decimals:{coordinate_decimals}')
                       if stored else 'not_stored'},
                      {'media_sha256': media_sha256, 'sidecar_id': sidecar_id, 'event_id': event_id,
                       'basis': BASIS[source_kind], 'status': status, 'coordinates_stored': 1 if stored else 0,
                       'lat': lat if stored else None, 'lon': lon if stored else None,
                       'alt': alt if stored else None, 'time_utc': time_utc, 'time_raw': time_raw,
                       'time_basis': time_basis, 'opt_in_json': dumps(opt_in), 'raw_json': dumps(raw_payload),
                       'issues_json': dumps(issues or [])})


def sidecar_media_sha256(db: sqlite3.Connection, sidecar_id: int) -> str | None:
    """Media identity of a sidecar: the latest match with exactly one described file, else None."""
    row = db.execute("SELECT candidates_json FROM trace_photo_matches WHERE sidecar_id=? AND status='single_candidate' "
                     'ORDER BY id DESC LIMIT 1', (sidecar_id,)).fetchone()
    if row is None:
        return None
    described = [c for c in json.loads(row['candidates_json']) if c.get('described')]
    return described[0].get('sha256') if len(described) == 1 else None


def iter_geotags(*, since: str | None = None, until: str | None = None, source_kinds: tuple[str, ...] | None = None,
                 include_provider: bool = True, collection_id: int | None = None,
                 limit: int | None = None) -> Iterator[dict]:
    """Stored geotags for place matching; see the module docstring for the exact contract."""
    sql = ['SELECT g.* FROM trace_geotags g WHERE g.coordinates_stored=1']
    params: list = []
    if since is not None:
        sql.append('AND g.time_utc >= ?')
        params.append(since)
    if until is not None:
        sql.append('AND g.time_utc < ?')
        params.append(until)
    if source_kinds:
        unknown = set(source_kinds) - set(SOURCE_KINDS)
        if unknown:
            raise ValueError(f'unknown source kinds: {sorted(unknown)}')
        sql.append(f'AND g.source_kind IN ({",".join("?" * len(source_kinds))})')
        params.extend(source_kinds)
    if not include_provider:
        sql.append("AND g.basis='raw_sample'")
    if collection_id is not None:
        sql.append('AND g.artifact_id IN (SELECT artifact_id FROM source_observations WHERE collection_id=?)')
        params.append(collection_id)
    sql.append('ORDER BY g.time_utc IS NULL, g.time_utc, g.id')
    if limit is not None:
        sql.append('LIMIT ?')
        params.append(int(limit))
    with session() as db:
        rows = db.execute(' '.join(sql), params).fetchall()
        collections, media = {}, {}
        for row in rows:
            collections[row['artifact_id']] = sorted({r[0] for r in db.execute(
                'SELECT DISTINCT collection_id FROM source_observations WHERE artifact_id=? AND collection_id IS NOT NULL',
                (row['artifact_id'],))})
            if row['sidecar_id'] is not None and row['sidecar_id'] not in media:
                media[row['sidecar_id']] = sidecar_media_sha256(db, row['sidecar_id'])
    for row in rows:
        raw = json.loads(row['raw_json'])
        yield {'geotag_id': row['id'], 'source_kind': row['source_kind'], 'basis': row['basis'],
               'lat': row['lat'], 'lon': row['lon'], 'alt': row['alt'], 'accuracy_m': raw.get('accuracy_m'),
               'stored_precision': row['stored_precision'], 'time_utc': row['time_utc'],
               'time_basis': row['time_basis'], 'artifact_id': row['artifact_id'],
               'media_sha256': row['media_sha256'] if row['sidecar_id'] is None else media.get(row['sidecar_id']),
               'sidecar_id': row['sidecar_id'], 'event_id': row['event_id'],
               'collection_ids': collections[row['artifact_id']], 'parser': row['parser'],
               'parser_version': row['parser_version'], 'opt_in': json.loads(row['opt_in_json'])}

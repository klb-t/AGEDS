"""Garmin/ANT FIT activity files behind an optional adapter (opt-in ``enable_location``; parser ``fit`` v1).

The adapter uses the third-party ``fitparse`` package **only if it is importable**;
otherwise ``import_fit`` raises ``FormatUnavailable`` before anything is acquired
and ``status()`` reports ``unavailable`` (the test suite passes without it; this
checkout has no ``fitparse``, so real FIT decoding was never executed here).

With the adapter: ``record`` messages with ``position_lat``/``position_long``
(semicircles, ``deg = semicircles x 180 / 2^31``) and ``timestamp`` (FIT times are
UTC by specification; ``fitparse`` returns naive UTC datetimes) become raw samples
(``track_point``); one ``location_track`` event summarizes them with statistics
computed by code. ``session`` summaries of the device are kept raw (bounded), not
used as our numbers.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path

from ..location import geo
from ..location.ingest import DEFAULT_LIMITS, FormatUnavailable, ImportParams, run_import
from ..location.tracks import TrackStats
from ..timeinfo import format_utc
from ..timeinfo import TimeCandidate

PARSER_NAME = 'fit'
PARSER_VERSION = '1'
SEMICIRCLE_DEG = 180.0 / 2 ** 31


def _module():
    try:
        return importlib.import_module('fitparse')
    except ImportError:
        return None


def status() -> dict:
    module = _module()
    if module is None:
        return {'adapter': 'fitparse', 'status': 'unavailable', 'reason': 'python package fitparse is not installed'}
    return {'adapter': 'fitparse', 'status': 'available', 'version': getattr(module, '__version__', 'unknown')}


def semicircles_to_degrees(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value * SEMICIRCLE_DEG


def fit_time(value, source: str) -> TimeCandidate | None:
    if not isinstance(value, datetime):
        return None
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return TimeCandidate('observed', value.isoformat(), source, utc=format_utc(aware), tz_status='explicit_offset',
                         tz='UTC', precision='second', metadata={'zone_declaration': 'fit_specification_utc'})


def _parse(path: Path, ctx):
    module = _module()
    if module is None:  # pragma: no cover - checked before acquisition
        yield ('issue', 'adapter_unavailable', {'adapter': 'fitparse'})
        yield ('coverage', {'complete': False})
        return
    source = f'{PARSER_NAME}:{PARSER_VERSION}'
    stats = TrackStats(ctx.params.store_decimals)
    stats.new_segment()
    try:
        fitfile = module.FitFile(str(path))
        messages = fitfile.get_messages()
        sessions = []
        index = 0
        for message in messages:
            name = getattr(message, 'name', None)
            values = message.get_values() if hasattr(message, 'get_values') else {}
            if name == 'session' and len(sessions) < 20:
                sessions.append({k: (v if isinstance(v, (int, float, str, bool)) or v is None else str(v))
                                 for k, v in list(values.items())[:60]})
                continue
            if name != 'record':
                continue
            locator = f'record[{index}]'
            index += 1
            lat, lon = semicircles_to_degrees(values.get('position_lat')), semicircles_to_degrees(values.get('position_long'))
            if lat is None or lon is None:
                yield ('issue', 'record_without_position', {'locator': locator})
                continue
            try:
                geo.check_range(lat, lon)
            except geo.CoordinateError as exc:
                yield ('issue', exc.code, {'locator': locator})
                continue
            coord = geo.Coordinate(lat, lon, 7, 'fit_semicircles')
            time = fit_time(values.get('timestamp'), f'{source}:record.timestamp')
            altitude = values.get('enhanced_altitude', values.get('altitude'))
            stats.add(coord, time, altitude)
            yield ('fix', locator, {'coord': coord, 'time': time, 'altitude_m': geo.finite(altitude),
                                    'speed_mps': geo.finite(values.get('enhanced_speed', values.get('speed'))),
                                    'fix_kind': 'track_point', 'signal_basis': 'raw_sample',
                                    'provider_source': 'fit', 'extra': {}})
    except Exception as exc:  # a broken FIT file is a parse result, not a crash
        yield ('issue', 'fit_decode_failed', {'error': f'{type(exc).__name__}: {exc}'[:300]})
        yield ('coverage', {'complete': False})
        sessions = locals().get('sessions', [])
    start, end = stats.times()
    if stats.points:
        yield ('event', {'event_type': 'location_track', 'signal_class': 'passive', 'basis': 'raw_sample',
                         'locator': {'format': 'fit', 'section': 'record', 'index': 'all'},
                         'start': start, 'end': end, 'subject': 'FIT activity',
                         'metadata': {'track': stats.summary(), 'device_sessions_raw': sessions,
                                      'adapter': status()}})


def import_fit(path: Path, *, enable_location: bool = False, store_decimals: int | None = None,
               stream: str | None = None, sample_session_gap_s: int = 1800, source_locator: str | None = None,
               original_name: str | None = None, collection_id: int | None = None, label: str | None = None,
               limits=None) -> dict:
    def check():
        if _module() is None:
            raise FormatUnavailable('FIT adapter unavailable: install the python package fitparse to import FIT files; '
                                    'nothing was acquired')
    params = ImportParams(store_decimals=store_decimals, stream=stream, sample_session_gap_s=sample_session_gap_s)
    return run_import(path, importer=PARSER_NAME, parser_version=PARSER_VERSION, parse=_parse,
                      enable_location=enable_location, params=params, source_kind='gps_track_file',
                      source_label='GPS track files', provider='fit_file', mime_type='application/vnd.ant.fit',
                      detect=lambda _p: 'fit', source_locator=source_locator, original_name=original_name,
                      collection_id=collection_id, label=label, limits=limits or DEFAULT_LIMITS, before_acquire=check)

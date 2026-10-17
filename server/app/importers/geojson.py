"""GeoJSON points and lines with time properties (opt-in ``enable_location``; parser ``geojson`` v1).

The ``features`` array is streamed with bounded memory. Coordinates are
``[lon, lat(, alt)]`` (RFC 7946). Time property names are **parameters** (data,
recorded per run): ``time_properties`` (point/feature time), ``start_properties``,
``end_properties`` and ``coord_times_properties`` (one time per vertex, e.g.
``coordTimes`` or ``coordinateProperties.times``; dotted paths allowed);
``epoch_unit`` (``s``/``ms``) says how numeric times are read — without it a
number is not a time (reported). ``accuracy_properties`` name an accuracy in metres.

* ``Point``/``MultiPoint`` with a time -> raw samples (``geojson_point``,
  ``raw_sample``); without a time -> declared points (stored, shown on the map,
  not on the timeline);
* ``LineString``/``MultiLineString`` with per-vertex times -> raw samples and a
  ``location_track`` event; without them -> vertices (``declared``) and a
  ``location_route`` event (feature start/end properties give its span);
* polygons are areas, not traces: counted and skipped; ``GeometryCollection`` recurses.
"""
from __future__ import annotations

from pathlib import Path

from ..importers.comm_common import epoch_candidate
from ..location import geo, jsonstream
from ..location.ingest import DEFAULT_LIMITS, ImportParams, run_import
from ..location.tracks import TrackStats
from ..timeinfo import from_iso

PARSER_NAME = 'geojson'
PARSER_VERSION = '1'
DEFAULT_OPTIONS = {
    'time_properties': ['time', 'timestamp', 'datetime', 'date', 'when'],
    'start_properties': ['start', 'begin', 'startTime', 'start_time', 'timeStart'],
    'end_properties': ['end', 'endTime', 'end_time', 'timeEnd'],
    'coord_times_properties': ['coordTimes', 'coordinateProperties.times', 'times'],
    'accuracy_properties': ['accuracy', 'accuracy_m', 'horizontal_accuracy'],
    'epoch_unit': None,
}
_LISTS = ('time_properties', 'start_properties', 'end_properties', 'coord_times_properties', 'accuracy_properties')


def normalize_options(options: dict | None) -> dict:
    merged = {**DEFAULT_OPTIONS, **(options or {})}
    unknown = sorted(set(merged) - set(DEFAULT_OPTIONS))
    if unknown:
        raise ValueError('unknown GeoJSON options: ' + ', '.join(unknown))
    for key in _LISTS:
        value = merged[key]
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or len(value) > 50 or not all(isinstance(v, str) and 0 < len(v) <= 200 for v in value):
            raise ValueError(f'{key} must be a list of up to 50 property names')
        merged[key] = list(value)
    if merged['epoch_unit'] not in (None, 's', 'ms'):
        raise ValueError('epoch_unit must be null, "s" or "ms"')
    return merged


def _lookup(properties, path: str):
    value = properties
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _first(properties, names):
    for name in names:
        value = _lookup(properties, name)
        if value is not None:
            return name, value
    return None, None


def _time(value, kind: str, source: str, epoch_unit):
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, 'time_unparsed'
    if isinstance(value, (int, float)):
        if epoch_unit is None:
            return None, 'time_unit_unknown'
        try:
            if isinstance(value, float) and not float(value).is_integer():
                return None, 'time_unparsed'
            return epoch_candidate(kind, str(int(value)), source, unit=epoch_unit), None
        except (ValueError, OverflowError):
            return None, 'time_unparsed'
    if isinstance(value, str):
        try:
            return from_iso(kind, value.strip(), source), None
        except ValueError:
            return None, 'time_unparsed'
    return None, 'time_unparsed'


def _position(value, locator: str):
    if not isinstance(value, list) or len(value) < 2:
        raise geo.CoordinateError('coordinate_unparsed', 'position must be [lon, lat(, alt)]', locator=locator)
    lon, lat = value[0], value[1]
    if isinstance(lon, bool) or isinstance(lat, bool) or not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        raise geo.CoordinateError('coordinate_unparsed', 'position values must be numbers', locator=locator)
    coord = geo.from_values(lat, lon, lat_text=getattr(lat, 'text', None) or repr(lat),
                            lon_text=getattr(lon, 'text', None) or repr(lon), basis='float_value')
    altitude = geo.finite(value[2]) if len(value) > 2 else None
    return coord, altitude


def _parse(path: Path, ctx):
    options = normalize_options(ctx.params.format_options)
    source = f'{PARSER_NAME}:{PARSER_VERSION}'
    report = jsonstream.StreamReport()
    counts = {'features': 0, 'points_timed': 0, 'points_untimed': 0, 'lines': 0, 'areas_skipped': 0,
              'null_geometries': 0}
    top: dict = {}
    with path.open('rb') as handle:
        for key, index, value in jsonstream.iter_document(handle, max_element_bytes=ctx.limits.max_element_bytes,
                                                          report=report, array_keys=('features',)):
            if key == 'features' and index is not None:
                counts['features'] += 1
                yield from _feature(value, f'features[{index}]', ctx, options, source, counts)
            elif key == '$':
                counts['features'] += 1
                yield from _feature(value, f'$[{index}]', ctx, options, source, counts)
            elif index is None:
                top[key] = value
    if report.top == 'object' and top.get('type') == 'Feature':
        counts['features'] += 1
        yield from _feature(top, 'feature', ctx, options, source, counts)
    elif report.top == 'object' and top.get('type') not in (None, 'FeatureCollection', 'Feature'):
        yield from _geometry(top, {}, 'geometry', ctx, options, source, counts, feature_index='geometry')
    elif report.top == 'object' and 'features' not in top and top.get('type') == 'FeatureCollection' and not counts['features']:
        yield ('issue', 'feature_collection_without_features', {})
    ctx.counts['geojson'] = {**counts, 'options': options, 'json_stream': report.as_dict()}
    for item in report.issues[:50]:
        yield ('issue', 'json_stream_' + item['code'], {k: v for k, v in item.items() if k != 'code'})
    if not report.complete:
        yield ('coverage', {'complete': False, 'json_stream_complete': False})


def _feature(feature, locator: str, ctx, options, source, counts):
    if not isinstance(feature, dict) or feature.get('type') != 'Feature':
        yield ('issue', 'not_a_feature', {'locator': locator})
        return
    properties = feature.get('properties') if isinstance(feature.get('properties'), dict) else {}
    geometry = feature.get('geometry')
    if geometry is None:
        counts['null_geometries'] += 1
        return
    yield from _geometry(geometry, properties, locator, ctx, options, source, counts, feature_index=locator)


def _geometry(geometry, properties, locator, ctx, options, source, counts, *, feature_index, depth: int = 0):
    if not isinstance(geometry, dict):
        yield ('issue', 'geometry_not_an_object', {'locator': locator})
        return
    gtype = geometry.get('type')
    coordinates = geometry.get('coordinates')
    if gtype == 'GeometryCollection' and depth < 4:
        for g_index, child in enumerate((geometry.get('geometries') or [])[:1000]):
            yield from _geometry(child, properties, f'{locator}/geometries[{g_index}]', ctx, options, source, counts,
                                 feature_index=feature_index, depth=depth + 1)
        return
    if gtype in ('Polygon', 'MultiPolygon'):
        counts['areas_skipped'] += 1
        yield ('issue', 'geometry_not_a_location_trace', {'locator': locator, 'geometry': gtype})
        return
    name, raw_time = _first(properties, options['time_properties'])
    _, raw_start = _first(properties, options['start_properties'])
    _, raw_end = _first(properties, options['end_properties'])
    accuracy = geo.finite(_first(properties, options['accuracy_properties'])[1])
    feature_time, problem = _time(raw_time, 'observed', f'{source}:properties.{name}', options['epoch_unit'])
    if problem:
        yield ('issue', problem, {'locator': locator, 'property': name})
    if gtype in ('Point', 'MultiPoint'):
        positions = [coordinates] if gtype == 'Point' else (coordinates if isinstance(coordinates, list) else [])
        for p_index, position in enumerate(positions):
            where = f'{locator}/{gtype}[{p_index}]' if gtype == 'MultiPoint' else f'{locator}/Point'
            try:
                coord, altitude = _position(position, where)
            except geo.CoordinateError as exc:
                yield ('issue', exc.code, {'locator': where, **{k: v for k, v in exc.details.items() if k != 'locator'}})
                continue
            timed = feature_time is not None
            counts['points_timed' if timed else 'points_untimed'] += 1
            yield ('fix', where, {'coord': coord, 'time': feature_time,
                                  'time_raw': str(raw_time)[:64] if raw_time is not None and not timed else None,
                                  'accuracy_m': accuracy, 'altitude_m': altitude,
                                  'fix_kind': 'geojson_point', 'signal_basis': 'raw_sample' if timed else 'declared',
                                  'provider_source': 'geojson', 'extra': {}})
        return
    if gtype in ('LineString', 'MultiLineString'):
        lines = [coordinates] if gtype == 'LineString' else (coordinates if isinstance(coordinates, list) else [])
        times_name, raw_times = _first(properties, options['coord_times_properties'])
        if gtype == 'LineString' and isinstance(raw_times, list) and raw_times and not isinstance(raw_times[0], list):
            raw_times = [raw_times]
        has_times = isinstance(raw_times, list) and len(raw_times) > 0
        stats = TrackStats(ctx.params.store_decimals)
        for l_index, line in enumerate(lines):
            stats.new_segment()
            line_times = raw_times[l_index] if has_times and l_index < len(raw_times) and isinstance(raw_times[l_index], list) else None
            if has_times and (line_times is None or len(line_times) != len(line or [])):
                yield ('issue', 'coord_times_count_mismatch', {'locator': f'{locator}/line[{l_index}]',
                                                               'property': times_name})
            for v_index, position in enumerate(line if isinstance(line, list) else []):
                if v_index >= ctx.limits.max_path_points:
                    yield ('issue', 'path_point_limit', {'limit': ctx.limits.max_path_points})
                    break
                where = f'{locator}/line[{l_index}]/coord[{v_index}]'
                try:
                    coord, altitude = _position(position, where)
                except geo.CoordinateError as exc:
                    yield ('issue', exc.code, {'locator': where})
                    continue
                vertex_time = None
                if line_times is not None and v_index < len(line_times):
                    vertex_time, problem = _time(line_times[v_index], 'observed', f'{source}:properties.{times_name}',
                                                 options['epoch_unit'])
                    if problem:
                        yield ('issue', problem, {'locator': where, 'property': times_name})
                stats.add(coord, vertex_time, altitude)
                yield ('fix', where, {'coord': coord, 'time': vertex_time, 'altitude_m': altitude,
                                      'fix_kind': 'track_point' if has_times else 'line_vertex',
                                      'signal_basis': 'raw_sample' if has_times else 'declared',
                                      'provider_source': 'geojson', 'extra': {}})
        counts['lines'] += 1
        start, end = stats.times() if has_times else (None, None)
        if not has_times:
            start, problem = _time(raw_start if raw_start is not None else raw_time, 'start',
                                   f'{source}:properties.start', options['epoch_unit'])
            end, problem2 = _time(raw_end, 'end', f'{source}:properties.end', options['epoch_unit'])
            for p in (problem, problem2):
                if p:
                    yield ('issue', p, {'locator': locator})
        label = properties.get('name') if isinstance(properties.get('name'), str) else None
        yield ('event', {'event_type': 'location_track' if has_times else 'location_route',
                         'signal_class': 'passive' if has_times else 'declared',
                         'basis': 'raw_sample' if has_times else 'declared',
                         'locator': {'format': 'geojson', 'section': 'features', 'index': locator},
                         'start': start, 'end': end, 'subject': (label or ('track' if has_times else 'path'))[:200],
                         'metadata': {'track': stats.summary(),
                                      'declared_text': {'name': label[:ctx.limits.max_raw_chars]} if label else {},
                                      'time_properties_used': {'coord_times': times_name if has_times else None}}})
        return
    yield ('issue', 'geometry_type_unrecognised', {'locator': locator, 'type': str(gtype)[:40]})


def import_geojson(path: Path, *, enable_location: bool = False, store_decimals: int | None = None,
                   stream: str | None = None, options: dict | None = None, sample_session_gap_s: int = 1800,
                   source_locator: str | None = None, original_name: str | None = None,
                   collection_id: int | None = None, label: str | None = None, limits=None) -> dict:
    normalized = normalize_options(options)
    params = ImportParams(store_decimals=store_decimals, stream=stream, sample_session_gap_s=sample_session_gap_s,
                          format_options=normalized)
    return run_import(path, importer=PARSER_NAME, parser_version=PARSER_VERSION, parse=_parse,
                      enable_location=enable_location, params=params, source_kind='gps_track_file',
                      source_label='GPS track files', provider='geojson_file', mime_type='application/geo+json',
                      detect=lambda _p: 'geojson', source_locator=source_locator, original_name=original_name,
                      collection_id=collection_id, label=label, limits=limits or DEFAULT_LIMITS)

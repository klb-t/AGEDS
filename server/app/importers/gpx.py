"""GPX 1.0/1.1 tracks, routes and waypoints (opt-in ``enable_location``; parser ``gpx`` v1).

Streaming (pyexpat, entity-free, bounded): every ``<trkpt>`` becomes a raw sample
in ``location_fixes`` (``fix_kind=track_point``, ``signal_basis=raw_sample``);
each ``<trk>`` becomes one ``location_track`` event whose statistics (points,
segments, haversine distance inside segments, duration, time-order violations,
bounding box) are computed by code. ``<rte>`` becomes a ``location_route`` event
and ``<wpt>`` a ``location_waypoint`` event, both ``signal.class=declared`` (a
planned route or a marked point is not an observed movement). ``<time>`` is
optional; a time without an offset stays ``unknown`` unless the per-run option
``gpx_naive_time=utc_per_schema`` applies the GPX schema's statement that times
are UTC (recorded on every candidate). ``<extensions>`` are not interpreted; a
bounded raw rendering is kept per point. HDOP/VDOP/PDOP and satellite counts are
kept raw; they are not converted to an accuracy in metres.
"""
from __future__ import annotations

from pathlib import Path

from ..location import geo, xmlstream
from ..location.ingest import DEFAULT_LIMITS, ImportParams, run_import
from ..location.tracks import TrackStats
from ..timeinfo import TimeCandidate, from_iso

PARSER_NAME = 'gpx'
PARSER_VERSION = '1'
NAIVE_POLICIES = ('unknown', 'utc_per_schema')
_POINT = {'trkpt': ('track_point', 'raw_sample'), 'rtept': ('route_point', 'declared'), 'wpt': ('waypoint', 'declared')}
_POINT_FIELDS = ('ele', 'time', 'hdop', 'vdop', 'pdop', 'sat', 'fix', 'name', 'desc', 'cmt', 'sym', 'type', 'speed',
                 'course', 'magvar', 'geoidheight', 'src', 'ageofdgpsdata', 'dgpsid')
_TEXT_LIMIT = 4096


def gpx_time(text: str | None, source: str, policy: str, kind: str = 'observed') -> tuple[TimeCandidate | None, str | None]:
    if text is None or not text.strip():
        return None, None
    try:
        candidate = from_iso(kind, text.strip(), source)
    except ValueError:
        return None, 'time_unparsed'
    if candidate.tz_status == 'unknown' and policy == 'utc_per_schema':
        utc = from_iso(kind, text.strip() + 'Z', source,
                       metadata={'zone_declaration': 'gpx_schema_declares_utc', 'raw_had_no_offset': True})
        return TimeCandidate(kind, text.strip(), source, utc=utc.utc, local=utc.local, tz_status='explicit_offset',
                             tz='UTC', precision=utc.precision, metadata=utc.metadata), None
    return candidate, None


def _parse(path: Path, ctx):
    policy = ctx.params.format_options.get('gpx_naive_time', 'unknown')
    store_decimals = ctx.params.store_decimals
    raw_limit = ctx.limits.max_raw_chars
    report = xmlstream.XmlReport()
    source = f'{PARSER_NAME}:{PARSER_VERSION}'
    counters = {'trk': 0, 'rte': 0, 'wpt': 0}
    seg = pt = 0
    container = None  # {'kind', 'index', 'stats', 'fields'}
    point = None
    field_name = None
    ext_depth = None
    meta_time = None
    in_metadata = False
    root_checked = False
    creator = version = None
    with path.open('rb') as handle:
        for ev in xmlstream.iter_events(handle, report):
            kind, name = ev[0], ev[1]
            if not root_checked and kind == 'start':
                root_checked = True
                if name != 'gpx':
                    yield ('issue', 'not_a_gpx_document', {'root': name[:64]})
                    yield ('coverage', {'complete': False})
                    return
                creator, version = ev[2].get('creator'), ev[2].get('version')
                continue
            if kind == 'start':
                if ext_depth is not None:
                    if point is not None and len(point['ext']) < raw_limit:
                        point['ext'] += f'<{name}>'
                    continue
                if name == 'extensions':
                    ext_depth = ev[3]
                    continue
                if name == 'metadata':
                    in_metadata = True
                elif name in ('trk', 'rte') and container is None:
                    container = {'kind': name, 'index': counters[name], 'stats': TrackStats(store_decimals), 'fields': {}}
                    counters[name] += 1
                    seg = pt = 0
                elif name == 'trkseg' and container is not None:
                    container['stats'].new_segment()
                    seg_index = seg
                    seg += 1
                    pt = 0
                    container['seg'] = seg_index
                elif name in _POINT:
                    point = {'tag': name, 'attrs': ev[2], 'fields': {}, 'ext': ''}
                    if name == 'wpt':
                        point['index'] = counters['wpt']
                        counters['wpt'] += 1
                    else:
                        point['index'] = pt
                        pt += 1
                elif point is not None and name in _POINT_FIELDS:
                    field_name = name
                    point['fields'][name] = ''
                elif container is not None and point is None and name in ('name', 'desc', 'type', 'cmt', 'src', 'number'):
                    field_name = name
                    container['fields'][name] = ''
                elif in_metadata and name in ('time', 'name', 'desc'):
                    field_name = f'metadata.{name}'
                    meta_time = meta_time or {}
                    meta_time[name] = ''
                continue
            if kind == 'text':
                if ext_depth is not None:
                    if point is not None and len(point['ext']) < raw_limit:
                        point['ext'] += ev[2].strip()[:raw_limit]
                elif field_name and point is not None and field_name in point['fields']:
                    if len(point['fields'][field_name]) < _TEXT_LIMIT:
                        point['fields'][field_name] += ev[2]
                elif field_name and container is not None and field_name in container['fields']:
                    if len(container['fields'][field_name]) < _TEXT_LIMIT:
                        container['fields'][field_name] += ev[2]
                elif field_name and field_name.startswith('metadata.'):
                    meta_time[field_name.split('.', 1)[1]] += ev[2][:_TEXT_LIMIT]
                continue
            # end
            if ext_depth is not None:
                if ev[2] == ext_depth and name == 'extensions':
                    ext_depth = None
                elif point is not None and len(point['ext']) < raw_limit:
                    point['ext'] += f'</{name}>'
                continue
            if name == field_name or (field_name and field_name.endswith('.' + name)):
                field_name = None
                continue
            if name == 'metadata':
                in_metadata = False
                continue
            if name in _POINT and point is not None:
                yield from _point(point, container, source=source, policy=policy, ctx=ctx)
                point = None
                continue
            if name in ('trk', 'rte') and container is not None and container['kind'] == name:
                yield ('event', _container_event(container, source=source, creator=creator, version=version,
                                                 raw_limit=raw_limit))
                container = None
                continue
    ctx.counts['gpx'] = {'version': version, 'creator': (creator or '')[:200] or None, **counters,
                         'xml': report.as_dict()}
    if meta_time and meta_time.get('time'):
        ctx.counts['gpx']['metadata_time_raw'] = meta_time['time'].strip()[:64]
    if not report.complete:
        yield ('issue', 'xml_doctype_refused' if report.doctype_refused else
               'xml_truncated' if report.truncated else 'xml_malformed',
               {'error': (report.error or '')[:200], 'line': report.error_line})
        yield ('coverage', {'complete': False, 'xml': report.as_dict()})


def _point(point: dict, container: dict | None, *, source: str, policy: str, ctx):
    tag = point['tag']
    fix_kind, basis = _POINT[tag]
    if tag == 'wpt':
        locator = f'wpt[{point["index"]}]'
    elif container is not None and tag == 'trkpt':
        locator = f'trk[{container["index"]}]/trkseg[{container.get("seg", 0)}]/trkpt[{point["index"]}]'
    elif container is not None:
        locator = f'rte[{container["index"]}]/rtept[{point["index"]}]'
    else:
        locator = f'{tag}[orphan:{point["index"]}]'
        yield ('issue', 'point_outside_container', {'locator': locator})
    attrs = point['attrs']
    try:
        coord = geo.from_values(attrs.get('lat'), attrs.get('lon'), lat_text=attrs.get('lat'), lon_text=attrs.get('lon'),
                                basis='decimal_text')
    except geo.CoordinateError as exc:
        yield ('issue', exc.code, {'locator': locator, **exc.details})
        return
    fields = {k: v.strip() for k, v in point['fields'].items()}
    time, problem = gpx_time(fields.get('time'), f'{source}:{tag}.time', policy)
    if problem:
        yield ('issue', problem, {'locator': locator, 'raw': fields.get('time', '')[:64]})
    extra = {k: fields[k][:256] for k in ('hdop', 'vdop', 'pdop', 'sat', 'fix', 'magvar', 'geoidheight', 'dgpsid',
                                          'ageofdgpsdata') if fields.get(k)}
    if point['ext']:
        extra['extensions_raw'] = point['ext'][:ctx.limits.max_raw_chars]
        extra['extensions'] = 'kept_raw_not_interpreted'
    fix = {'coord': coord, 'time': time, 'time_raw': fields.get('time') if time is None else None,
           'altitude_m': geo.finite(fields.get('ele')), 'speed_mps': geo.finite(fields.get('speed')),
           'heading_deg': geo.finite(fields.get('course')), 'fix_kind': fix_kind, 'signal_basis': basis,
           'provider_source': 'gpx', 'extra': extra}
    if container is not None:
        container['stats'].add(coord, time, fields.get('ele'))
    yield ('fix', locator, fix)
    if tag == 'wpt':
        lat, lon, decimals = geo.round_to(coord, ctx.params.store_decimals)
        yield ('event', {
            'event_type': 'location_waypoint', 'signal_class': 'declared', 'basis': 'declared',
            'locator': {'format': 'gpx', 'section': 'wpt', 'index': point['index']},
            'start': None if time is None else TimeCandidate('start', time.raw, time.source, utc=time.utc,
                                                            local=time.local, tz_status=time.tz_status, tz=time.tz,
                                                            precision=time.precision, metadata=time.metadata),
            'end': None, 'subject': (fields.get('name') or 'waypoint')[:200],
            'metadata': {'location': {'lat': lat, 'lon': lon, 'decimals': decimals, 'source_decimals': coord.decimals},
                         'declared_text': {k: fields[k][:ctx.limits.max_raw_chars] for k in ('name', 'desc', 'cmt', 'sym', 'type')
                                           if fields.get(k)},
                         'time_raw': fields.get('time') or None}})


def _container_event(container: dict, *, source: str, creator, version, raw_limit: int) -> dict:
    stats: TrackStats = container['stats']
    summary = stats.summary()
    fields = {k: v.strip()[:raw_limit] for k, v in container['fields'].items() if v.strip()}
    is_track = container['kind'] == 'trk'
    start, end = stats.times()
    return {'event_type': 'location_track' if is_track else 'location_route',
            'signal_class': 'passive' if is_track else 'declared', 'basis': 'raw_sample' if is_track else 'declared',
            'locator': {'format': 'gpx', 'section': container['kind'], 'index': container['index']},
            'start': start, 'end': end, 'subject': (fields.get('name') or ('track' if is_track else 'route'))[:200],
            'metadata': {'track': summary, 'declared_text': fields,
                         'producer': {'creator': (creator or '')[:200] or None, 'gpx_version': version}}}


def import_gpx(path: Path, *, enable_location: bool = False, store_decimals: int | None = None,
               stream: str | None = None, gpx_naive_time: str = 'unknown', sample_session_gap_s: int = 1800,
               source_locator: str | None = None, original_name: str | None = None,
               collection_id: int | None = None, label: str | None = None, limits=None) -> dict:
    if gpx_naive_time not in NAIVE_POLICIES:
        raise ValueError(f'gpx_naive_time must be one of {NAIVE_POLICIES}')
    params = ImportParams(store_decimals=store_decimals, stream=stream, sample_session_gap_s=sample_session_gap_s,
                          format_options={'gpx_naive_time': gpx_naive_time})
    return run_import(path, importer=PARSER_NAME, parser_version=PARSER_VERSION, parse=_parse,
                      enable_location=enable_location, params=params, source_kind='gps_track_file',
                      source_label='GPS track files', provider='gpx_file', mime_type='application/gpx+xml',
                      detect=lambda _p: 'gpx', source_locator=source_locator, original_name=original_name,
                      collection_id=collection_id, label=label, limits=limits or DEFAULT_LIMITS)

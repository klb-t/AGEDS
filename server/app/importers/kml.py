"""KML and KMZ placemarks, paths and ``gx:Track`` tracks (opt-in ``enable_location``; parser ``kml`` v1).

Streaming (pyexpat, entity-free; ``<coordinates>`` parsed in chunks):

* ``gx:Track`` (also inside ``gx:MultiTrack``): ``<when>`` and ``<gx:coord>`` paired by
  position -> raw samples (``track_point``) and one ``location_track`` event with
  statistics computed by code; unequal counts are reported and only pairs are used;
* ``LineString`` -> vertices without sample times (``line_vertex``, ``declared``) and a
  ``location_route`` event; a ``TimeSpan`` of the placemark gives its start/end;
* ``Point`` -> a ``location_waypoint`` event (``declared``) with the placemark's
  ``TimeStamp`` (or ``TimeSpan`` begin) as its time;
* ``Polygon``/``LinearRing``/``Model`` are areas or models, not traces: counted and skipped.

KML coordinates are ``lon,lat[,alt]``. A time without offset stays ``unknown``.
KMZ: the root ``doc.kml`` (else the first ``.kml`` by name) of the zip is read with
member-size limits; other members (icons, images) are listed, not imported.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from ..location import geo, xmlstream
from ..location.ingest import DEFAULT_LIMITS, ImportParams, run_import
from ..location.tracks import TrackStats
from ..timeinfo import from_iso

PARSER_NAME = 'kml'
PARSER_VERSION = '1'
_AREAS = ('Polygon', 'LinearRing', 'Model', 'gx:LatLonQuad', 'LatLonBox')
_TEXT_LIMIT = 4096


def _time(text, kind: str, source: str):
    if not text or not text.strip():
        return None, None
    try:
        return from_iso(kind, text.strip(), source), None
    except ValueError:
        return None, 'time_unparsed'


def _tuple(token: str, lon_first: bool = True, separator: str = ','):
    parts = token.split(separator) if separator == ',' else token.split()
    if len(parts) < 2:
        raise geo.CoordinateError('coordinate_unparsed', 'coordinate tuple needs at least lon and lat', raw=token[:64])
    lon_text, lat_text = (parts[0], parts[1]) if lon_first else (parts[1], parts[0])
    coord = geo.from_values(lat_text, lon_text, lat_text=lat_text, lon_text=lon_text, basis='decimal_text')
    altitude = geo.finite(parts[2]) if len(parts) > 2 else None
    return coord, altitude


class _Coordinates:
    """Incremental ``lon,lat[,alt]`` tuple splitter for chunked character data."""

    def __init__(self):
        self.rest = ''

    def feed(self, text: str):
        data = self.rest + text
        tokens = data.split()
        if data and not data[-1].isspace() and tokens:
            self.rest = tokens.pop()
        else:
            self.rest = ''
        return tokens

    def close(self):
        tokens = self.rest.split()
        self.rest = ''
        return tokens


def parse_stream(handle, ctx, *, prefix: str = '', fmt: str = 'kml'):
    store_decimals = ctx.params.store_decimals
    source = f'{PARSER_NAME}:{PARSER_VERSION}'
    report = xmlstream.XmlReport()
    placemarks = 0
    skipped_areas = 0
    pm = None
    geom = None
    text_target = None
    root_checked = False
    for ev in xmlstream.iter_events(handle, report):
        kind, name = ev[0], ev[1]
        if not root_checked and kind == 'start':
            root_checked = True
            if name != 'kml':
                yield ('issue', 'not_a_kml_document', {'root': name[:64]})
                yield ('coverage', {'complete': False})
                return
            continue
        if kind == 'start':
            if name == 'Placemark':
                pm = {'index': placemarks, 'fields': {}, 'times': {}, 'geoms': 0}
                placemarks += 1
            elif pm is None:
                continue
            elif name in ('Point', 'LineString', 'gx:Track') and geom is None:
                geom = {'type': name, 'index': pm['geoms'], 'stats': TrackStats(store_decimals), 'coords': _Coordinates(),
                        'whens': [], 'gx': [], 'n': 0, 'first': None}
                geom['stats'].new_segment()
                pm['geoms'] += 1
            elif name in _AREAS:
                skipped_areas += 1
                yield ('issue', 'geometry_not_a_location_trace', {'locator': f'{prefix}Placemark[{pm["index"]}]',
                                                                  'geometry': name})
            elif name == 'coordinates' and geom is not None and geom['type'] in ('Point', 'LineString'):
                text_target = 'coordinates'
            elif name == 'when' and geom is not None and geom['type'] == 'gx:Track':
                text_target = 'track_when'
                geom['whens'].append('')
            elif name == 'gx:coord' and geom is not None:
                text_target = 'track_coord'
                geom['gx'].append('')
            elif name in ('when', 'begin', 'end') and geom is None:
                text_target = f'time:{name}'
                pm['times'][name] = ''
            elif name in ('name', 'description') and geom is None:
                text_target = f'field:{name}'
                pm['fields'][name] = ''
            continue
        if kind == 'text':
            if text_target == 'coordinates':
                for token in geom['coords'].feed(ev[2]):
                    yield from _vertex(token, geom, pm, prefix=prefix, ctx=ctx, source=source)
            elif text_target == 'track_when' and len(geom['whens'][-1]) < 128:
                geom['whens'][-1] += ev[2]
            elif text_target == 'track_coord' and len(geom['gx'][-1]) < 256:
                geom['gx'][-1] += ev[2]
            elif text_target and text_target.startswith('time:') and pm is not None:
                key = text_target.split(':', 1)[1]
                pm['times'][key] = (pm['times'][key] + ev[2])[:128]
            elif text_target and text_target.startswith('field:') and pm is not None:
                key = text_target.split(':', 1)[1]
                if len(pm['fields'][key]) < _TEXT_LIMIT:
                    pm['fields'][key] += ev[2]
            continue
        # end
        if name == 'coordinates' and text_target == 'coordinates':
            for token in geom['coords'].close():
                yield from _vertex(token, geom, pm, prefix=prefix, ctx=ctx, source=source)
            text_target = None
        elif name in ('when', 'gx:coord', 'begin', 'end', 'name', 'description'):
            text_target = None
        elif geom is not None and name == geom['type']:
            if geom['type'] == 'gx:Track':
                yield from _track_points(geom, pm, prefix=prefix, ctx=ctx, source=source)
            yield from _geometry_event(geom, pm, prefix=prefix, ctx=ctx, source=source, fmt=fmt)
            geom = None
        elif name == 'Placemark':
            pm = None
    ctx.counts.setdefault('kml', {})
    ctx.counts['kml'].update({'placemarks': placemarks, 'areas_skipped': skipped_areas, 'xml': report.as_dict()})
    if not report.complete:
        yield ('issue', 'xml_doctype_refused' if report.doctype_refused else
               'xml_truncated' if report.truncated else 'xml_malformed',
               {'error': (report.error or '')[:200], 'line': report.error_line})
        yield ('coverage', {'complete': False, 'xml': report.as_dict()})


def _placemark_times(pm, source):
    times = pm['times']
    out = {}
    for key in ('when', 'begin', 'end'):
        if times.get(key):
            kind = {'when': 'start', 'begin': 'start', 'end': 'end'}[key]
            cand, problem = _time(times[key], kind, f'{source}:{"TimeStamp" if key == "when" else "TimeSpan"}.{key}')
            out[key] = (cand, problem)
    return out


def _vertex(token: str, geom, pm, *, prefix, ctx, source):
    index = geom['n']
    geom['n'] += 1
    locator = f'{prefix}Placemark[{pm["index"]}]/{geom["type"]}[{geom["index"]}]/coord[{index}]'
    try:
        coord, altitude = _tuple(token)
    except geo.CoordinateError as exc:
        yield ('issue', exc.code, {'locator': locator, **exc.details})
        return
    if geom['type'] == 'Point':
        if geom['first'] is None:
            geom['first'] = (coord, altitude)
        return
    geom['stats'].add(coord, None, altitude)
    yield ('fix', locator, {'coord': coord, 'time': None, 'altitude_m': altitude, 'fix_kind': 'line_vertex',
                            'signal_basis': 'declared', 'provider_source': 'kml', 'extra': {}})


def _track_points(geom, pm, *, prefix, ctx, source):
    whens, coords = geom['whens'], geom['gx']
    if len(whens) != len(coords):
        yield ('issue', 'track_when_coord_count_mismatch',
               {'locator': f'{prefix}Placemark[{pm["index"]}]/gx:Track[{geom["index"]}]', 'when': len(whens),
                'coord': len(coords)})
    for index, (when, text) in enumerate(zip(whens, coords)):
        if index >= ctx.limits.max_path_points:
            yield ('issue', 'path_point_limit', {'limit': ctx.limits.max_path_points})
            break
        locator = f'{prefix}Placemark[{pm["index"]}]/gx:Track[{geom["index"]}]/point[{index}]'
        try:
            coord, altitude = _tuple(text.strip(), separator=' ')
        except geo.CoordinateError as exc:
            yield ('issue', exc.code, {'locator': locator, **exc.details})
            continue
        time, problem = _time(when, 'observed', f'{source}:gx:Track.when')
        if problem:
            yield ('issue', problem, {'locator': locator, 'raw': when.strip()[:64]})
        geom['stats'].add(coord, time, altitude)
        yield ('fix', locator, {'coord': coord, 'time': time, 'time_raw': when.strip()[:64] if time is None else None,
                                'altitude_m': altitude, 'fix_kind': 'track_point', 'signal_basis': 'raw_sample',
                                'provider_source': 'kml', 'extra': {}})


def _geometry_event(geom, pm, *, prefix, ctx, source, fmt):
    fields = {k: v.strip()[:ctx.limits.max_raw_chars] for k, v in pm['fields'].items() if v.strip()}
    times = _placemark_times(pm, source)
    for key, (cand, problem) in times.items():
        if problem:
            yield ('issue', problem, {'locator': f'{prefix}Placemark[{pm["index"]}]', 'field': key})
    when = times.get('when', (None, None))[0]
    begin = times.get('begin', (None, None))[0]
    end = times.get('end', (None, None))[0]
    locator = {'format': fmt, 'section': f'{prefix}Placemark', 'index': f'{pm["index"]}/{geom["type"]}[{geom["index"]}]'}
    if geom['type'] == 'Point':
        if geom['first'] is None:
            return
        coord, altitude = geom['first']
        yield ('fix', f'{prefix}Placemark[{pm["index"]}]/Point[{geom["index"]}]',
               {'coord': coord, 'time': when or begin, 'altitude_m': altitude, 'fix_kind': 'placemark_point',
                'signal_basis': 'declared', 'provider_source': 'kml', 'extra': {}})
        lat, lon, decimals = geo.round_to(coord, ctx.params.store_decimals)
        yield ('event', {'event_type': 'location_waypoint', 'signal_class': 'declared', 'basis': 'declared',
                         'locator': locator, 'start': when or begin, 'end': end if when is None else None,
                         'subject': (fields.get('name') or 'placemark')[:200],
                         'metadata': {'location': {'lat': lat, 'lon': lon, 'decimals': decimals,
                                                   'source_decimals': coord.decimals}, 'declared_text': fields}})
        return
    stats: TrackStats = geom['stats']
    if geom['type'] == 'gx:Track':
        start, stop = stats.times()
        yield ('event', {'event_type': 'location_track', 'signal_class': 'passive', 'basis': 'raw_sample',
                         'locator': locator, 'start': start, 'end': stop,
                         'subject': (fields.get('name') or 'track')[:200],
                         'metadata': {'track': stats.summary(), 'declared_text': fields}})
        return
    yield ('event', {'event_type': 'location_route', 'signal_class': 'declared', 'basis': 'declared',
                     'locator': locator, 'start': begin or when, 'end': end,
                     'subject': (fields.get('name') or 'path')[:200],
                     'metadata': {'track': stats.summary(), 'declared_text': fields,
                                  'note': 'path geometry without per-point sample times; a TimeSpan states the '
                                          'span the file assigns to the whole path'}})


def _parse_kml(path: Path, ctx):
    with path.open('rb') as handle:
        yield from parse_stream(handle, ctx)


def _parse_kmz(path: Path, ctx):
    limits = ctx.limits
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        yield ('issue', 'kmz_not_a_zip', {'error': str(exc)[:200]})
        yield ('coverage', {'complete': False})
        return
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_archive_members:
            yield ('issue', 'kmz_member_limit', {'members': len(infos)})
            infos = infos[:limits.max_archive_members]
        kml = [i for i in infos if i.filename.lower().endswith('.kml') and not i.is_dir()]
        chosen = next((i for i in kml if i.filename == 'doc.kml'), None) or \
            next(iter(sorted((i for i in kml if '/' not in i.filename), key=lambda i: i.filename)), None) or \
            next(iter(sorted(kml, key=lambda i: i.filename)), None)
        ctx.counts['kml'] = {'kmz_members': len(infos), 'kmz_kml_members': [i.filename[:200] for i in kml[:20]],
                             'kmz_member_used': chosen.filename[:200] if chosen else None}
        if chosen is None:
            yield ('issue', 'kmz_without_kml', {})
            yield ('coverage', {'complete': False})
            return
        if len(kml) > 1:
            yield ('issue', 'kmz_other_kml_members_not_imported', {'count': len(kml) - 1})
        if chosen.file_size > limits.max_archive_member_bytes:
            yield ('issue', 'kmz_member_too_large', {'size': chosen.file_size})
            yield ('coverage', {'complete': False})
            return
        with archive.open(chosen) as member:
            bounded = _Bounded(member, limits.max_archive_member_bytes)
            yield from parse_stream(bounded, ctx, prefix=f'{chosen.filename}!/', fmt='kmz')
            if bounded.exceeded:
                yield ('issue', 'kmz_member_too_large', {'read': bounded.read_bytes})
                yield ('coverage', {'complete': False})


class _Bounded:
    def __init__(self, handle, limit: int):
        self.handle, self.limit, self.read_bytes, self.exceeded = handle, limit, 0, False

    def read(self, size: int = -1) -> bytes:
        if self.read_bytes >= self.limit:
            self.exceeded = self.exceeded or bool(self.handle.read(1))
            return b''
        size = self.limit - self.read_bytes if size < 0 else min(size, self.limit - self.read_bytes)
        data = self.handle.read(size)
        self.read_bytes += len(data)
        return data


def _is_kmz(path: Path) -> bool:
    with path.open('rb') as handle:
        return handle.read(4) == b'PK\x03\x04'


def import_kml(path: Path, *, enable_location: bool = False, store_decimals: int | None = None,
               stream: str | None = None, sample_session_gap_s: int = 1800, source_locator: str | None = None,
               original_name: str | None = None, collection_id: int | None = None, label: str | None = None,
               limits=None) -> dict:
    """KML or KMZ (decided by the zip signature of the bytes, not by the name)."""
    params = ImportParams(store_decimals=store_decimals, stream=stream, sample_session_gap_s=sample_session_gap_s)
    kmz = _is_kmz(path)
    return run_import(path, importer=PARSER_NAME, parser_version=PARSER_VERSION, parse=_parse_kmz if kmz else _parse_kml,
                      enable_location=enable_location, params=params, source_kind='gps_track_file',
                      source_label='GPS track files', provider='kml_file',
                      mime_type='application/vnd.google-earth.kmz' if kmz else 'application/vnd.google-earth.kml+xml',
                      detect=lambda _p: 'kmz' if kmz else 'kml', source_locator=source_locator,
                      original_name=original_name, collection_id=collection_id, label=label,
                      limits=limits or DEFAULT_LIMITS)

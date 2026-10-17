"""Google location history / Timeline → coarse ``presence`` events (opt-in only).

Disabled unless ``enable_location`` is set; the opt-in and the coarsening are
recorded on every event and run. Processing is local. Event metadata keeps
only coordinates rounded to ``coarse_decimals`` (default 2 ≈ 1 km) and a
whitelist of non-identifying fields; exact coordinates, addresses, place names
and place IDs stay only in the preserved original file.

Formats: ``Records.json`` (raw points, aggregated into runs of the same coarse
cell), ``Semantic Location History`` monthly files (``timelineObjects``) and the
newer on-device ``Timeline.json`` (``semanticSegments``).

**Parser version 2** (``import_location_history_v2``, 2026-10-17, task L1) is the
current importer: raw samples go to ``location_fixes``/``location_signals`` at a
recorded stored precision (default: the source's), provider inferences become
``location_visit``/``location_segment``/``location_profile`` events, ``rawSignals``,
``userLocationProfile`` and the iOS ``location-history.json`` are read, and big files
are streamed with bounded memory (``server/app/location``, ``docs/LOCATION.md``).
Version 1 above stays callable and unchanged: its stored ``presence`` events are
never reinterpreted, and a v2 import of the same file adds separate rows.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..timeinfo import TimeCandidate, from_iso
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, add_event_with_times, epoch_candidate,
                          legacy_instant, record_import_run, stored_path)

PARSER_NAME = 'google_location'
PARSER_VERSION = '1'
_KEEP = ('activityType', 'confidence', 'probability', 'semanticType', 'placeVisitType', 'visitConfidence',
         'editConfirmationStatus', 'hierarchyLevel', 'type', 'distanceMeters', 'source')
_LATLNG = re.compile(r'^\s*(-?\d+(?:\.\d+)?)°?\s*,\s*(-?\d+(?:\.\d+)?)°?\s*$')


from ..location.ingest import LocationOptInRequired  # noqa: E402  (one opt-in error type for v1 and v2)


def _coarse(lat, lon, decimals: int):
    if lat is None or lon is None:
        return None
    return {'lat': round(float(lat), decimals), 'lon': round(float(lon), decimals), 'decimals': decimals}


def _e7(container: dict | None, decimals: int):
    if not isinstance(container, dict):
        return None
    lat, lon = container.get('latitudeE7'), container.get('longitudeE7')
    if isinstance(lat, int) and isinstance(lon, int):
        return _coarse(lat / 1e7, lon / 1e7, decimals)
    match = _LATLNG.match(str(container.get('latLng') or ''))
    if match:
        return _coarse(match.group(1), match.group(2), decimals)
    return None


def _time(raw, kind: str, source: str, issues: Issues, where: dict) -> TimeCandidate | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)) or (isinstance(raw, str) and re.fullmatch(r'\d{10,16}', raw)):
            return epoch_candidate(kind, str(raw), source, unit='ms')
        return from_iso(kind, str(raw), source)
    except (ValueError, OverflowError):
        issues.add('time_unparsed', raw=str(raw)[:64], **where)
        return None


def _kept(record: dict) -> dict:
    return {k: record[k] for k in _KEEP if k in record and isinstance(record[k], (str, int, float, bool))}


def _segments(data, decimals: int, limits: CommLimits, issues: Issues):
    """Yield (kind, locator, start_raw, end_raw, coarse, extra) from any supported layout."""
    if isinstance(data, dict) and isinstance(data.get('locations'), list):
        run = None
        for index, point in enumerate(data['locations']):
            if not isinstance(point, dict):
                issues.add('point_not_an_object', index=index)
                continue
            cell = _e7(point, decimals)
            stamp = point.get('timestamp') or point.get('timestampMs')
            key = (cell['lat'], cell['lon']) if cell else None
            if run is not None and run['key'] == key:
                run['end'], run['last'] = stamp, index
                run['points'] += 1
                continue
            if run is not None:
                yield ('raw_points', {'format': 'records_json', 'first_index': run['first'], 'last_index': run['last']},
                       run['start'], run['end'], run['cell'], {'points': run['points']})
            run = {'key': key, 'cell': cell, 'start': stamp, 'end': stamp, 'first': index, 'last': index, 'points': 1}
        if run is not None:
            yield ('raw_points', {'format': 'records_json', 'first_index': run['first'], 'last_index': run['last']},
                   run['start'], run['end'], run['cell'], {'points': run['points']})
        return
    if isinstance(data, dict) and isinstance(data.get('timelineObjects'), list):
        for index, item in enumerate(data['timelineObjects']):
            if not isinstance(item, dict):
                continue
            if 'placeVisit' in item:
                visit = item['placeVisit'] or {}
                duration = visit.get('duration') or {}
                yield ('visit', {'format': 'semantic_location_history', 'index': index},
                       duration.get('startTimestamp') or duration.get('startTimestampMs'),
                       duration.get('endTimestamp') or duration.get('endTimestampMs'),
                       _e7(visit.get('location'), decimals), {**_kept(visit), **_kept(visit.get('location') or {})})
            elif 'activitySegment' in item:
                segment = item['activitySegment'] or {}
                duration = segment.get('duration') or {}
                yield ('activity', {'format': 'semantic_location_history', 'index': index},
                       duration.get('startTimestamp') or duration.get('startTimestampMs'),
                       duration.get('endTimestamp') or duration.get('endTimestampMs'),
                       _e7(segment.get('startLocation'), decimals),
                       {**_kept(segment), 'end_coarse': _e7(segment.get('endLocation'), decimals)})
            else:
                issues.add('timeline_object_unrecognised', index=index, keys=sorted(item)[:10])
        return
    segments = data.get('semanticSegments') if isinstance(data, dict) else data
    if isinstance(segments, list):
        if isinstance(data, dict):
            for key in ('rawSignals', 'userLocationProfile'):
                if key in data:
                    issues.add('section_not_imported', section=key)
        for index, item in enumerate(segments):
            if not isinstance(item, dict):
                continue
            extra = {}
            cell = None
            if 'visit' in item:
                kind = 'visit'
                candidate = (item['visit'] or {}).get('topCandidate') or {}
                cell = _e7(candidate.get('placeLocation'), decimals)
                extra = {**_kept(item['visit'] or {}), **_kept(candidate)}
            elif 'activity' in item:
                kind = 'activity'
                activity = item['activity'] or {}
                cell = _e7(activity.get('start'), decimals)
                extra = {**_kept(activity), **_kept(activity.get('topCandidate') or {}),
                         'end_coarse': _e7(activity.get('end'), decimals)}
            elif 'timelinePath' in item:
                kind = 'path'
                extra = {'path_points': len(item['timelinePath'] or [])}
            else:
                kind = 'segment'
            yield (kind, {'format': 'timeline_json', 'index': index}, item.get('startTime'), item.get('endTime'),
                   cell, extra)
        return
    raise ValueError('unrecognised location history layout')


def import_location_history(path: Path, *, enable_location: bool = False, coarse_decimals: int = 2,
                            source_locator: str | None = None, original_name: str | None = None,
                            collection_id: int | None = None, label: str | None = None,
                            limits: CommLimits = DEFAULT_LIMITS) -> dict:
    if not enable_location:
        raise LocationOptInRequired('location history import is disabled; pass enable_location=True '
                                    '(CLI --enable-location-history) to opt in')
    if not 0 <= coarse_decimals <= 4:
        raise ValueError('coarse_decimals must be between 0 and 4')
    opt_in = {'enable_location': True, 'coarse_decimals': coarse_decimals, 'processing': 'local_only',
              'exact_coordinates_in_metadata': False}
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(
        path, source_kind='google_takeout', source_label=label or 'Takeout location history', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name,
        collection_id=collection_id, options=opt_in, mime_type='application/json')
    issues = Issues(limits.max_issues)
    snapshot = stored_path(artifact_id)
    counts = {'events': 0, 'raw_points_aggregated': 0}
    if snapshot.stat().st_size > limits.max_json_bytes:
        issues.add('document_size_limit', limit=limits.max_json_bytes)
        coverage = {'complete': False}
        record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                          options=opt_in, counts=counts, coverage=coverage, issues=issues)
        return {'artifact_id': artifact_id, **counts, 'coverage': coverage, 'issues': issues.as_dict()}
    data = json.loads(snapshot.read_bytes().decode('utf-8-sig'))
    complete = True
    for kind, where, start_raw, end_raw, cell, extra in _segments(data, coarse_decimals, limits, issues):
        if counts['events'] >= limits.max_records:
            issues.add('record_limit', limit=limits.max_records)
            complete = False
            break
        start = _time(start_raw, 'start', f'{PARSER_NAME}:{where["format"]}:start', issues, where)
        end = _time(end_raw, 'end', f'{PARSER_NAME}:{where["format"]}:end', issues, where)
        if kind == 'raw_points':
            counts['raw_points_aggregated'] += extra['points']
        candidates = [c for c in (start, end) if c is not None]
        if start is not None and end is not None and start.raw == end.raw:
            candidates = [start]
        metadata = {'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION}, 'locator': {'artifact_id': artifact_id, **where},
                    'presence_kind': kind, 'coarse_location': cell, 'fields': extra, 'opt_in': opt_in,
                    'semantics': 'service-inferred presence; coarse, not a verified position',
                    'issues': [] if cell else ['no_coordinates']}
        index_key = where.get('index', where.get('first_index'))
        add_event_with_times(
            candidates, source_id=source_id, artifact_id=artifact_id, event_type='presence',
            ts_start=legacy_instant(start), ts_end=legacy_instant(end), subject=kind,
            external_id=f'{PARSER_NAME}:{where["format"]}:{index_key}', confidence=None, metadata=metadata)
        counts['events'] += 1
    coverage = {'complete': complete}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=opt_in,
                      counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser_version': PARSER_VERSION, **counts,
            'options': opt_in, 'coverage': coverage, 'issues': issues.as_dict()}


# ---------------------------------------------------------------------------
# Parser version 2 (streaming, raw samples + provider inferences)

PARSER_VERSION_V2 = '2'


def _parse_google_v2(path: Path, ctx):
    from ..location import google, jsonstream
    gctx = google.Context(keep_place_text=ctx.params.provider_place_text == 'keep',
                          max_path_points=ctx.limits.max_path_points, raw_limits=ctx.limits.raw())
    store_decimals = ctx.params.store_decimals
    source = f'{PARSER_NAME}:{PARSER_VERSION_V2}'
    report = jsonstream.StreamReport()
    sections: dict[str, int] = {}
    unknown_reported: set[str] = set()
    with path.open('rb') as handle:
        for key, index, value in jsonstream.iter_document(handle, max_element_bytes=ctx.limits.max_element_bytes,
                                                          report=report):
            sections[key] = sections.get(key, 0) + 1
            issues: list = []
            records: list = []
            if key == 'locations' and index is not None:
                fix = google.records_point(index, value, source=f'{source}:records_json', issues_out=issues)
                if fix is not None:
                    records.append(('fix', f'locations[{index}]', fix))
            elif key == 'timelineObjects' and index is not None:
                records += [('event', e) for e in google.semantic_object(
                    index, value, store_decimals=store_decimals, ctx=gctx,
                    source=f'{source}:semantic_location_history', issues=issues)]
            elif key == 'semanticSegments' and index is not None:
                records += [('event', e) for e in google.device_segment(
                    index, value, store_decimals=store_decimals, ctx=gctx, source=f'{source}:timeline_device',
                    issues=issues)]
            elif key == 'rawSignals' and index is not None:
                found = google.raw_signal(index, value, source=f'{source}:timeline_device', issues=issues)
                if found is not None:
                    records.append(found)
            elif key == 'userLocationProfile' and index is None:
                event = google.location_profile(value, store_decimals=store_decimals, ctx=gctx)
                if event is not None:
                    records.append(('event', event))
            elif key == '$':
                records += [('event', e) for e in google.device_segment(
                    index, value, store_decimals=store_decimals, ctx=gctx, source=f'{source}:ios_location_history',
                    issues=issues, fmt='ios_location_history', section='$')]
            elif key not in unknown_reported:
                unknown_reported.add(key)
                issues.append(('section_not_imported', {'section': key[:64],
                                                        'shape': 'array' if index is not None else 'value'}))
            for code, details in issues:
                yield ('issue', code, details)
            yield from records
    for item in report.issues[:50]:
        yield ('issue', 'json_stream_' + item['code'], {k: v for k, v in item.items() if k != 'code'})
    ctx.counts['sections'] = dict(sorted(sections.items()))
    ctx.counts['json_stream'] = report.as_dict()
    if not report.complete:
        yield ('coverage', {'complete': False, 'json_stream_complete': False})


def detect_google_format(path: Path) -> str:
    from ..location import google
    from ..location.ingest import read_prefix
    return google.detect_format(read_prefix(path))


def import_location_history_v2(path: Path, *, enable_location: bool = False, store_decimals: int | None = None,
                               stream: str | None = None, provider_place_text: str = 'keep',
                               sample_session_gap_s: int = 1800, source_locator: str | None = None,
                               original_name: str | None = None, collection_id: int | None = None,
                               label: str | None = None, limits=None) -> dict:
    """Google location exports (all four layouts) with parser version 2; see ``docs/LOCATION.md``."""
    from ..location.ingest import DEFAULT_LIMITS, ImportParams, run_import
    params = ImportParams(store_decimals=store_decimals, stream=stream, provider_place_text=provider_place_text,
                          sample_session_gap_s=sample_session_gap_s)
    return run_import(path, importer=PARSER_NAME, parser_version=PARSER_VERSION_V2, parse=_parse_google_v2,
                      enable_location=enable_location, params=params, source_kind='google_takeout',
                      source_label='Google location history', provider='google_location_history',
                      mime_type='application/json', detect=detect_google_format, source_locator=source_locator,
                      original_name=original_name, collection_id=collection_id, label=label,
                      limits=limits or DEFAULT_LIMITS)

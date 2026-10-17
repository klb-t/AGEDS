"""Google location exports -> raw samples, provider inferences and their times (parser ``google_location`` v2).

Formats (public observations of export layouts; real exports may differ):

``records_json``           legacy Takeout ``Records.json``: ``{"locations": [...]}`` with
                           ``latitudeE7``/``longitudeE7``, ``accuracy``, ``altitude``,
                           ``verticalAccuracy``, ``velocity``, ``heading``, ``source``,
                           ``deviceTag``, ``activity`` lists and ``timestamp`` (ISO) or
                           ``timestampMs`` (epoch ms text).
``semantic_location_history``  monthly ``timelineObjects`` with ``placeVisit``
                           (``location``, ``duration``, ``placeConfidence``,
                           ``visitConfidence``, ``otherCandidateLocations``,
                           ``editConfirmationStatus``, ``simplifiedRawPath``,
                           ``childVisits``) and ``activitySegment`` (start/end
                           location, ``activities``, ``waypointPath``,
                           ``simplifiedRawPath``, ``distance``).
``timeline_device``        Android on-device Timeline export: ``semanticSegments``
                           (``visit``/``activity``/``timelinePath`` with ``"48.1°, 11.5°"``
                           strings and ``start/endTimeTimezoneUtcOffsetMinutes``),
                           ``rawSignals`` (``position``/``wifiScan``/``activityRecord``)
                           and ``userLocationProfile``.
``ios_location_history``   iOS Google Maps ``location-history.json``: a top-level array of
                           ``visit``/``activity``/``timelinePath`` entries with string
                           values and ``"geo:lat,lon"`` coordinates.

Records yielded to the importer: ``('fix', locator, fix)``, ``('signal', locator,
signal)``, ``('event', event)`` and ``('issue', code, details)``. Provider
confidences and probabilities are copied as **labels of the provider**
(``provider_labels``), never as our probability.
"""
from __future__ import annotations

import re
from datetime import timedelta

from ..importers.comm_common import bound_json, epoch_candidate
from ..timeinfo import TimeCandidate, format_utc, from_iso
from . import geo

PARSER = 'google_location'
VERSION = '2'
USER_EDIT_STATUSES = ('CONFIRMED', 'EDITED', 'USER_CONFIRMED', 'USER_EDITED')
NOT_EDITED_STATUSES = ('NOT_CONFIRMED', 'UNKNOWN_CONFIRMATION_STATUS', '')
_LABEL_KEYS = ('placeConfidence', 'visitConfidence', 'locationConfidence', 'calibratedProbability', 'probability',
               'confidence', 'semanticType', 'placeVisitType', 'placeVisitImportance', 'hierarchyLevel',
               'activityType', 'type', 'travelMode', 'isTimelessVisit', 'visitConfidence', 'sectionId')
_DIGITS = re.compile(r'^-?\d{1,20}$')


class Context:
    """What a parser needs from the importer: the stored-text policy and bounded issue reporting."""

    def __init__(self, *, keep_place_text: bool, max_path_points: int, raw_limits):
        self.keep_place_text = keep_place_text
        self.max_path_points = max_path_points
        self.raw_limits = raw_limits


def detect_format(prefix: bytes) -> str:
    """Format label from a bounded prefix (first top-level key or array)."""
    text = prefix.decode('utf-8', 'replace').lstrip('﻿ \t\r\n')
    if text.startswith('['):
        return 'ios_location_history'
    match = re.match(r'\{\s*"((?:[^"\\]|\\.)*)"\s*:', text)
    key = match.group(1) if match else None
    return {'locations': 'records_json', 'timelineObjects': 'semantic_location_history',
            'semanticSegments': 'timeline_device', 'rawSignals': 'timeline_device',
            'userLocationProfile': 'timeline_device'}.get(key, 'google_json_unrecognised')


def time_candidate(raw, kind: str, source: str, *, unit: str | None = None, metadata: dict | None = None):
    """(candidate, issue) — ``unit='ms'`` for ``*Ms`` fields; ISO text otherwise."""
    if raw is None or raw == '':
        return None, None
    try:
        if unit == 'ms':
            text = str(raw).strip()
            if isinstance(raw, bool) or not _DIGITS.match(text):
                return None, ('time_unparsed', {'raw': str(raw)[:64], 'expected': 'epoch_ms'})
            return epoch_candidate(kind, text, source, unit='ms', metadata=metadata), None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return None, ('time_unit_unknown', {'raw': str(raw)[:64]})
        return from_iso(kind, str(raw), source, metadata=metadata), None
    except (ValueError, OverflowError):
        return None, ('time_unparsed', {'raw': str(raw)[:64]})


def _labels(record: dict, ctx: Context) -> dict:
    out = {}
    for key in _LABEL_KEYS:
        value = record.get(key) if isinstance(record, dict) else None
        if isinstance(value, (str, int, float, bool)) and not (isinstance(value, float) and value != value):
            out[key] = bound_json(value, ctx.raw_limits)[0]
    return out


def _e7(container, lat_key='latitudeE7', lon_key='longitudeE7'):
    if not isinstance(container, dict):
        return None, None
    if lat_key in container or lon_key in container:
        try:
            return geo.from_e7(container.get(lat_key), container.get(lon_key)), None
        except geo.CoordinateError as exc:
            return None, (exc.code, exc.details)
    if 'latLng' in container or 'LatLng' in container:
        try:
            return geo.parse_pair(container.get('latLng', container.get('LatLng'))), None
        except geo.CoordinateError as exc:
            return None, (exc.code, exc.details)
    return None, None


def _pair(text):
    if text is None:
        return None, None
    if isinstance(text, dict):
        return _e7(text)
    try:
        return geo.parse_pair(text), None
    except geo.CoordinateError as exc:
        return None, (exc.code, exc.details)


def _coord_record(coord: geo.Coordinate | None, store_decimals: int | None) -> dict | None:
    if coord is None:
        return None
    lat, lon, decimals = geo.round_to(coord, store_decimals)
    return {'lat': lat, 'lon': lon, 'decimals': decimals, 'source_decimals': coord.decimals, 'basis': coord.basis}


def _edit(record: dict, issues: list) -> tuple[str, dict | None]:
    status = record.get('editConfirmationStatus') if isinstance(record, dict) else None
    if status is None:
        return 'provider_inference', None
    text = str(status)[:64]
    if text in USER_EDIT_STATUSES:
        return 'provider_user_edit', {'editConfirmationStatus': text, 'flagged': True}
    if text not in NOT_EDITED_STATUSES:
        issues.append(('edit_status_unrecognised', {'raw': text}))
    return 'provider_inference', {'editConfirmationStatus': text, 'flagged': False}


def _number(value):
    return geo.finite(value)


# ---------------------------------------------------------------- Records.json

def records_point(index: int, point, *, source: str, issues_out: list):
    """One ``locations[i]`` element -> fix dict or None (issues appended)."""
    locator = f'locations[{index}]'
    if not isinstance(point, dict):
        issues_out.append(('point_not_an_object', {'locator': locator}))
        return None
    coord, problem = _e7(point)
    if coord is None:
        issues_out.append((problem[0] if problem else 'coordinate_missing', {'locator': locator, **(problem[1] if problem else {})}))
        return None
    stamp, stamp_ms = point.get('timestamp'), point.get('timestampMs')
    time, issue = (time_candidate(stamp, 'observed', f'{source}:timestamp') if stamp is not None
                   else time_candidate(stamp_ms, 'observed', f'{source}:timestampMs', unit='ms'))
    if issue:
        issues_out.append((issue[0], {'locator': locator, **issue[1]}))
    extra = {}
    if stamp is not None and stamp_ms is not None:
        other, _ = time_candidate(stamp_ms, 'observed', f'{source}:timestampMs', unit='ms')
        extra['timestampMs'] = str(stamp_ms)[:32]
        if other is not None and time is not None and other.utc != time.utc:
            issues_out.append(('timestamp_variants_disagree', {'locator': locator}))
            extra['timestamp_variants_disagree'] = True
    for key in ('platformType', 'formFactor', 'osLevel', 'batteryCharging', 'serverTimestamp', 'deviceTimestamp',
                'deviceDesignation'):
        if key in point and isinstance(point[key], (str, int, float, bool)):
            extra[key] = point[key] if not isinstance(point[key], str) else point[key][:64]
    activity = point.get('activity')
    if isinstance(activity, list) and activity:
        guesses = []
        for entry in activity[:3]:
            if isinstance(entry, dict) and isinstance(entry.get('activity'), list):
                guesses.append({'timestamp': str(entry.get('timestamp') or entry.get('timestampMs') or '')[:40],
                                'activity': [{'type': str(a.get('type'))[:40], 'confidence': a.get('confidence')}
                                             for a in entry['activity'][:5] if isinstance(a, dict)]})
        extra['provider_activity_guesses'] = guesses
        extra['provider_activity_guesses_total'] = len(activity)
    known = {'latitudeE7', 'longitudeE7', 'accuracy', 'altitude', 'verticalAccuracy', 'velocity', 'heading', 'source',
             'deviceTag', 'timestamp', 'timestampMs', 'activity', 'platformType', 'formFactor', 'osLevel',
             'batteryCharging', 'serverTimestamp', 'deviceTimestamp', 'deviceDesignation'}
    unknown = sorted(k for k in point if k not in known)
    if unknown:
        extra['unknown_keys'] = unknown[:20]
    device = point.get('deviceTag')
    return {'coord': coord, 'time': time, 'time_raw': None if time is not None or (stamp is None and stamp_ms is None)
            else str(stamp if stamp is not None else stamp_ms)[:64], 'accuracy_m': _number(point.get('accuracy')),
            'altitude_m': _number(point.get('altitude')), 'vertical_accuracy_m': _number(point.get('verticalAccuracy')),
            'speed_mps': _number(point.get('velocity')), 'heading_deg': _number(point.get('heading')),
            'fix_kind': 'raw_position', 'signal_basis': 'raw_sample',
            'provider_source': str(point['source'])[:32] if point.get('source') is not None else None,
            'device_tag': str(device)[:64] if device is not None else None, 'extra': extra}


# ---------------------------------------------------------------- shared event building

def _path_fixes(points, *, kind: str, basis: str, source: str, locator: str, ctx: Context, issues: list,
                lat_key='latE7', lon_key='lngE7', start: TimeCandidate | None = None):
    """Path points -> [(locator, fix)] (bounded by ``max_path_points``)."""
    out = []
    if not isinstance(points, list):
        return out, 0
    for index, point in enumerate(points):
        if index >= ctx.max_path_points:
            issues.append(('path_point_limit', {'locator': locator, 'limit': ctx.max_path_points}))
            break
        where = f'{locator}/{kind}[{index}]'
        if isinstance(point, dict) and ('point' in point):
            coord, problem = _pair(point.get('point'))
        elif isinstance(point, dict):
            coord, problem = _e7(point, lat_key, lon_key)
            if coord is None and problem is None:
                coord, problem = _e7(point)
        else:
            coord, problem = None, ('point_not_an_object', {})
        if coord is None:
            issues.append((problem[0] if problem else 'coordinate_missing', {'locator': where, **(problem[1] if problem else {})}))
            continue
        time, issue = None, None
        if isinstance(point, dict):
            if 'timestamp' in point or 'time' in point:
                time, issue = time_candidate(point.get('timestamp', point.get('time')), 'observed', f'{source}:path_time')
            elif 'timestampMs' in point:
                time, issue = time_candidate(point.get('timestampMs'), 'observed', f'{source}:path_timestampMs', unit='ms')
            elif 'durationMinutesOffsetFromStartTime' in point and start is not None:
                time, issue = _offset_time(start, point.get('durationMinutesOffsetFromStartTime'), source)
        if issue:
            issues.append((issue[0], {'locator': where, **issue[1]}))
        out.append((where, {'coord': coord, 'time': time,
                            'accuracy_m': _number(point.get('accuracyMeters')) if isinstance(point, dict) else None,
                            'fix_kind': kind, 'signal_basis': basis, 'extra': {}}))
    return out, len(points)


def _offset_time(start: TimeCandidate, offset, source: str):
    text = str(offset).strip() if offset is not None else ''
    if not re.fullmatch(r'\d{1,6}', text) or start.utc is None:
        return None, ('time_unparsed', {'raw': text[:32], 'expected': 'minutes offset from a start with a fixed instant'})
    from datetime import datetime
    moment = datetime.fromisoformat(start.utc.replace('Z', '+00:00')) + timedelta(minutes=int(text))
    return TimeCandidate('observed', f'{start.raw}+{text}min', f'{source}:durationMinutesOffsetFromStartTime',
                         utc=format_utc(moment), tz_status=start.tz_status, tz=start.tz, precision='minute',
                         metadata={'projection': 'start_plus_minutes_offset', 'offset_minutes': int(text)}), None


def _event(*, event_type: str, fmt: str, section: str, index, start, end, basis: str, edit, subject: str,
           labels: dict, location, end_location=None, candidates=None, provider_place=None, extra=None,
           path=(), path_total=0, path_kind=None, issues=()):
    return {'event_type': event_type, 'locator': {'format': fmt, 'section': section, 'index': index},
            'start': start, 'end': end, 'basis': basis, 'subject': subject,
            'metadata': {'provider_labels': labels, 'location': location, 'end_location': end_location,
                         'other_candidates': candidates or [], 'provider_place': provider_place,
                         'user_edit': edit, 'path': {'kind': path_kind, 'points_in_export': path_total,
                                                     'stored_as_fixes': len(path)} if path_kind else None,
                         **(extra or {})},
            'path': list(path), 'issues': [code for code, _ in issues]}


def _place_text(location: dict, ctx: Context) -> dict | None:
    if not isinstance(location, dict):
        return None
    place = {}
    for key, name in (('placeId', 'place_id'), ('placeID', 'place_id'), ('semanticType', 'semantic_type')):
        if isinstance(location.get(key), str):
            place[name] = location[key][:256]
    if ctx.keep_place_text:
        for key in ('name', 'address'):
            if isinstance(location.get(key), str):
                place[key] = location[key][:ctx.raw_limits.max_raw_chars]
    else:
        place['text_fields'] = 'omitted_by_parameter'
    if place:
        place['resolution'] = 'provider_identifier_kept_raw_never_resolved'
    return place or None


# ---------------------------------------------------------------- Semantic Location History

def semantic_object(index: int, item, *, store_decimals, ctx: Context, source: str, issues: list):
    """One ``timelineObjects[i]`` -> list of event dicts (child visits included)."""
    if not isinstance(item, dict):
        issues.append(('timeline_object_not_an_object', {'index': index}))
        return []
    if 'placeVisit' in item:
        return _place_visit(item['placeVisit'], f'{index}', store_decimals=store_decimals, ctx=ctx, source=source,
                            issues=issues)
    if 'activitySegment' in item:
        return [_activity_segment(item['activitySegment'], f'{index}', store_decimals=store_decimals, ctx=ctx,
                                  source=source, issues=issues)]
    issues.append(('timeline_object_unrecognised', {'index': index, 'keys': sorted(item)[:10]}))
    return []


def _duration(record: dict, source: str, issues: list, where: str):
    duration = record.get('duration') if isinstance(record.get('duration'), dict) else {}
    out = []
    for kind, keys in (('start', ('startTimestamp', 'startTimestampMs')), ('end', ('endTimestamp', 'endTimestampMs'))):
        if duration.get(keys[0]) is not None:
            cand, issue = time_candidate(duration[keys[0]], kind, f'{source}:duration.{keys[0]}')
        else:
            cand, issue = time_candidate(duration.get(keys[1]), kind, f'{source}:duration.{keys[1]}', unit='ms')
        if issue:
            issues.append((issue[0], {'locator': where, **issue[1]}))
        out.append(cand)
    return out


def _place_visit(visit, where: str, *, store_decimals, ctx, source, issues, depth: int = 0):
    if not isinstance(visit, dict):
        issues.append(('place_visit_not_an_object', {'locator': where}))
        return []
    local_issues: list = []
    start, end = _duration(visit, source, local_issues, where)
    location = visit.get('location') if isinstance(visit.get('location'), dict) else {}
    coord, problem = _e7(location)
    if coord is None:
        center, _ = _e7(visit, 'centerLatE7', 'centerLngE7')
        coord = center
        if coord is None:
            local_issues.append((problem[0] if problem else 'no_coordinates', {'locator': where}))
    basis, edit = _edit(visit, local_issues)
    candidates = []
    for c_index, other in enumerate((visit.get('otherCandidateLocations') or [])[:20]):
        c_coord, _ = _e7(other)
        candidates.append({'location': _coord_record(c_coord, store_decimals), 'provider_labels': _labels(other, ctx),
                           'provider_place': _place_text(other, ctx)})
    path, total = _path_fixes((visit.get('simplifiedRawPath') or {}).get('points'), kind='simplified_raw_path',
                              basis='raw_sample', source=source, locator=f'timelineObjects[{where}]', ctx=ctx,
                              issues=local_issues)
    labels = {**_labels(visit, ctx), **{f'location.{k}': v for k, v in _labels(location, ctx).items()}}
    device = (location.get('sourceInfo') or {}).get('deviceTag') if isinstance(location.get('sourceInfo'), dict) else None
    events = [_event(event_type='location_visit', fmt='semantic_location_history', section='timelineObjects',
                     index=where, start=start, end=end, basis=basis, edit=edit,
                     subject=str(location.get('semanticType') or 'visit')[:80], labels=labels,
                     location=_coord_record(coord, store_decimals), candidates=candidates,
                     provider_place=_place_text(location, ctx),
                     extra={'device_tag': str(device)[:64] if device is not None else None,
                            'child_visits': len(visit.get('childVisits') or [])},
                     path=path, path_total=total, path_kind='simplified_raw_path' if total else None,
                     issues=local_issues)]
    issues.extend(local_issues)
    if depth < 3:
        for c_index, child in enumerate((visit.get('childVisits') or [])[:100]):
            events.extend(_place_visit(child, f'{where}/childVisits[{c_index}]', store_decimals=store_decimals,
                                       ctx=ctx, source=source, issues=issues, depth=depth + 1))
    return events


def _activity_segment(segment, where: str, *, store_decimals, ctx, source, issues):
    local_issues: list = []
    if not isinstance(segment, dict):
        issues.append(('activity_segment_not_an_object', {'locator': where}))
        segment = {}
    start, end = _duration(segment, source, local_issues, where)
    s_coord, _ = _e7(segment.get('startLocation'))
    e_coord, _ = _e7(segment.get('endLocation'))
    basis, edit = _edit(segment, local_issues)
    activities = [{'activityType': str(a.get('activityType'))[:40], 'provider_probability': a.get('probability')}
                  for a in (segment.get('activities') or [])[:10] if isinstance(a, dict)]
    path, total = [], 0
    raw_path = (segment.get('simplifiedRawPath') or {}).get('points') if isinstance(segment.get('simplifiedRawPath'), dict) else None
    if raw_path:
        path, total = _path_fixes(raw_path, kind='simplified_raw_path', basis='raw_sample', source=source,
                                  locator=f'timelineObjects[{where}]', ctx=ctx, issues=local_issues)
    waypoints = (segment.get('waypointPath') or {}).get('waypoints') if isinstance(segment.get('waypointPath'), dict) else None
    w_path, w_total = _path_fixes(waypoints, kind='waypoint_path', basis='provider_inference', source=source,
                                  locator=f'timelineObjects[{where}]', ctx=ctx, issues=local_issues)
    waypoint_labels = _labels(segment.get('waypointPath') or {}, ctx) if isinstance(segment.get('waypointPath'), dict) else {}
    event = _event(event_type='location_segment', fmt='semantic_location_history', section='timelineObjects',
                   index=where, start=start, end=end, basis=basis, edit=edit,
                   subject=str(segment.get('activityType') or 'activity')[:80], labels=_labels(segment, ctx),
                   location=_coord_record(s_coord, store_decimals), end_location=_coord_record(e_coord, store_decimals),
                   extra={'provider_activities': activities,
                          'provider_distance_m': _number(segment.get('distance', segment.get('distanceMeters'))),
                          'waypoint_path': {'labels': waypoint_labels, 'points_in_export': w_total,
                                            'stored_as_fixes': len(w_path)} if w_total else None,
                          'parking_event': bool(segment.get('parkingEvent'))},
                   path=path + w_path, path_total=total, path_kind='simplified_raw_path' if total else None,
                   issues=local_issues)
    issues.extend(local_issues)
    return event


# ---------------------------------------------------------------- on-device Timeline (Android)

def _segment_times(item: dict, source: str, issues: list, where: str):
    out = []
    for kind, key in (('start', 'startTime'), ('end', 'endTime')):
        offset = item.get(f'{key}TimezoneUtcOffsetMinutes')
        meta = {}
        if offset is not None:
            meta['provider_utc_offset_minutes'] = offset if isinstance(offset, int) and not isinstance(offset, bool) \
                else str(offset)[:16]
        cand, issue = time_candidate(item.get(key), kind, f'{source}:{key}', metadata=meta)
        if issue:
            issues.append((issue[0], {'locator': where, **issue[1]}))
        if cand is not None and isinstance(offset, int) and not isinstance(offset, bool) and cand.tz and cand.tz != 'UTC':
            sign = 1 if cand.tz[0] == '+' else -1
            stated = sign * (int(cand.tz[1:3]) * 60 + int(cand.tz[4:6]))
            if stated != offset:
                issues.append(('provider_offset_differs_from_timestamp_offset', {'locator': where, 'field': key}))
                cand = TimeCandidate(cand.kind, cand.raw, cand.source, utc=cand.utc, local=cand.local,
                                     tz_status=cand.tz_status, tz=cand.tz, precision=cand.precision,
                                     metadata={**cand.metadata, 'provider_offset_differs': True})
        out.append(cand)
    return out


def device_segment(index: int, item, *, store_decimals, ctx, source, issues, fmt='timeline_device',
                   section='semanticSegments'):
    """One ``semanticSegments[i]`` (Android) or iOS array entry -> list of event dicts."""
    where = f'{index}'
    if not isinstance(item, dict):
        issues.append(('segment_not_an_object', {'index': index}))
        return []
    local_issues: list = []
    start, end = _segment_times(item, source, local_issues, f'{section}[{where}]')
    events = []
    if isinstance(item.get('visit'), dict):
        visit = item['visit']
        top = visit.get('topCandidate') if isinstance(visit.get('topCandidate'), dict) else {}
        coord, problem = _pair(top.get('placeLocation'))
        if coord is None:
            local_issues.append((problem[0] if problem else 'no_coordinates', {'locator': f'{section}[{where}].visit'}))
        basis, edit = _edit(visit, local_issues)
        labels = {**_labels(visit, ctx), **{f'topCandidate.{k}': v for k, v in _labels(top, ctx).items()}}
        events.append(_event(event_type='location_visit', fmt=fmt, section=section, index=where, start=start, end=end,
                             basis=basis, edit=edit, subject=str(top.get('semanticType') or 'visit')[:80],
                             labels=labels, location=_coord_record(coord, store_decimals),
                             provider_place=_place_text(top, ctx), issues=local_issues))
    if isinstance(item.get('activity'), dict):
        activity = item['activity']
        top = activity.get('topCandidate') if isinstance(activity.get('topCandidate'), dict) else {}
        s_coord, _ = _pair(activity.get('start'))
        e_coord, _ = _pair(activity.get('end'))
        basis, edit = _edit(activity, local_issues)
        labels = {**_labels(activity, ctx), **{f'topCandidate.{k}': v for k, v in _labels(top, ctx).items()}}
        events.append(_event(event_type='location_segment', fmt=fmt, section=section, index=where, start=start, end=end,
                             basis=basis, edit=edit, subject=str(top.get('type') or 'activity')[:80], labels=labels,
                             location=_coord_record(s_coord, store_decimals),
                             end_location=_coord_record(e_coord, store_decimals),
                             extra={'provider_distance_m': _number(activity.get('distanceMeters'))},
                             issues=local_issues))
    if isinstance(item.get('timelinePath'), list):
        path, total = _path_fixes(item['timelinePath'], kind='timeline_path', basis='provider_inference', source=source,
                                  locator=f'{section}[{where}]', ctx=ctx, issues=local_issues, start=start)
        events.append(_event(event_type='location_segment', fmt=fmt, section=section, index=f'{where}/timelinePath',
                             start=start, end=end, basis='provider_inference', edit=None, subject='timeline_path',
                             labels={}, location=None, path=path, path_total=total, path_kind='timeline_path',
                             issues=local_issues))
    if not events:
        if 'timelineMemory' in item:
            events.append(_event(event_type='location_segment', fmt=fmt, section=section, index=f'{where}/timelineMemory',
                                 start=start, end=end, basis='provider_inference', edit=None, subject='timeline_memory',
                                 labels={}, location=None,
                                 extra={'timeline_memory': bound_json(item['timelineMemory'], ctx.raw_limits)[0]},
                                 issues=local_issues))
        else:
            local_issues.append(('segment_unrecognised', {'index': index, 'keys': sorted(item)[:10]}))
    issues.extend(local_issues)
    return events


def raw_signal(index: int, item, *, source: str, issues: list):
    """One ``rawSignals[i]`` -> ('fix'|'signal', locator, dict) or None."""
    locator = f'rawSignals[{index}]'
    if not isinstance(item, dict):
        issues.append(('raw_signal_not_an_object', {'locator': locator}))
        return None
    if isinstance(item.get('position'), dict):
        position = item['position']
        coord, problem = _pair(position.get('LatLng', position.get('latLng', position.get('point'))))
        if coord is None:
            issues.append((problem[0] if problem else 'coordinate_missing', {'locator': locator, **(problem[1] if problem else {})}))
            return None
        time, issue = time_candidate(position.get('timestamp'), 'observed', f'{source}:rawSignals.position.timestamp')
        if issue:
            issues.append((issue[0], {'locator': locator, **issue[1]}))
        return ('fix', locator + '.position', {
            'coord': coord, 'time': time, 'accuracy_m': _number(position.get('accuracyMeters')),
            'altitude_m': _number(position.get('altitudeMeters')), 'speed_mps': _number(position.get('speedMetersPerSecond')),
            'fix_kind': 'raw_position', 'signal_basis': 'raw_sample',
            'provider_source': str(position['source'])[:32] if position.get('source') is not None else None,
            'extra': {}})
    if isinstance(item.get('wifiScan'), dict):
        scan = item['wifiScan']
        time, issue = time_candidate(scan.get('deliveryTime'), 'observed', f'{source}:rawSignals.wifiScan.deliveryTime')
        if issue:
            issues.append((issue[0], {'locator': locator, **issue[1]}))
        records = scan.get('devicesRecords') if isinstance(scan.get('devicesRecords'), list) else []
        rssi = [r.get('rawRssi') for r in records if isinstance(r, dict) and isinstance(r.get('rawRssi'), (int, float))
                and not isinstance(r.get('rawRssi'), bool)]
        return ('signal', locator + '.wifiScan', {
            'time': time, 'signal_kind': 'wifi_scan', 'signal_basis': 'raw_sample',
            'summary': {'devices': len(records), 'strongest_rssi': max(rssi) if rssi else None,
                        'device_identifiers': 'not_copied_kept_only_in_the_original'}})
    if isinstance(item.get('activityRecord'), dict):
        record = item['activityRecord']
        time, issue = time_candidate(record.get('timestamp'), 'observed', f'{source}:rawSignals.activityRecord.timestamp')
        if issue:
            issues.append((issue[0], {'locator': locator, **issue[1]}))
        activities = [{'type': str(a.get('type'))[:40], 'provider_confidence': a.get('confidence')}
                      for a in (record.get('probableActivities') or [])[:5] if isinstance(a, dict)]
        return ('signal', locator + '.activityRecord', {
            'time': time, 'signal_kind': 'activity_record', 'signal_basis': 'provider_inference',
            'summary': {'provider_activities': activities}})
    issues.append(('raw_signal_unrecognised', {'locator': locator, 'keys': sorted(item)[:10]}))
    return None


def location_profile(value, *, store_decimals, ctx: Context, fmt: str = 'timeline_device'):
    """``userLocationProfile`` -> one undated provider-inference event (frequent places, persona)."""
    if not isinstance(value, dict):
        return None
    places = []
    for place in (value.get('frequentPlaces') or [])[:100]:
        if not isinstance(place, dict):
            continue
        coord, _ = _pair(place.get('placeLocation'))
        places.append({'location': _coord_record(coord, store_decimals),
                       'provider_label': str(place.get('label'))[:64] if place.get('label') is not None else None,
                       'provider_place': _place_text(place, ctx)})
    persona = bound_json(value.get('persona'), ctx.raw_limits)[0] if 'persona' in value else None
    return _event(event_type='location_profile', fmt=fmt, section='userLocationProfile', index='0', start=None, end=None,
                  basis='provider_inference', edit=None, subject='user_location_profile', labels={}, location=None,
                  extra={'provider_frequent_places': places, 'provider_persona': persona,
                         'note': 'provider inference of frequent places; never used as a place definition automatically'})

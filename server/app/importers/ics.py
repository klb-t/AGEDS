"""iCalendar (ICS) import: one ``calendar_event`` per VEVENT.

* ``DTSTART``/``DTEND`` become ``start``/``end`` candidates. ``Z`` is UTC; a
  ``TZID`` parameter uses the file's own VTIMEZONE definition (preferred, RFC
  5545) or the IANA zone of that name; a floating time (neither) stays
  ``unknown``. An all-day ``DATE`` has precision ``day`` and no zone.
* ``DURATION`` without ``DTEND`` yields a labelled ``end`` projection.
* ``RRULE``/``RDATE``/``EXDATE`` are kept raw and **not** expanded on import.
  ``expand_recurrence`` gives an optional bounded projection, not stored rows.
* ``RECURRENCE-ID`` overrides are separate events sharing ``thread_id`` = UID.
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..db import session
from ..timeinfo import TimeCandidate
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, add_event_with_times, bounded_text,
                          content_id, declared_zone_candidate, legacy_instant, local_candidate, offset_candidate,
                          parse_content_line, physical_lines, record_import_run, split_unescaped, stored_path,
                          unescape_text)

PARSER_NAME = 'ics_rfc5545'
PARSER_VERSION = '1'
PROJECTION_RULE = 'ics_recurrence_projection/v1'
_DT = re.compile(r'^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$')
_DURATION = re.compile(r'^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$')


class Calendar:
    """Logical lines and components of one ICS document."""

    def __init__(self, data: bytes, limits: CommLimits, issues: Issues):
        lines, self.encoding = physical_lines(data)
        if self.encoding != 'utf-8':
            issues.add('ics_invalid_utf8_latin1_fallback')
        logical: list[tuple[str, int, int]] = []
        for number, line in enumerate(lines, start=1):
            if line[:1] in (' ', '\t') and logical:
                text, start, _ = logical[-1]
                logical[-1] = (text + line[1:], start, number)
            elif line.strip():
                logical.append((line, number, number))
        self.components: list[dict] = []
        self.timezones: dict[str, str] = {}
        stack: list[dict] = []
        for text, start, end in logical:
            try:
                line = parse_content_line(text, start, end)
            except ValueError as exc:
                issues.add('ics_malformed_content_line', line=start, error=str(exc), raw=text[:200])
                continue
            if line.name == 'BEGIN':
                component = {'name': line.value.strip().upper(), 'lines': [], 'children': [], 'line_start': start,
                             'line_end': None, 'index': None}
                if stack:
                    stack[-1]['children'].append(component)
                else:
                    self.components.append(component)
                stack.append(component)
            elif line.name == 'END':
                if not stack or stack[-1]['name'] != line.value.strip().upper():
                    issues.add('ics_unbalanced_end', line=start, value=line.value[:64])
                    continue
                stack[-1]['line_end'] = end
                stack.pop()
            elif stack:
                stack[-1]['lines'].append(line)
            else:
                issues.add('ics_property_outside_component', line=start)
        for component in stack:
            issues.add('ics_unterminated_component', component=component['name'], line=component['line_start'])
        self.vevents: list[dict] = []
        for top in self.components:
            for child in top['children']:
                if child['name'] == 'VEVENT':
                    child['index'] = len(self.vevents)
                    self.vevents.append(child)
                elif child['name'] == 'VTIMEZONE':
                    tzid = next((l.value for l in child['lines'] if l.name == 'TZID'), None)
                    if tzid:
                        first, last = child['line_start'], child['line_end'] or len(lines)
                        self.timezones[tzid] = '\n'.join(lines[first - 1:last])
        self._zones: dict[str, tuple] = {}

    def zone(self, tzid: str):
        """Return (tzinfo, declaration, iana_tzinfo_or_None) for a TZID, or None."""
        if tzid in self._zones:
            return self._zones[tzid]
        iana = None
        try:
            iana = ZoneInfo(tzid)
        except (ZoneInfoNotFoundError, ValueError):
            iana = None
        result = None
        block = self.timezones.get(tzid)
        if block is not None:
            try:
                from dateutil import tz as dateutil_tz
                parsed = dateutil_tz.tzical(io.StringIO(block))
                result = (parsed.get(tzid), 'vtimezone_in_file', iana)
            except ImportError:
                result = None
            except Exception:  # malformed VTIMEZONE: fall back to the IANA name if any
                result = None
        if result is None and iana is not None:
            result = (iana, 'iana_tzdata_by_tzid_name', None)
        self._zones[tzid] = result
        return result


def _value_type(line) -> str:
    declared = (line.param('VALUE') or [''])[0].upper()
    if declared:
        return declared
    return 'DATE' if re.fullmatch(r'\d{8}', line.value.strip()) else 'DATE-TIME'


def time_candidate(line, kind: str, calendar: Calendar, issues: Issues, *, extra: dict | None = None):
    """Candidate for a DATE/DATE-TIME property; None when unparsable."""
    raw = line.value.strip()
    source = f'ics:{line.name}'
    meta = {'property': line.name, 'params': {k: list(v) if v else None for k, v in line.params},
            'line': line.line_start, **(extra or {})}
    match = _DT.match(raw)
    if not match:
        issues.add('ics_unparsable_datetime', property=line.name, line=line.line_start, raw=raw[:64])
        return None
    year, month, day, hour, minute, second, utc_flag = match.groups()
    try:
        if hour is None:
            naive = datetime(int(year), int(month), int(day))
            meta['all_day_date'] = True
            meta['zone_status'] = 'date_value_has_no_zone'
            return local_candidate(kind, raw, source, naive, 'day', meta)
        naive = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    except ValueError:
        issues.add('ics_invalid_calendar_value', property=line.name, line=line.line_start, raw=raw[:64])
        return None
    precision = 'second' if second is not None else 'minute'
    if utc_flag:
        meta['zone_status'] = 'utc_designator'
        return offset_candidate(kind, raw, source, naive, 0, precision, meta, tz_label='UTC')
    tzid = (line.param('TZID') or [None])[0]
    if tzid is None:
        meta['zone_status'] = 'floating_time_zone_unknown'
        return local_candidate(kind, raw, source, naive, precision, meta)
    resolved = calendar.zone(tzid)
    if resolved is None:
        meta['zone_status'] = 'tzid_unresolved'
        meta['tzid'] = tzid
        issues.add('ics_tzid_unresolved', tzid=tzid[:64], line=line.line_start)
        return local_candidate(kind, raw, source, naive, precision, meta)
    zone, declaration, iana = resolved
    candidate = declared_zone_candidate(kind, raw, source, naive, zone, tzid, precision, declaration, meta)
    if iana is not None and candidate.utc is not None:
        alternative = declared_zone_candidate(kind, raw, source, naive, iana, tzid, precision, 'iana_tzdata_by_tzid_name')
        if alternative.utc != candidate.utc:
            issues.add('ics_vtimezone_disagrees_with_iana', tzid=tzid[:64], line=line.line_start)
            candidate = TimeCandidate(candidate.kind, candidate.raw, candidate.source, utc=candidate.utc,
                                      local=candidate.local, tz_status=candidate.tz_status, tz=candidate.tz,
                                      precision=candidate.precision,
                                      metadata={**candidate.metadata, 'iana_alternative_utc': alternative.utc})
    if candidate.metadata.get('dst'):
        issues.add(f"ics_{candidate.metadata['dst']}", property=line.name, line=line.line_start)
    return candidate


def parse_duration(raw: str) -> timedelta | None:
    match = _DURATION.match(raw.strip())
    if not match or not any(match.groups()[1:]):
        return None
    sign, weeks, days, hours, minutes, seconds = match.groups()
    delta = timedelta(weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0),
                      minutes=int(minutes or 0), seconds=int(seconds or 0))
    return -delta if sign == '-' else delta


def _end_from_duration(start: TimeCandidate, duration: timedelta, raw: str, calendar: Calendar, start_line) -> TimeCandidate:
    """Labelled projection: nominal wall-clock addition in the start's zone (RFC 5545 §3.3.6)."""
    local = datetime.fromisoformat(start.local) + duration
    meta = {'projection': 'dtstart_plus_duration', 'duration_raw': raw, 'property': 'DURATION'}
    if start.tz_status == 'unknown':
        return local_candidate('end', raw, 'ics:DURATION', local, start.precision, {**start.metadata, **meta})
    if start.tz == 'UTC':
        return offset_candidate('end', raw, 'ics:DURATION', local, 0, start.precision, meta, tz_label='UTC')
    resolved = calendar.zone(start.tz)
    zone, declaration, _ = resolved
    return declared_zone_candidate('end', raw, 'ics:DURATION', local, zone, start.tz, start.precision, declaration, meta)


def _text(lines, name):
    line = next((l for l in lines if l.name == name), None)
    return unescape_text(line.value) if line is not None else None


def import_ics(path: Path, *, source_locator: str | None = None, original_name: str | None = None,
               collection_id: int | None = None, label: str | None = None,
               limits: CommLimits = DEFAULT_LIMITS) -> dict:
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(
        path, source_kind='calendar_export', source_label=label or 'ICS', locator=locator, importer=PARSER_NAME,
        parser_version=PARSER_VERSION, original_name=original_name, collection_id=collection_id,
        mime_type='text/calendar', options={'recurrence_expansion_on_import': False})
    issues = Issues(limits.max_issues)
    snapshot = stored_path(artifact_id)
    size = snapshot.stat().st_size
    if size > limits.max_json_bytes:
        issues.add('document_size_limit', size_bytes=size)
        coverage = {'complete': False}
        record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options={},
                          counts={'events': 0}, coverage=coverage, issues=issues)
        return {'artifact_id': artifact_id, 'events': 0, 'coverage': coverage, 'issues': issues.as_dict()}
    calendar = Calendar(snapshot.read_bytes(), limits, issues)
    counts = {'events': 0, 'recurring_masters': 0, 'overrides': 0, 'floating': 0, 'all_day': 0}
    seen: dict[str, int] = {}
    complete = True
    for vevent in calendar.vevents:
        if counts['events'] >= limits.max_records:
            issues.add('record_limit', limit=limits.max_records)
            complete = False
            break
        lines = vevent['lines']
        by_name: dict[str, list] = {}
        for line in lines:
            by_name.setdefault(line.name, []).append(line)
        uid = _text(lines, 'UID')
        recurrence_id = by_name.get('RECURRENCE-ID', [None])[0]
        problems = []
        if uid is None:
            problems.append('uid_missing_content_derived_id')
            base = 'content-sha256:' + content_id([l.raw for l in lines])
        else:
            base = f'uid:{uid}'
        if recurrence_id is not None:
            base += f'|recurrence-id:{recurrence_id.value.strip()}'
            counts['overrides'] += 1
        occurrence = seen.get(base, 0) + 1
        seen[base] = occurrence
        external_id = base if occurrence == 1 else f'{base}#occurrence-{occurrence}'
        if occurrence > 1:
            problems.append('duplicate_identity_in_file')
        candidates: list[TimeCandidate] = []
        start = end = None
        if 'DTSTART' in by_name:
            start = time_candidate(by_name['DTSTART'][0], 'start', calendar, issues)
        else:
            problems.append('dtstart_missing')
        if 'DTEND' in by_name:
            end = time_candidate(by_name['DTEND'][0], 'end', calendar, issues)
        elif 'DURATION' in by_name and start is not None:
            duration = parse_duration(by_name['DURATION'][0].value)
            if duration is None:
                problems.append('duration_unparsable')
            else:
                end = _end_from_duration(start, duration, by_name['DURATION'][0].value.strip(), calendar,
                                         by_name['DURATION'][0].line_start)
        for candidate in (start, end):
            if candidate is not None:
                candidates.append(candidate)
                if candidate.metadata.get('zone_status') == 'floating_time_zone_unknown' and candidate is start:
                    counts['floating'] += 1
                if candidate.metadata.get('all_day_date') and candidate is start:
                    counts['all_day'] += 1
        for name, kind in (('CREATED', 'created'), ('LAST-MODIFIED', 'updated'), ('DTSTAMP', 'other'),
                           ('RECURRENCE-ID', 'other')):
            for line in by_name.get(name, [])[:1]:
                extra = {'semantics': 'rfc5545_dtstamp_depends_on_method'} if name == 'DTSTAMP' else None
                candidate = time_candidate(line, kind, calendar, issues, extra=extra)
                if candidate is not None:
                    candidates.append(candidate)
        recurrence = {
            'rrule': [l.value for l in by_name.get('RRULE', [])],
            'rdate': [{'value': l.value, 'tzid': (l.param('TZID') or [None])[0]} for l in by_name.get('RDATE', [])],
            'exdate': [{'value': l.value, 'tzid': (l.param('TZID') or [None])[0]} for l in by_name.get('EXDATE', [])],
            'recurrence_id': recurrence_id.value if recurrence_id is not None else None,
            'expanded_on_import': False,
        }
        if recurrence['rrule'] or recurrence['rdate']:
            counts['recurring_masters'] += 1
        raw_lines = []
        for line in lines[:limits.max_raw_items]:
            kept, ok = bounded_text(line.raw, limits.max_raw_chars)
            raw_lines.append(kept if ok else {'truncated': kept})
        description = _text(lines, 'DESCRIPTION')
        body, body_complete = bounded_text(description, limits.max_body_chars)
        metadata = {
            'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
            'locator': {'artifact_id': artifact_id, 'vevent_index': vevent['index'],
                        'line_start': vevent['line_start'], 'line_end': vevent['line_end']},
            'uid': uid, 'raw_lines': raw_lines, 'raw_lines_total': len(lines),
            'location': _text(lines, 'LOCATION'), 'status': _text(lines, 'STATUS'),
            'categories': [unescape_text(v) for l in by_name.get('CATEGORIES', []) for v in split_unescaped(l.value, ',')],
            'organizer': [l.value for l in by_name.get('ORGANIZER', [])],
            'attendees': [l.value for l in by_name.get('ATTENDEE', [])][:limits.max_raw_items],
            'recurrence': recurrence, 'body_complete': body_complete,
            'time_policy': 'ts_start/ts_end only when the instant is fixed (UTC or resolved TZID, no DST fold)',
            'issues': problems,
        }
        add_event_with_times(
            candidates, source_id=source_id, artifact_id=artifact_id, event_type='calendar_event',
            ts_start=legacy_instant(start), ts_end=legacy_instant(end), subject=_text(lines, 'SUMMARY'),
            body=body, external_id=external_id, thread_id=f'uid:{uid}' if uid else None, confidence=None,
            metadata=metadata)
        for problem in problems:
            issues.add(problem, vevent_index=vevent['index'])
        counts['events'] += 1
    coverage = {'complete': complete}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                      options={'recurrence_expansion_on_import': False}, counts=counts, coverage=coverage,
                      issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser_version': PARSER_VERSION, **counts,
            'coverage': coverage, 'issues': issues.as_dict()}


def expand_recurrence(event_id: int, *, max_instances: int = 100, until: str | None = None,
                      limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """Bounded projection of a recurring VEVENT; nothing is stored.

    Occurrences are generated in the event's own wall-clock time (RFC 5545) and
    then mapped through its declared zone, so DST shifts the UTC instant while
    the local time stays fixed. EXDATEs are removed and RECURRENCE-ID overrides
    in the same file are marked, not merged.
    """
    max_instances = max(1, min(max_instances, limits.max_recurrence_instances))
    with session() as db:
        event = db.execute("SELECT * FROM events WHERE id=? AND event_type='calendar_event'", (event_id,)).fetchone()
    if event is None:
        raise ValueError('calendar event does not exist')
    metadata = json.loads(event['metadata_json'])
    if metadata.get('parser', {}).get('name') != PARSER_NAME:
        raise ValueError('event was not produced by the ICS importer')
    try:
        from dateutil import __version__ as dateutil_version
        from dateutil.rrule import rrulestr
    except ImportError:
        return {'status': 'unavailable', 'reason': 'python-dateutil not installed', 'rule': PROJECTION_RULE}
    issues = Issues(limits.max_issues)
    calendar = Calendar(stored_path(metadata['locator']['artifact_id']).read_bytes(), limits, issues)
    vevent = calendar.vevents[metadata['locator']['vevent_index']]
    dtstart_line = next((l for l in vevent['lines'] if l.name == 'DTSTART'), None)
    rules = [l.value for l in vevent['lines'] if l.name == 'RRULE']
    rdates = [l for l in vevent['lines'] if l.name == 'RDATE']
    if dtstart_line is None or not (rules or rdates):
        return {'status': 'not_recurring', 'rule': PROJECTION_RULE, 'occurrences': []}
    start = time_candidate(dtstart_line, 'start', calendar, issues)
    if start is None:
        return {'status': 'unparsable_dtstart', 'rule': PROJECTION_RULE, 'occurrences': []}
    naive_start = datetime.fromisoformat(start.local)
    lines = [f'RRULE:{rule}' for rule in rules]
    for line in rdates:
        lines.append(f'RDATE:{line.value}')
    exdates = set()
    for line in (l for l in vevent['lines'] if l.name == 'EXDATE'):
        for value in line.value.split(','):
            match = _DT.match(value.strip())
            if match:
                y, mo, d, h, mi, s, _ = match.groups()
                exdates.add(datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0)))
    overrides = set()
    uid = metadata.get('uid')
    for other in calendar.vevents:
        if uid and any(l.name == 'UID' and unescape_text(l.value) == uid for l in other['lines']):
            rid = next((l for l in other['lines'] if l.name == 'RECURRENCE-ID'), None)
            if rid is not None:
                match = _DT.match(rid.value.strip())
                if match:
                    y, mo, d, h, mi, s, _ = match.groups()
                    overrides.add(datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0)))
    try:
        # ignoretz: a UTC UNTIL is compared as a naive wall-clock bound; the
        # projection labels this simplification instead of hiding it.
        ruleset = rrulestr('\n'.join(lines), dtstart=naive_start, forceset=True, ignoretz=True)
    except (ValueError, TypeError) as exc:
        return {'status': 'unsupported_rule', 'rule': PROJECTION_RULE, 'error': str(exc)[:200], 'occurrences': []}
    bound = datetime.fromisoformat(until) if until else None
    occurrences = []
    truncated = False
    for value in ruleset:
        if bound is not None and value > bound:
            break
        if len(occurrences) >= max_instances:
            truncated = True
            break
        if value in exdates:
            continue
        if start.tz_status == 'unknown' or start.metadata.get('all_day_date'):
            candidate = local_candidate('start', None, 'ics:RRULE', value, start.precision)
        elif start.tz == 'UTC':
            candidate = offset_candidate('start', None, 'ics:RRULE', value, 0, start.precision, tz_label='UTC')
        else:
            zone, declaration, _ = calendar.zone(start.tz)
            candidate = declared_zone_candidate('start', None, 'ics:RRULE', value, zone, start.tz, start.precision,
                                                declaration)
        occurrences.append({'local': candidate.local, 'utc': candidate.utc, 'tz_status': candidate.tz_status,
                            'tz': candidate.tz, 'dst': candidate.metadata.get('dst'),
                            'overridden_by_recurrence_id': value in overrides})
    return {'status': 'projected', 'rule': PROJECTION_RULE, 'engine': f'python-dateutil {dateutil_version}',
            'event_id': event_id, 'max_instances': max_instances, 'truncated': truncated,
            'until_simplification': 'UNTIL compared as wall-clock time of DTSTART',
            'stored': False, 'occurrences': occurrences}

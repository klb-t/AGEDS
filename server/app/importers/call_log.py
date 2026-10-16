"""SMS Backup & Restore ``calls.xml`` import as ``call`` events with time candidates.

The XML is read with a streaming pull parser; DTD/entity declarations are
rejected. ``date`` (Unix milliseconds) is the start candidate; ``duration`` in
seconds yields a separately labelled ``end`` projection. Type/presentation
codes are mapped through ``data/call_log_codes.v1.json``; unmapped codes stay
raw. ``readable_date`` is the app's display string in an unknown zone and is
kept raw, not parsed.

The older ``sms_backup`` importer also reads ``calls.xml`` (parser
``sms_backup``). Importing one file with both yields two parser projections of
the same rows; the result reports when the other projection exists.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from ..db import session
from ..timeinfo import TimeCandidate, format_utc
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, add_event_with_times, bound_json,
                          epoch_candidate, legacy_instant, load_data, record_import_run, stored_path)

PARSER_NAME = 'calls_xml'
PARSER_VERSION = '1'
CHUNK = 64 * 1024


def iter_xml_elements(path: Path, tag: str, issues: Issues, limits: CommLimits):
    """Yield (index, attributes) for ``tag`` elements; bounded and DTD-free."""
    parser = ET.XMLPullParser(events=('start', 'end'))
    tail = b''
    index = 0
    root_tag = None
    with path.open('rb') as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            window = (tail + chunk).upper()
            if b'<!DOCTYPE' in window or b'<!ENTITY' in window:
                raise ValueError('XML document type/entity declarations are not accepted')
            tail = chunk[-16:]
            parser.feed(chunk)
            for event, element in parser.read_events():
                if event == 'start' and root_tag is None:
                    root_tag = element.tag
                    yield 'root', element.tag
                elif event == 'end' and element.tag == tag:
                    index += 1
                    if index > limits.max_records:
                        issues.add('record_limit', limit=limits.max_records)
                        return
                    yield index, dict(element.attrib)
                    element.clear()
                elif event == 'end' and element.tag != root_tag:
                    issues.add('unexpected_element', tag=str(element.tag)[:64])
                    element.clear()
        parser.close()


def call_candidates(attrs: dict, issues_for_row: list[str]) -> tuple[TimeCandidate | None, TimeCandidate | None, dict]:
    """Start from epoch ms; end as start + duration seconds (projection)."""
    date_raw, duration_raw = attrs.get('date'), attrs.get('duration')
    start = end = None
    duration = None
    meta = {'duration_raw': duration_raw}
    if date_raw is None or not date_raw.strip():
        issues_for_row.append('timestamp_missing')
    else:
        try:
            start = epoch_candidate('start', date_raw, 'calls_xml:date', unit='ms')
        except (ValueError, OverflowError):
            issues_for_row.append('timestamp_invalid')
    if duration_raw is None or not duration_raw.strip():
        issues_for_row.append('duration_missing')
    else:
        try:
            duration = int(duration_raw)
            if duration < 0:
                issues_for_row.append('duration_negative')
                duration = None
        except ValueError:
            issues_for_row.append('duration_invalid')
    meta['duration_seconds'] = duration
    if start is not None and duration is not None:
        moment = datetime.fromisoformat(start.utc.replace('Z', '+00:00')) + timedelta(seconds=duration)
        end = TimeCandidate('end', duration_raw, 'calls_xml:date+duration', utc=format_utc(moment),
                            tz_status='utc_epoch', tz='UTC', precision=start.precision,
                            metadata={'projection': 'start_plus_duration_seconds', 'start_raw': date_raw,
                                      'duration_unit': 'seconds', 'duration_semantics': 'as_logged_by_device'})
    return start, end, meta


def import_calls_xml(path: Path, *, source_locator: str | None = None, original_name: str | None = None,
                     collection_id: int | None = None, label: str | None = None,
                     limits: CommLimits = DEFAULT_LIMITS) -> dict:
    codes = load_data('call_log_codes.v1.json')
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(
        path, source_kind='android_backup', source_label=label or 'Call log (calls.xml)', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name,
        collection_id=collection_id, mime_type='application/xml',
        options={'code_mapping': f"{codes['schema']}/v{codes['version']}"})
    issues = Issues(limits.max_issues)
    counts = {'calls': 0, 'unmapped_type_codes': 0, 'without_end_projection': 0}
    complete = True
    for index, attrs in iter_xml_elements(stored_path(artifact_id), 'call', issues, limits):
        if index == 'root':
            if str(attrs).lower() != 'calls':
                raise ValueError(f'unsupported calls.xml root element: {attrs}')
            continue
        problems: list[str] = []
        start, end, duration_meta = call_candidates(attrs, problems)
        type_code = attrs.get('type')
        direction = codes['type'].get(type_code or '')
        if direction is None:
            problems.append('type_code_unmapped')
            counts['unmapped_type_codes'] += 1
        presentation_code = attrs.get('presentation')
        presentation = codes['presentation'].get(presentation_code or '') if presentation_code is not None else None
        raw, truncated = bound_json(attrs, limits)
        if end is None:
            counts['without_end_projection'] += 1
        metadata = {
            'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION, 'row': index},
            'locator': {'artifact_id': artifact_id, 'element': 'call', 'ordinal': index},
            'raw_attributes': raw, 'raw_truncated_values': truncated,
            'type': {'raw': type_code, 'label': direction, 'mapping': f"{codes['schema']}/v{codes['version']}"},
            'presentation': {'raw': presentation_code, 'label': presentation},
            'duration': duration_meta,
            'readable_date': {'raw': attrs.get('readable_date'), 'interpretation': 'display_string_zone_unknown_not_parsed'},
            'contact_name_semantics': 'device_address_book_label_at_backup_time',
            'issues': problems,
        }
        add_event_with_times(
            [c for c in (start, end) if c is not None], source_id=source_id, artifact_id=artifact_id,
            event_type='call', ts_start=legacy_instant(start), ts_end=legacy_instant(end),
            direction=direction or type_code, contact_label=attrs.get('contact_name'),
            phone_or_address=attrs.get('number'), external_id=f'{PARSER_NAME}:row:{index}', confidence=None,
            metadata=metadata)
        for problem in problems:
            issues.add(problem, row=index)
        counts['calls'] += 1
    if issues.counts.get('record_limit'):
        complete = False
    with session() as db:
        other = db.execute("SELECT COUNT(*) FROM events WHERE artifact_id=? AND event_type='call' AND external_id LIKE 'sms_backup:row:%'",
                            (artifact_id,)).fetchone()[0]
    if other:
        issues.add('other_parser_projection_exists', parser='sms_backup', events=other)
    coverage = {'complete': complete}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                      options={'code_mapping': f"{codes['schema']}/v{codes['version']}"}, counts=counts,
                      coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser_version': PARSER_VERSION, **counts,
            'coverage': coverage, 'issues': issues.as_dict()}

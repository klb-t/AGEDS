"""Google Takeout Fit → samples and ``health_session`` events. Opt-in (``enable_health``).

* ``All Sessions/*.json`` (one object per session): ``fitnessActivity``,
  ``startTime``/``endTime`` (``Z``), declared ``duration`` (``"1803.512s"``),
  ``segment`` (e.g. sleep stages) and ``aggregate`` metrics → one session event,
  segments and aggregates as linked samples. JSON numbers keep their text
  (``parse_float=str``): no float enters a stored value.
* ``Daily activity metrics.csv`` (one row per day): every metric column → a
  daily sample (day precision, zone unknown). Latitude/longitude columns are
  *not* imported (location belongs to the opt-in location importers); their
  names are listed in the result.
* ``YYYY-MM-DD.csv`` interval files (``Start time``/``End time`` like
  ``00:15:00.000+01:00``): the date comes from the *file name* (recorded as
  ``date_basis: file_name``), the time and offset from the cell.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..ledger import health, textio, timeparse
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, bound_json, load_data, local_candidate,
                          offset_candidate, record_import_run, stored_path)

PARSER_NAME = 'google_fit_takeout'
PARSER_VERSION = '1'
PROVIDER = 'google_fit'


def _unit(header: str) -> str | None:
    match = re.search(r'\(([^)]+)\)\s*$', header)
    return match.group(1) if match else None


def import_google_fit(path: Path, *, enable_health: bool = False, source_locator: str | None = None,
                      original_name: str | None = None, collection_id: int | None = None, label: str | None = None,
                      limits: CommLimits = DEFAULT_LIMITS) -> dict:
    health.require_opt_in(enable_health)
    mapping = load_data('health_google_fit.v1.json')
    name = original_name or path.name
    locator = source_locator if source_locator is not None else str(path)
    options = {'enable_health': True}
    source_id, artifact_id = acquire(
        path, source_kind='health_export', source_label=label or 'Google Fit (Takeout)', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name, collection_id=collection_id,
        options=options)
    raw = stored_path(artifact_id).read_bytes()
    if len(raw) > limits.max_json_bytes:
        raise ValueError(f'file larger than {limits.max_json_bytes} bytes')
    issues = Issues(limits.max_issues)
    text, encoding = textio.decode(raw)
    ctx = {'artifact_id': artifact_id, 'source_id': source_id, 'mapping': mapping, 'issues': issues,
           'limits': limits, 'name': name}
    if text.lstrip().startswith('{'):
        result = _session(json.loads(text, parse_float=str), ctx)
    else:
        result = _table(text, ctx)
    coverage = {'complete': not result.pop('limited', False)}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts=result, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION,
            **result, 'coverage': coverage, 'issues': issues.as_dict(), 'opt_in': health.opt_in_record()}


def _session(data: dict, ctx: dict) -> dict:
    mapping, formats = ctx['mapping'], ctx['mapping']['time_formats']
    if not isinstance(data, dict) or 'fitnessActivity' not in data:
        raise ValueError('not a Google Fit session object (no fitnessActivity)')
    problems: list[str] = []
    activity = str(data.get('fitnessActivity'))
    start, problem = health.time_candidate('start', data.get('startTime'), f'{PROVIDER}:session:start', formats)
    problems += [problem] if problem else []
    end, problem = health.time_candidate('end', data.get('endTime'), f'{PROVIDER}:session:end', formats)
    problems += [problem] if problem else []
    declared = data.get('duration')
    declared_s = None
    if isinstance(declared, str) and re.fullmatch(r'\d+(?:\.\d+)?s', declared):
        declared_s = health.exact_value(declared[:-1])
    span = health.duration(start, end)
    if span['status'] == 'ok' and declared_s is not None and abs(float(declared_s) - span['seconds']) > 1:
        problems.append('declared_duration_differs_from_span')
    samples = []
    for index, segment in enumerate(data.get('segment') or []):
        s_start, p1 = health.time_candidate('start', segment.get('startTime'), f'{PROVIDER}:segment[{index}]:start', formats)
        s_end, p2 = health.time_candidate('end', segment.get('endTime'), f'{PROVIDER}:segment[{index}]:end', formats)
        problems += [p for p in (p1, p2) if p]
        samples.append(health.sample_row(
            artifact_id=ctx['artifact_id'], parser=PARSER_NAME, parser_version=PARSER_VERSION,
            position=f'segment[{index}]', sample_type=f"segment:{segment.get('fitnessActivity')}",
            sample_class='session_segment', value_raw=None, unit=None, start=s_start, end=s_end, source_name=None,
            source_version=None, device_raw=None, basis='provider_inference',
            metadata={'span': health.duration(s_start, s_end)}))
    for index, aggregate in enumerate(data.get('aggregate') or []):
        value = next((aggregate[k] for k in ('intValue', 'floatValue', 'stringValue') if k in aggregate), None)
        samples.append(health.sample_row(
            artifact_id=ctx['artifact_id'], parser=PARSER_NAME, parser_version=PARSER_VERSION,
            position=f'aggregate[{index}]', sample_type=str(aggregate.get('metricName')), sample_class='session_aggregate',
            value_raw=value, unit=None, start=start, end=end, source_name=None, source_version=None, device_raw=None,
            basis='provider_inference', metadata={}))
    for problem in problems:
        ctx['issues'].add(problem)
    kind = 'sleep' if activity in mapping['sleep_activities'] else 'workout'
    application = data.get('application') if isinstance(data.get('application'), dict) else {}
    source_name = application.get('packageName') or application.get('name')
    raw, truncated = bound_json({k: v for k, v in data.items() if k not in ('segment', 'aggregate')}, ctx['limits'])
    event = health.session_event(
        source_id=ctx['source_id'], artifact_id=ctx['artifact_id'], external_id=f'{PARSER_NAME}:session', kind=kind,
        activity_raw=activity, start=start, end=end, provider=PROVIDER, source_name=source_name,
        signal=health.session_signal(PROVIDER, source_name, cls='passive' if kind == 'sleep' else 'active',
                                     note='activity type, segments and aggregates are Google Fit inferences'),
        metadata={'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                  'locator': {'artifact_id': ctx['artifact_id'], 'file': ctx['name']},
                  'session': {'declared_duration': {'raw': declared, 'seconds_exact': declared_s},
                              'name': data.get('name'), 'description': data.get('description')},
                  'raw_object': raw, 'raw_truncated_values': truncated, 'issues': problems})
    health.write_session(event, [c for c in (start, end) if c is not None], samples)
    return {'sessions': 1, 'samples': len(samples)}


def _table(text: str, ctx: dict) -> dict:
    mapping, limits, issues = ctx['mapping'], ctx['limits'], ctx['issues']
    lines = textio.physical_lines(text)
    parsed = textio.parse_records(lines, start=0, delimiter=',', max_records=limits.max_records + 1,
                                  max_raw_chars=limits.max_raw_chars)
    if parsed['error']:
        issues.add('csv_record_parse_failed', **parsed['error'])
    records = parsed['records']
    if not records:
        raise ValueError('empty CSV')
    header = [h.strip() for h in records[0]['values']]
    location = re.compile(mapping['location_column_regex'])
    skipped = [h for h in header if location.search(h)]
    writer = health.SampleWriter()
    rows = 0
    if header and header[0] == 'Date':
        mode, file_date = 'daily', None
        metric_columns = [(i, h) for i, h in enumerate(header) if i > 0 and h not in skipped]
    elif 'Start time' in header and 'End time' in header:
        mode = 'intervals'
        match = re.search(mapping['interval_file_date_regex'], ctx['name'])
        file_date = match.group(1) if match else None
        if file_date is None:
            issues.add('interval_file_name_has_no_date')
        skip = {header.index('Start time'), header.index('End time')}
        metric_columns = [(i, h) for i, h in enumerate(header) if i not in skip and h not in skipped]
    else:
        raise ValueError('not a Google Fit activity metrics CSV (expected a Date or Start time/End time header)')
    for record in records[1:limits.max_records + 1]:
        values = record['values']
        if textio.blank(values):
            continue
        rows += 1
        problems: list[str] = []
        position = f"row:{record['index']}"
        if mode == 'daily':
            start = _day(values[0] if values else None, f'{PROVIDER}:{position}:day', mapping, problems)
            end = None
        else:
            start = _interval(values[header.index('Start time')], file_date, f'{PROVIDER}:{position}:start', mapping, problems)
            end = _interval(values[header.index('End time')], file_date, f'{PROVIDER}:{position}:end', mapping, problems)
        for problem in problems:
            issues.add(problem, position=position)
        for index, column in metric_columns:
            value = values[index].strip() if index < len(values) else ''
            if value == '':
                continue
            writer.add(health.sample_row(
                artifact_id=ctx['artifact_id'], parser=PARSER_NAME, parser_version=PARSER_VERSION,
                position=f'{position}/{column}', sample_type=column, sample_class='daily_metric' if mode == 'daily'
                else 'interval_metric', value_raw=value, unit=_unit(column), start=start, end=end, source_name=None,
                source_version=None, device_raw=None, basis='provider_inference',
                metadata={'date_basis': 'file_name' if mode == 'intervals' else 'date_column',
                          'line': record['start_line'], 'issues': problems}))
    writer.flush()
    return {'mode': mode, 'rows': rows, 'samples_inserted': writer.inserted, 'samples_existing': writer.existing,
            'location_columns_not_imported': skipped, 'file_date': file_date,
            'limited': len(records) - 1 > limits.max_records}


def _day(text, source, mapping, problems):
    parsed = timeparse.parse(text, mapping['date_formats'])
    if parsed['status'] != 'ok':
        problems.append(f"date_{parsed['status']}")
        return None
    return local_candidate('start', text, source, parsed['naive'], 'day', {'zone_status': 'export_zone_not_stated',
                                                                         'date_kind': 'metric_day'})


def _interval(text, file_date, source, mapping, problems):
    parsed = timeparse.parse(text, mapping['interval_time_formats'])
    if parsed['status'] != 'ok':
        problems.append(f"interval_time_{parsed['status']}")
        return None
    if file_date is None:
        problems.append('interval_date_unknown')
        return None
    day = timeparse.parse(file_date, mapping['date_formats'])['naive']
    naive = day.replace(hour=parsed['naive'].hour, minute=parsed['naive'].minute, second=parsed['naive'].second,
                        microsecond=parsed['naive'].microsecond)
    meta = {'date_basis': 'file_name', 'file_date': file_date, 'zone_status': 'offset_stated_by_export'}
    if parsed['offset_minutes'] is None:
        return local_candidate(source.rsplit(':', 1)[-1], f'{file_date} {text}', source, naive, parsed['precision'],
                               {**meta, 'zone_status': 'export_zone_not_stated'})
    return offset_candidate(source.rsplit(':', 1)[-1], f'{file_date} {text}', source, naive, parsed['offset_minutes'],
                            parsed['precision'], meta)

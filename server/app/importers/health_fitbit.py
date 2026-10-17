"""Fitbit data export JSON (``sleep-*.json``, ``exercise-*.json``) → sessions and samples. Opt-in.

* Sleep logs: ``startTime``/``endTime`` are wall clocks *without a zone*
  (``unknown``), ``duration`` in milliseconds, summary minutes and ``levels``
  (``data``/``shortData`` stage entries with ``dateTime`` and ``seconds``, kept
  as stage samples whose end is a projection ``start + seconds``).
* Exercise logs: ``startTime`` is written month-first with a two-digit year
  (``01/02/24 18:00:00``) — read with the mapping's explicit, recorded
  ``two_digit_year_base`` — the end is a projection ``start + duration``.
  ``logType`` says who created the record: ``manual`` → ``provider_user_edit``
  (declared by the user), ``auto_detected`` → passive provider inference.
JSON numbers keep their text (``parse_float=str``).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from ..ledger import health, textio
from ..timeinfo import TimeCandidate
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, bound_json, load_data, local_candidate,
                          record_import_run, stored_path)

PARSER_NAME = 'fitbit_export'
PARSER_VERSION = '1'
PROVIDER = 'fitbit'


def import_fitbit(path: Path, *, enable_health: bool = False, source_locator: str | None = None,
                  original_name: str | None = None, collection_id: int | None = None, label: str | None = None,
                  limits: CommLimits = DEFAULT_LIMITS) -> dict:
    health.require_opt_in(enable_health)
    mapping = load_data('health_fitbit.v1.json')
    name = original_name or path.name
    locator = source_locator if source_locator is not None else str(path)
    options = {'enable_health': True, 'two_digit_year_base': mapping['two_digit_year_base']}
    source_id, artifact_id = acquire(
        path, source_kind='health_export', source_label=label or 'Fitbit export', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name, collection_id=collection_id,
        options=options)
    raw = stored_path(artifact_id).read_bytes()
    if len(raw) > limits.max_json_bytes:
        raise ValueError(f'file larger than {limits.max_json_bytes} bytes')
    issues = Issues(limits.max_issues)
    text, _ = textio.decode(raw)
    data = json.loads(text, parse_float=str)
    if not isinstance(data, list):
        raise ValueError('a Fitbit export file is a JSON list of logs')
    lower = name.lower()
    if lower.startswith('sleep') or (data and isinstance(data[0], dict) and 'dateOfSleep' in data[0]):
        kind = 'sleep'
    elif lower.startswith('exercise') or (data and isinstance(data[0], dict) and 'activityName' in data[0]):
        kind = 'exercise'
    else:
        raise ValueError('not a Fitbit sleep or exercise export (file name or fields not recognized)')
    counts = {'kind': kind, 'logs': 0, 'sessions': 0, 'samples': 0}
    for index, log in enumerate(data[:limits.max_records]):
        if not isinstance(log, dict):
            issues.add('log_not_an_object', index=index)
            continue
        counts['logs'] += 1
        (_sleep if kind == 'sleep' else _exercise)(log, index, mapping, artifact_id, source_id, issues, counts, limits)
    coverage = {'complete': len(data) <= limits.max_records}
    if not coverage['complete']:
        issues.add('record_limit', limit=limits.max_records)
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts={k: v for k, v in counts.items() if isinstance(v, int)}, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION,
            **counts, 'coverage': coverage, 'issues': issues.as_dict(), 'opt_in': health.opt_in_record()}


def _projection(start: TimeCandidate | None, seconds, source: str, basis: str) -> TimeCandidate | None:
    """End = start + a declared duration (zone status of the start is kept)."""
    if start is None or start.local is None or seconds is None:
        return None
    try:
        delta = timedelta(milliseconds=int(seconds)) if basis == 'ms' else timedelta(seconds=int(seconds))
    except (TypeError, ValueError):
        return None
    naive = datetime.fromisoformat(start.local) + delta
    return local_candidate('end', str(seconds), source, naive, start.precision,
                           {'projection': f'start_plus_declared_duration_{basis}', 'zone_status': 'export_zone_not_stated'})


def _session_id(log: dict, index: int) -> str:
    return f'logId:{log["logId"]}' if log.get('logId') is not None else f'index:{index}'


def _sleep(log, index, mapping, artifact_id, source_id, issues, counts, limits):
    problems: list[str] = []
    formats = mapping['sleep_time_formats']
    ident = _session_id(log, index)
    source = f'{PROVIDER}:sleep:{ident}'
    start, p1 = health.time_candidate('start', log.get('startTime'), f'{source}:start', formats)
    end, p2 = health.time_candidate('end', log.get('endTime'), f'{source}:end', formats)
    problems += [p for p in (p1, p2) if p]
    samples = []
    for field in mapping['sleep_summary_fields']:
        if log.get(field) is not None:
            samples.append(health.sample_row(
                artifact_id=artifact_id, parser=PARSER_NAME, parser_version=PARSER_VERSION,
                position=f'{ident}/{field}', sample_type=f'sleep.{field}', sample_class='session_summary',
                value_raw=log[field], unit='min' if field.startswith('minutes') or field == 'timeInBed' else None,
                start=start, end=end, source_name=PROVIDER, source_version=None, device_raw=None,
                basis='provider_inference', metadata={}))
    levels = log.get('levels') if isinstance(log.get('levels'), dict) else {}
    for series in ('data', 'shortData'):
        for position, stage in enumerate(levels.get(series) or []):
            s_start, problem = health.time_candidate('start', stage.get('dateTime'),
                                                     f'{source}:{series}[{position}]:start', formats)
            if problem:
                problems.append(problem)
            s_end = _projection(s_start, stage.get('seconds'), f'{source}:{series}[{position}]:end', 'seconds')
            samples.append(health.sample_row(
                artifact_id=artifact_id, parser=PARSER_NAME, parser_version=PARSER_VERSION,
                position=f'{ident}/{series}[{position}]', sample_type=f"sleep_stage:{stage.get('level')}",
                sample_class='sleep_stage' if series == 'data' else 'sleep_short_stage', value_raw=stage.get('seconds'),
                unit='s', start=s_start, end=s_end, source_name=PROVIDER, source_version=None, device_raw=None,
                basis='provider_inference', metadata={'series': series}))
    span = health.duration(start, end)
    declared = log.get('duration')
    if span['status'] == 'ok' and isinstance(declared, int) and abs(declared / 1000 - span['seconds']) > 60:
        problems.append('declared_duration_differs_from_span')
    for problem in problems:
        issues.add(problem, log=ident)
    log_type = log.get('logType')
    signal_spec = mapping['log_types'].get(log_type, {'class': 'passive', 'basis': 'provider_inference'})
    raw, truncated = bound_json({k: v for k, v in log.items() if k != 'levels'}, limits)
    event = health.session_event(
        source_id=source_id, artifact_id=artifact_id, external_id=f'{PARSER_NAME}:sleep:{ident}', kind='sleep',
        activity_raw=log.get('type'), start=start, end=end, provider=PROVIDER, source_name=PROVIDER,
        signal=health.session_signal(PROVIDER, PROVIDER, cls=signal_spec['class'], basis=signal_spec['basis'],
                                     log_type=log_type),
        metadata={'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                  'locator': {'artifact_id': artifact_id, 'index': index, 'log_id': log.get('logId')},
                  'session': {'date_of_sleep': log.get('dateOfSleep'), 'main_sleep': log.get('mainSleep'),
                              'declared_duration_ms': declared, 'levels_summary': levels.get('summary')},
                  'raw_log': raw, 'raw_truncated_values': truncated, 'issues': problems})
    health.write_session(event, [c for c in (start, end) if c is not None], samples)
    counts['sessions'] += 1
    counts['samples'] += len(samples)


def _exercise(log, index, mapping, artifact_id, source_id, issues, counts, limits):
    problems: list[str] = []
    ident = _session_id(log, index)
    source = f'{PROVIDER}:exercise:{ident}'
    start, problem = health.time_candidate('start', log.get('startTime'), f'{source}:start',
                                           mapping['exercise_time_formats'],
                                           two_digit_year_base=mapping['two_digit_year_base'])
    if problem:
        problems.append(problem)
    end = _projection(start, log.get('duration'), f'{source}:end', 'ms')
    if start is not None and log.get('duration') is not None and end is None:
        problems.append('duration_unparsed')
    samples = [health.sample_row(
        artifact_id=artifact_id, parser=PARSER_NAME, parser_version=PARSER_VERSION, position=f'{ident}/{field}',
        sample_type=f'exercise.{field}', sample_class='session_summary', value_raw=log[field],
        unit=log.get('distanceUnit') if field == 'distance' else ('ms' if field == 'activeDuration' else None),
        start=start, end=end, source_name=PROVIDER, source_version=None, device_raw=None, basis='provider_inference',
        metadata={}) for field in mapping['exercise_summary_fields'] if log.get(field) is not None]
    for problem in problems:
        issues.add(problem, log=ident)
    log_type = log.get('logType')
    if log_type not in mapping['log_types']:
        problems.append('log_type_unmapped')
    signal_spec = mapping['log_types'].get(log_type, {'class': 'passive', 'basis': 'provider_inference'})
    raw, truncated = bound_json({k: v for k, v in log.items() if k not in ('heartRateZones', 'activityLevel')}, limits)
    event = health.session_event(
        source_id=source_id, artifact_id=artifact_id, external_id=f'{PARSER_NAME}:exercise:{ident}', kind='workout',
        activity_raw=log.get('activityName'), start=start, end=end, provider=PROVIDER, source_name=PROVIDER,
        signal=health.session_signal(PROVIDER, PROVIDER, cls=signal_spec['class'], basis=signal_spec['basis'],
                                     log_type=log_type),
        metadata={'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                  'locator': {'artifact_id': artifact_id, 'index': index, 'log_id': log.get('logId')},
                  'session': {'activity_type_id': log.get('activityTypeId'), 'declared_duration_ms': log.get('duration'),
                              'end_basis': 'start_plus_declared_duration' if end else None,
                              'heart_rate_zones': log.get('heartRateZones'), 'activity_level': log.get('activityLevel')},
                  'raw_log': raw, 'raw_truncated_values': truncated, 'issues': problems})
    health.write_session(event, [c for c in (start, end) if c is not None], samples)
    counts['sessions'] += 1
    counts['samples'] += len(samples)

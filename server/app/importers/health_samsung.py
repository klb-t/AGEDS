"""Samsung Health personal-data CSV → samples and ``health_session`` events. Opt-in (``enable_health``).

Each export table is one CSV: the first line names the table
(``com.samsung.shealth.sleep,…``, kept raw), the second is the header (column
names prefixed with the data type, e.g. ``com.samsung.health.sleep.start_time``;
the last dotted part is used), rows may end with an extra empty cell. The
example mapping (``data/health_samsung.v1.json``) says how each table is read.
Its ``time_basis`` is explicit: ``utc_wall_clock_with_offset_column`` reads the
clock text as UTC and ``time_offset`` (``UTC+0100``) as the device's local
offset; ``local_wall_clock_with_offset_column`` reads the text as local time.
Tables the mapping does not name are acquired but not interpreted (reported).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..ledger import health, store, textio, timeparse
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, bound_json, epoch_candidate, load_data,
                          offset_candidate, record_import_run, stored_path)

PARSER_NAME = 'samsung_health_csv'
PARSER_VERSION = '1'
PROVIDER = 'samsung_health'
TIME_BASES = ('utc_wall_clock_with_offset_column', 'local_wall_clock_with_offset_column')


def import_samsung_health(path: Path, *, enable_health: bool = False, mapping_path: Path | None = None,
                          source_locator: str | None = None, original_name: str | None = None,
                          collection_id: int | None = None, label: str | None = None,
                          limits: CommLimits = DEFAULT_LIMITS) -> dict:
    health.require_opt_in(enable_health)
    if mapping_path is None:
        mapping = load_data('health_samsung.v1.json')
    else:
        mapping = json.loads(Path(mapping_path).read_text(encoding='utf-8'))
    if mapping.get('time_basis') not in TIME_BASES:
        raise ValueError(f'mapping time_basis must be one of {TIME_BASES}')
    locator = source_locator if source_locator is not None else str(path)
    mapping_label = f"{mapping.get('schema', 'private')}/v{mapping.get('version', 'unknown')}"
    options = {'enable_health': True, 'mapping': mapping_label, 'time_basis': mapping['time_basis']}
    source_id, artifact_id = acquire(
        path, source_kind='health_export', source_label=label or 'Samsung Health export', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name, collection_id=collection_id,
        options=options)
    raw = stored_path(artifact_id).read_bytes()
    if len(raw) > limits.max_json_bytes:
        raise ValueError(f'file larger than {limits.max_json_bytes} bytes')
    issues = Issues(limits.max_issues)
    text, encoding = textio.decode(raw)
    lines = textio.physical_lines(text)
    parsed = textio.parse_records(lines, start=0, delimiter=',', max_records=limits.max_records + 2,
                                  max_raw_chars=limits.max_raw_chars)
    if parsed['error']:
        issues.add('csv_record_parse_failed', **parsed['error'])
    records = parsed['records']
    if len(records) < 2 or not re.fullmatch(r'com\.samsung\.[\w.]+', (records[0]['values'] or [''])[0].strip()):
        raise ValueError('not a Samsung Health table export (first line must name a com.samsung.* table)')
    table = records[0]['values'][0].strip()
    full_header = [h.strip() for h in records[1]['values']]
    header, collisions = [], []
    for name in full_header:
        short = name.rsplit('.', 1)[-1] if name.startswith('com.samsung.') else name
        if short in header and short:
            collisions.append(name)
            short = name
        header.append(short)
    spec = mapping['tables'].get(table)
    reading = {'table': table, 'mapping': mapping_label, 'time_basis': mapping['time_basis']}
    reading_key = store.sha256_of(reading)[:12]
    counts = {'table': table, 'rows': 0, 'sessions': 0, 'samples': 0, 'table_metadata_line': records[0]['raw'],
              'header_collisions': collisions}
    if spec is None:
        issues.add('samsung_table_not_mapped', table=table)
        counts['rows'] = sum(1 for r in records[2:] if not textio.blank(r['values']))
        result = counts
    else:
        result = _rows(records[2:limits.max_records + 2], header, spec, mapping, table, reading_key, artifact_id,
                       source_id, issues, counts, limits)
    coverage = {'complete': spec is not None and len(records) - 2 <= limits.max_records and not parsed['error']}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts={k: v for k, v in result.items() if isinstance(v, int)}, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION,
            'reading_key': reading_key, **result, 'coverage': coverage, 'issues': issues.as_dict(),
            'opt_in': health.opt_in_record()}


def _value(row: dict, name: str | None) -> str | None:
    if not name:
        return None
    value = row.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _time(kind, row, column, spec, mapping, source, problems):
    text = _value(row, spec.get(column))
    if text is None:
        return None
    offset_raw = _value(row, spec.get('offset'))
    if mapping['time_basis'] == 'utc_wall_clock_with_offset_column':
        candidate, problem = health.utc_wall_clock_candidate(kind, text, source, mapping['time_formats'], offset_raw)
    else:
        offset = health.parse_offset_label(offset_raw)
        candidate, problem = health.time_candidate(kind, text, source, mapping['time_formats'])
        if candidate is not None and offset is not None:
            parsed = timeparse.parse(text, mapping['time_formats'])
            candidate = offset_candidate(kind, text, source, parsed['naive'], offset, parsed['precision'],
                                         {'time_basis': mapping['time_basis'], 'offset_raw': offset_raw})
    if problem:
        problems.append(problem)
    return candidate


def _rows(records, header, spec, mapping, table, reading_key, artifact_id, source_id, issues, counts, limits) -> dict:
    writer = health.SampleWriter()
    kind = spec['kind']
    for record in records:
        values = record['values']
        if textio.blank(values):
            continue
        counts['rows'] += 1
        if len(values) > len(header) and all(not v.strip() for v in values[len(header):]):
            values = values[:len(header)]  # trailing separator
        row = dict(zip(header, values))
        problems: list[str] = []
        ident = _value(row, spec.get('id'))
        position = f"row:{record['index']}"
        source = f'{PROVIDER}:{table}:{position}'
        device = _value(row, spec.get('device'))
        if kind == 'daily_samples':
            epoch = _value(row, spec.get('day_epoch_ms'))
            start = None
            if epoch is not None:
                try:
                    start = epoch_candidate('start', epoch, f'{source}:day', unit='ms',
                                            metadata={'date_kind': 'day_start_epoch'})
                except ValueError:
                    problems.append('day_epoch_unparsed')
            for name in spec.get('values', []):
                value = _value(row, name)
                if value is not None:
                    writer.add(health.sample_row(
                        artifact_id=artifact_id, parser=PARSER_NAME, parser_version=PARSER_VERSION,
                        position=f'{reading_key}:{position}/{name}', sample_type=f'{table}.{name}',
                        sample_class='daily_summary', value_raw=value, unit=None, start=start, end=None,
                        source_name=PROVIDER, source_version=None, device_raw=device, basis='provider_inference',
                        metadata={'datauuid': ident, 'issues': problems}))
                    counts['samples'] += 1
            continue
        start = _time('start', row, 'start', spec, mapping, f'{source}:start', problems)
        end = _time('end', row, 'end', spec, mapping, f'{source}:end', problems)
        span = health.duration(start, end)
        if span['status'] == 'negative':
            problems.append('end_before_start')
        for problem in problems:
            issues.add(problem, position=position)
        extra_values = {name: _value(row, name) for name in spec.get('values', []) if _value(row, name) is not None}
        if kind == 'samples':
            for name, value in extra_values.items():
                writer.add(health.sample_row(
                    artifact_id=artifact_id, parser=PARSER_NAME, parser_version=PARSER_VERSION,
                    position=f'{reading_key}:{position}/{name}', sample_type=f'{table}.{name}', sample_class='quantity',
                    value_raw=value, unit=None, start=start, end=end, source_name=PROVIDER, source_version=None,
                    device_raw=device, basis='raw_sample', metadata={'datauuid': ident, 'issues': problems}))
                counts['samples'] += 1
            continue
        session_kind = 'sleep' if kind == 'sleep_session' else 'workout'
        activity = _value(row, spec.get('type'))
        labels = mapping.get('exercise_type_labels_example', {})
        samples = [health.sample_row(
            artifact_id=artifact_id, parser=PARSER_NAME, parser_version=PARSER_VERSION,
            position=f'{reading_key}:{position}/{name}', sample_type=f'{table}.{name}', sample_class='session_summary',
            value_raw=value, unit=None, start=start, end=end, source_name=PROVIDER, source_version=None,
            device_raw=device, basis='provider_inference', metadata={'datauuid': ident}) for name, value in extra_values.items()]
        raw_row, truncated = bound_json(row, limits)
        declared_ms = _value(row, spec.get('declared_duration_ms'))
        event = health.session_event(
            source_id=source_id, artifact_id=artifact_id,
            external_id=f'{PARSER_NAME}:{reading_key}:{table}:{position}', kind=session_kind,
            activity_raw=activity, start=start, end=end, provider=PROVIDER, source_name=device,
            signal=health.session_signal(PROVIDER, device, cls='passive' if session_kind == 'sleep' else 'active'),
            metadata={'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION}, 'reading_key': reading_key,
                      'locator': {'artifact_id': artifact_id, 'table': table, 'row': record['index'],
                                  'line': record['start_line']},
                      'session': {'datauuid': ident, 'declared_duration_ms': declared_ms,
                                  'activity_label_example': labels.get(activity) if activity else None},
                      'raw_row': raw_row, 'raw_truncated_values': truncated, 'issues': problems})
        health.write_session(event, [c for c in (start, end) if c is not None], samples)
        counts['sessions'] += 1
        counts['samples'] += len(samples)
    writer.flush()
    return counts


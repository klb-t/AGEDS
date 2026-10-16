"""Operator billing tables (CSV/TSV/XLSX/XLS) → ``call`` (or mapped) events.

Tables are read with the read-only scanner's bounded readers (dialect
detection, raw CSV records, XLSX formula text without evaluation). Columns are
mapped by a *profile* from ``data/billing_profiles.v1.json`` (examples only) or
a private profile file. Day/month order is a profile policy: ``day_first``,
``month_first`` or ``infer_from_file`` (one unambiguous row decides only if the
whole file agrees; otherwise both readings are recorded). Billing times have
an unknown zone unless the profile names an ``assumed_zone``, recorded as an
extra ``assumed_zone`` candidate, never as a replacement.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from ..scanner import ScanLimits, _Scan
from ..timeinfo import TimeCandidate, assume_zone
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, add_event_with_times, bound_json,
                          legacy_instant, load_data, local_candidate, record_import_run, stored_path)

PARSER_NAME = 'billing_table'
PARSER_VERSION = '1'
_DATE = re.compile(r'^\s*(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})\s*$')
_TIME = re.compile(r'^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,](\d{1,6}))?\s*([AaPp]\.?[Mm]\.?)?\s*$')
_DATETIME_SPLIT = re.compile(r'^\s*(\S+)[ T]+(.+?)\s*$')


def _norm(value) -> str:
    text = unicodedata.normalize('NFKC', str(value or '')).strip().lower()
    return re.sub(r'\s+', ' ', text)


def load_profiles(path: Path | None = None) -> dict:
    if path is None:
        return load_data('billing_profiles.v1.json')
    with Path(path).open('r', encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data.get('profiles'), list):
        raise ValueError('profile file must contain a "profiles" list')
    return data


def _resolve_columns(profile: dict, header: list) -> dict[str, int]:
    names = [_norm(h) for h in header]
    resolved = {}
    for field, aliases in profile['columns'].items():
        wanted = {_norm(a) for a in aliases}
        for index, name in enumerate(names):
            if name in wanted:
                resolved[field] = index
                break
    return resolved


def choose_profile(profiles: dict, rows: list[dict], profile_id: str | None) -> tuple[dict, int, dict, str]:
    """Return (profile, header_row_position, column_map, selection_method)."""
    candidates = [p for p in profiles['profiles'] if profile_id is None or p['id'] == profile_id]
    if profile_id is not None and not candidates:
        raise ValueError(f'billing profile not found: {profile_id}')
    matches = []
    for position, row in enumerate(rows[:20]):
        values = [cell['value'] for cell in row['cells']]
        for profile in candidates:
            columns = _resolve_columns(profile, values)
            if all(field in columns for field in profile['required']):
                matches.append((profile, position, columns))
        if matches:
            break
    if not matches:
        raise ValueError('no billing profile matches the table header within the first 20 rows')
    if len(matches) > 1:
        raise ValueError('several billing profiles match; choose one explicitly: ' + ', '.join(m[0]['id'] for m in matches))
    profile, position, columns = matches[0]
    return profile, position, columns, 'explicit' if profile_id else 'unique_header_match'


def _cell(row: dict, column: int | None):
    if column is None:
        return None
    for cell in row['cells']:
        if cell['column'] == column + 1:
            return cell
    return None


def _date_parts(text: str):
    match = _DATE.match(text)
    if not match:
        return None
    a, b, c = match.groups()
    if len(a) == 4:
        return {'year': int(a), 'month': int(b), 'day': int(c), 'order': 'year_first', 'ambiguous': False}
    if len(c) not in (2, 4):
        return None
    first, second = int(a), int(b)
    return {'first': first, 'second': second, 'year': int(c), 'year_digits': len(c),
            'ambiguous': first <= 12 and second <= 12 and first != second}


def _time_parts(text: str):
    match = _TIME.match(text)
    if not match:
        return None
    hour, minute, second, fraction, ampm = match.groups()
    hour = int(hour)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm[0].lower() == 'p' else 0)
    micro = int((fraction + '000000')[:6]) if fraction else 0
    return hour, int(minute), int(second or 0), micro, second is not None, ampm


def infer_date_order(values: list[str]) -> dict:
    """File-level evidence for day/month order from rows that are not ambiguous."""
    day_first = month_first = 0
    for text in values:
        parts = _date_parts(text)
        if not parts or parts.get('order') == 'year_first':
            continue
        if parts['first'] > 12 >= parts['second']:
            day_first += 1
        elif parts['second'] > 12 >= parts['first']:
            month_first += 1
    if day_first and not month_first:
        decision = 'day_first'
    elif month_first and not day_first:
        decision = 'month_first'
    else:
        decision = 'conflict' if day_first and month_first else 'undetermined'
    return {'decision': decision, 'day_first_evidence_rows': day_first, 'month_first_evidence_rows': month_first}


def parse_duration(cell, fmt: str) -> tuple[float | None, dict]:
    if cell is None or cell['value'] in (None, ''):
        return None, {'status': 'missing'}
    value, kind = cell['value'], cell.get('type')
    meta = {'raw': value, 'cell_type': kind, 'format_policy': fmt}
    if kind in ('time',) and isinstance(value, str):
        h, m, s = (value.split(':') + ['0', '0'])[:3]
        meta['interpretation'] = 'typed_time_cell_as_hh_mm_ss'
        return int(h) * 3600 + int(m) * 60 + float(s), meta
    if kind in ('int', 'float') and fmt in ('seconds', 'auto'):
        meta['interpretation'] = 'numeric_seconds' if fmt == 'seconds' else 'numeric_assumed_seconds'
        return float(value), meta
    text = str(value).strip()
    colon = text.count(':')
    if re.fullmatch(r'\d+(?:[.,]\d+)?', text) and fmt in ('seconds', 'auto'):
        meta['interpretation'] = 'seconds' if fmt == 'seconds' else 'digits_assumed_seconds'
        return float(text.replace(',', '.')), meta
    if colon == 2 and fmt in ('hh:mm:ss', 'auto', 'mm:ss'):
        h, m, s = text.split(':')
        if fmt == 'mm:ss':
            meta['format_mismatch'] = 'three_fields_under_mm_ss_profile_read_as_hh_mm_ss'
        if re.fullmatch(r'\d+', h) and re.fullmatch(r'\d{2}', m) and re.fullmatch(r'\d{2}', s) and int(m) < 60 and int(s) < 60:
            meta['interpretation'] = 'hh:mm:ss'
            return int(h) * 3600 + int(m) * 60 + int(s), meta
    if colon == 1 and fmt in ('mm:ss', 'auto', 'hh:mm:ss'):
        m, s = text.split(':')
        if re.fullmatch(r'\d+', m) and re.fullmatch(r'\d{2}', s) and int(s) < 60:
            if fmt == 'hh:mm:ss':
                meta['format_mismatch'] = 'two_fields_under_hh_mm_ss_profile_read_as_mm_ss'
            meta['interpretation'] = 'mm:ss' if fmt == 'mm:ss' else 'two_fields_assumed_mm_ss'
            return int(m) * 60 + int(s), meta
    meta['status'] = 'unparsed'
    return None, meta


def _start_candidates(row, columns, profile, order_policy, source, problems) -> tuple[list[TimeCandidate], dict]:
    """Start candidates for one row: typed cell, explicit order, or both readings."""
    meta: dict = {}
    if 'datetime' in columns:
        cell = _cell(row, columns['datetime'])
        if cell is None or cell['value'] in (None, ''):
            problems.append('start_missing')
            return [], meta
        meta['raw'] = cell['value']
        if cell.get('type') == 'datetime':
            naive = datetime.fromisoformat(cell['value'])
            meta['interpretation'] = 'typed_spreadsheet_datetime'
            return [local_candidate('start', cell['value'], source, naive, 'second', {'cell': cell.get('coordinate')})], meta
        split = _DATETIME_SPLIT.match(str(cell['value']))
        date_text, time_text = (split.group(1), split.group(2)) if split else (str(cell['value']), None)
    else:
        date_cell, time_cell = _cell(row, columns.get('date')), _cell(row, columns.get('time'))
        if date_cell is None or date_cell['value'] in (None, ''):
            problems.append('start_missing')
            return [], meta
        meta['raw'] = {'date': date_cell['value'], 'time': time_cell['value'] if time_cell else None}
        if date_cell.get('type') in ('datetime', 'date'):
            date_text = date_cell['value'][:10]
            meta['date_interpretation'] = 'typed_spreadsheet_date'
        else:
            date_text = str(date_cell['value'])
        time_text = str(time_cell['value']) if time_cell and time_cell['value'] not in (None, '') else None
        if time_cell is not None and time_cell.get('type') == 'datetime' and time_text:
            time_text = time_text[11:]
    raw = date_text if time_text is None else f'{date_text} {time_text}'
    clock = (0, 0, 0, 0, False, None)
    if time_text is not None:
        parsed_time = _time_parts(time_text)
        if parsed_time is None:
            problems.append('time_unparsed')
            return [], meta
        clock = parsed_time
    precision = 'day' if time_text is None else ('microsecond' if clock[3] else 'second' if clock[4] else 'minute')
    readings = []
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_text):
        readings.append(('iso', int(date_text[:4]), int(date_text[5:7]), int(date_text[8:10])))
    else:
        parts = _date_parts(date_text)
        if parts is None:
            problems.append('date_unparsed')
            return [], meta
        if parts.get('order') == 'year_first':
            readings.append(('year_first', parts['year'], parts['month'], parts['day']))
        else:
            if parts['year_digits'] == 2:
                problems.append('two_digit_year_century_not_assumed')
                meta['two_digit_year'] = True
                return [], meta
            day_first = ('day_first', parts['year'], parts['second'], parts['first'])
            month_first = ('month_first', parts['year'], parts['first'], parts['second'])
            if not parts['ambiguous']:
                if parts['first'] == parts['second']:
                    readings.append(day_first)
                elif parts['first'] > 12:
                    readings.append(day_first)
                    if order_policy == 'month_first':
                        problems.append('row_contradicts_month_first_policy')
                else:
                    readings.append(month_first)
                    if order_policy == 'day_first':
                        problems.append('row_contradicts_day_first_policy')
            elif order_policy in ('day_first', 'month_first'):
                readings.append(day_first if order_policy == 'day_first' else month_first)
                meta['date_order_ambiguous_resolved_by'] = 'profile_or_file_policy'
            else:
                readings.extend([day_first, month_first])
                problems.append('date_order_ambiguous_both_readings_recorded')
    candidates = []
    for label, year, month, day in readings:
        try:
            naive = datetime(year, month, day, clock[0], clock[1], clock[2], clock[3])
        except ValueError:
            problems.append(f'invalid_calendar_value_{label}')
            continue
        suffix = f':{label}' if len(readings) > 1 else ''
        candidate = local_candidate('start', raw, source + suffix, naive, precision,
                                    {'date_order': label, 'ampm': clock[5],
                                     'zone_status': 'billing_time_zone_not_stated'})
        candidates.append(candidate)
        if profile.get('assumed_zone'):
            candidates.append(assume_zone(TimeCandidate('start', raw, source + suffix + ':assumed_zone', local=candidate.local,
                                                        precision=precision, metadata=candidate.metadata),
                                          profile['assumed_zone'], rule=f"billing_profile:{profile['id']}"))
    return candidates, meta


def _read_table(raw: bytes, name: str, extension: str, artifact_id: int, scan_limits: ScanLimits,
                delimiter: str | None = None):
    """Read with the scanner; an optional separator hint uses its ``sep=`` support."""
    data, line_offset, delimiter_source = raw, 0, 'scanner_dialect_detection'
    if delimiter is not None:
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            return _read_table(raw, name, extension, artifact_id, scan_limits)
        bom = b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b''
        data = bom + b'sep=' + delimiter.encode('ascii') + b'\n' + raw[len(bom):]
        line_offset, delimiter_source = 1, f'header_match_retry:{delimiter!r}'
    reader = _Scan(Path('.'), scan_limits)
    file = {'file_id': f'artifact:{artifact_id}', 'relative_path': name, 'extension': extension}
    reader.parse_table(file, data)
    return reader, file, line_offset, delimiter_source


def _any_profile_matches(profiles: dict, reader, file: dict, profile_id: str | None) -> bool:
    for table in reader.manifest['tables']:
        if table['file_id'] == file['file_id'] and table['rows']:
            try:
                choose_profile(profiles, table['rows'], profile_id)
                return True
            except ValueError:
                continue
    return False


def import_billing(path: Path, *, profile_id: str | None = None, profiles_path: Path | None = None,
                   source_locator: str | None = None, original_name: str | None = None,
                   collection_id: int | None = None, label: str | None = None,
                   limits: CommLimits = DEFAULT_LIMITS) -> dict:
    profiles = load_profiles(profiles_path)
    name = original_name or path.name
    extension = Path(name).suffix.lower()
    if extension not in ('.csv', '.tsv', '.xlsx', '.xls'):
        raise ValueError('billing import supports .csv, .tsv, .xlsx and .xls')
    locator = source_locator if source_locator is not None else str(path)
    profile_set = f"{profiles.get('schema', 'private')}/v{profiles.get('version', 'unknown')}"
    source_id, artifact_id = acquire(
        path, source_kind='operator_billing', source_label=label or 'Operator billing', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name,
        collection_id=collection_id, options={'profile_id': profile_id, 'profile_set': profile_set})
    issues = Issues(limits.max_issues)
    snapshot = stored_path(artifact_id)
    scan_limits = ScanLimits(max_rows_per_file=limits.max_records, max_total_rows=limits.max_records,
                             max_total_cell_characters=max(2_000_000, limits.max_records * 512),
                             max_table_bytes=max(16 * 1024 * 1024, limits.max_json_bytes))
    raw_bytes = snapshot.read_bytes()
    reader, file, line_offset, delimiter_source = _read_table(raw_bytes, name, extension, artifact_id, scan_limits)
    if extension in ('.csv', '.tsv') and not _any_profile_matches(profiles, reader, file, profile_id):
        # The scanner sniffs the dialect from the whole text; a preamble (operator
        # name, period) can mislead it. A header match under an explicit
        # separator is stronger evidence; the hint is in-memory only.
        for delimiter in [p.get('delimiter') for p in profiles['profiles'] if p.get('delimiter')] + [';', ',', '\t', '|']:
            retry = _read_table(raw_bytes, name, extension, artifact_id, scan_limits, delimiter=delimiter)
            if _any_profile_matches(profiles, retry[0], retry[1], profile_id):
                reader, file, line_offset, delimiter_source = retry
                break
    for issue in reader.manifest['issues']:
        if issue['code'] == 'row_width_mismatch':
            continue  # relative to the scanner's first-row header guess; re-checked below
        issues.add(f"table_{issue['code']}", **{k: v for k, v in issue.items() if k not in ('code', 'path')})
    if file['parse_status'] not in ('complete', 'limited'):
        raise ValueError(f"billing table could not be read: {file['parse_status']}")
    tables = [t for t in reader.manifest['tables'] if t['file_id'] == file['file_id']]
    for table in tables:
        for row in table['rows']:
            if row.get('start_line') is not None:
                row['start_line'] -= line_offset
    counts = {'rows': 0, 'events': 0, 'skipped_service': 0, 'ambiguous_dates': 0, 'unparsed_durations': 0}
    selections = []
    complete = all(t['complete'] for t in tables) and file['parse_status'] == 'complete'
    for table in tables:
        if not table['rows']:
            continue
        try:
            profile, header_position, columns, method = choose_profile(profiles, table['rows'], profile_id)
        except ValueError as exc:
            issues.add('table_without_matching_profile', table=table['name'], error=str(exc))
            complete = False
            continue
        data_rows = table['rows'][header_position + 1:]
        header_width = len(table['rows'][header_position]['cells'])
        order_policy = profile.get('date_order', 'undetermined')
        inference = None
        if order_policy == 'infer_from_file':
            date_column = columns.get('datetime', columns.get('date'))
            texts = []
            for row in data_rows:
                cell = _cell(row, date_column)
                if cell and cell.get('type') == 'str':
                    split = _DATETIME_SPLIT.match(cell['value'])
                    texts.append(split.group(1) if split and 'datetime' in columns else cell['value'])
            inference = infer_date_order(texts)
            order_policy = inference['decision']
        selections.append({'table': table['name'], 'profile_id': profile['id'], 'selection': method,
                           'delimiter_source': delimiter_source,
                           'header_row': table['rows'][header_position]['row'], 'columns': columns,
                           'date_order_policy': profile.get('date_order'), 'date_order_applied': order_policy,
                           'date_order_inference': inference})
        source = f"billing:{profile['id']}"
        for row in data_rows:
            counts['rows'] += 1
            problems: list[str] = []
            if len(row['cells']) != header_width:
                problems.append('row_width_differs_from_profile_header')
            service_cell = _cell(row, columns.get('service'))
            event_type = 'call'
            service_raw = service_cell['value'] if service_cell else None
            if service_cell is not None and service_raw not in (None, ''):
                mapped = {_norm(k): v for k, v in profile.get('service_map', {}).items()}
                key = _norm(service_raw)
                if key in mapped and mapped[key] is None:
                    counts['skipped_service'] += 1
                    continue
                event_type = mapped.get(key) or 'billing_record'
                if key not in mapped:
                    problems.append('service_value_unmapped')
            candidates, time_meta = _start_candidates(row, columns, profile, order_policy, source, problems)
            if 'date_order_ambiguous_both_readings_recorded' in problems:
                counts['ambiguous_dates'] += 1
            duration, duration_meta = parse_duration(_cell(row, columns.get('duration')), profile.get('duration_format', 'auto'))
            if duration is None:
                problems.append('duration_unparsed')
                counts['unparsed_durations'] += 1
            elif duration_meta.get('format_mismatch'):
                problems.append(duration_meta['format_mismatch'])
            ends = []
            for start in candidates:
                if duration is None:
                    continue
                if start.tz_status == 'unknown':
                    end_local = datetime.fromisoformat(start.local) + timedelta(seconds=duration)
                    ends.append(local_candidate('end', str(duration_meta.get('raw')), start.source, end_local,
                                                start.precision, {**start.metadata, 'projection': 'start_plus_duration',
                                                                  'duration_seconds': duration}))
            direction_cell = _cell(row, columns.get('direction'))
            direction_raw = direction_cell['value'] if direction_cell else None
            direction = None
            if direction_raw not in (None, ''):
                direction = {_norm(k): v for k, v in profile.get('direction_map', {}).items()}.get(_norm(direction_raw))
                if direction is None:
                    problems.append('direction_value_unmapped')
            number_cell = _cell(row, columns.get('number'))
            number = None
            if number_cell is not None and number_cell['value'] not in (None, ''):
                number = str(number_cell['value'])
                if number_cell.get('type') in ('int', 'float'):
                    problems.append('numeric_number_cell_may_have_lost_leading_zero_or_plus')
            cost_cell = _cell(row, columns.get('cost'))
            raw_cells, truncated = bound_json(row['cells'], limits)
            metadata = {
                'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                'profile': {'id': profile['id'], 'set': profile_set, 'selection': method},
                'locator': {'artifact_id': artifact_id, 'table': table['name'], 'row': row['row'],
                            'start_line': row.get('start_line')},
                'raw_cells': raw_cells, 'raw_record': row.get('raw_record'), 'raw_truncated_values': truncated,
                'start': time_meta, 'date_order_applied': order_policy,
                'duration': {**duration_meta, 'seconds': duration},
                'direction': {'raw': direction_raw, 'label': direction},
                'service': {'raw': service_raw}, 'cost': {'raw': cost_cell['value'] if cost_cell else None},
                'number_semantics': 'as_listed_by_operator_export',
                'issues': problems,
            }
            primary = candidates[0] if len(candidates) == 1 else None
            add_event_with_times(
                candidates + ends, source_id=source_id, artifact_id=artifact_id, event_type=event_type,
                ts_start=legacy_instant(primary), direction=direction or (str(direction_raw) if direction_raw else None),
                phone_or_address=number,
                external_id=f"{PARSER_NAME}:{profile['id']}:{table['name']}:row:{row['row']}", confidence=None,
                metadata=metadata)
            for problem in problems:
                issues.add(problem, table=table['name'], row=row['row'])
            counts['events'] += 1
    coverage = {'complete': complete}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                      options={'profile_id': profile_id, 'profile_set': profile_set}, counts=counts,
                      coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser_version': PARSER_VERSION, **counts,
            'profiles': selections, 'coverage': coverage, 'issues': issues.as_dict()}

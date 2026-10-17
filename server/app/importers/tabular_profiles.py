"""Generic tabular profiles: bank statements, transit card histories, ride-hailing trips.

A *profile* (data, versioned: ``data/tabular_profiles.v1.json`` — examples only —
or a private file) maps a table to records: header aliases or 1-based column
positions, preamble search, encoding (explicit or a recorded hypothesis, see
``ledger.textio``), delimiter, number format (decimal/thousands separators,
negative notations), amount mode (signed, debit/credit columns, D/C indicator),
currency, and named *time points* (``booking_date``, ``value_date``,
``transaction_date``, ``check_in``, ``check_out``, ``tap``, ``request``,
``start``, ``end``) with explicit formats or a day/month order policy.

Semantics kept from the operator-billing profiles (``billing.py``, which now
imports the shared date helpers below unchanged): ``day_first``,
``month_first`` or ``infer_from_file`` (only unambiguous rows count; an
undecidable row keeps both readings as separate candidates); two-digit years
get no century unless the profile names ``two_digit_year_base``; an
``assumed_zone`` adds a separate ``assumed_zone`` candidate, never a
replacement. Times without an offset stay ``unknown`` zone.

Amounts are exact decimal strings plus currency plus integer minor units
computed by code (``ledger.money``); floats never enter the computation. Every
source row is one record at a deterministic position: identical rows at
different positions are different records (both kept); re-importing the same
file under the same reading reuses every row.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from ..db import session
from ..ledger import money, store, textio, timeparse
from ..timeinfo import TimeCandidate, assume_zone
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, bound_json, legacy_instant, load_data,
                          local_candidate, offset_candidate, record_import_run, stored_path)

PARSER_NAME = 'ledger_tabular'
PARSER_VERSION = '1'
PROFILE_SCHEMA = 'ageds.tabular-profiles'
RECORD_KINDS = ('financial_transaction', 'travel_segment', 'travel_taps', 'ride')
# Profile time kinds -> timeinfo kinds (the timeinfo kind list is closed; the
# profile kind is kept as ``metadata.date_kind`` on every candidate).
DATE_KIND_TO_TIME_KIND = {
    'transaction_date': 'start', 'booking_date': 'created', 'value_date': 'other', 'check_in': 'start',
    'check_out': 'end', 'tap': 'start', 'request': 'created', 'start': 'start', 'end': 'end',
}
SOURCE_KINDS = {'financial_transaction': 'bank_export', 'travel_segment': 'transit_history',
                'travel_taps': 'transit_history', 'ride': 'ride_history'}
TAP_PAIRING_RULE = 'ledger.tap_pairing.v1'
BATCH = 500

# ---------------------------------------------------------------------------
# Shared profile mechanics (moved unchanged from billing.py, its version 1).

_DATE = re.compile(r'^\s*(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})\s*$')
_TIME = re.compile(r'^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,](\d{1,6}))?\s*([AaPp]\.?[Mm]\.?)?\s*$')
_DATETIME_SPLIT = re.compile(r'^\s*(\S+)[ T]+(.+?)\s*$')


def _norm(value) -> str:
    text = unicodedata.normalize('NFKC', str(value or '')).strip().lower()
    return re.sub(r'\s+', ' ', text)


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


# ---------------------------------------------------------------------------
# Profiles

def load_profiles(path: Path | None = None) -> dict:
    if path is None:
        data = load_data('tabular_profiles.v1.json')
    else:
        with Path(path).open('r', encoding='utf-8') as handle:
            data = json.load(handle)
    profiles = data.get('profiles')
    if not isinstance(profiles, list):
        raise ValueError('profile file must contain a "profiles" list')
    seen = set()
    for profile in profiles:
        pid = profile.get('id')
        if not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', pid) or pid in seen:
            raise ValueError(f'invalid or duplicate profile id: {pid!r}')
        seen.add(pid)
        if profile.get('record_kind') not in RECORD_KINDS:
            raise ValueError(f'profile {pid}: record_kind must be one of {RECORD_KINDS}')
        if not isinstance(profile.get('columns'), dict) or not isinstance(profile.get('times', {}), dict):
            raise ValueError(f'profile {pid}: columns and times must be objects')
        money.NumberFormat.from_spec(profile.get('number'))
        specs = list(profile.get('times', {}).items())
        for extra in profile.get('times_by_row_kind', {}).values():
            specs.extend(extra.items())
        for name, spec in specs:
            if spec.get('kind') not in DATE_KIND_TO_TIME_KIND:
                raise ValueError(f'profile {pid}: time point {name} has an unknown kind')
            for fmt in spec.get('formats', []) + spec.get('date_formats', []) + spec.get('time_formats', []):
                timeparse.compile_format(fmt)
    return data


def profile_set_label(profiles: dict) -> str:
    return f"{profiles.get('schema', 'private')}/v{profiles.get('version', 'unknown')}"


def _header_names(profile: dict) -> dict[str, set]:
    out = {}
    for field, spec in profile['columns'].items():
        if isinstance(spec, list):
            out[field] = {_norm(a) for a in spec}
    return out


def resolve_profile_columns(profile: dict, header: list | None) -> dict[str, int]:
    """Field -> 0-based column index from aliases (header row) or 1-based ``{"column": n}``."""
    resolved = {}
    names = [_norm(h) for h in header] if header is not None else None
    for field, spec in profile['columns'].items():
        if isinstance(spec, dict) and 'column' in spec:
            resolved[field] = int(spec['column']) - 1
        elif names is not None:
            wanted = {_norm(a) for a in spec}
            for index, name in enumerate(names):
                if name in wanted:
                    resolved[field] = index
                    break
    return resolved


# ---------------------------------------------------------------------------
# Reading

def _text_tables(raw: bytes, profiles: list[dict], overrides: dict, limits: CommLimits) -> tuple[list, dict]:
    """Try each candidate profile's reading on the text; return matches (profile, reading, records, header)."""
    decoded: dict = {}
    matches = []
    attempts = []
    for profile in profiles:
        read = profile.get('read', {})
        encoding = overrides.get('encoding') or read.get('encoding', 'auto')
        preference = tuple(read.get('encoding_preference', textio.SINGLE_BYTE))
        key = (encoding, preference)
        if key not in decoded:
            try:
                decoded[key] = textio.decode(raw, encoding, preference)
            except ValueError as exc:
                decoded[key] = exc
        if isinstance(decoded[key], ValueError):
            attempts.append({'profile_id': profile['id'], 'error': str(decoded[key])})
            continue
        text, encoding_info = decoded[key]
        lines = textio.physical_lines(text)
        requested = overrides.get('delimiter') or read.get('delimiter', 'auto')
        if requested == 'auto':
            guess, guess_details = textio.guess_delimiter(lines)
            delimiters = [guess] + [d for d in textio.DELIMITERS if d != guess]
        else:
            delimiters, guess_details = [requested], None
        search = int(read.get('header_search_rows', 30))
        header_mode = read.get('header', 'names')
        for delimiter in delimiters:
            parsed = textio.parse_records(lines, start=0, delimiter=delimiter, quotechar=read.get('quotechar', '"'),
                                          max_records=search if header_mode == 'names' else 1,
                                          max_raw_chars=limits.max_raw_chars)
            header_index = None
            if header_mode == 'none':
                header_index = int(read.get('skip_lines', 0)) + 1  # index of the first data record
                columns = resolve_profile_columns(profile, None)
            else:
                for record in parsed['records']:
                    columns = resolve_profile_columns(profile, record['values'])
                    if all(f in columns for f in profile.get('required', [])):
                        header_index = record['index']
                        break
            if header_index is None:
                continue
            matches.append({'profile': profile, 'text': text, 'lines': lines, 'encoding': encoding_info,
                            'delimiter': delimiter,
                            'delimiter_basis': 'explicit' if requested != 'auto' else 'header_match_under_hypothesis',
                            'delimiter_guess': guess_details, 'header_index': header_index, 'header_mode': header_mode})
            break
    return matches, {'attempts': attempts}


def _sheet_tables(raw: bytes, name: str, extension: str, artifact_id: int, limits: CommLimits) -> list[dict]:
    from ..scanner import ScanLimits, _Scan
    scan_limits = ScanLimits(max_rows_per_file=limits.max_records, max_total_rows=limits.max_records,
                             max_total_cell_characters=max(2_000_000, limits.max_records * 512),
                             max_table_bytes=max(16 * 1024 * 1024, len(raw) + 1))
    reader = _Scan(Path('.'), scan_limits)
    file = {'file_id': f'artifact:{artifact_id}', 'relative_path': name, 'extension': extension}
    reader.parse_table(file, raw)
    if file['parse_status'] not in ('complete', 'limited'):
        raise ValueError(f"spreadsheet could not be read: {file['parse_status']}")
    tables = []
    for table in reader.manifest['tables']:
        records = []
        for row in table['rows']:
            values, types = [], []
            for cell in row['cells']:
                value = cell['value']
                kind = cell.get('type')
                if kind in ('int', 'float'):
                    values.append(repr(value) if kind == 'float' else str(value))
                elif value is None:
                    values.append('')
                else:
                    values.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                types.append(kind)
            records.append({'index': row['row'], 'start_line': None, 'end_line': None, 'values': values,
                            'types': types, 'raw': None, 'raw_complete': False})
        tables.append({'name': table['name'], 'records': records, 'complete': table['complete'],
                       'issues': [i for i in reader.manifest['issues'] if i.get('code') != 'row_width_mismatch']})
    return tables


# ---------------------------------------------------------------------------
# Row values

def _cell(record: dict, columns: dict, field: str | None) -> str | None:
    if field is None or field not in columns:
        return None
    index = columns[field]
    values = record['values']
    if index >= len(values):
        return None
    value = values[index]
    return None if value is None or not str(value).strip() else str(value)


def _cell_type(record: dict, columns: dict, field: str | None) -> str | None:
    if field is None or field not in columns or 'types' not in record:
        return None
    index = columns[field]
    return record['types'][index] if index < len(record['types']) else None


def _readings(spec: dict, record: dict, columns: dict, policy: str | None, base: int | None,
              problems: list[str]) -> tuple[list[dict], dict]:
    """Wall-clock readings of one time point: [{label, naive, precision, offset, meta}]."""
    meta: dict = {}
    if 'datetime' in spec:
        text = _cell(record, columns, spec['datetime'])
        if text is None:
            return [], {'status': 'missing'}
        meta['raw'] = text
        typed = _cell_type(record, columns, spec['datetime'])
        if typed == 'datetime':
            naive = datetime.fromisoformat(text)
            return [{'label': None, 'naive': naive, 'precision': 'second', 'offset': None,
                     'meta': {'interpretation': 'typed_spreadsheet_datetime'}}], meta
        if spec.get('formats'):
            parsed = timeparse.parse(text, spec['formats'], two_digit_year_base=base)
            if parsed['status'] != 'ok':
                problems.append(f"time_{parsed['status']}")
                return [], {**meta, 'status': parsed['status']}
            if parsed['offset_minutes'] not in (None, 0) and re.search(r'\bUTC\b', text):
                problems.append('utc_label_with_nonzero_offset')
                meta['zone_conflict'] = 'text names UTC and a non-zero numeric offset; the numeric offset is used'
            return [{'label': None, 'naive': parsed['naive'], 'precision': parsed['precision'],
                     'offset': parsed['offset_minutes'], 'meta': {'format': parsed['format'],
                                                                  **({'year_rule': parsed['year_rule']} if 'year_rule' in parsed else {})}}], meta
        split = _DATETIME_SPLIT.match(text)
        date_text, time_text = (split.group(1), split.group(2)) if split else (text, None)
    else:
        date_text = _cell(record, columns, spec.get('date'))
        time_text = _cell(record, columns, spec.get('time')) if spec.get('time') else None
        if date_text is None:
            return [], {'status': 'missing' if time_text is None else 'date_missing', 'raw_time': time_text}
        meta['raw'] = {'date': date_text, 'time': time_text} if spec.get('time') else date_text
        if spec.get('time') and time_text is None:
            return [], {**meta, 'status': 'time_missing'}
        typed = _cell_type(record, columns, spec.get('date'))
        if typed in ('datetime', 'date'):
            date_text = date_text[:10]
            meta['date_interpretation'] = 'typed_spreadsheet_date'
        if spec.get('date_formats') and typed not in ('datetime', 'date'):
            date_parsed = timeparse.parse(date_text, spec['date_formats'], two_digit_year_base=base)
            if date_parsed['status'] != 'ok':
                problems.append(f"date_{date_parsed['status']}")
                return [], {**meta, 'status': date_parsed['status']}
            naive, precision, offset, fmt_meta = date_parsed['naive'], date_parsed['precision'], None, \
                {'date_format': date_parsed['format']}
            if time_text is not None:
                time_parsed = timeparse.parse(time_text, spec.get('time_formats', ['HH:mm:ss', 'HH:mm']))
                if time_parsed['status'] != 'ok':
                    problems.append(f"time_{time_parsed['status']}")
                    return [], {**meta, 'status': time_parsed['status']}
                t = time_parsed['naive']
                naive = naive.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=t.microsecond)
                precision, offset = time_parsed['precision'], time_parsed['offset_minutes']
                fmt_meta['time_format'] = time_parsed['format']
            return [{'label': None, 'naive': naive, 'precision': precision, 'offset': offset, 'meta': fmt_meta}], meta
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_text or ''):
        choices = [('iso', int(date_text[:4]), int(date_text[5:7]), int(date_text[8:10]))]
    else:
        parts = _date_parts(date_text)
        if parts is None:
            problems.append('date_unparsed')
            return [], {**meta, 'status': 'unparsed'}
        if parts.get('order') == 'year_first':
            choices = [('year_first', parts['year'], parts['month'], parts['day'])]
        else:
            if parts['year_digits'] == 2:
                if base is None:
                    problems.append('two_digit_year_century_not_assumed')
                    return [], {**meta, 'status': 'two_digit_year_century_not_assumed'}
                parts['year'] += base
                meta['year_rule'] = f'two_digit_year_base:{base}'
            day_first = ('day_first', parts['year'], parts['second'], parts['first'])
            month_first = ('month_first', parts['year'], parts['first'], parts['second'])
            if not parts['ambiguous']:
                if parts['first'] == parts['second'] or parts['first'] > 12:
                    choices = [day_first]
                    if parts['first'] > 12 and policy == 'month_first':
                        problems.append('row_contradicts_month_first_policy')
                else:
                    choices = [month_first]
                    if policy == 'day_first':
                        problems.append('row_contradicts_day_first_policy')
            elif policy in ('day_first', 'month_first'):
                choices = [day_first if policy == 'day_first' else month_first]
                meta['date_order_ambiguous_resolved_by'] = 'profile_or_file_policy'
            else:
                choices = [day_first, month_first]
                problems.append('date_order_ambiguous_both_readings_recorded')
    clock = (0, 0, 0, 0, False, None)
    if time_text is not None:
        parsed_time = _time_parts(time_text)
        if parsed_time is None:
            problems.append('time_unparsed')
            return [], {**meta, 'status': 'time_unparsed'}
        clock = parsed_time
    precision = 'day' if time_text is None else ('microsecond' if clock[3] else 'second' if clock[4] else 'minute')
    out = []
    for label, year, month, day in choices:
        try:
            naive = datetime(year, month, day, clock[0], clock[1], clock[2], clock[3])
        except ValueError:
            problems.append(f'invalid_calendar_value_{label}')
            continue
        out.append({'label': label if len(choices) > 1 else None, 'naive': naive, 'precision': precision,
                    'offset': None, 'meta': {'date_order': label, **({'ampm': clock[5]} if clock[5] else {})}})
    return out, meta


def _candidates(name: str, spec: dict, readings: list[dict], raw, profile: dict) -> list[TimeCandidate]:
    kind = DATE_KIND_TO_TIME_KIND[spec['kind']]
    base_source = f"ledger:{profile['id']}:{name}"
    raw_text = raw if isinstance(raw, str) or raw is None else json.dumps(raw, ensure_ascii=False, sort_keys=True)
    out = []
    zone = spec.get('assumed_zone', profile.get('assumed_zone'))
    for reading in readings:
        source = base_source + (f":{reading['label']}" if reading['label'] else '')
        meta = {'date_kind': spec['kind'], **reading['meta']}
        if reading.get('rollover'):
            meta['rollover'] = reading['rollover']
        if reading['offset'] is not None:
            out.append(offset_candidate(kind, raw_text, source, reading['naive'], reading['offset'],
                                        reading['precision'], {**meta, 'zone_status': 'offset_stated_by_export'}))
            continue
        candidate = local_candidate(kind, raw_text, source, reading['naive'], reading['precision'],
                                    {**meta, 'zone_status': 'export_zone_not_stated'})
        out.append(candidate)
        if zone:
            out.append(assume_zone(TimeCandidate(kind, raw_text, source + ':assumed_zone', local=candidate.local,
                                                 precision=candidate.precision, metadata=candidate.metadata),
                                   zone, rule=f"tabular_profile:{profile['id']}"))
    return out


def _instant(reading: dict) -> tuple[str, datetime] | None:
    """Comparable instant of a single reading: ('utc', aware->naive UTC) or ('local', naive wall clock)."""
    if reading['offset'] is not None:
        return 'utc', reading['naive'] - timedelta(minutes=reading['offset'])
    return 'local', reading['naive']


def _duration(start: dict | None, end: dict | None) -> tuple[int | None, dict]:
    if not start or not end:
        return None, {'status': 'not_computable', 'reason': 'missing_endpoint'}
    a, b = _instant(start), _instant(end)
    if a[0] != b[0]:
        return None, {'status': 'not_computable', 'reason': 'one_endpoint_has_offset_the_other_not'}
    seconds = (b[1] - a[1]).total_seconds()
    meta = {'status': 'ok', 'basis': 'utc_difference' if a[0] == 'utc' else 'wall_clock_difference_zone_unknown'}
    if a[0] == 'local':
        meta['caveat'] = 'a DST change between the endpoints would shift the true duration by the offset change'
    if seconds < 0:
        return None, {'status': 'negative', 'seconds': seconds, 'basis': meta['basis']}
    return int(seconds) if seconds == int(seconds) else None, {**meta, 'seconds': seconds}


# ---------------------------------------------------------------------------
# Amounts

def _number(text: str | None, fmt: money.NumberFormat, decided: str | None) -> dict:
    if text is None:
        return {'status': 'missing'}
    body, embedded = money.split_currency(text)
    parsed = money.parse_number(body, fmt)
    if parsed['status'] == 'ambiguous' and decided in (',', '.'):
        reading = next(r for r in parsed['readings'] if r['decimal'] == decided)
        parsed = {**parsed, 'status': 'ok', 'exact': reading['exact'], 'decimal_resolved_by': 'file_level_inference',
                  'scale': len(reading['exact'].split('.', 1)[1]) if '.' in reading['exact'] else 0}
    if embedded:
        parsed['currency_in_cell'] = embedded
    parsed['raw'] = text
    return parsed


def infer_decimal(texts: list[str]) -> dict:
    """File-level decimal separator evidence from values that only one reading accepts."""
    comma = point = 0
    fmt = money.NumberFormat('auto')
    for text in texts:
        body, _ = money.split_currency(text)
        parsed = money.parse_number(body, fmt)
        if parsed.get('status') == 'ok' and parsed.get('decimal_inferred') in (',', '.'):
            if parsed['decimal_inferred'] == ',':
                comma += 1
            else:
                point += 1
    decision = ',' if comma and not point else '.' if point and not comma else ('conflict' if comma and point else 'undetermined')
    return {'decision': decision, 'comma_evidence_values': comma, 'point_evidence_values': point}


def _amount(record: dict, columns: dict, profile: dict, fmt: money.NumberFormat, decided: str | None,
            problems: list[str]) -> dict:
    spec = profile.get('amount', {})
    mode = spec.get('mode', 'signed')
    out: dict = {'mode': mode}
    exact = flow = None
    for field in (spec.get('column', 'amount'), 'debit', 'credit'):
        if _cell_type(record, columns, field) == 'float':
            problems.append('amount_from_spreadsheet_float_cell_repr')  # the cell's own text is not exposed
            break
    if mode == 'signed':
        parsed = _number(_cell(record, columns, spec.get('column', 'amount')), fmt, decided)
        out['amount'] = parsed
        if parsed['status'] == 'ok':
            exact = parsed['exact']
        elif parsed['status'] != 'missing':
            problems.append(f"amount_{parsed['status']}")
    elif mode == 'debit_credit':
        debit = _number(_cell(record, columns, 'debit'), fmt, decided)
        credit = _number(_cell(record, columns, 'credit'), fmt, decided)
        out.update(debit=debit, credit=credit)
        present = {k: v for k, v in (('debit', debit), ('credit', credit)) if v['status'] != 'missing'}
        bad = [k for k, v in present.items() if v['status'] != 'ok']
        nonzero = {k: v for k, v in present.items() if v['status'] == 'ok' and money.Decimal(v['exact']) != 0}
        if bad:
            problems.extend(f'{k}_{present[k]["status"]}' for k in bad)
        elif len(nonzero) > 1:
            problems.append('both_debit_and_credit_present')
        elif nonzero:
            side, value = next(iter(nonzero.items()))
            if money.is_negative(value['exact']):
                problems.append(f'{side}_column_value_already_negative')
                exact = value['exact']
            else:
                exact = money.negate(value['exact']) if side == 'debit' else value['exact']
            if len(present) > 1:
                out['zero_in_other_column'] = True
        elif present:
            exact = '0'
    elif mode == 'indicator':
        parsed = _number(_cell(record, columns, spec.get('column', 'amount')), fmt, decided)
        indicator_raw = _cell(record, columns, 'indicator')
        mapping = {_norm(k): v for k, v in spec.get('indicator_map', {}).items()}
        indicator = mapping.get(_norm(indicator_raw)) if indicator_raw is not None else None
        out.update(amount=parsed, indicator={'raw': indicator_raw, 'label': indicator})
        if parsed['status'] != 'ok':
            if parsed['status'] != 'missing':
                problems.append(f"amount_{parsed['status']}")
        elif indicator is None:
            problems.append('indicator_missing' if indicator_raw is None else 'indicator_unmapped')
        else:
            value = parsed['exact']
            if parsed.get('sign_notation') in ('leading_minus', 'trailing_minus', 'parentheses'):
                if indicator == 'credit':
                    problems.append('sign_contradicts_indicator')
                else:
                    exact = value
                    out['sign_and_indicator_agree'] = True
            else:
                exact = money.negate(value) if indicator == 'debit' else value
    else:
        raise ValueError(f"profile {profile['id']}: unknown amount mode {mode!r}")
    out['sign_convention'] = 'holder_signed_negative_is_money_out'
    if exact is not None and mode == 'signed' and spec.get('positive_is', 'credit') == 'debit':
        # e.g. a fare column lists what the holder paid as a positive number
        exact = money.negate(exact)
        out['source_sign_inverted'] = 'profile_positive_is_debit'
    if exact is not None:
        flow = None if money.Decimal(exact) == 0 else ('debit' if money.is_negative(exact) else 'credit')
    # currency
    column_currency = _cell(record, columns, spec.get('currency_column', 'currency'))
    embedded = [v.get('currency_in_cell') for v in (out.get('amount'), out.get('debit'), out.get('credit'))
                if isinstance(v, dict) and v.get('currency_in_cell')]
    readings = {}
    if column_currency is not None:
        readings['column'] = money.currency_code(column_currency)
    for value in embedded:
        readings.setdefault('in_amount_cell', money.currency_code(value))
    codes = {r['code'] for r in readings.values() if r['status'] == 'ok'}
    currency = None
    if len(codes) > 1:
        problems.append('currency_conflict')
    elif codes:
        currency = codes.pop()
        basis = 'column' if readings.get('column', {}).get('code') == currency else 'in_amount_cell'
    elif spec.get('default_currency') and not any(r['status'] in ('ambiguous', 'unparsed') for r in readings.values()):
        currency, basis = spec['default_currency'], 'profile_default_currency'
    else:
        basis = None
        for r in readings.values():
            if r['status'] in ('ambiguous', 'unparsed'):
                problems.append(f"currency_{r['status']}")
    minor = money.minor_units(exact, currency)
    if exact is not None and currency is not None and minor['status'] != 'ok':
        problems.append(minor['status'])
    out.update(exact=exact, flow=flow, currency=currency, currency_basis=basis if currency else None,
               currency_readings=readings, minor=minor)
    return out


# ---------------------------------------------------------------------------
# Import

def _reading_key(values: dict) -> str:
    return store.sha256_of(values)[:12]


def _content_key(kind: str, times: dict, amount: dict | None, text_fields: dict) -> str:
    return store.sha256_of({'kind': kind, 'times': times, 'amount': amount and [amount.get('exact'), amount.get('currency')],
                            'text': text_fields})


def import_tabular(path: Path, *, profile_id: str | None = None, profiles_path: Path | None = None,
                   encoding: str | None = None, delimiter: str | None = None, source_locator: str | None = None,
                   original_name: str | None = None, collection_id: int | None = None, label: str | None = None,
                   limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """Import one CSV/TSV/TXT/XLSX/XLS table through a profile; see the module docstring."""
    profiles = load_profiles(profiles_path)
    ids = {p['id'] for p in profiles['profiles']}
    if profile_id is not None and profile_id not in ids:
        raise ValueError(f'tabular profile not found: {profile_id}')
    if encoding not in (None, '', 'auto'):
        textio.canonical_encoding(encoding)
    if delimiter not in (None, '', 'auto') and delimiter not in textio.DELIMITERS:
        raise ValueError(f'delimiter must be one of {textio.DELIMITERS!r}')
    name = original_name or path.name
    extension = Path(name).suffix.lower()
    if extension not in ('.csv', '.tsv', '.txt', '.xlsx', '.xls'):
        raise ValueError('tabular import supports .csv, .tsv, .txt, .xlsx and .xls')
    candidates = [p for p in profiles['profiles'] if profile_id is None or p['id'] == profile_id]
    kind_hint = candidates[0]['record_kind'] if len(candidates) == 1 else 'tabular'
    locator = source_locator if source_locator is not None else str(path)
    set_label = profile_set_label(profiles)
    options = {'profile_id': profile_id, 'profile_set': set_label, 'encoding': encoding or None,
               'delimiter': delimiter or None}
    source_id, artifact_id = acquire(
        path, source_kind=SOURCE_KINDS.get(kind_hint, 'tabular_export'), source_label=label or 'Tabular export',
        locator=locator, importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name,
        collection_id=collection_id, options=options)
    issues = Issues(limits.max_issues)
    snapshot = stored_path(artifact_id)
    size = snapshot.stat().st_size
    complete = True
    with snapshot.open('rb') as handle:
        raw = handle.read(limits.max_json_bytes + 1)
    if len(raw) > limits.max_json_bytes:
        cut = raw.rfind(b'\n', 0, limits.max_json_bytes)
        raw = raw[:cut + 1 if cut >= 0 else limits.max_json_bytes]
        issues.add('file_byte_limit', limit=limits.max_json_bytes, size_bytes=size)
        complete = False
    overrides = {'encoding': None if encoding in (None, '', 'auto') else encoding,
                 'delimiter': None if delimiter in (None, '', 'auto') else ('\t' if delimiter == '\t' else delimiter)}
    if extension in ('.xlsx', '.xls'):
        tables = _sheet_tables(raw, name, extension, artifact_id, limits)
        selections = []
        for table in tables:
            for record in table['records'][:40]:
                for profile in candidates:
                    columns = resolve_profile_columns(profile, record['values'])
                    if all(f in columns for f in profile.get('required', [])):
                        selections.append({'profile': profile, 'table': table, 'header_index': record['index'],
                                           'reading': {'container': extension[1:], 'sheet': table['name']}})
                        break
                if selections and selections[-1]['table'] is table:
                    break
        for table in tables:
            for issue in table['issues']:
                issues.add(f"table_{issue['code']}", **{k: v for k, v in issue.items() if k not in ('code', 'path')})
            complete = complete and table['complete']
    else:
        if extension == '.tsv' and overrides['delimiter'] is None:
            overrides['delimiter'] = '\t'
        matches, attempt_info = _text_tables(raw, candidates, overrides, limits)
        selections = []
        for match in matches:
            parsed = textio.parse_records(match['lines'], start=0, delimiter=match['delimiter'],
                                          quotechar=match['profile'].get('read', {}).get('quotechar', '"'),
                                          max_records=limits.max_records + match['header_index'],
                                          max_raw_chars=limits.max_raw_chars)
            table = {'name': 'csv', 'records': parsed['records'], 'complete': not parsed['limited'] and not parsed['error'],
                     'error': parsed['error']}
            selections.append({'profile': match['profile'], 'table': table, 'header_index': match['header_index'],
                               'reading': {'container': 'text', 'encoding': match['encoding'],
                                           'delimiter': match['delimiter'], 'delimiter_basis': match['delimiter_basis'],
                                           'delimiter_guess': match['delimiter_guess']}})
        if not matches:
            for attempt in attempt_info['attempts']:
                issues.add('profile_reading_failed', **attempt)
    if not selections:
        message = (f'profile {profile_id} does not match the table header' if profile_id else
                   'no tabular profile matches the table header within its header search rows')
        failed = [a['error'] for a in (attempt_info['attempts'] if extension not in ('.xlsx', '.xls') else [])]
        if failed:
            message += f' (reading failed: {failed[0]})'
        record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                          options=options, counts={'records': 0}, coverage={'complete': False}, issues=issues)
        raise ValueError(message)
    matched_ids = sorted({s['profile']['id'] for s in selections})
    if len(matched_ids) > 1:
        record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                          options=options, counts={'records': 0}, coverage={'complete': False}, issues=issues)
        raise ValueError('several tabular profiles match; choose one explicitly: ' + ', '.join(matched_ids))
    counts = {'rows': 0, 'records': 0, 'events': 0, 'skipped_rows': 0, 'ambiguous_dates': 0, 'unparsed_amounts': 0,
              'segments': 0, 'unpaired_taps': 0}
    summaries = []
    for selection in selections:
        summary = _import_table(selection, source_id=source_id, artifact_id=artifact_id, set_label=set_label,
                                method='explicit' if profile_id else 'unique_header_match', issues=issues,
                                counts=counts, limits=limits)
        complete = complete and summary.pop('complete')
        summaries.append(summary)
    coverage = {'complete': complete}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION,
            **counts, 'tables': summaries, 'coverage': coverage, 'issues': issues.as_dict()}


def _import_table(selection: dict, *, source_id: int, artifact_id: int, set_label: str, method: str, issues: Issues,
                  counts: dict, limits: CommLimits) -> dict:
    profile, table = selection['profile'], selection['table']
    records = table['records']
    header_position = next((i for i, r in enumerate(records) if r['index'] == selection['header_index']), None)
    if header_position is None:
        raise ValueError(f"table {table['name']} has no record {selection['header_index']} (header or first data row)")
    header_mode = profile.get('read', {}).get('header', 'names')
    if header_mode == 'none':
        header = None
        data = records[header_position:]
        preamble = records[:header_position]
    else:
        header = records[header_position]
        data = records[header_position + 1:]
        preamble = records[:header_position]
    columns = resolve_profile_columns(profile, header['values'] if header else None)
    fmt = money.NumberFormat.from_spec(profile.get('number'))
    complete = table['complete']
    if table.get('error'):
        issues.add('csv_record_parse_failed', **table['error'])
    base = profile.get('two_digit_year_base')
    # file-level policies
    policies, inference = {}, {}
    all_specs = dict(profile.get('times', {}))
    for specs in profile.get('times_by_row_kind', {}).values():
        all_specs.update({k: v for k, v in specs.items() if k not in all_specs})
    for name, spec in all_specs.items():
        order = spec.get('order', 'undetermined')
        if order == 'infer_from_file':
            field = spec.get('date') or spec.get('datetime')
            texts = []
            for record in data:
                text = _cell(record, columns, field)
                if text is not None:
                    split = _DATETIME_SPLIT.match(text) if 'datetime' in spec else None
                    texts.append(split.group(1) if split else text)
            inference[name] = infer_date_order(texts)
            order = inference[name]['decision']
        policies[name] = order
    decided = None
    amount_columns = [c for c in ('amount', 'debit', 'credit', profile.get('amount', {}).get('column')) if c]
    if fmt.decimal == 'auto':
        texts = [t for r in data for c in amount_columns for t in [_cell(r, columns, c)] if t is not None]
        inference['decimal'] = infer_decimal(texts)
        decided = inference['decimal']['decision'] if inference['decimal']['decision'] in (',', '.') else None
    reading = {'profile_set': set_label, 'profile_id': profile['id'], **{k: v for k, v in selection['reading'].items()
                                                                          if k in ('container', 'sheet', 'delimiter')},
               'encoding': (selection['reading'].get('encoding') or {}).get('used'),
               'header_row': selection['header_index'], 'policies': policies, 'decimal': decided}
    reading_key = _reading_key(reading)
    summary = {'table': table['name'], 'profile_id': profile['id'], 'selection': method, 'reading_key': reading_key,
               'reading': {**selection['reading'], 'header_row': selection['header_index'], 'columns': columns,
                           'date_order_policies': policies, 'inference': inference},
               'preamble': [{'line': r['start_line'], 'row': r['index'], 'raw': (r['raw'] or '')[:limits.max_raw_chars]
                             if r.get('raw') is not None else r['values']} for r in preamble[:50]],
               'preamble_rows': len(preamble)}
    header_width = len(header['values']) if header and selection['reading'].get('container') == 'text' else None
    kind = profile['record_kind']
    built = []
    taps = []
    running = []
    for record in data:
        if textio.blank(record['values']):
            continue
        counts['rows'] += 1
        problems: list[str] = []
        if header_width is not None and len(record['values']) != header_width:
            problems.append('row_width_differs_from_header')
        row_kind = kind
        row_type_raw = _cell(record, columns, 'row_type')
        if profile.get('row_type_map'):
            mapping = {_norm(k): v for k, v in profile['row_type_map'].items()}
            mapped = mapping.get(_norm(row_type_raw)) if row_type_raw is not None else None
            if mapped is None and _norm(row_type_raw) not in mapping:
                mapped = mapping.get('*')
                if row_type_raw is not None:
                    problems.append('row_type_unmapped_default_used')
            if mapped is None:
                counts['skipped_rows'] += 1
                issues.add('row_type_skipped', row=record['index'], raw=row_type_raw)
                continue
            if mapped == 'financial_transaction':
                row_kind = 'financial_transaction'
        specs = _time_specs(profile, row_kind)
        times, raw_times = {}, {}
        for name, spec in specs.items():
            readings, meta = _readings(spec, record, columns, policies.get(name), base, problems)
            times[name] = readings
            raw_times[name] = meta
        for name, spec in specs.items():
            ref = spec.get('rollover_from')
            if ref and len(times.get(name, [])) == 1 and len(times.get(ref, [])) == 1 \
                    and spec.get('date') == specs[ref].get('date'):
                this, other = times[name][0], times[ref][0]
                if _instant(this)[0] == _instant(other)[0] and this['naive'] < other['naive']:
                    this['naive'] = this['naive'] + timedelta(days=1)
                    this['rollover'] = f'next_day_assumed:{name}_earlier_than_{ref}_on_one_date'
                    problems.append('time_rolled_over_to_next_day')
        if 'date_order_ambiguous_both_readings_recorded' in problems:
            counts['ambiguous_dates'] += 1
        if not any(times.values()):
            counts['skipped_rows'] += 1
            issues.add('row_without_parseable_time', row=record['index'], problems=problems[:10])
            continue
        amount = None
        if row_kind == 'financial_transaction' or profile.get('amount', {}).get('column') or 'amount' in columns:
            amount = _amount(record, columns, profile, fmt, decided, problems)
            if amount['exact'] is None and row_kind == 'financial_transaction':
                counts['unparsed_amounts'] += 1
        position = f"{table['name']}:row:{record['index']}"
        raw_cells, truncated = bound_json(record['values'], limits)
        text = {f: _cell(record, columns, f) for f in ('counterparty_name', 'counterparty_account', 'description',
                                                      'transaction_type', 'origin', 'destination', 'stop', 'card',
                                                      'status', 'product', 'city', 'own_account', 'row_type')}
        base_meta = {
            'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
            'profile': {'id': profile['id'], 'set': set_label, 'selection': method, 'record_kind': kind},
            'reading_key': reading_key,
            'locator': {'artifact_id': artifact_id, 'table': table['name'], 'row': record['index'],
                        'start_line': record.get('start_line'), 'end_line': record.get('end_line')},
            'raw_cells': raw_cells, 'raw_record': record.get('raw'), 'raw_truncated_values': truncated,
            'times_raw': raw_times,
        }
        row = {'record': record, 'position': position, 'times': times, 'raw_times': raw_times, 'amount': amount,
               'text': text, 'meta': base_meta, 'problems': problems, 'row_kind': row_kind, 'specs': specs,
               'fields': {'distance': _cell(record, columns, 'distance')},
               'distance_header': header['values'][columns['distance']] if header and 'distance' in columns
               and columns['distance'] < len(header['values']) else None}
        if amount is not None and kind == 'financial_transaction':
            balance = _number(_cell(record, columns, 'balance_after'), fmt, decided)
            row['balance_after'] = balance
            if balance['status'] == 'ok' and amount['exact'] is not None:
                running.append((record['index'], amount['exact'], balance['exact']))
        if row_kind == 'travel_taps':
            tap_raw = _cell(record, columns, 'tap')
            tap = {_norm(k): v for k, v in profile.get('tap_map', {}).items()}.get(_norm(tap_raw)) if tap_raw else None
            row['tap'] = {'raw': tap_raw, 'label': tap}
            if tap is None:
                problems.append('tap_type_unmapped' if tap_raw else 'tap_type_missing')
            taps.append(row)
            continue
        built.append(row)
    if running:
        summary['running_balance_check'] = _running_balance(running)
    occurrences: dict[str, int] = {}
    to_write = []
    for row in built:
        to_write.append(_record_for_row(row, profile, kind, occurrences, reading_key, source_id, artifact_id, counts))
    if taps:
        summary['tap_pairing'] = {'rule': TAP_PAIRING_RULE, **profile.get('pairing', {})}
        to_write.extend(_pair_taps(taps, profile, occurrences, reading_key, source_id, artifact_id, counts))
    for record in to_write:
        for problem in record['problems']:
            issues.add(problem, table=table['name'], position=record['position'])
    _write(to_write, profile, set_label, artifact_id, reading_key)
    counts['records'] += sum(len(r['entries']) for r in to_write)
    counts['events'] += len(to_write)
    summary['complete'] = complete
    return summary


def _time_specs(profile: dict, row_kind: str) -> dict:
    """Time points for a row: ``times_by_row_kind[row_kind]`` (e.g. a top-up row in a transit history) or ``times``."""
    return profile.get('times_by_row_kind', {}).get(row_kind) or profile.get('times', {})


def _running_balance(rows: list[tuple[int, str, str]]) -> dict:
    """Consecutive balance-after values: forward (prev + amount = this) or reverse (newest first) consistency."""
    forward = backward = neither = 0
    examples = []
    for (i0, a0, b0), (i1, a1, b1) in zip(rows, rows[1:]):
        if money.Decimal(b0) + money.Decimal(a1) == money.Decimal(b1):
            forward += 1
        elif money.Decimal(b1) + money.Decimal(a0) == money.Decimal(b0):
            backward += 1
        else:
            neither += 1
            if len(examples) < 20:
                examples.append({'rows': [i0, i1], 'balances': [b0, b1], 'amounts': [a0, a1]})
    order = 'file_order_oldest_first' if forward and not backward else \
        'file_order_newest_first' if backward and not forward else 'undetermined'
    return {'method': 'consecutive_balance_after_differences_computed_by_code', 'pairs': len(rows) - 1,
            'forward_consistent': forward, 'reverse_consistent': backward, 'inconsistent': neither,
            'order_inferred': order, 'inconsistent_examples': examples, 'corrected': False}


def _times_flat(times: dict) -> dict:
    return {name: [r['naive'].isoformat() + (f"{r['offset']:+d}m" if r['offset'] is not None else '') for r in readings]
            for name, readings in times.items()}


def _amount_meta(amount: dict | None) -> dict | None:
    if amount is None:
        return None
    return {k: v for k, v in amount.items()}


def _entry(row: dict, record_kind: str, profile: dict, occurrence: int, content_key: str, *, duration=None,
           extra: dict | None = None) -> dict:
    amount = row['amount'] or {}
    text = row['text']
    minor = amount.get('minor') or {}
    return {'record_kind': record_kind, 'profile_id': profile['id'], 'source_position': row['position'],
            'amount_exact': amount.get('exact'), 'currency': amount.get('currency'), 'amount_minor': minor.get('minor'),
            'currency_exponent': minor.get('exponent'), 'flow': amount.get('flow'),
            'counterparty_name': text.get('counterparty_name'), 'counterparty_account': text.get('counterparty_account'),
            'description': text.get('description'), 'duration_s': duration, 'content_key': content_key,
            'content_occurrence': occurrence,
            'metadata_json': {'locator': row['meta']['locator'], 'amount': _amount_meta(row['amount']),
                              'text': text, **(extra or {})}}


def _record_for_row(row: dict, profile: dict, kind: str, occurrences: dict, reading_key: str, source_id: int,
                    artifact_id: int, counts: dict) -> dict:
    times, text, amount = row['times'], row['text'], row['amount']
    row_kind = row['row_kind']
    content_key = _content_key(row_kind, _times_flat(times), amount, {k: v for k, v in text.items() if v is not None})
    occurrences[content_key] = occurrences.get(content_key, 0) + 1
    occurrence = occurrences[content_key]
    candidates = []
    for name, spec in row['specs'].items():
        candidates.extend(_candidates(name, spec, times.get(name, []), row['raw_times'].get(name, {}).get('raw'), profile))
    problems = row['problems']
    meta = {**row['meta'], 'issues': problems, 'content_key': content_key, 'content_occurrence': occurrence}
    provider = profile.get('provider', 'tabular_export')
    duration = None
    if row_kind == 'financial_transaction':
        event_type = 'financial_transaction'
        meta['signal'] = store.signal('active', 'raw_sample', provider, text.get('own_account'))
        meta['amount'] = amount
        if row.get('balance_after') and row['balance_after']['status'] != 'missing':
            meta['balance_after'] = row['balance_after']
        account = text.get('counterparty_account')
        if account:
            meta['counterparty_account_check'] = store.iban_check(account)
        event = {'direction': amount['flow'] if amount else None, 'contact_label': text.get('counterparty_name'),
                 'phone_or_address': account, 'subject': text.get('transaction_type'), 'body': text.get('description')}
        primary = _primary(candidates, ('start', 'created', 'other'))
        end = None
        record_kind = 'financial_transaction'
    elif row_kind == 'travel_segment':
        start = (times.get('check_in') or [None])[0] if len(times.get('check_in') or []) == 1 else None
        stop = (times.get('check_out') or [None])[0] if len(times.get('check_out') or []) == 1 else None
        has_in, has_out = bool(times.get('check_in')), bool(times.get('check_out'))
        transport = {'mode': profile.get('transport_mode', 'public_transport'), 'origin': text.get('origin'),
                     'destination': text.get('destination'), 'card': text.get('card'), 'row_type': text.get('row_type')}
        if has_in and has_out:
            event_type = 'travel_segment'
            duration, duration_meta = _duration(start, stop)
            transport['duration'] = duration_meta
            if duration_meta['status'] == 'negative':
                problems.append('check_out_before_check_in')
            meta['pairing'] = {'status': 'paired_by_provider_row'}
            meta['signal'] = store.signal('active', 'provider_inference', provider, text.get('card'),
                                          note='journey row assembled by the provider from check-in and check-out')
            counts['segments'] += 1
        else:
            event_type = 'travel_tap'
            meta['pairing'] = {'status': 'unpaired', 'reason': 'missing_check_out' if has_in else 'missing_check_in'}
            meta['signal'] = store.signal('active', 'raw_sample', provider, text.get('card'))
            counts['unpaired_taps'] += 1
        meta['transport'] = transport
        if amount is not None:
            meta['amount'] = amount
        event = {'direction': None, 'contact_label': text.get('origin'), 'phone_or_address': None,
                 'subject': ' → '.join(x for x in (text.get('origin'), text.get('destination')) if x) or None, 'body': None}
        primary = _primary(candidates, ('start',))
        end = _primary(candidates, ('end',))
        record_kind = 'travel_segment'
    else:  # ride
        status_raw = text.get('status')
        status = {_norm(k): v for k, v in profile.get('status_map', {}).items()}.get(_norm(status_raw)) if status_raw else None
        if status_raw and status is None:
            problems.append('ride_status_unmapped')
        has_trip = len(times.get('begin') or []) == 1 and len(times.get('dropoff') or []) == 1
        distance = _distance(row, profile)
        transport = {'mode': profile.get('transport_mode', 'ride_hailing'), 'status': {'raw': status_raw, 'label': status},
                     'product': text.get('product'), 'city': text.get('city'), 'distance': distance}
        if has_trip:
            event_type = 'travel_segment'
            duration, duration_meta = _duration(times['begin'][0], times['dropoff'][0])
            transport['duration'] = duration_meta
            if duration_meta['status'] == 'negative':
                problems.append('dropoff_before_begin')
            meta['signal'] = store.signal('active', 'provider_inference', provider, None,
                                          note='trip record (begin/dropoff) kept by the ride provider')
            counts['segments'] += 1
        else:
            event_type = 'ride_request'
            meta['signal'] = store.signal('active', 'raw_sample', provider, None)
        meta['transport'] = transport
        meta['amount'] = amount
        event = {'direction': None, 'contact_label': None, 'phone_or_address': None,
                 'subject': text.get('product'), 'body': None}
        primary = _primary(candidates, ('start', 'created'))
        end = _primary(candidates, ('end',))
        record_kind = 'ride'
    external = f"{PARSER_NAME}:{profile['id']}:{reading_key}:{row['position']}"
    entry = _entry(row, record_kind, profile, occurrence, content_key, duration=duration)
    return {'position': row['position'], 'problems': problems, 'candidates': candidates,
            'event': {'source_id': source_id, 'artifact_id': artifact_id, 'event_type': event_type,
                      'ts_start': legacy_instant(primary), 'ts_end': legacy_instant(end), **event,
                      'external_id': external, 'confidence': None, 'metadata': meta},
            'entries': [entry]}


def _primary(candidates: list[TimeCandidate], kinds: tuple[str, ...]) -> TimeCandidate | None:
    for kind in kinds:
        found = [c for c in candidates if c.kind == kind and c.tz_status != 'assumed_zone']
        if len(found) == 1:
            return found[0]
        if found:
            return None
    return None


_DISTANCE_FACTORS = {'m': '1', 'km': '1000', 'mi': '1609.344'}


def _distance(row: dict, profile: dict) -> dict | None:
    """Distance as written plus exact metres when the unit is stated (profile or header name)."""
    value = row['fields'].get('distance')
    if value is None:
        return None
    parsed = money.parse_number(value, money.NumberFormat.from_spec(profile.get('number')))
    unit, basis = profile.get('distance_unit'), 'profile'
    if unit is None and row.get('distance_header') is not None:
        unit = {_norm(k): v for k, v in profile.get('distance_unit_by_header', {}).items()}.get(_norm(row['distance_header']))
        basis = 'header_name' if unit else None
    out = {'raw': value, 'parsed': parsed, 'unit': unit, 'unit_basis': basis if unit else None}
    if parsed.get('status') == 'ok' and unit in _DISTANCE_FACTORS:
        meters = money.Decimal(parsed['exact']) * money.Decimal(_DISTANCE_FACTORS[unit])
        out['meters_exact'] = format(meters, 'f')
        out['conversion'] = f'1 {unit} = {_DISTANCE_FACTORS[unit]} m (exact definition)'
    elif parsed.get('status') == 'ok':
        out['meters_exact'] = None
        row['problems'].append('distance_unit_unknown')
    return out


def _pair_taps(taps: list[dict], profile: dict, occurrences: dict, reading_key: str, source_id: int, artifact_id: int,
               counts: dict) -> list[dict]:
    """Rule ``ledger.tap_pairing.v1``: per card, chronological; an entry pairs with the next exit within the gap."""
    max_gap = int(profile.get('pairing', {}).get('max_pair_gap_s', 18000))
    by_card: dict[str, list[dict]] = {}
    unpaired: list[tuple[dict, str]] = []
    for tap in taps:
        readings = tap['times'].get('tap') or []
        if tap['tap']['label'] is None:
            unpaired.append((tap, tap['problems'][-1] if tap['problems'] else 'tap_type_unmapped'))
        elif len(readings) != 1:
            unpaired.append((tap, 'time_not_comparable'))
        else:
            by_card.setdefault(tap['text'].get('card') or '', []).append(tap)
    pairs: list[tuple[dict, dict]] = []
    for card, group in sorted(by_card.items()):
        classes = {_instant(t['times']['tap'][0])[0] for t in group}
        if len(classes) > 1:
            unpaired.extend((t, 'mixed_zone_status_not_comparable') for t in group)
            continue
        group.sort(key=lambda t: (_instant(t['times']['tap'][0])[1], t['record']['index']))
        pending = None
        for tap in group:
            if tap['tap']['label'] == 'in':
                if pending is not None:
                    unpaired.append((pending, 'next_tap_was_check_in'))
                pending = tap
                continue
            if pending is None:
                unpaired.append((tap, 'no_preceding_check_in'))
                continue
            gap = (_instant(tap['times']['tap'][0])[1] - _instant(pending['times']['tap'][0])[1]).total_seconds()
            if gap > max_gap:
                unpaired.append((pending, 'gap_exceeds_max_pair_gap'))
                unpaired.append((tap, 'gap_exceeds_max_pair_gap'))
            else:
                pairs.append((pending, tap))
            pending = None
        if pending is not None:
            unpaired.append((pending, 'no_following_check_out'))
    out = []
    provider = profile.get('provider', 'tabular_export')
    rule = {'rule': TAP_PAIRING_RULE, 'max_pair_gap_s': max_gap}
    for entry_tap, exit_tap in pairs:
        candidates = []
        for tap, point in ((entry_tap, 'check_in'), (exit_tap, 'check_out')):
            spec = {**profile['times']['tap'], 'kind': point}
            candidates.extend(_candidates(f'tap:{point}', spec, tap['times']['tap'], tap['raw_times']['tap'].get('raw'), profile))
        duration, duration_meta = _duration(entry_tap['times']['tap'][0], exit_tap['times']['tap'][0])
        problems = entry_tap['problems'] + exit_tap['problems']
        positions = [entry_tap['position'], exit_tap['position']]
        content_key = _content_key('travel_tap_pair', {'in': _times_flat(entry_tap['times']), 'out': _times_flat(exit_tap['times'])},
                                   None, {'in': entry_tap['text'].get('stop'), 'out': exit_tap['text'].get('stop')})
        occurrences[content_key] = occurrences.get(content_key, 0) + 1
        transport = {'mode': profile.get('transport_mode', 'public_transport'), 'origin': entry_tap['text'].get('stop'),
                     'destination': exit_tap['text'].get('stop'), 'card': entry_tap['text'].get('card'),
                     'duration': duration_meta}
        meta = {**entry_tap['meta'], 'locator': {'artifact_id': artifact_id, 'rows': [entry_tap['meta']['locator'],
                                                                                    exit_tap['meta']['locator']]},
                'raw_cells': [entry_tap['meta']['raw_cells'], exit_tap['meta']['raw_cells']],
                'raw_record': [entry_tap['meta']['raw_record'], exit_tap['meta']['raw_record']],
                'times_raw': {'in': entry_tap['raw_times'], 'out': exit_tap['raw_times']},
                'pairing': {'status': 'paired_by_rule', **rule}, 'transport': transport,
                'signal': store.signal('active', 'derived', provider, entry_tap['text'].get('card'),
                                       derivation=rule, from_basis='raw_sample'),
                'issues': problems, 'content_key': content_key, 'content_occurrence': occurrences[content_key],
                'taps': [entry_tap['tap'], exit_tap['tap']],
                'amounts': [entry_tap['amount'], exit_tap['amount']]}
        counts['segments'] += 1
        primary, end = _primary(candidates, ('start',)), _primary(candidates, ('end',))
        entries = []
        for tap in (entry_tap, exit_tap):
            key = _content_key('travel_tap', _times_flat(tap['times']), tap['amount'],
                               {k: v for k, v in tap['text'].items() if v is not None} | {'tap': tap['tap']['raw']})
            occurrences[key] = occurrences.get(key, 0) + 1
            entries.append(_entry(tap, 'travel_tap', profile, occurrences[key], key, duration=duration,
                                  extra={'tap': tap['tap'], 'pairing': 'paired_by_rule'}))
        out.append({'position': '+'.join(positions), 'problems': problems, 'candidates': candidates,
                    'event': {'source_id': source_id, 'artifact_id': artifact_id, 'event_type': 'travel_segment',
                              'ts_start': legacy_instant(primary), 'ts_end': legacy_instant(end),
                              'direction': None, 'contact_label': transport['origin'], 'phone_or_address': None,
                              'subject': ' → '.join(x for x in (transport['origin'], transport['destination']) if x) or None,
                              'body': None,
                              'external_id': f"{PARSER_NAME}:{profile['id']}:{reading_key}:pair:{'+'.join(positions)}"
                                             f":{TAP_PAIRING_RULE}:gap{max_gap}",
                              'confidence': None, 'metadata': meta},
                    'entries': entries})
    for tap, reason in sorted(unpaired, key=lambda item: item[0]['record']['index']):
        counts['unpaired_taps'] += 1
        key = _content_key('travel_tap', _times_flat(tap['times']), tap['amount'],
                           {k: v for k, v in tap['text'].items() if v is not None} | {'tap': tap['tap']['raw']})
        occurrences[key] = occurrences.get(key, 0) + 1
        point = {'in': 'check_in', 'out': 'check_out'}.get(tap['tap']['label'], 'tap')
        spec = {**profile['times']['tap'], 'kind': point if point != 'tap' else 'tap'}
        candidates = _candidates('tap', spec, tap['times'].get('tap') or [], tap['raw_times']['tap'].get('raw'), profile)
        meta = {**tap['meta'], 'pairing': {'status': 'unpaired', 'reason': reason, **rule}, 'tap': tap['tap'],
                'transport': {'mode': profile.get('transport_mode', 'public_transport'), 'stop': tap['text'].get('stop'),
                              'card': tap['text'].get('card')},
                'signal': store.signal('active', 'raw_sample', provider, tap['text'].get('card')),
                'amount': tap['amount'], 'issues': tap['problems'], 'content_key': key,
                'content_occurrence': occurrences[key]}
        primary = _primary(candidates, ('start', 'end'))
        out.append({'position': tap['position'], 'problems': tap['problems'], 'candidates': candidates,
                    'event': {'source_id': source_id, 'artifact_id': artifact_id, 'event_type': 'travel_tap',
                              'ts_start': legacy_instant(primary), 'ts_end': None, 'direction': None,
                              'contact_label': tap['text'].get('stop'), 'phone_or_address': None,
                              'subject': tap['tap']['raw'], 'body': None,
                              'external_id': f"{PARSER_NAME}:{profile['id']}:{reading_key}:tap:{tap['position']}"
                                             f":{TAP_PAIRING_RULE}:gap{max_gap}",
                              'confidence': None, 'metadata': meta},
                    'entries': [_entry(tap, 'travel_tap', profile, occurrences[key], key,
                                       extra={'tap': tap['tap'], 'pairing': 'unpaired', 'reason': reason})]})
    return out


def _write(records: list[dict], profile: dict, set_label: str, artifact_id: int, reading_key: str) -> None:
    for offset in range(0, len(records), BATCH):
        with session() as db:
            db.execute('BEGIN IMMEDIATE')
            for record in records[offset:offset + BATCH]:
                event_id = store.add_event(db, record['candidates'], **record['event'])
                for entry in record['entries']:
                    store.insert_row(db, 'ledger_entries', {
                        'event_id': event_id, 'artifact_id': artifact_id, 'statement_id': None,
                        'parser': PARSER_NAME, 'parser_version': PARSER_VERSION, 'reading_key': reading_key, **entry,
                        'metadata_json': {**entry['metadata_json'], 'profile_set': set_label}})

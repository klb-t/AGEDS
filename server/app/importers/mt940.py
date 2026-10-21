"""SWIFT MT940 customer statements → ``financial_transaction`` events.

Fields are read from the text (SWIFT ``{1:…}{4:`` envelopes and ``-}``/``-``
terminators are tolerated); several statements per file are separate
statements (each ``:20:`` after a statement's lines starts a new one).

``:61:`` statement lines: value date ``YYMMDD``, optional entry (booking) date
``MMDD`` *without a year*, mark ``C``/``D``/``RC``/``RD`` (reversals invert the
sign: ``RC`` reduces, ``RD`` increases the balance), optional funds code (third
letter of the currency, checked), amount with a decimal comma, transaction type,
references and supplementary details; the following ``:86:`` is kept raw and,
when it has numbered subfields (``?20``/``<20``) or slash codes (``/NAME/``),
parsed with the example hints in ``data/mt940_details.v1.json``.

Two-digit years get a century from ``two_digit_year_base`` (default 2000, a
recorded parameter). The entry date's year is *inferred relative to the value
date*: the nearest of the previous, same and next year; crossing a year
boundary is flagged; when the nearest reading is more than 92 days away, or a
second reading is within 31 days of it, both readings are kept as separate
candidates and flagged ambiguous. The ``:60F:``/``:60M:`` + entries =
``:62F:``/``:62M:`` balance check is computed by code; a mismatch is reported,
never corrected.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from ..ledger import bank, money, store, textio
from ..timeinfo import TimeCandidate
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, bound_json, load_data, local_candidate,
                          record_import_run, stored_path)

PARSER_NAME = 'mt940'
PARSER_VERSION = '1'
_FIELD = re.compile(r'^:(\d{2}[A-Z]?):(.*)$')
_BALANCE = re.compile(r'^(?P<mark>[CD])(?P<date>\d{6})(?P<ccy>[A-Z]{3})(?P<amount>\d{1,15},\d*)\s*$')
_LINE61 = re.compile(r'^(?P<vd>\d{6})(?P<ed>\d{4})?(?P<mark>RC|RD|C|D)(?P<fc>[A-Z])?(?P<amount>\d{1,15},\d*)'
                     r'(?P<type>[NFS][A-Z0-9]{3})(?P<cref>[^\n]*?)(?://(?P<bref>[^\n]*))?(?:\n(?P<supp>.*))?$', re.S)
SIGN = {'C': 1, 'D': -1, 'RC': -1, 'RD': 1}
ENTRY_DATE_MAX_DISTANCE_DAYS = 92
ENTRY_DATE_RIVAL_MARGIN_DAYS = 31


def swift_amount(text: str) -> str | None:
    match = re.fullmatch(r'(\d{1,15}),(\d*)', text.strip())
    if not match:
        return None
    integer, fraction = match.groups()
    integer = integer.lstrip('0') or '0'
    return integer + ('.' + fraction if fraction else '')


def _yymmdd(text: str, base: int) -> date | None:
    try:
        return date(base + int(text[:2]), int(text[2:4]), int(text[4:6]))
    except ValueError:
        return None


def infer_entry_date(value: date, mmdd: str) -> dict:
    """Year of an ``MMDD`` entry date relative to the value date (see module docstring)."""
    month, day = int(mmdd[:2]), int(mmdd[2:])
    options = []
    for year in (value.year - 1, value.year, value.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        options.append((abs((candidate - value).days), candidate))
    if not options:
        return {'status': 'invalid_calendar_value', 'raw': mmdd}
    options.sort()
    distance, best = options[0]
    out = {'status': 'ok', 'raw': mmdd, 'rule': 'nearest_year_to_value_date', 'readings': [best.isoformat()],
           'offset_days': (best - value).days,
           'alternatives': [{'date': d.isoformat(), 'offset_days': (d - value).days} for _, d in options]}
    if best.year != value.year:
        out['year_boundary'] = f'entry date year {best.year} differs from value date year {value.year}'
    rival = options[1] if len(options) > 1 else None
    if distance > ENTRY_DATE_MAX_DISTANCE_DAYS or (rival and rival[0] - distance <= ENTRY_DATE_RIVAL_MARGIN_DAYS):
        out['ambiguous'] = True
        out['readings'] = [d.isoformat() for _, d in options[:2]]
    return out


def parse_details(text: str | None, hints: dict) -> dict:
    """Structure observation of a ``:86:`` text; the raw text is the authority."""
    if text is None:
        return {'structure': 'absent'}
    compact = text.replace('\r', '').replace('\n', '')
    for separator in hints.get('subfield_separators', []):
        parts = re.split(re.escape(separator) + r'(\d{2})', compact)
        if len(parts) >= 3:
            subfields = list(zip(parts[1::2], parts[2::2]))
            mapped: dict[str, str] = {}
            for code, value in subfields:
                label = hints['subfield_codes'].get(code)
                if label:
                    mapped[label] = mapped.get(label, '') + value
            return {'structure': f'numbered_subfields:{separator}', 'prefix': parts[0] or None,
                    'subfields': [[c, v] for c, v in subfields], 'mapped': mapped}
    codes = hints.get('slash_codes', {})
    if codes:
        pattern = re.compile(r'/(' + '|'.join(re.escape(c) for c in sorted(codes, key=len, reverse=True)) + r')/')
        found = list(pattern.finditer(compact))
        if found:
            mapped = {}
            pairs = []
            for index, match in enumerate(found):
                end = found[index + 1].start() if index + 1 < len(found) else len(compact)
                value = compact[match.end():end]
                pairs.append([match.group(1), value])
                mapped[codes[match.group(1)]] = (mapped.get(codes[match.group(1)], '') + value)
            return {'structure': 'slash_codes', 'prefix': compact[:found[0].start()] or None, 'subfields': pairs,
                    'mapped': mapped}
    return {'structure': 'free_text', 'lines': text.split('\n')}


def _messages(text: str, issues: Issues) -> list[dict]:
    messages: list[dict] = []
    current: dict | None = None
    for number, line in enumerate(text.split('\n'), start=1):
        line = line.rstrip('\r')
        if '{4:' in line:
            line = line.split('{4:', 1)[1]
        stripped = line.strip()
        if stripped in ('-}', '-', '}') or stripped.startswith('{5:'):
            current = None
            continue
        if not stripped and current is None:
            continue
        match = _FIELD.match(line)
        if match:
            tag, value = match.groups()
            if current is None or (tag == '20' and any(f['tag'][:2] in ('61', '62') for f in current['fields'])):
                current = {'index': len(messages), 'fields': [], 'first_line': number}
                messages.append(current)
            current['fields'].append({'tag': tag, 'value': value, 'line': number})
        elif current is not None and current['fields']:
            current['fields'][-1]['value'] += '\n' + line
        elif stripped:
            issues.add('text_outside_fields', line=number)
    return messages


def _balance(field: dict | None, base: int, problems: list[str]) -> dict | None:
    if field is None:
        return None
    match = _BALANCE.match(field['value'].strip())
    if not match:
        problems.append(f"balance_{field['tag']}_unparsed")
        return {'raw': field['value'], 'tag': field['tag'], 'exact': None, 'status': 'unparsed'}
    day = _yymmdd(match.group('date'), base)
    stated = bank.balance(match.group('mark'), swift_amount(match.group('amount')), match.group('ccy'),
                          date=day.isoformat() if day else None, kind=field['tag'], raw=field['value'])
    if day is None:
        problems.append(f"balance_{field['tag']}_date_invalid")
    return {**stated, 'line': field['line']}


def import_mt940(path: Path, *, two_digit_year_base: int = 2000, encoding: str | None = None,
                 source_locator: str | None = None, original_name: str | None = None,
                 collection_id: int | None = None, label: str | None = None,
                 limits: CommLimits = DEFAULT_LIMITS) -> dict:
    if isinstance(two_digit_year_base, bool) or not isinstance(two_digit_year_base, int) or \
            two_digit_year_base % 100 or not 1900 <= two_digit_year_base <= 2100:
        raise ValueError('two_digit_year_base must be a century such as 1900 or 2000')
    locator = source_locator if source_locator is not None else str(path)
    options = {'two_digit_year_base': two_digit_year_base, 'encoding': encoding or None}
    source_id, artifact_id = acquire(
        path, source_kind='bank_statement', source_label=label or 'Bank statement (MT940)', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name, collection_id=collection_id,
        options=options)
    issues = Issues(limits.max_issues)
    raw = stored_path(artifact_id).read_bytes()
    if len(raw) > limits.max_json_bytes:
        raise ValueError(f'MT940 file larger than {limits.max_json_bytes} bytes')
    text, encoding_info = textio.decode(raw, encoding or 'auto')
    hints = load_data('mt940_details.v1.json')
    reading = {'two_digit_year_base': two_digit_year_base, 'encoding': encoding_info['used'],
               'details_hints': f"{hints['schema']}/v{hints['version']}"}
    reading_key = store.sha256_of(reading)[:12]
    messages = _messages(text, issues)
    if not any(f['tag'] == '61' or f['tag'].startswith('60') for m in messages for f in m['fields']):
        raise ValueError('no MT940 statement fields (:60F:/:61:) found')
    counts = {'statement_count': 0, 'entries': 0, 'events': 0, 'balance_mismatches': 0, 'ambiguous_entry_dates': 0,
              'year_boundary_entry_dates': 0}
    statements = []
    complete = True
    total = 0
    for message in messages:
        result = _statement(message, artifact_id=artifact_id, source_id=source_id, base=two_digit_year_base,
                            hints=hints, reading_key=reading_key, issues=issues, counts=counts, limits=limits,
                            remaining=limits.max_records - total)
        total += result.pop('_entries')
        if result.pop('_limited'):
            complete = False
        statements.append(result)
    coverage = {'complete': complete}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION,
            'reading_key': reading_key, 'encoding': encoding_info, **counts, 'statements': statements,
            'coverage': coverage, 'issues': issues.as_dict()}


def _statement(message: dict, *, artifact_id: int, source_id: int, base: int, hints: dict, reading_key: str,
               issues: Issues, counts: dict, limits: CommLimits, remaining: int) -> dict:
    fields = message['fields']
    problems: list[str] = []
    first = {}
    for field in fields:
        first.setdefault(field['tag'], field)
    account = first.get('25', {}).get('value', '').strip() or None
    opening = _balance(first.get('60F') or first.get('60M'), base, problems)
    closing = _balance(first.get('62F') or first.get('62M'), base, problems)
    currency = (opening or {}).get('currency') or (closing or {}).get('currency')
    records = []
    statement_info = []
    limited = False
    index = 0
    for position, field in enumerate(fields):
        if field['tag'] == '86' and (position == 0 or fields[position - 1]['tag'] != '61'):
            statement_info.append(field['value'])
        if field['tag'] != '61':
            continue
        if len(records) >= remaining:
            limited = True
            issues.add('record_limit', limit=limits.max_records)
            break
        details_field = fields[position + 1] if position + 1 < len(fields) and fields[position + 1]['tag'] == '86' else None
        records.append(_line(field, details_field, message, index, account, currency, base, hints, reading_key,
                             artifact_id, source_id, counts, limits))
        index += 1
    if opening is None:
        problems.append('opening_balance_missing')
    if closing is None:
        problems.append('closing_balance_missing')
    status, detail = bank.balance_check(opening, closing, [r['movement'] for r in records])
    occurrences: dict[str, int] = {}
    for record in records:
        key = record['entries'][0]['content_key']
        occurrences[key] = occurrences.get(key, 0) + 1
        record['entries'][0]['content_occurrence'] = occurrences[key]
        record['event']['metadata']['content_occurrence'] = occurrences[key]
        for problem in record['problems']:
            issues.add(problem, position=record['position'])
    if status == 'mismatch':
        counts['balance_mismatches'] += 1
        issues.add('balance_mismatch', statement=message['index'], difference=detail['difference'])
    for problem in problems:
        issues.add(problem, statement=message['index'])
    counts['statement_count'] += 1
    counts['entries'] += len(records)
    counts['events'] += len(records)
    raw_fields, truncated = bound_json([[f['tag'], f['value']] for f in fields if f['tag'] not in ('61', '86')], limits)
    row = {'artifact_id': artifact_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION,
           'reading_key': reading_key, 'statement_key': f"msg[{message['index']}]", 'account_raw': account,
           'currency': currency, 'opening_json': opening or {}, 'closing_json': closing or {},
           'entry_count': len(records), 'balance_check': status, 'balance_check_json': detail,
           'locator_json': {'message': message['index'], 'first_line': message['first_line']},
           'metadata_json': {'reference_20': first.get('20', {}).get('value'), 'related_21': first.get('21', {}).get('value'),
                             'statement_number_28c': (first.get('28C') or first.get('28') or {}).get('value'),
                             'closing_available_64': (first.get('64') or {}).get('value'),
                             'forward_available_65': [f['value'] for f in fields if f['tag'] == '65'],
                             'statement_info_86': statement_info, 'raw_fields': raw_fields,
                             'raw_truncated_values': truncated, 'issues': problems}}
    statement_id = bank.write_statement(row, records)
    return {'statement_id': statement_id, 'index': message['index'], 'account': account, 'entries': len(records),
            'balance_check': status, 'balance': {k: detail.get(k) for k in ('opening', 'sum_of_entries', 'computed_closing',
                                                                             'stated_closing', 'difference', 'reason')
                                                 if k in detail},
            '_entries': len(records), '_limited': limited}


def _line(field: dict, details_field: dict | None, message: dict, index: int, account: str | None, currency: str | None,
          base: int, hints: dict, reading_key: str, artifact_id: int, source_id: int, counts: dict,
          limits: CommLimits) -> dict:
    problems: list[str] = []
    source = f"mt940:msg{message['index']}"
    match = _LINE61.match(field['value'].strip('\n'))
    position = f"msg[{message['index']}]/61[{index}]"
    candidates: list[TimeCandidate] = []
    exact = mark = None
    parsed: dict = {'raw': field['value']}
    entry_date = None
    if not match:
        problems.append('statement_line_61_unparsed')
    else:
        g = match.groupdict()
        mark = g['mark']
        unsigned = swift_amount(g['amount'])
        exact = unsigned if SIGN[mark] > 0 else money.negate(unsigned)
        parsed.update(value_date_raw=g['vd'], entry_date_raw=g['ed'], mark=mark, funds_code=g['fc'],
                      amount_raw=g['amount'], transaction_type=g['type'], customer_reference=g['cref'] or None,
                      bank_reference=g['bref'], supplementary=g['supp'])
        if g['fc'] and currency and g['fc'] != currency[2]:
            problems.append('funds_code_differs_from_statement_currency')
        value = _yymmdd(g['vd'], base)
        if value is None:
            problems.append('value_date_invalid')
        else:
            candidates.append(local_candidate('other', g['vd'], f'{source}:value_date', datetime.combine(value, datetime.min.time()),
                                              'day', {'date_kind': 'value_date', 'year_rule': f'two_digit_year_base:{base}',
                                                      'zone_status': 'export_zone_not_stated'}))
            if g['ed']:
                entry_date = infer_entry_date(value, g['ed'])
                if entry_date['status'] != 'ok':
                    problems.append('entry_date_invalid')
                else:
                    if entry_date.get('year_boundary'):
                        problems.append('entry_date_year_inferred_across_year_boundary')
                        counts['year_boundary_entry_dates'] += 1
                    if entry_date.get('ambiguous'):
                        problems.append('entry_date_year_ambiguous_both_readings_recorded')
                        counts['ambiguous_entry_dates'] += 1
                    several = len(entry_date['readings']) > 1
                    for reading in entry_date['readings']:
                        day = date.fromisoformat(reading)
                        suffix = f':year_{day.year}' if several else ''
                        candidates.append(local_candidate(
                            'created', g['ed'], f'{source}:booking_date{suffix}', datetime.combine(day, datetime.min.time()),
                            'day', {'date_kind': 'booking_date', 'year_rule': entry_date['rule'],
                                    'relative_to_value_date': value.isoformat(),
                                    'zone_status': 'export_zone_not_stated',
                                    **({'year_boundary': entry_date['year_boundary']} if entry_date.get('year_boundary') else {}),
                                    **({'ambiguous': True} if several else {})}))
    details_raw = details_field['value'] if details_field else None
    details = parse_details(details_raw, hints)
    mapped = details.get('mapped', {})
    counterparty_name = mapped.get('counterparty_name') or mapped.get('counterparty') or None
    counterparty_account = mapped.get('counterparty_iban') or mapped.get('counterparty_account') or None
    if details['structure'] == 'free_text':
        description = ' '.join(line.strip() for line in details['lines'] if line.strip()) or None
    else:
        description = mapped.get('remittance') or None
    code = currency
    minor = money.minor_units(exact, code)
    if exact is not None and code and minor['status'] != 'ok':
        problems.append(minor['status'])
    flow = None if exact is None or money.Decimal(exact) == 0 else ('debit' if money.is_negative(exact) else 'credit')
    amount = {'raw': parsed.get('amount_raw'), 'mark': mark, 'exact': exact, 'currency': code,
              'currency_basis': 'statement_balance_currency' if code else None, 'minor': minor, 'flow': flow,
              'reversal': mark in ('RC', 'RD') if mark else None, 'sign_convention': 'holder_signed_negative_is_money_out'}
    content_key = store.sha256_of({'kind': 'mt940', 'line': parsed.get('value_date_raw'), 'entry': parsed.get('entry_date_raw'),
                                   'amount': [exact, code], 'type': parsed.get('transaction_type'),
                                   'refs': [parsed.get('customer_reference'), parsed.get('bank_reference')],
                                   'details': details_raw})
    raw61, truncated = bound_json(field['value'], limits)
    raw86, truncated86 = bound_json(details_raw, limits)
    metadata = {
        'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION}, 'reading_key': reading_key,
        'locator': {'message': message['index'], 'line_61': field['line'],
                    'line_86': details_field['line'] if details_field else None, 'entry': index},
        'raw_61': raw61, 'raw_86': raw86, 'raw_truncated_values': truncated + truncated86, 'statement_line': parsed,
        'entry_date': entry_date, 'details': details, 'amount': amount,
        'signal': store.signal('active', 'raw_sample', 'swift_mt940', account), 'issues': problems,
        'content_key': content_key,
    }
    if counterparty_account:
        metadata['counterparty_account_check'] = store.iban_check(counterparty_account)
    entry = {'artifact_id': artifact_id, 'record_kind': 'financial_transaction', 'parser': PARSER_NAME,
             'parser_version': PARSER_VERSION, 'profile_id': None, 'reading_key': reading_key,
             'source_position': position, 'amount_exact': exact, 'currency': code, 'amount_minor': minor['minor'],
             'currency_exponent': minor['exponent'], 'flow': flow, 'counterparty_name': counterparty_name,
             'counterparty_account': counterparty_account, 'description': description, 'duration_s': None,
             'content_key': content_key, 'content_occurrence': 0,
             'metadata_json': {'locator': metadata['locator'], 'amount': amount, 'details_structure': details['structure']}}
    event = {'source_id': source_id, 'artifact_id': artifact_id, 'event_type': 'financial_transaction',
             'ts_start': None, 'ts_end': None, 'direction': flow, 'contact_label': counterparty_name,
             'phone_or_address': counterparty_account, 'subject': parsed.get('transaction_type'), 'body': description,
             'external_id': f'{PARSER_NAME}:{reading_key}:{position}', 'confidence': None, 'metadata': metadata}
    return {'position': position, 'problems': problems, 'candidates': candidates, 'event': event, 'entries': [entry],
            'movement': {'exact': exact, 'currency': code, 'booked': True}}

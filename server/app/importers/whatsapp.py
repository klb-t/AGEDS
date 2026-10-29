from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ..db import session
from ..evidence import add_event, ensure_source, ingest_file

PARSER_NAME = 'whatsapp_txt'
# '3' (2026-10-30): the date order is read from the file itself. Every header of one export uses
# one date format; a first field above 12 confirms day-first, a second field above 12 shows
# month-first (e.g. US ``m/d/yy``). Without such a line the day-first policy stays and the
# ambiguity is flagged; with both, the dates stay unparsed. Version 2 always applied day-first,
# so valid US dates were reported as ``invalid``.
PARSER_VERSION = '3'
# Both Android "date, time - sender" and iOS "[date, time] sender".
LINE = re.compile(
    r'^[\u200e\u200f]*(?P<open>\[)?'
    r'(?P<date>\d{1,2}[./-]\d{1,2}[./-]\d{2,4}),?\s+'
    r'(?P<time>\d{1,2}:\d{2}(?::\d{2})?)'
    r'(?:[\s\u202f]*(?P<ampm>[aApP]\.?[mM]\.?))?'
    r'[\u200e\u200f]*(?P<close>\])?\s*(?P<separator>[-–])?\s*(?P<rest>.*)$'
)
MSG = re.compile(r'^(?P<sender>[^:]{1,120}):\s(?P<body>.*)$')


POLICY_ORDER = ('day_first', 'parser_policy_not_source_locale')


def date_order(dates: list[str]) -> tuple[str | None, str]:
    """(order, status) from the date fields of all headers of one file; never from today or a locale."""
    first_high = second_high = False
    for date in dates:
        parts = re.split(r'[./-]', date)
        if len(parts) == 3:
            first_high |= int(parts[0]) > 12
            second_high |= int(parts[1]) > 12
    if first_high and second_high:
        return None, 'conflicting_orders_in_file'
    if second_high:
        return 'month_first', 'inferred_from_file_second_field_above_12'
    if first_high:
        return 'day_first', 'inferred_from_file_first_field_above_12'
    return POLICY_ORDER


def _timestamp(date: str, time: str, ampm: str | None,
               order: tuple[str | None, str] = POLICY_ORDER) -> tuple[str | None, dict]:
    """Use the file's date order (``date_order``); never consult today's date or local zone."""
    metadata = {
        'raw_date': date, 'raw_time': time, 'raw_ampm': ampm,
        'date_order': order[0], 'date_order_status': order[1],
        'timezone': None, 'timezone_status': 'unknown', 'status': 'invalid',
    }
    if order[0] is None:
        metadata['status'] = 'date_order_conflict'
        return None, metadata
    try:
        day, month, raw_year = re.split(r'[./-]', date)
        if order[0] == 'month_first':
            day, month = month, day
        year = int(raw_year)
        if len(raw_year) not in {2, 4}:
            raise ValueError('Unsupported year width')
        clock = [int(part) for part in time.split(':')]
        hour, minute = clock[:2]
        second = clock[2] if len(clock) == 3 else 0
        if ampm:
            if not 1 <= hour <= 12:
                raise ValueError('Invalid 12-hour time')
            marker = ampm.replace('.', '').upper()
            hour = hour % 12 + (12 if marker == 'PM' else 0)
        if len(raw_year) == 2:
            # Validate the independent components without inventing a century.
            # Year 2000 allows every possibly valid day/month (including Feb 29).
            datetime(2000, int(month), int(day), hour, minute, second)
            metadata['status'] = 'unknown_century'
            metadata['two_digit_year_policy'] = 'century_not_assumed'
            return None, metadata
        ts = datetime(year, int(month), int(day), hour, minute, second).isoformat()
        metadata['status'] = 'parsed_local_time'
        metadata['date_order_ambiguous'] = (order == POLICY_ORDER and int(day) <= 12 and int(month) <= 12
                                            and int(day) != int(month))
        return ts, metadata
    except (ValueError, OverflowError):
        return None, metadata


def _stored_path(artifact_id: int) -> Path:
    with session() as db:
        row = db.execute('SELECT stored_path FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
        if not row or not row['stored_path']:
            raise ValueError('Imported artifact has no stored snapshot')
        return Path(row['stored_path'])


def import_whatsapp_txt(
    path: Path, chat_label: str | None = None, *,
    source_locator: str | None = None, original_name: str | None = None,
) -> dict:
    locator = source_locator if source_locator is not None else str(path)
    label = chat_label or Path(original_name or path.name).stem
    source_id = ensure_source('whatsapp_export', label, locator)
    artifact_id = ingest_file(
        path, source_id=source_id, source_locator=locator, original_name=original_name,
        metadata={'importer': PARSER_NAME, 'parser_version': PARSER_VERSION},
    )
    current = None
    count = 0
    issues = []

    def flush(msg):
        nonlocal count
        if msg is None:
            return
        timestamp = msg['timestamp']
        row_issues = ['timezone_unknown']
        if msg['ts'] is None:
            row_issues.append(f"timestamp_{timestamp['status']}")
        if timestamp.get('date_order_ambiguous'):
            row_issues.append('date_order_ambiguous')
        add_event(
            source_id=source_id, artifact_id=artifact_id, event_type='whatsapp_message',
            ts_start=msg['ts'], sender=msg['sender'], thread_id=label, body=msg['body'],
            external_id=f"{PARSER_NAME}:line:{msg['line_start']}", confidence=None,
            metadata={
                'raw_header': msg['header'], 'raw_lines': msg['raw_lines'],
                'parser': {'name': PARSER_NAME, 'version': version,
                           'line_start': msg['line_start'], 'line_end': msg['line_end']},
                'timestamp': timestamp, 'issues': row_issues,
            },
        )
        count += 1
        issues.append({'line': msg['line_start'], 'issues': row_issues})

    # Invalid UTF-8 is retained in the original artifact and reported here.
    content_bytes = _stored_path(artifact_id).read_bytes()
    try:
        content = content_bytes.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = content_bytes.decode('utf-8-sig', errors='replace')
        issues.append({'issues': ['invalid_utf8_replacement_in_parsed_text']})
    lines = content.splitlines()

    def header(raw):
        match = LINE.match(raw)
        if match and not (match.group('separator') or (match.group('open') and match.group('close'))):
            return None
        return match

    with session() as db:
        earlier = db.execute("SELECT 1 FROM events WHERE artifact_id=? AND source_id=? AND event_type='whatsapp_message' "
                             "AND json_extract(metadata_json,'$.parser.version')='2' LIMIT 1",
                             (artifact_id, source_id)).fetchone()
    # A chat already imported by version 2 keeps version 2, so re-importing it stays idempotent.
    version = '2' if earlier else PARSER_VERSION
    order = POLICY_ORDER if earlier else date_order([m.group('date') for m in map(header, lines) if m])
    for line_number, raw in enumerate(lines, start=1):
        match = header(raw)
        if match:
            flush(current)
            rest = match.group('rest')
            message = MSG.match(rest)
            ts, timestamp = _timestamp(match.group('date'), match.group('time'), match.group('ampm'), order)
            current = {
                'ts': ts, 'timestamp': timestamp,
                'sender': message.group('sender') if message else None,
                'body': message.group('body') if message else rest,
                'header': raw, 'raw_lines': [raw],
                'line_start': line_number, 'line_end': line_number,
            }
        elif current is not None:
            current['body'] += '\n' + raw
            current['raw_lines'].append(raw)
            current['line_end'] = line_number
        else:
            issues.append({'line': line_number, 'issues': ['unparsed_line_before_first_message'], 'raw': raw})
    flush(current)
    return {'artifact_id': artifact_id, 'messages': count, 'parser_version': version, 'issues': issues,
            'date_order': {'order': order[0], 'status': order[1]}}

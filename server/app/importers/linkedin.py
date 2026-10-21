"""LinkedIn ``messages.csv`` (data download) — traces area.

Columns are matched through ``data/linkedin.v1.json``. Each row → a
``chat_message`` in thread ``linkedin:<CONVERSATION ID>``. ``DATE`` such as
``2023-03-01 10:00:00 UTC`` fixes the instant; a value without zone stays
``unknown``. ``CONTENT`` may hold HTML: the body is a text projection (the raw
cell is kept, bounded, and HTML is never rendered). LinkedIn rows carry no
message id: the record id is derived from (conversation, sender profile, date,
content) — basis ``content_derived_linkedin_v1``. Direction needs ``owner_name``.
Rows are read with Python's ``csv`` module (quoted multi-line cells), bounded by
``max_records`` and ``max_line_bytes`` per cell.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..traces import messaging
from ..traces.common import TextLayers, mapping, signal
from .comm_common import DEFAULT_LIMITS, CommLimits, Issues, local_candidate, offset_candidate
from .email_mbox import html_to_text

PARSER_NAME = 'linkedin_messages'
PARSER_VERSION = '1'
MAPPING = 'linkedin.v1.json'


def load_csv(path: Path, limits: CommLimits, issues: Issues):
    size = path.stat().st_size
    if size > limits.max_json_bytes:
        issues.add('document_size_limit', size_bytes=size, limit=limits.max_json_bytes)
        raise ValueError('CSV larger than max_json_bytes')
    data = path.read_bytes()
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('utf-8', errors='replace')
        issues.add('invalid_utf8_replaced')
    csv.field_size_limit(max(limits.max_line_bytes, 131072))
    reader = csv.reader(io.StringIO(text, newline=''))
    try:
        rows = []
        for row in reader:
            rows.append((reader.line_num, row))
            if len(rows) > limits.max_records + 1:
                issues.add('record_limit', limit=limits.max_records)
                break
    except csv.Error as exc:
        raise ValueError(f'CSV not readable: {exc}') from exc
    return rows


def _date(raw: str, source: str, config: dict):
    match = re.match(config['date_regex'], raw.strip())
    if not match:
        return None
    parts = match.groupdict()
    try:
        naive = datetime(int(parts['y']), int(parts['mo']), int(parts['d']), int(parts['h']), int(parts['mi']),
                         int(parts['s'] or 0))
    except ValueError:
        return None
    precision = 'second' if parts['s'] else 'minute'
    zone = parts['zone']
    if zone in ('UTC', 'GMT', 'Z'):
        return offset_candidate('created', raw, source, naive, 0, precision, {'field': 'DATE', 'zone_text': zone})
    if zone:
        digits = zone[1:].replace(':', '')
        minutes = int(digits[:2]) * 60 + int(digits[2:])
        return offset_candidate('created', raw, source, naive, -minutes if zone[0] == '-' else minutes, precision,
                                {'field': 'DATE'})
    return local_candidate('created', raw, source, naive, precision, {'field': 'DATE', 'zone_status': 'zone_missing'})


def parse(rows, ctx: messaging.FileContext) -> list[messaging.Thread]:
    config, _ = mapping(MAPPING)
    if not rows:
        raise ValueError('empty CSV')
    header = [cell.strip().upper() for cell in rows[0][1]]
    columns = {}
    for field_name, aliases in config['columns'].items():
        for alias in aliases:
            if alias in header:
                columns[field_name] = header.index(alias)
                break
    missing = [f for f in config['required'] if f not in columns]
    if missing:
        raise ValueError(f'not a LinkedIn messages.csv: missing columns {missing}')
    threads: dict[str, list] = {}
    titles: dict[str, str | None] = {}
    seen = Counter()
    for position, (line, row) in enumerate(rows[1:ctx.limits.max_records + 1], start=1):
        def cell(name):
            index = columns.get(name)
            return row[index] if index is not None and index < len(row) else None
        problems = []
        if len(row) != len(header):
            problems.append('row_width_differs_from_header')
        layers = TextLayers(None, ctx.limits)
        conversation = cell('conversation_id') or messaging.derived_record_id('linkedin_row', position)
        titles.setdefault(conversation, cell('conversation_title'))
        raw_date = cell('date') or ''
        candidate = _date(raw_date, ctx.time_source('DATE'), config) if raw_date else None
        if candidate is None:
            problems.append('time_unparsed:DATE')
        elif candidate.tz_status == 'unknown':
            problems.append('time_zone_not_fixed:DATE')
        content = cell('content') or ''
        is_html = bool(re.search(r'<[a-zA-Z][^>]*>', content))
        body = layers.text('content', html_to_text(content) if is_html else content)
        base = messaging.derived_record_id(conversation, cell('sender_profile') or cell('from'), raw_date, content)
        seen[base] += 1
        record_id = base if seen[base] == 1 else f'{base}~dup{seen[base] - 1}'
        sender = layers.text('from', cell('from'))
        direction, basis = ctx.owner.direction(sender_name=sender)
        chat = {'folder': cell('folder'), 'subject': cell('subject'), 'content_was_html': is_html,
                'html_rendering': 'never_rendered_as_trusted_markup' if is_html else None,
                'draft': cell('draft'), 'attachments_raw': cell('attachments')}
        threads.setdefault(conversation, []).append(messaging.Message(
            key=f'{conversation}/row{line}', record_id=record_id, record_id_basis='content_derived_linkedin_v1',
            body=body, times=[candidate] if candidate else [], content_kind='text' if body else 'empty', chat=chat,
            raw=dict(zip(header, row)), text_layers=layers.as_dict(),
            signal=signal('active', 'raw_sample', 'linkedin', 'account'), sender=sender,
            sender_id=cell('sender_profile'), recipients=layers.text('to', cell('to')), direction=direction,
            direction_basis=basis, subject=layers.text('subject', cell('subject')), issues=problems,
            locator={'csv_line': line, 'row_index': position}))
        for problem in problems:
            ctx.issues.add(problem, line=line)
    return [messaging.Thread(cid, titles.get(cid), [], items, {}) for cid, items in threads.items()]


def import_messages_csv(path: Path, *, owner_name: str | None = None, export_label: str | None = None,
                        source_locator: str | None = None, original_name: str | None = None,
                        collection_id: int | None = None, label: str | None = None,
                        limits: CommLimits = DEFAULT_LIMITS) -> dict:
    config, sha = mapping(MAPPING)
    path = Path(path)
    if path.is_dir():
        candidates = [p for p in sorted(path.rglob('messages.csv')) if p.is_file() and not p.is_symlink()]
        if len(candidates) != 1:
            raise ValueError(f'expected exactly one messages.csv in the folder, found {len(candidates)}')
        relative = candidates[0].relative_to(path).as_posix()
        source_locator = f'{source_locator}/{relative}' if source_locator else None
        path = candidates[0]
    files = [messaging.single_input(path, source_locator=source_locator, original_name=original_name)]
    return messaging.import_files(files, namespace='linkedin', platform='linkedin', parser=PARSER_NAME,
                                  parser_version=PARSER_VERSION, mapping_version=config['version'], mapping_sha256=sha,
                                  parse=parse, load=load_csv, owner=messaging.Owner(name=owner_name,
                                                                                    basis='owner_name_parameter'),
                                  resolver=messaging.no_files_resolver('linkedin_exports_no_attachment_files'),
                                  export_label=export_label, collection_id=collection_id,
                                  label=label or 'LinkedIn data download', limits=limits,
                                  options={'owner_name_given': owner_name is not None, 'export_label': export_label},
                                  mime_type='text/csv')

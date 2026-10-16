"""Google Takeout "My Activity" (JSON and HTML variants) → ``activity`` events.

Covers any product folder, including Gemini Apps. JSON ``time`` is ISO-8601
with ``Z`` (explicit UTC). The HTML variant prints a localized display time with
a zone *abbreviation* (e.g. ``CET``): that abbreviation is kept raw and the
candidate stays ``unknown`` unless it is a numeric ``GMT±hh:mm``/``UTC``.

Records of browsing-like products (Chrome, YouTube; see the data file) are
skipped unless ``enable_browser_history`` is set; skipped records are counted
and the opt-in is recorded with each run and event.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from ..timeinfo import TimeCandidate, from_iso
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, add_event_with_times, bound_json,
                          bounded_text, content_id, legacy_instant, load_data, local_candidate, offset_candidate,
                          record_import_run, stored_path)
from .email_mbox import html_to_text

PARSER_NAME = 'takeout_my_activity'
PARSER_VERSION = '1'
_SPACES = re.compile(r'[   \s]+')
_NUMERIC_ZONE = re.compile(r'^(?:GMT|UTC)(?:([+-])(\d{1,2})(?::?(\d{2}))?)?$', re.IGNORECASE)


def settings() -> dict:
    data = load_data('takeout_activity.v1.json')
    data['compiled'] = [(p['id'], re.compile(p['regex'])) for p in data['html_date_patterns']]
    return data


def parse_display_time(text: str, config: dict, source: str) -> tuple[TimeCandidate | None, dict]:
    """Localized HTML display time; zone abbreviations are not converted."""
    clean = _SPACES.sub(' ', text).strip()
    meta: dict = {'raw_display': text}
    for pattern_id, regex in config['compiled']:
        match = regex.match(clean)
        if not match:
            continue
        parts = match.groupdict()
        month = config['month_names'].get(parts['mon'].lower().rstrip('.'))
        if month is None:
            meta['status'] = 'unknown_month_name'
            meta['month_raw'] = parts['mon']
            continue
        hour = int(parts['h'])
        if parts.get('ampm'):
            if not 1 <= hour <= 12:
                meta['status'] = 'invalid_12_hour_value'
                return None, meta
            hour = hour % 12 + (12 if parts['ampm'][0].lower() == 'p' else 0)
        try:
            naive = datetime(int(parts['year']), month, int(parts['day']), hour, int(parts['m']), int(parts['s'] or 0))
        except ValueError:
            meta['status'] = 'invalid_calendar_value'
            return None, meta
        meta.update(status='parsed', pattern=pattern_id)
        precision = 'second' if parts['s'] else 'minute'
        zone = (parts.get('zone') or '').strip()
        numeric = _NUMERIC_ZONE.match(zone) if zone else None
        if numeric:
            sign, hours, minutes = numeric.groups()
            offset = 0 if sign is None else (int(hours) * 60 + int(minutes or 0)) * (1 if sign == '+' else -1)
            meta['zone_status'] = 'numeric_zone_in_display_text'
            return offset_candidate('created', text, source, naive, offset, precision, meta), meta
        meta['zone_status'] = 'abbreviation_not_converted' if zone else 'zone_missing'
        meta['zone_abbreviation'] = zone or None
        return local_candidate('created', text, source, naive, precision, meta), meta
    meta.setdefault('status', 'unparsed')
    return None, meta


class _ActivityHTML(HTMLParser):
    """Collect Takeout outer-cell records: header, content lines, links, caption."""

    def __init__(self, max_records: int):
        super().__init__(convert_charrefs=True)
        self.records: list[dict] = []
        self.max_records = max_records
        self.overflow = False
        self._depth = 0
        self._cell: list[tuple[int, str]] = []  # (depth, role)
        self._current: dict | None = None
        self._outer_depth = None
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        if tag == 'div':
            self._depth += 1
            classes = (dict(attrs).get('class') or '').split()
            if 'outer-cell' in classes:
                if len(self.records) >= self.max_records:
                    self.overflow = True
                    self._current = None
                else:
                    self._current = {'header': [], 'content': [[]], 'links': [], 'caption': [], 'line': self.getpos()[0]}
                    self.records.append(self._current)
                self._outer_depth = self._depth
            elif self._current is not None:
                if 'header-cell' in classes:
                    self._cell.append((self._depth, 'header'))
                elif 'content-cell' in classes and 'mdl-typography--caption' in classes:
                    self._cell.append((self._depth, 'caption'))
                elif 'content-cell' in classes and 'mdl-typography--text-right' not in classes:
                    self._cell.append((self._depth, 'content'))
                else:
                    self._cell.append((self._depth, 'other'))
        elif tag == 'br' and self._current is not None and self._cell:
            role = self._cell[-1][1]
            if role == 'content':
                self._current['content'].append([])
            elif role in ('caption', 'header'):
                self._current[role].append('\n')
        elif tag == 'a' and self._current is not None and self._cell and self._cell[-1][1] == 'content':
            self._current['links'].append({'href': dict(attrs).get('href'), 'text': ''})
            self._in_link = True

    def handle_endtag(self, tag):
        if tag == 'div':
            if self._cell and self._cell[-1][0] == self._depth:
                self._cell.pop()
            if self._outer_depth == self._depth:
                self._current = None
                self._outer_depth = None
            self._depth -= 1
        elif tag == 'a':
            self._in_link = False

    def handle_data(self, data):
        if self._current is None or not self._cell:
            return
        role = self._cell[-1][1]
        if role == 'content':
            self._current['content'][-1].append(data)
            if self._in_link and self._current['links']:
                self._current['links'][-1]['text'] += data
        elif role in ('caption', 'header'):
            self._current[role].append(data)


def _is_browser_like(record: dict, config: dict) -> bool:
    products = set(record.get('products') or []) | {record.get('header')}
    return bool(products & set(config['browser_history_products']))


def _event(record: dict, *, index: int, variant: str, candidate, time_meta, opt_in: dict, source_id: int,
           artifact_id: int, config: dict, limits: CommLimits, issues: Issues, line: int | None = None) -> int:
    raw, truncated = bound_json(record, limits)
    problems = []
    if candidate is None:
        problems.append('time_unparsed' if record.get('time') or time_meta else 'time_missing')
    body_parts = [record.get('title') or '']
    body_parts += [s.get('name', '') for s in record.get('subtitles') or [] if isinstance(s, dict)]
    body_parts += [d.get('name', '') for d in record.get('details') or [] if isinstance(d, dict)]
    response_text = None
    for item in record.get('safeHtmlItem') or []:
        if isinstance(item, dict) and isinstance(item.get('html'), str):
            response_text = (response_text or '') + html_to_text(item['html']) + '\n'
    if response_text:
        body_parts.append(response_text)
        problems.append('response_html_kept_as_data_text_projection_in_body')
    body, complete = bounded_text('\n'.join(p for p in body_parts if p).strip() or None, limits.max_body_chars)
    content_key = content_id(record.get('header'), record.get('title'), record.get('titleUrl'),
                             record.get('time') or (time_meta or {}).get('raw_display'))
    metadata = {
        'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION, 'variant': variant},
        'locator': {'artifact_id': artifact_id, 'variant': variant, 'record_index': index, 'html_line': line},
        'raw_record': raw, 'raw_truncated_values': truncated, 'time': time_meta,
        'products': record.get('products'), 'header': record.get('header'),
        'opt_in': opt_in, 'content_key': content_key, 'body_complete': complete,
        'semantics': 'activity record as exported by the service; not proof of attention or duration',
        'issues': problems,
    }
    event_id = add_event_with_times(
        [candidate] if candidate is not None else [], source_id=source_id, artifact_id=artifact_id,
        event_type='activity', ts_start=legacy_instant(candidate), contact_label=record.get('header'),
        subject=record.get('title'), body=body, external_id=f'{PARSER_NAME}:{variant}:{index}', confidence=None,
        metadata=metadata)
    for problem in problems:
        issues.add(problem, record_index=index)
    return event_id


def import_my_activity(path: Path, *, enable_browser_history: bool = False, source_locator: str | None = None,
                       original_name: str | None = None, collection_id: int | None = None,
                       label: str | None = None, limits: CommLimits = DEFAULT_LIMITS) -> dict:
    config = settings()
    opt_in = {'enable_browser_history': bool(enable_browser_history)}
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(
        path, source_kind='google_takeout', source_label=label or 'Takeout My Activity', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name,
        collection_id=collection_id, options=opt_in)
    issues = Issues(limits.max_issues)
    snapshot = stored_path(artifact_id)
    size = snapshot.stat().st_size
    counts = {'records': 0, 'events': 0, 'skipped_browser_history_not_enabled': 0, 'gemini_records': 0}
    if size > limits.max_json_bytes:
        issues.add('document_size_limit', size_bytes=size, limit=limits.max_json_bytes)
        coverage = {'complete': False}
        record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                          options=opt_in, counts=counts, coverage=coverage, issues=issues)
        return {'artifact_id': artifact_id, **counts, 'coverage': coverage, 'issues': issues.as_dict()}
    data = snapshot.read_bytes()
    head = data.lstrip()[:1]
    complete = True
    if head in (b'[', b'{'):
        variant = 'json'
        try:
            records = json.loads(data.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f'My Activity JSON is not valid: {exc}') from exc
        if not isinstance(records, list):
            # Never unwrap other Takeout objects (e.g. Timeline.json): that would
            # import opt-in data such as location history without its opt-in.
            raise ValueError('My Activity JSON must be a list of records')
        items = [(i, r, None) for i, r in enumerate(records)]
    else:
        variant = 'html'
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('utf-8', errors='replace')
            issues.add('html_invalid_utf8_replaced')
        parser = _ActivityHTML(limits.max_records)
        parser.feed(text)
        parser.close()
        if parser.overflow:
            issues.add('record_limit', limit=limits.max_records)
            complete = False
        items = []
        for i, cell in enumerate(parser.records):
            lines = [re.sub(r'\s+', ' ', ''.join(part)).strip() for part in cell['content']]
            lines = [line for line in lines if line]
            caption = ''.join(cell['caption'])
            products = []
            match = re.search(r'Products:\s*\n(.*?)(?:\n\s*\n|\nWhy is this here\?|$)', caption, re.S)
            if match:
                products = [p.strip() for p in match.group(1).split('\n') if p.strip()]
            record = {'header': ''.join(cell['header']).strip() or None,
                      'title': lines[0] if lines else None,
                      'titleUrl': cell['links'][0]['href'] if cell['links'] else None,
                      'subtitles': [{'name': line} for line in lines[1:-1]],
                      'display_time': lines[-1] if len(lines) > 1 else None,
                      'products': products, 'links': cell['links'][:limits.max_raw_items],
                      'caption_text': caption.strip()}
            items.append((i, record, cell['line']))
    for index, record, line in items:
        if counts['records'] >= limits.max_records:
            issues.add('record_limit', limit=limits.max_records)
            complete = False
            break
        counts['records'] += 1
        if not isinstance(record, dict):
            issues.add('record_not_an_object', record_index=index)
            continue
        if not ({'header', 'title', 'products'} & set(record)):
            issues.add('record_not_my_activity_shape', record_index=index, keys=sorted(record)[:10])
            continue
        if _is_browser_like(record, config) and not enable_browser_history:
            counts['skipped_browser_history_not_enabled'] += 1
            continue
        if record.get('header') in config['html_response_products']:
            counts['gemini_records'] += 1
        candidate, time_meta = None, None
        if variant == 'json':
            raw_time = record.get('time')
            if isinstance(raw_time, str):
                try:
                    candidate = from_iso('created', raw_time, f'{PARSER_NAME}:time',
                                         metadata={'field': 'time', 'semantics': 'activity_record_time'})
                except ValueError:
                    time_meta = {'raw': raw_time, 'status': 'unparsed'}
        elif record.get('display_time'):
            candidate, time_meta = parse_display_time(record['display_time'], config, f'{PARSER_NAME}:html_display_time')
        _event(record, index=index, variant=variant, candidate=candidate, time_meta=time_meta, opt_in=opt_in,
               source_id=source_id, artifact_id=artifact_id, config=config, limits=limits, issues=issues, line=line)
        counts['events'] += 1
    coverage = {'complete': complete}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=opt_in,
                      counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser_version': PARSER_VERSION,
            'variant': variant, **counts, 'options': opt_in, 'coverage': coverage, 'issues': issues.as_dict()}

"""MBOX (e.g. Gmail Takeout) and single EML import.

Each message becomes one ``email`` event. Raw header lines (bounded), a byte
locator into the preserved MBOX/EML copy and every observed timestamp are kept:
the ``Date`` header (kind ``header_date``), each ``Received`` header (kind
``received``) and the mbox separator line (kind ``other``). Attachments and
inline parts become child artifacts of the preserved MBOX/EML, so identical
bytes elsewhere (e.g. the same PDF on disk) share one SHA-256.

Date header rules (RFC 5322 §3.3 and §4.3), see docs/IMPORTERS_COMM.md:
``+hhmm``/``-hhmm`` are explicit offsets; ``-0000`` fixes the UTC instant while
saying nothing about the sender's local zone; ``UT``/``GMT`` and the US zones of
§4.3 map to their defined offsets; ``Z`` is UTC. Other letters and unknown
alphabetic zones are **not** converted: the RFC's "treat as -0000" reading is
kept as an alternative in metadata while the candidate stays ``unknown``.
"""
from __future__ import annotations

import base64
import binascii
import email
import hashlib
import email.policy
import email.utils
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from ..timeinfo import TimeCandidate
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, acquire_bytes, add_event_with_times,
                          add_media_reference, bounded_text, container_scheme, legacy_instant, local_candidate,
                          numeric_offset_minutes, offset_candidate, out_of_range_offset_candidate,
                          record_import_run, safe_name, stored_path)

PARSER_NAME = 'email_rfc5322'
# '2' (2026-10-16, review finding 5): attachment locators carry the container's
# SHA-256 (comm_common.container_scheme); a container imported by '1' stays '1'.
PARSER_VERSION = '2'

_MONTHS = {m: i for i, m in enumerate(('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}
_DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
# RFC 5322 §4.3 obs-zone table: the only alphabetic zones with defined offsets.
_OBS_ZONES = {'UT': 0, 'GMT': 0, 'EST': -300, 'EDT': -240, 'CST': -360, 'CDT': -300,
              'MST': -420, 'MDT': -360, 'PST': -480, 'PDT': -420}
_RFC5322 = re.compile(
    r'^\s*(?:(?P<dow>[A-Za-z]{2,9})\s*,?\s*)?(?P<day>\d{1,2})[\s-]+(?P<mon>[A-Za-z]{3,9})\.?[\s-]+(?P<year>\d{2,4})'
    r'\s+(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?\s*(?P<zone>[+-]\d{4}|[A-Za-z]{1,5})?\s*(?P<comment>\(.*\))?\s*$')
_ASCTIME = re.compile(
    r'^\s*(?P<dow>[A-Za-z]{3})\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?'
    r'\s+(?:(?P<zone>[+-]\d{4}|[A-Za-z]{1,5})\s+)?(?P<year>\d{4})\s*$')
_ENCODED_WORD = re.compile(r'=\?([^?\s]+)\?([QqBb])\?([^?\s]*)\?=')
_MSGID = re.compile(r'<[^<>\s]{1,998}>')
_SEPARATOR_STRICT = re.compile(rb'^From \S+\s+[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}(:\d{2})?')


# ---------------------------------------------------------------------------
# Header decoding

def decode_words(value: str) -> tuple[str, list[str]]:
    """Decode RFC 2047 encoded-words; undecodable words stay verbatim and flagged."""
    problems: list[str] = []
    out: list[str] = []
    position = 0
    previous_encoded = False
    for match in _ENCODED_WORD.finditer(value):
        between = value[position:match.start()]
        if not (previous_encoded and between.strip() == ''):
            out.append(between)
        charset, encoding, payload = match.groups()
        charset = charset.split('*', 1)[0]
        try:
            raw = payload.encode('ascii')
            if encoding in 'Bb':
                data = base64.b64decode(raw + b'=' * (-len(raw) % 4))
            else:
                data = binascii.a2b_qp(raw, header=True)
            try:
                out.append(data.decode(charset))
            except UnicodeDecodeError:
                out.append(data.decode(charset, errors='replace'))
                problems.append(f'encoded_word_invalid_bytes:{charset}')
            previous_encoded = True
        except LookupError:
            out.append(match.group(0))
            problems.append(f'encoded_word_unknown_charset:{charset}')
            previous_encoded = False
        except (UnicodeEncodeError, binascii.Error, ValueError):
            out.append(match.group(0))
            problems.append('encoded_word_malformed')
            previous_encoded = False
        position = match.end()
    out.append(value[position:])
    return ''.join(out), problems


def split_headers(raw: bytes, limits: CommLimits) -> tuple[list[tuple[str, str]], bytes, dict]:
    """Unfold header lines from raw bytes; bytes are decoded UTF-8, else Latin-1."""
    match = re.search(rb'\r?\n\r?\n', raw)
    header_end = match.start() if match else len(raw)
    body_start = match.end() if match else len(raw)
    info = {'header_bytes': header_end, 'header_complete': header_end <= limits.max_header_bytes,
            'decoding': 'utf-8', 'blank_line_found': match is not None}
    block = raw[:min(header_end, limits.max_header_bytes)]
    try:
        text = block.decode('utf-8')
    except UnicodeDecodeError:
        text = block.decode('latin-1')
        info['decoding'] = 'latin-1_fallback_for_non_utf8_header_bytes'
    headers: list[tuple[str, str]] = []
    malformed = 0
    for line in re.split(r'\r?\n', text):
        if not line:
            continue
        if line[0] in ' \t' and headers:
            name, value = headers[-1]
            headers[-1] = (name, value + '\n' + line)
        elif ':' in line and re.match(r'^[!-9;-~]+:', line):
            name, value = line.split(':', 1)
            headers.append((name, value.strip(' \t')))
        else:
            malformed += 1
            headers.append(('X-AGEDS-Unparsed-Header-Line', line))
    info['malformed_lines'] = malformed
    return headers, raw[body_start:], info


def _unfold(value: str) -> str:
    return re.sub(r'\r?\n[ \t]+', ' ', value)


def _first(headers, name):
    lowered = name.lower()
    return next((value for key, value in headers if key.lower() == lowered), None)


def _all(headers, name):
    lowered = name.lower()
    return [(index, value) for index, (key, value) in enumerate(headers) if key.lower() == lowered]


# ---------------------------------------------------------------------------
# Dates

def _two_digit_year(text: str, meta: dict) -> int:
    year = int(text)
    if len(text) == 2:
        meta['year_rule'] = 'rfc5322_4_3_two_digit_00_49_2000_50_99_1900'
        return year + (2000 if year < 50 else 1900)
    if len(text) == 3:
        meta['year_rule'] = 'rfc5322_4_3_three_digit_plus_1900'
        return year + 1900
    return year


def parse_mail_date(raw: str, kind: str, source: str) -> tuple[TimeCandidate | None, dict]:
    """Parse an RFC 5322 / RFC 850 / asctime date; never use the local machine zone."""
    text = _unfold(raw).strip()
    meta: dict = {'raw_header_value': raw}
    match = _RFC5322.match(text) or _ASCTIME.match(text)
    if not match:
        meta['status'] = 'unparsed'
        return None, meta
    parts = match.groupdict()
    meta['syntax'] = 'asctime' if match.re is _ASCTIME else 'rfc5322_or_rfc850'
    month = _MONTHS.get(parts['mon'][:3].lower())
    if month is None:
        meta['status'] = 'unknown_month_name'
        return None, meta
    second = parts.get('s')
    if second == '60':
        meta['status'] = 'leap_second_not_representable'
        return None, meta
    try:
        year = _two_digit_year(parts['year'], meta)
        naive = datetime(year, month, int(parts['day']), int(parts['h']), int(parts['m']), int(second or 0))
    except ValueError:
        meta['status'] = 'invalid_calendar_value'
        return None, meta
    dow = (parts.get('dow') or '')[:3].lower()
    if dow and dow in _DAYS and _DAYS.index(dow) != naive.weekday():
        meta['day_of_week_mismatch'] = parts['dow']
    if parts.get('comment'):
        meta['zone_comment'] = parts['comment']
    precision = 'minute' if second is None else 'second'
    zone = parts.get('zone')
    meta['status'] = 'parsed'
    if zone is None:
        meta['zone_status'] = 'missing'
        return local_candidate(kind, raw, source, naive, precision, meta), meta
    if zone[0] in '+-':
        hours, minutes = int(zone[1:3]), int(zone[3:5])
        if minutes >= 60:
            meta['zone_status'] = 'invalid_numeric_zone'
            return local_candidate(kind, raw, source, naive, precision, meta), meta
        offset = numeric_offset_minutes(zone[0], hours, minutes)
        if offset is None:  # e.g. +2400 or +9900: not an offset; the wall clock stays, zone unknown
            return out_of_range_offset_candidate(kind, raw, source, naive, precision, meta, zone), meta
        if abs(offset) > 14 * 60:
            meta['zone_offset_outside_civil_range'] = True
        if zone == '-0000':
            meta['zone_status'] = 'rfc5322_minus_0000_utc_instant_local_zone_unknown'
            return offset_candidate(kind, raw, source, naive, 0, precision, meta, tz_label='-00:00'), meta
        meta['zone_status'] = 'numeric_offset'
        return offset_candidate(kind, raw, source, naive, offset, precision, meta), meta
    upper = zone.upper()
    if upper in _OBS_ZONES:
        meta['zone_status'] = 'rfc5322_4_3_obsolete_zone'
        meta['obsolete_zone'] = upper
        return offset_candidate(kind, raw, source, naive, _OBS_ZONES[upper], precision, meta), meta
    if upper == 'Z':
        meta['zone_status'] = 'military_z_utc'
        return offset_candidate(kind, raw, source, naive, 0, precision, meta, tz_label='-00:00'), meta
    # RFC 5322 §4.3 says unknown zones SHOULD be read as -0000 (UTC). We keep that
    # reading visible but do not adopt it: e.g. "CET" would be off by an hour.
    rfc_reading = offset_candidate(kind, raw, source, naive, 0, precision)
    meta['zone_status'] = 'unrecognized_alphabetic_zone_not_converted'
    meta['zone_raw'] = zone
    meta['rfc5322_4_3_reading'] = {'equivalent': '-0000', 'utc': rfc_reading.utc, 'adopted': False}
    return local_candidate(kind, raw, source, naive, precision, meta), meta


def _received_date(value: str) -> str | None:
    unfolded = _unfold(value)
    return unfolded.rsplit(';', 1)[1].strip() if ';' in unfolded else None


# ---------------------------------------------------------------------------
# Bodies

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self._skip += 1
        elif tag in ('br', 'p', 'div', 'tr', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head') and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Plain-text projection of an HTML part; markup is data, never rendered."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return re.sub(r'\n{3,}', '\n\n', re.sub(r'[ \t]+', ' ', ''.join(parser.parts))).strip()


def _decode_text(part, issues: list[str]) -> str | None:
    payload = part.get_payload(decode=True)
    if payload is None:
        return None
    charset = part.get_content_charset() or 'us-ascii'
    try:
        return payload.decode(charset)
    except LookupError:
        issues.append(f'unknown_charset:{charset}')
    except UnicodeDecodeError:
        issues.append(f'invalid_bytes_for_charset:{charset}')
    try:
        return payload.decode('utf-8')
    except UnicodeDecodeError:
        return payload.decode('latin-1')


def _part_paths(message, limits: CommLimits):
    """Yield (path, part) depth-first with IMAP-like numbering, bounded."""
    count = 0
    stack = [('', message)]
    while stack:
        path, part = stack.pop()
        count += 1
        if count > limits.max_mime_parts:
            yield None, None
            return
        yield path or '0', part
        if part.is_multipart():
            children = part.get_payload()
            if isinstance(children, list):
                for index in range(len(children) - 1, -1, -1):
                    stack.append((f'{path}.{index + 1}' if path else str(index + 1), children[index]))


# ---------------------------------------------------------------------------
# MBOX framing

def iter_mbox(path: Path, limits: CommLimits, issues: Issues):
    """Yield framed messages from an mbox without loading the file.

    A separator is a ``From `` line at file start or after a blank line; a
    strict asctime-shaped ``From `` line without a blank line is accepted and
    flagged. ``>From`` unescaping (mboxrd: one ``>`` removed) applies to the
    parsed copy only; byte offsets locate the escaped original.
    """
    with path.open('rb') as handle:
        offset = 0
        at_line_start = True
        previous_blank = True
        current: dict | None = None

        def finish(entry):
            if entry is not None:
                entry['byte_end'] = offset
                return entry
            return None

        while True:
            piece = handle.readline(limits.max_line_bytes)
            if not piece:
                break
            starts_line = at_line_start
            at_line_start = piece.endswith(b'\n')
            stripped = piece.rstrip(b'\r\n')
            is_separator = False
            if starts_line and piece.startswith(b'From '):
                if previous_blank:
                    is_separator = True
                elif _SEPARATOR_STRICT.match(piece):
                    is_separator = True
                    issues.add('mbox_separator_without_blank_line', byte_offset=offset)
            if is_separator:
                done = finish(current)
                if done is not None:
                    yield done
                current = {'from_line': stripped.decode('latin-1')[:1024], 'byte_start': offset,
                           'chunks': [], 'size': 0, 'truncated': False, 'unescaped': 0, 'pending_blank': False}
            elif current is None:
                if stripped:
                    issues.add('mbox_bytes_before_first_separator', byte_offset=offset)
            else:
                line = piece
                if starts_line and re.match(rb'^>+From ', piece):
                    line = piece[1:]
                    current['unescaped'] += 1
                if current['size'] + len(line) > limits.max_message_bytes:
                    current['truncated'] = True
                else:
                    current['chunks'].append(line)
                    current['size'] += len(line)
            if starts_line:
                previous_blank = stripped == b''
            elif stripped:
                previous_blank = False
            offset += len(piece)
        done = finish(current)
        if done is not None:
            yield done


# ---------------------------------------------------------------------------
# Message import

def _addresses(headers, name, issues):
    entries = _all(headers, name)
    if not entries:
        return None
    raws = [_unfold(value) for _, value in entries]
    decoded_values = []
    for raw in raws:
        decoded, problems = decode_words(raw)
        issues.extend(problems)
        decoded_values.append(decoded)
    # Parse the undecoded value: a decoded display name may contain commas.
    parsed = []
    for name, address in email.utils.getaddresses(raws)[:500]:
        parsed.append({'name': decode_words(name)[0] if name else None, 'address': address or None})
    return {'raw': raws, 'decoded': decoded_values, 'parsed': parsed}


def _labels(raw: str) -> list[str]:
    labels, current, quoted = [], '', False
    for ch in raw:
        if ch == '"':
            quoted = not quoted
        elif ch == ',' and not quoted:
            labels.append(current.strip())
            current = ''
        else:
            current += ch
    if current.strip():
        labels.append(current.strip())
    return labels


def scan_ids(raw: bytes, limits: CommLimits) -> dict:
    headers, _, _ = split_headers(raw, limits)
    message_id = _MSGID.search(_first(headers, 'Message-ID') or '')
    references = _MSGID.findall(_unfold(_first(headers, 'References') or ''))
    in_reply_to = _MSGID.findall(_unfold(_first(headers, 'In-Reply-To') or ''))
    return {'message_id': message_id.group(0) if message_id else None, 'references': references,
            'in_reply_to': in_reply_to, 'gm_thrid': (_first(headers, 'X-GM-THRID') or '').strip() or None}


def reconstruct_roots(index: list[dict]) -> list[dict]:
    """Thread root per message: References[0], else follow In-Reply-To in the file."""
    by_id = {}
    for entry in index:
        if entry['message_id'] and entry['message_id'] not in by_id:
            by_id[entry['message_id']] = entry
    results = []
    for entry in index:
        if entry['references']:
            results.append({'root': entry['references'][0], 'method': 'references_first'})
            continue
        current, steps, seen = entry, 0, set()
        while current['in_reply_to'] and steps < 1000:
            parent_id = current['in_reply_to'][0]
            if parent_id in seen:
                break
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            if parent is None:
                current = {'message_id': parent_id, 'references': [], 'in_reply_to': []}
                break
            if parent['references']:
                current = {'message_id': parent['references'][0], 'references': [], 'in_reply_to': []}
                break
            current, steps = parent, steps + 1
        if current is entry:
            results.append({'root': entry['message_id'], 'method': 'self' if entry['message_id'] else 'none'})
        else:
            results.append({'root': current['message_id'], 'method': 'in_reply_to_chain_within_file'})
    return results


class _Importer:
    def __init__(self, *, source_id, artifact_id, base_locator, collection_id, limits, issues, version=PARSER_VERSION):
        self.source_id, self.artifact_id = source_id, artifact_id
        self.base_locator, self.collection_id = base_locator, collection_id
        self.version = version
        self.limits, self.issues = limits, issues
        self.seen_ids: dict[str, int] = {}
        self.counts = {'messages': 0, 'attachments': 0, 'inline_parts': 0, 'attachments_not_extracted': 0,
                       'missing_message_id': 0, 'duplicate_message_id': 0, 'truncated_messages': 0}

    def message(self, raw: bytes, *, index: int, locator: dict, thread: dict, from_line: str | None,
                truncated: bool) -> int:
        limits = self.limits
        problems: list[str] = []
        headers, _, header_info = split_headers(raw, limits)
        parsed = email.message_from_bytes(raw, policy=email.policy.compat32)
        defects = [type(d).__name__ for d in parsed.defects]
        for path, part in _part_paths(parsed, limits):
            if part is not None and part is not parsed:
                defects.extend(f'{path}:{type(d).__name__}' for d in getattr(part, 'defects', []))
        message_id_raw = _first(headers, 'Message-ID')
        message_id_match = _MSGID.search(message_id_raw or '')
        message_id = message_id_match.group(0) if message_id_match else None
        if message_id:
            base_id, id_kind = message_id, 'message_id'
        else:
            base_id, id_kind = f'content-sha256:{_sha(raw)}', 'content_derived_missing_message_id'
            self.counts['missing_message_id'] += 1
            problems.append('message_id_missing_content_derived_id')
        occurrence = self.seen_ids.get(base_id, 0) + 1
        self.seen_ids[base_id] = occurrence
        external_id = base_id if occurrence == 1 else f'{base_id}#occurrence-{occurrence}'
        if occurrence > 1:
            id_kind += '_repeated_in_file'
            self.counts['duplicate_message_id'] += 1
            problems.append('duplicate_identity_in_file')
        if truncated:
            problems.append('message_truncated_at_limit')
            self.counts['truncated_messages'] += 1

        subject_raw = _first(headers, 'Subject')
        subject, subject_problems = decode_words(_unfold(subject_raw)) if subject_raw is not None else (None, [])
        problems.extend(subject_problems)
        addresses = {name.lower(): _addresses(headers, name, problems)
                     for name in ('From', 'To', 'Cc', 'Bcc', 'Reply-To', 'Sender')}

        candidates: list[TimeCandidate] = []
        date_raw = _first(headers, 'Date')
        date_meta = None
        date_candidate = None
        if date_raw is None:
            problems.append('date_header_missing')
        else:
            date_candidate, date_meta = parse_mail_date(date_raw, 'header_date', 'email:Date')
            if date_candidate is None:
                problems.append(f"date_header_{date_meta['status']}")
            else:
                candidates.append(date_candidate)
                if date_candidate.tz_status == 'unknown':
                    problems.append(f"date_header_zone_{date_meta.get('zone_status')}")
        received = []
        for position, value in _all(headers, 'Received'):
            stamp = _received_date(value)
            if stamp is None:
                received.append({'header_index': position, 'status': 'no_date_clause'})
                continue
            candidate, meta = parse_mail_date(stamp, 'received', f'email:Received[{position}]')
            received.append({'header_index': position, 'status': meta['status']})
            if candidate is not None:
                candidates.append(candidate)
        if from_line:
            match = re.match(r'^From \S+\s+(.*)$', from_line)
            if match:
                candidate, meta = parse_mail_date(match.group(1), 'other', 'mbox:From_line')
                if candidate is not None:
                    candidates.append(TimeCandidate(candidate.kind, candidate.raw, candidate.source, utc=candidate.utc,
                                                    local=candidate.local, tz_status=candidate.tz_status, tz=candidate.tz,
                                                    precision=candidate.precision,
                                                    metadata={**candidate.metadata,
                                                              'semantics': 'mbox_separator_time_writer_defined'}))

        body, body_meta, html_parts, attachments = self._parts(parsed, index=index, problems=problems)
        labels_raw = _first(headers, 'X-Gmail-Labels')
        gm_thrid = (_first(headers, 'X-GM-THRID') or '').strip() or None
        thread_id = f'gmail-thrid:{gm_thrid}' if gm_thrid else thread.get('root')

        raw_headers = []
        for name, value in headers[:limits.max_raw_items]:
            bounded, complete = bounded_text(value, limits.max_raw_chars)
            raw_headers.append([name, bounded] if complete else [name, bounded, 'truncated'])
        recipients = []
        for key in ('to', 'cc', 'bcc'):
            if addresses[key]:
                recipients.extend(a['address'] for a in addresses[key]['parsed'] if a['address'])
        sender = addresses['from']['parsed'][0] if addresses['from'] and addresses['from']['parsed'] else {}
        metadata = {
            'parser': {'name': PARSER_NAME, 'version': self.version, 'message_index': index},
            'locator': locator,
            'message_id': {'raw': message_id_raw, 'value': message_id, 'external_id_kind': id_kind},
            'headers_raw': raw_headers, 'headers_total': len(headers), 'header_parse': header_info,
            'subject': {'raw': subject_raw, 'decoded': subject},
            'addresses': addresses,
            'date': date_meta, 'received': received,
            'gmail': {'labels_raw': labels_raw, 'labels': _labels(labels_raw) if labels_raw else None,
                      'thread_id': gm_thrid},
            'thread': {**thread, 'gmail_thread_id': gm_thrid,
                       'in_reply_to': _MSGID.findall(_unfold(_first(headers, 'In-Reply-To') or '')),
                       'references': _MSGID.findall(_unfold(_first(headers, 'References') or ''))[:limits.max_raw_items]},
            'body': body_meta, 'html_parts': html_parts,
            'attachments': [{k: v for k, v in a.items() if k != 'payload'} for a in attachments],
            'mime': {'defects': defects, 'content_type': parsed.get_content_type()},
            'mbox': {'from_line': from_line} if from_line is not None else None,
            'time_policy': 'ts_start only from an offset-bearing Date; all candidates in event_times',
            'issues': sorted(set(problems)),
        }
        event_id = add_event_with_times(
            candidates, source_id=self.source_id, artifact_id=self.artifact_id, event_type='email',
            ts_start=legacy_instant(date_candidate), sender=(addresses['from']['decoded'][0] if addresses['from'] else None),
            recipients='; '.join(recipients) or None, contact_label=sender.get('name'),
            phone_or_address=sender.get('address'), subject=subject, body=body, external_id=external_id,
            thread_id=thread_id, confidence=None, metadata=metadata)
        for item in attachments:
            add_media_reference(
                event_id=event_id, artifact_id=item.get('artifact_id'), reference_kind=item['reference_kind'],
                reference_key=f"mime:{item['part']}", reference_name=item['filename'], status=item['status'],
                parser=PARSER_NAME, parser_version=self.version,
                locator={**locator, 'mime_part': item['part']},
                metadata={'content_type': item['content_type'], 'size_bytes': item.get('size_bytes'),
                          'sha256': item.get('sha256'), 'reason': item.get('reason')})
        for problem in sorted(set(problems)):
            self.issues.add(problem, message_index=index)
        if defects:
            self.issues.add('mime_defects', message_index=index, defects=defects[:20])
        self.counts['messages'] += 1
        return event_id

    def _parts(self, parsed, *, index: int, problems: list[str]):
        limits = self.limits
        body = None
        body_meta = {'source': None}
        html_parts = []
        attachments = []
        html_fallback = None
        for path, part in _part_paths(parsed, limits):
            if part is None:
                problems.append('mime_part_limit')
                break
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            if part.get_content_maintype() == 'multipart':
                # Broken MIME: a multipart without a usable boundary. Keep its
                # undecoded text as the body view; the original stays in the store.
                problems.append('multipart_without_parseable_boundary')
                if body is None:
                    payload = part.get_payload(decode=False)
                    text = payload if isinstance(payload, str) else ''
                    body, complete = bounded_text(text, limits.max_body_chars)
                    body_meta = {'source': 'unparsed_multipart_payload', 'part': path, 'complete': complete}
                continue
            disposition = (part.get_content_disposition() or '').lower()
            filename = part.get_filename()
            if filename is not None:
                filename, name_problems = decode_words(str(filename))
                problems.extend(name_problems)
            is_text = content_type in ('text/plain', 'text/html') and disposition != 'attachment' and not filename
            if is_text and content_type == 'text/plain':
                text = _decode_text(part, problems)
                if text is not None and body is None:
                    body, complete = bounded_text(text, limits.max_body_chars)
                    body_meta = {'source': 'text/plain', 'part': path, 'charset': part.get_content_charset(),
                                 'complete': complete}
                elif text is not None:
                    kept, complete = bounded_text(text, limits.max_raw_chars)
                    body_meta.setdefault('other_text_plain_parts', []).append(
                        {'part': path, 'text': kept, 'complete': complete})
                continue
            if is_text and content_type == 'text/html':
                markup = _decode_text(part, problems) or ''
                kept, complete = bounded_text(markup, limits.max_html_chars)
                html_parts.append({'part': path, 'charset': part.get_content_charset(), 'html_as_data': kept,
                                   'complete': complete, 'rendering': 'never_rendered_as_trusted_markup'})
                if html_fallback is None:
                    html_fallback = (path, markup)
                continue
            if is_text:
                continue
            attachments.append(self._attachment(part, path=path, index=index, filename=filename,
                                                content_type=content_type, disposition=disposition))
        if body is None and html_fallback is not None:
            text = html_to_text(html_fallback[1])
            body, complete = bounded_text(text, limits.max_body_chars)
            body_meta = {'source': 'text_extracted_from_html', 'part': html_fallback[0], 'complete': complete,
                         'derivation': 'html.parser text nodes; markup discarded'}
        return body, body_meta, html_parts, attachments

    def _attachment(self, part, *, path, index, filename, content_type, disposition):
        inline = disposition == 'inline' or (not disposition and part.get('Content-ID') is not None)
        kind = 'email_inline' if inline else 'email_attachment'
        item = {'part': path, 'filename': filename, 'content_type': content_type, 'disposition': disposition or None,
                'content_id': part.get('Content-ID'), 'reference_kind': kind}
        is_raw = True
        if content_type == 'message/rfc822':
            inner = part.get_payload()
            payload = inner[0].as_bytes() if isinstance(inner, list) and inner else None
            item['extraction'] = 'reserialized_message_not_byte_exact'
            is_raw = False
        else:
            payload = part.get_payload(decode=True)
            item['extraction'] = f"content_transfer_decoding:{(part.get('Content-Transfer-Encoding') or '7bit').strip().lower()}"
        if payload is None:
            item.update(status='unreadable', reason='payload_not_decodable')
            self.counts['attachments_not_extracted'] += 1
            return item
        if len(payload) > self.limits.max_attachment_bytes:
            item.update(status='not_extracted', reason='attachment_size_limit', size_bytes=len(payload))
            self.counts['attachments_not_extracted'] += 1
            return item
        name = safe_name(filename, f'part-{path}')
        if not filename:
            item['filename_status'] = 'missing_in_source_synthesized'
        locator = f'{self.base_locator}!/message/{index}/mime-part/{path}'
        artifact_id = acquire_bytes(
            payload, name=name, source_id=self.source_id, locator=locator, importer=PARSER_NAME,
            parser_version=self.version, parent_artifact_id=self.artifact_id, mime_type=content_type,
            collection_id=self.collection_id, is_raw=is_raw,
            extra_metadata={'extracted_from': {'message_index': index, 'mime_part': path},
                            'raw_filename': filename, 'extraction': item['extraction']})
        item.update(status='present', artifact_id=artifact_id, size_bytes=len(payload), sha256=_sha(payload),
                    locator=locator)
        self.counts['inline_parts' if inline else 'attachments'] += 1
        return item


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _options(limits: CommLimits) -> dict:
    return {'max_message_bytes': limits.max_message_bytes, 'max_attachment_bytes': limits.max_attachment_bytes,
            'max_records': limits.max_records}


def import_mbox(path: Path, *, source_locator: str | None = None, original_name: str | None = None,
                collection_id: int | None = None, label: str | None = None,
                limits: CommLimits = DEFAULT_LIMITS) -> dict:
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(
        path, source_kind='email_export', source_label=label or 'MBOX', locator=locator, importer=PARSER_NAME,
        parser_version=PARSER_VERSION, original_name=original_name, options=_options(limits),
        collection_id=collection_id, mime_type='application/mbox')
    issues = Issues(limits.max_issues)
    snapshot = stored_path(artifact_id)
    # Pass 1: identifiers only (bounded), for in-file thread reconstruction.
    index = []
    for count, framed in enumerate(iter_mbox(snapshot, limits, Issues(limits.max_issues))):
        if count >= limits.max_records:
            break
        index.append(scan_ids(b''.join(framed['chunks']), limits))
    roots = reconstruct_roots(index)
    version, child_base = container_scheme(artifact_id, locator, PARSER_NAME, PARSER_VERSION)
    importer = _Importer(source_id=source_id, artifact_id=artifact_id, base_locator=child_base,
                         collection_id=collection_id, limits=limits, issues=issues, version=version)
    complete = True
    for count, framed in enumerate(iter_mbox(snapshot, limits, issues)):
        if count >= limits.max_records:
            issues.add('record_limit', limit=limits.max_records)
            complete = False
            break
        raw = b''.join(framed['chunks'])
        importer.message(raw, index=count, thread=roots[count], from_line=framed['from_line'],
                         truncated=framed['truncated'],
                         locator={'artifact_id': artifact_id, 'container': 'mbox', 'message_index': count,
                                  'byte_start': framed['byte_start'], 'byte_end': framed['byte_end'],
                                  'from_escaping': {'policy': 'mboxrd_remove_one_gt_in_parsed_copy',
                                                    'lines_unescaped': framed['unescaped']}})
        if framed['truncated']:
            complete = False
    counts = importer.counts
    coverage = {'complete': complete and not counts['truncated_messages'] and not counts['attachments_not_extracted']}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=version,
                      options=_options(limits), counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser_version': version, **counts,
            'coverage': coverage, 'issues': issues.as_dict()}


def import_eml(path: Path, *, source_locator: str | None = None, original_name: str | None = None,
               collection_id: int | None = None, label: str | None = None,
               limits: CommLimits = DEFAULT_LIMITS) -> dict:
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(
        path, source_kind='email_export', source_label=label or 'EML', locator=locator, importer=PARSER_NAME,
        parser_version=PARSER_VERSION, original_name=original_name, options=_options(limits),
        collection_id=collection_id, mime_type='message/rfc822')
    issues = Issues(limits.max_issues)
    snapshot = stored_path(artifact_id)
    size = snapshot.stat().st_size
    with snapshot.open('rb') as handle:
        raw = handle.read(limits.max_message_bytes)
    truncated = size > limits.max_message_bytes
    ids = scan_ids(raw, limits)
    thread = reconstruct_roots([ids])[0]
    version, child_base = container_scheme(artifact_id, locator, PARSER_NAME, PARSER_VERSION)
    importer = _Importer(source_id=source_id, artifact_id=artifact_id, base_locator=child_base,
                         collection_id=collection_id, limits=limits, issues=issues, version=version)
    importer.message(raw, index=0, thread=thread, from_line=None, truncated=truncated,
                     locator={'artifact_id': artifact_id, 'container': 'eml', 'message_index': 0,
                              'byte_start': 0, 'byte_end': min(size, limits.max_message_bytes)})
    counts = importer.counts
    coverage = {'complete': not truncated and not counts['attachments_not_extracted']}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=version,
                      options=_options(limits), counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser_version': version, **counts,
            'coverage': coverage, 'issues': issues.as_dict()}

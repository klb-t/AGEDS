"""Shared mechanics for the communication, Takeout and media importers.

Only mechanics with identical semantics across importers live here: bounded raw
capture, issue collection, acquisition of a source file or extracted child,
event creation with observed time candidates, media references and the
append-only import-run audit entry. Format semantics stay in each importer.

Time policy (all importers in this family):

* every timestamp a source states becomes a ``TimeCandidate`` in ``event_times``
  with its kind, raw text, zone status and precision;
* ``events.ts_start``/``ts_end`` are filled only with a UTC instant the source
  itself fixes (explicit offset, declared zone or epoch). A wall-clock value in
  an unknown zone leaves the legacy column empty; the candidate keeps it;
* the importing machine's local zone is never consulted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from .. import evidence
from ..db import session
from ..timeinfo import TimeCandidate, format_utc, record_event_times

DATA_DIR = Path(__file__).with_name('data')


@dataclass(frozen=True)
class CommLimits:
    """Explicit resource bounds; reaching one is reported as partial coverage."""
    max_records: int = 200_000
    max_issues: int = 500
    max_raw_chars: int = 4096
    max_raw_items: int = 200
    max_body_chars: int = 1_000_000
    max_html_chars: int = 65_536
    max_message_bytes: int = 64 * 1024 * 1024
    max_header_bytes: int = 1024 * 1024
    max_line_bytes: int = 1024 * 1024
    max_mime_parts: int = 500
    max_attachment_bytes: int = 256 * 1024 * 1024
    max_json_bytes: int = 512 * 1024 * 1024
    max_archive_members: int = 20_000
    max_archive_expanded_bytes: int = 8 * 1024 * 1024 * 1024
    max_walk_entries: int = 100_000
    max_exif_bytes: int = 1024 * 1024
    max_recurrence_instances: int = 500

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f'limit {name} must be a positive integer')

    def replace(self, **changes) -> 'CommLimits':
        return CommLimits(**{**asdict(self), **{k: v for k, v in changes.items() if v is not None}})


DEFAULT_LIMITS = CommLimits()


def load_data(name: str) -> dict:
    """Load a versioned data file shipped next to the importers."""
    with (DATA_DIR / name).open('r', encoding='utf-8') as handle:
        return json.load(handle)


def content_id(*parts) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class Issues:
    """Bounded issue list with exact per-code counts."""

    def __init__(self, limit: int):
        self.limit = limit
        self.items: list[dict] = []
        self.counts: dict[str, int] = {}

    def add(self, code: str, **details) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1
        if len(self.items) < self.limit:
            self.items.append({'code': code, **details})

    def as_dict(self) -> dict:
        total = sum(self.counts.values())
        return {'total': total, 'by_code': dict(sorted(self.counts.items())),
                'listed': self.items, 'listed_complete': len(self.items) == total}


def bounded_text(value: str | None, limit: int) -> tuple[str | None, bool]:
    if value is None:
        return None, True
    return (value, True) if len(value) <= limit else (value[:limit], False)


def bound_json(value, limits: CommLimits = DEFAULT_LIMITS, _depth: int = 0) -> tuple[object, int]:
    """Copy a JSON-like value with bounded strings/containers; count truncations."""
    if isinstance(value, str):
        return (value, 0) if len(value) <= limits.max_raw_chars else (value[:limits.max_raw_chars], 1)
    if value is None or isinstance(value, (bool, int)):
        return value, 0
    if isinstance(value, float):
        return (value, 0) if value == value and abs(value) != float('inf') else (repr(value), 1)
    if _depth >= 8:
        return {'truncated_depth': True}, 1
    truncated = 0
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= limits.max_raw_items:
                out['__truncated_keys__'] = len(value) - index
                truncated += 1
                break
            out[str(key)[:256]], count = bound_json(item, limits, _depth + 1)
            truncated += count
        return out, truncated
    if isinstance(value, (list, tuple)):
        out_list = []
        for index, item in enumerate(value):
            if index >= limits.max_raw_items:
                out_list.append({'__truncated_items__': len(value) - index})
                truncated += 1
                break
            bounded, count = bound_json(item, limits, _depth + 1)
            out_list.append(bounded)
            truncated += count
        return out_list, truncated
    return {'representation': repr(value)[:limits.max_raw_chars], 'type': type(value).__name__}, 1


def safe_name(name: str | None, fallback: str) -> str:
    """Display/original name only: strip path parts and control characters."""
    text = (name or '').replace('\\', '/').split('/')[-1]
    text = ''.join(ch for ch in text if unicodedata.category(ch)[0] != 'C').strip()
    return text[:255] or fallback


def safe_suffix(name: str | None) -> str:
    match = re.search(r'(\.[A-Za-z0-9]{1,10})$', name or '')
    return match.group(1).lower() if match else ''


def stored_path(artifact_id: int) -> Path:
    with session() as db:
        row = db.execute('SELECT stored_path FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    if not row or not row['stored_path']:
        raise ValueError('imported artifact has no stored snapshot')
    return Path(row['stored_path'])




def acquire(path: Path, *, source_kind: str, source_label: str, locator: str | None, importer: str,
            parser_version: str, original_name: str | None = None, options: dict | None = None,
            collection_id: int | None = None, mime_type: str | None = None,
            source_id: int | None = None, parent_artifact_id: int | None = None,
            is_raw: bool = True, extra_metadata: dict | None = None,
            source_stat: str | None = None) -> tuple[int, int]:
    """Acquire a file (or an extracted child) through ``evidence.ingest_file``.

    ``source_stat`` is passed through (``None`` = the caller's context default).
    """
    locator = locator if locator is not None else str(path)
    if source_id is None:
        source_id = evidence.ensure_source(source_kind, source_label, locator)
    metadata = {'importer': importer, 'parser_version': parser_version}
    if options:
        metadata['options'] = options
    if extra_metadata:
        metadata.update(extra_metadata)
    artifact_id = evidence.ingest_file(
        path, source_id=source_id, source_locator=locator, original_name=original_name,
        mime_type=mime_type, metadata=metadata, is_raw=is_raw, collection_id=collection_id,
        parent_artifact_id=parent_artifact_id, source_stat=source_stat)
    return source_id, artifact_id


def acquire_bytes(payload: bytes, *, name: str, source_id: int, locator: str, importer: str,
                  parser_version: str, parent_artifact_id: int | None, mime_type: str | None = None,
                  collection_id: int | None = None, is_raw: bool = True,
                  extra_metadata: dict | None = None) -> int:
    """Acquire extracted bytes as a child artifact via a private temporary file.

    The temporary file lives in the system temp directory, never in a source tree;
    its name carries only a sanitized suffix so the store keeps a useful extension.
    Its filesystem times describe the extraction, so none is recorded as a source time.
    """
    with tempfile.TemporaryDirectory(prefix='ageds-extract-') as directory:
        temporary = Path(directory) / ('payload' + safe_suffix(name))
        with temporary.open('wb') as handle:
            handle.write(payload)
        _, artifact_id = acquire(
            temporary, source_kind='', source_label='', locator=locator, importer=importer,
            parser_version=parser_version, original_name=name, collection_id=collection_id,
            mime_type=mime_type, source_id=source_id, parent_artifact_id=parent_artifact_id,
            is_raw=is_raw, extra_metadata=extra_metadata, source_stat='temporary_copy')
        return artifact_id


def legacy_instant(candidate: TimeCandidate | None) -> str | None:
    """UTC for the legacy ``events.ts_*`` columns only when the source fixed it."""
    if candidate is None or candidate.tz_status not in ('explicit_offset', 'utc_epoch'):
        return None
    if candidate.metadata.get('dst') == 'ambiguous_fold':
        return None
    return candidate.utc


def add_event_with_times(candidates: list[TimeCandidate], **event) -> int:
    """Create (or re-observe) an event, then append its time candidates."""
    event_id = evidence.add_event(**event)
    if candidates:
        with session() as db:
            db.execute('BEGIN IMMEDIATE')
            record_event_times(db, event_id, candidates)
    return event_id


def add_media_reference(*, event_id: int, artifact_id: int | None, reference_kind: str, reference_key: str,
                        reference_name: str | None, status: str, parser: str, parser_version: str,
                        locator: dict, metadata: dict | None = None) -> int:
    """Append a reference row; an identical earlier observation is reused."""
    locator_json = json.dumps(locator, ensure_ascii=False, sort_keys=True)
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute(
            '''SELECT id FROM event_media_references WHERE event_id=? AND reference_key=? AND parser=?
               AND parser_version=? AND artifact_id IS ? AND status=? AND locator_json=? AND metadata_json=?
               AND reference_kind=? AND reference_name IS ? ORDER BY id LIMIT 1''',
            (event_id, reference_key, parser, parser_version, artifact_id, status, locator_json, metadata_json,
             reference_kind, reference_name)).fetchone()
        if existing:
            return int(existing['id'])
        cur = db.execute(
            '''INSERT INTO event_media_references(event_id,artifact_id,reference_kind,reference_key,reference_name,
               status,parser,parser_version,locator_json,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (event_id, artifact_id, reference_kind, reference_key, reference_name, status, parser, parser_version,
             locator_json, metadata_json))
        return int(cur.lastrowid)


def record_import_run(*, artifact_id: int | None, importer: str, parser_version: str, options: dict,
                      counts: dict, coverage: dict, issues: Issues) -> None:
    """Append one audit entry per run: opt-ins, counts, coverage and issue codes."""
    details = {'importer': importer, 'parser_version': parser_version, 'options': options, 'counts': counts,
               'coverage': coverage, 'issue_counts': issues.as_dict()['by_code']}
    with session() as db:
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('import_run','artifact',?,?)",
                   (artifact_id, json.dumps(details, ensure_ascii=False, sort_keys=True)))


# ---------------------------------------------------------------------------
# Time candidates



def local_candidate(kind: str, raw: str | None, source: str, naive: datetime, precision: str,
                    metadata: dict | None = None) -> TimeCandidate:
    """Wall-clock time whose zone the source does not state."""
    return TimeCandidate(kind, raw, source, local=naive.replace(tzinfo=None).isoformat(), tz_status='unknown',
                         precision=precision, metadata=dict(metadata or {}))


# ``datetime.timezone`` accepts offsets strictly inside +-24 h. Civil offsets lie
# within -12:00..+14:00; values in between are kept (callers flag them).
MAX_OFFSET_MINUTES = 24 * 60 - 1


def numeric_offset_minutes(sign: str | None, hours, minutes=0) -> int | None:
    """Signed minutes of a numeric zone, or None when the text is not a valid offset.

    Minutes must be 0..59 and the total strictly less than 24 hours: ``+2400``,
    ``+99:00`` or ``GMT+25`` are not offsets and must never reach
    ``datetime.timezone`` (which raises).
    """
    hours, minutes = int(hours), int(minutes or 0)
    if minutes >= 60 or hours * 60 + minutes > MAX_OFFSET_MINUTES:
        return None
    total = hours * 60 + minutes
    return -total if sign == '-' else total


def out_of_range_offset_candidate(kind: str, raw: str | None, source: str, naive: datetime, precision: str,
                                  metadata: dict, offset_raw: str | None) -> TimeCandidate:
    """Wall-clock reading (zone unknown) of a value whose numeric zone is not a valid offset.

    ``metadata`` is updated in place so callers that also return it report the same status.
    """
    metadata['zone_status'] = 'numeric_offset_out_of_range'
    metadata['offset_raw'] = offset_raw
    return local_candidate(kind, raw, source, naive, precision, metadata)


def offset_candidate(kind: str, raw: str | None, source: str, naive: datetime, offset_minutes: int,
                     precision: str, metadata: dict | None = None, tz_label: str | None = None) -> TimeCandidate:
    if isinstance(offset_minutes, bool) or not isinstance(offset_minutes, int) or abs(offset_minutes) > MAX_OFFSET_MINUTES:
        # Defensive: callers range-check first; a parsed value must never raise here.
        return out_of_range_offset_candidate(kind, raw, source, naive, precision, dict(metadata or {}),
                                             str(offset_minutes))
    zone = timezone(timedelta(minutes=offset_minutes))
    sign = '+' if offset_minutes >= 0 else '-'
    hours, minutes = divmod(abs(offset_minutes), 60)
    label = tz_label or f'{sign}{hours:02d}:{minutes:02d}'
    aware = naive.replace(tzinfo=zone)
    return TimeCandidate(kind, raw, source, utc=format_utc(aware), local=naive.isoformat(),
                         tz_status='explicit_offset', tz=label, precision=precision, metadata=dict(metadata or {}))


def declared_zone_candidate(kind: str, raw: str | None, source: str, naive: datetime, zone: tzinfo, zone_name: str,
                            precision: str, declaration: str, metadata: dict | None = None) -> TimeCandidate:
    """A wall-clock time in a zone the source itself names (e.g. ICS ``TZID``).

    The offset follows from that zone's rules. DST folds keep both instants and
    leave the legacy column empty; a DST gap (time that never existed) stays
    ``unknown``. ``declaration`` says where the zone rules came from.
    """
    meta = {**(metadata or {}), 'zone_declaration': declaration, 'declared_zone': zone_name}
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    roundtrip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if roundtrip != naive:
        meta['dst'] = 'nonexistent_local_time'
        return TimeCandidate(kind, raw, source, local=naive.isoformat(), tz_status='unknown', precision=precision,
                             metadata=meta)
    if first.utcoffset() != second.utcoffset():
        meta['dst'] = 'ambiguous_fold'
        meta['utc_alternatives'] = [format_utc(first), format_utc(second)]
    return TimeCandidate(kind, raw, source, utc=format_utc(first), local=naive.isoformat(),
                         tz_status='explicit_offset', tz=zone_name, precision=precision, metadata=meta)


def epoch_candidate(kind: str, raw: str, source: str, *, unit: str, metadata: dict | None = None) -> TimeCandidate:
    """Epoch in seconds, milliseconds or microseconds; integer arithmetic only."""
    text = raw.strip()
    if not re.fullmatch(r'-?\d{1,20}', text):
        raise ValueError(f'not an integer epoch: {raw!r}')
    number = int(text)
    divisor = {'s': 1, 'ms': 1000, 'us': 1_000_000}[unit]
    seconds, remainder = divmod(number, divisor)
    moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds,
                                                                   microseconds=remainder * (1_000_000 // divisor))
    precision = {'s': 'second', 'ms': 'millisecond', 'us': 'microsecond'}[unit]
    return TimeCandidate(kind, raw, source, utc=format_utc(moment), tz_status='utc_epoch', tz='UTC',
                         precision=precision, metadata={'epoch_unit': unit, **(metadata or {})})




def walk_files(root: Path, limits: CommLimits, issues: Issues) -> list[Path]:
    """Bounded, read-only, symlink-free directory walk in deterministic order."""
    found: list[Path] = []
    seen = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            issues.add('directory_unreadable', path=str(directory), error=type(exc).__name__)
            continue
        subdirs = []
        for entry in entries:
            seen += 1
            if seen > limits.max_walk_entries:
                issues.add('walk_entry_limit', limit=limits.max_walk_entries)
                return sorted(found)
            if entry.is_symlink():
                issues.add('symlink_skipped', path=entry.path)
            elif entry.is_dir(follow_symlinks=False):
                subdirs.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                found.append(Path(entry.path))
        stack.extend(reversed(subdirs))
    return sorted(found)


# ---------------------------------------------------------------------------
# RFC 5545 / RFC 6350 content lines (shared grammar of iCalendar and vCard)

_CL_NAME = re.compile(r'(?:([A-Za-z0-9-]+)\.)?([A-Za-z0-9-]+)')
_CL_PARAM_NAME = re.compile(r'[A-Za-z0-9-]+')
_CL_PARAM_VALUE = re.compile(r'[^";:,]*')


@dataclass(frozen=True)
class ContentLine:
    group: str | None
    name: str
    params: tuple  # ((NAME, (values...) | None), ...); None = vCard 2.1 bare parameter
    value: str
    line_start: int
    line_end: int
    raw: str

    def param(self, name: str) -> list[str]:
        values: list[str] = []
        for key, items in self.params:
            if key == name and items:
                values.extend(items)
        return values

    def bare_params(self) -> list[str]:
        return [key for key, items in self.params if items is None]


def parse_content_line(text: str, line_start: int, line_end: int) -> ContentLine:
    match = _CL_NAME.match(text)
    if not match or match.end() == 0:
        raise ValueError('content line has no property name')
    group, name = match.group(1), match.group(2).upper()
    i, n = match.end(), len(text)
    params = []
    while i < n and text[i] == ';':
        i += 1
        key_match = _CL_PARAM_NAME.match(text, i)
        if not key_match:
            raise ValueError('malformed parameter name')
        key, i = key_match.group(0).upper(), key_match.end()
        if i < n and text[i] == '=':
            i += 1
            values = []
            while True:
                if i < n and text[i] == '"':
                    close = text.find('"', i + 1)
                    if close < 0:
                        raise ValueError('unterminated quoted parameter value')
                    values.append(text[i + 1:close])
                    i = close + 1
                else:
                    value_match = _CL_PARAM_VALUE.match(text, i)
                    values.append(value_match.group(0))
                    i = value_match.end()
                if i < n and text[i] == ',':
                    i += 1
                    continue
                break
            params.append((key, tuple(values)))
        else:
            params.append((key, None))
    if i >= n or text[i] != ':':
        raise ValueError('content line has no value separator')
    return ContentLine(group, name, tuple(params), text[i + 1:], line_start, line_end, text)


def physical_lines(data: bytes) -> tuple[list[str], str]:
    """Decode UTF-8 (BOM tolerated); invalid bytes fall back to Latin-1, flagged."""
    try:
        text, encoding = data.decode('utf-8-sig'), 'utf-8'
    except UnicodeDecodeError:
        text, encoding = data.decode('latin-1'), 'latin-1_fallback_invalid_utf8'
    return re.split(r'\r\n|\n|\r', text), encoding


def unescape_text(value: str) -> str:
    """RFC 5545 §3.3.11 / RFC 6350 §3.4 TEXT unescaping."""
    return re.sub(r'\\([\;,nN])', lambda m: '\n' if m.group(1) in 'nN' else m.group(1), value)


def split_unescaped(value: str, separator: str) -> list[str]:
    parts, current, i = [], '', 0
    while i < len(value):
        ch = value[i]
        if ch == '\\' and i + 1 < len(value):
            current += value[i:i + 2]
            i += 2
            continue
        if ch == separator:
            parts.append(current)
            current = ''
        else:
            current += ch
        i += 1
    parts.append(current)
    return parts

"""Mechanics shared by the traces importers (identical semantics only).

* ``signal()`` builds ``metadata.signal`` per the digital-traces contract
  (``docs/plans/DIGITAL_TRACES_2026-10-17.md``): class, basis, provider and
  whether the trace describes a device or an account — never a person.
* ``clean()``/``bounded()`` make raw JSON values storable: lone surrogates become
  U+FFFD (SQLite needs valid UTF-8), strings and containers are bounded with
  exact truncation counts; the preserved original stays the authority.
* ``write_event()`` writes one event and its time candidates in the caller's
  transaction (``evidence.add_event(db=...)`` + ``timeinfo.record_event_times``).
* ``append_row()`` is the append-only rule of this area's tables: an identical
  re-observation returns the existing row, a differing payload under the same
  unique key raises ``IntegrityError`` (a new interpretation needs a new parser
  version), nothing is updated.
* ``decode_latin1_utf8()`` is rule ``fb_latin1_utf8_v1``: Facebook/Instagram JSON
  downloads escape each UTF-8 byte as its own ``\\u00XX`` code point. The decoded
  text is a separate layer named by the rule; the exported string is kept raw.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from ..evidence import IntegrityError, add_event
from ..importers.comm_common import CommLimits, Issues
from ..timeinfo import TimeCandidate, record_event_times

SIGNAL_CLASSES = ('passive', 'active', 'declared')
SIGNAL_BASES = ('raw_sample', 'provider_inference', 'provider_user_edit', 'derived')
MOJIBAKE_RULE = 'fb_latin1_utf8_v1'
_SURROGATE = re.compile('[\ud800-\udfff]')


class OptInRequired(PermissionError):
    """An importer whose data class needs an explicit per-run opt-in was called without it."""


def signal(cls: str, basis: str, provider: str, device_or_account: str, **extra) -> dict:
    if cls not in SIGNAL_CLASSES or basis not in SIGNAL_BASES:
        raise ValueError(f'invalid signal class/basis: {cls}/{basis}')
    return {'class': cls, 'basis': basis, 'provider': provider, 'device_or_account': device_or_account, **extra}


def clean_text(value):
    if isinstance(value, str) and _SURROGATE.search(value):
        return _SURROGATE.sub('�', value)
    return value


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def canonical_sha256(value) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8', 'surrogatepass')).hexdigest()


def bounded(value, limits: CommLimits, _depth: int = 0) -> tuple[object, int]:
    """Storable copy of a JSON-like value: bounded strings/containers, no lone surrogates."""
    if isinstance(value, str):
        text = clean_text(value)
        return (text, 0) if len(text) <= limits.max_raw_chars else (text[:limits.max_raw_chars], 1)
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
            out[clean_text(str(key))[:256]], count = bounded(item, limits, _depth + 1)
            truncated += count
        return out, truncated
    if isinstance(value, (list, tuple)):
        items = []
        for index, item in enumerate(value):
            if index >= limits.max_raw_items:
                items.append({'__truncated_items__': len(value) - index})
                truncated += 1
                break
            copy, count = bounded(item, limits, _depth + 1)
            items.append(copy)
            truncated += count
        return items, truncated
    return {'representation': repr(value)[:limits.max_raw_chars], 'type': type(value).__name__}, 1


def bounded_body(text: str | None, limits: CommLimits) -> tuple[str | None, dict]:
    if text is None:
        return None, {'chars': 0, 'truncated': False}
    info = {'chars': len(text), 'truncated': False, 'sha256': text_sha256(text)}
    if _SURROGATE.search(text):
        text = clean_text(text)
        info['lone_surrogates_replaced'] = True
    if len(text) > limits.max_body_chars:
        text = text[:limits.max_body_chars]
        info.update(truncated=True, stored_chars=limits.max_body_chars)
    return text, info


def decode_latin1_utf8(text):
    """Rule ``fb_latin1_utf8_v1``: (decoded text, status). Never applied to text above U+00FF."""
    if not isinstance(text, str):
        return text, 'not_text'
    if all(ord(ch) < 0x80 for ch in text):
        return text, 'unchanged_ascii'
    try:
        data = text.encode('latin-1')
    except UnicodeEncodeError:
        return text, 'not_applicable_codepoint_above_ff'
    try:
        return data.decode('utf-8'), 'decoded'
    except UnicodeDecodeError:
        return text, 'not_applicable_invalid_utf8'


class TextLayers:
    """Collects raw → decoded pairs of one record under a decoding rule (or none)."""

    def __init__(self, rule: str | None, limits: CommLimits):
        self.rule, self.limits = rule, limits
        self.fields: dict[str, dict] = {}

    def text(self, field: str, value):
        if not isinstance(value, str):
            return None
        entry = None
        if self.rule is None:
            decoded, status = value, 'as_exported'
        else:
            decoded, status = decode_latin1_utf8(value)
            if status == 'decoded':
                raw, cut = bounded(value, self.limits)
                entry = {'raw': raw, 'raw_truncated': bool(cut), 'raw_sha256': text_sha256(value), 'status': status}
            elif status.startswith('not_applicable'):
                entry = {'status': status}
        if _SURROGATE.search(decoded):
            entry = entry or {'status': status}
            entry.update(lone_surrogates_replaced=True, raw_sha256=text_sha256(value))
        if entry is not None:
            self.fields[field] = entry
        return clean_text(decoded)

    def as_dict(self) -> dict:
        statuses = {}
        for item in self.fields.values():
            statuses[item['status']] = statuses.get(item['status'], 0) + 1
        return {'rule': self.rule, 'layer': 'decoded' if self.rule else 'as_exported',
                'fields': self.fields, 'status_counts': dict(sorted(statuses.items()))}


def write_event(db: sqlite3.Connection, candidates: list[TimeCandidate], **event) -> int:
    event_id = add_event(db=db, **event)
    if candidates:
        record_event_times(db, event_id, candidates)
    return event_id


def append_row(db: sqlite3.Connection, table: str, key: dict, values: dict) -> int:
    """Insert unless an identical row exists under ``key``; a differing one raises."""
    where = ' AND '.join(f'{column} IS ?' for column in key)
    row = db.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY id LIMIT 1', tuple(key.values())).fetchone()
    if row is not None:
        differing = [column for column, value in values.items() if row[column] != value]
        if differing:
            raise IntegrityError(f'{table} row conflicts with an earlier observation under the same key '
                                 f'({", ".join(differing[:5])}); a changed interpretation needs a new parser version')
        return int(row['id'])
    columns = {**key, **values}
    marks = ','.join('?' * len(columns))
    cur = db.execute(f'INSERT INTO {table}({",".join(columns)}) VALUES ({marks})', tuple(columns.values()))
    return int(cur.lastrowid)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class JsonTooLarge(ValueError):
    pass


def read_json(path: Path, limits: CommLimits, issues: Issues):
    """Decode a stored JSON snapshot within ``max_json_bytes``; NaN/Infinity are not JSON."""
    size = path.stat().st_size
    if size > limits.max_json_bytes:
        issues.add('document_size_limit', size_bytes=size, limit=limits.max_json_bytes)
        raise JsonTooLarge(f'JSON document of {size} bytes exceeds max_json_bytes={limits.max_json_bytes}')
    data = path.read_bytes()
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        raise ValueError('UTF-16 JSON is not supported (expected UTF-8)')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('utf-8', errors='replace')
        issues.add('invalid_utf8_replaced')

    def reject(name):
        raise ValueError(f'non-standard JSON constant {name}')
    try:
        return json.loads(text, parse_constant=reject)
    except json.JSONDecodeError as exc:
        raise ValueError(f'invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}') from exc


def strip_js_assignment(text: str) -> tuple[str, str | None]:
    """X/Twitter archive ``window.YTD.<name>.partN = [...]``: (JSON text, assigned name)."""
    match = re.match(r'\s*window\.YTD\.([A-Za-z0-9_]+)\.part(\d+)\s*=\s*', text)
    if not match:
        return text, None
    return text[match.end():], f'{match.group(1)}.part{match.group(2)}'


def snapshot_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


_INT = re.compile(r'-?\d{1,20}')
_DECIMAL = re.compile(r'-?\d{1,15}\.\d{1,9}')
YEAR_RANGE = (1800, 2200)


def epoch_time(kind: str, raw, source: str, *, unit: str, metadata: dict | None = None,
               zero_is_placeholder: bool = True) -> tuple[TimeCandidate | None, str | None]:
    """(candidate, problem) for an epoch value in the unit the format declares.

    The unit is never guessed from the magnitude: a value whose instant falls
    outside 1800–2200 in the declared unit is reported (``epoch_implausible_for_unit_<u>``)
    with no candidate. ``0`` is treated as an exporter placeholder by default.
    """
    from ..importers.comm_common import epoch_candidate
    from ..timeinfo import from_epoch
    if raw is None:
        return None, 'time_missing'
    if isinstance(raw, bool):
        return None, 'time_not_a_number'
    if isinstance(raw, int):
        text = str(raw)
    elif isinstance(raw, float):
        text = repr(raw)
    elif isinstance(raw, str):
        text = raw.strip()
    else:
        return None, 'time_not_a_number'
    try:
        if _INT.fullmatch(text):
            if zero_is_placeholder and int(text) == 0:
                return None, 'epoch_zero_placeholder'
            candidate = epoch_candidate(kind, text, source, unit=unit, metadata=metadata)
        elif _DECIMAL.fullmatch(text) and unit in ('s', 'ms'):
            candidate = from_epoch(kind, text, source, unit=unit, metadata=metadata)
        else:
            return None, 'time_unparsed'
    except (OverflowError, ValueError):
        return None, f'epoch_implausible_for_unit_{unit}'
    year = int(candidate.utc[:4]) if candidate.utc and candidate.utc[:4].isdigit() else None
    if year is None or not YEAR_RANGE[0] <= year <= YEAR_RANGE[1]:
        return None, f'epoch_implausible_for_unit_{unit}'
    return candidate, None


def mapping(name: str) -> tuple[dict, str]:
    """A versioned data file next to the importers and the SHA-256 of its exact bytes."""
    from ..importers.comm_common import DATA_DIR
    data = (DATA_DIR / name).read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()

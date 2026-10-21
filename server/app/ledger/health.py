"""Health/fitness exports: opt-in, samples and sessions (special category data, local only).

Every health importer refuses to run — and acquires nothing — unless the caller
passes ``enable_health=True`` for that run; the opt-in is recorded in the run
audit row and on every session event. No network call exists in this code.

Samples (heart rate, steps, sleep stages, daily summaries) are rows in
``health_samples``, never one event each. Sessions (sleep, workouts) are
``health_session`` events with start/end candidates and their durations. A
session is a device/app *inference* about a person (``signal.basis =
provider_inference``), not an observation of the person: the time budget counts
it as an estimate unless its parameters say otherwise (docs/LEDGER.md).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..db import session
from ..timeinfo import TimeCandidate, format_utc
from . import money, store, timeparse
from ..importers.comm_common import local_candidate, offset_candidate

OPT_IN = 'enable_health'
REFUSAL = ('health/fitness exports are special-category data: this import needs the explicit per-run opt-in '
           'enable_health (CLI --enable-health); processing stays local')
SAMPLE_BATCH = 2000
SLEEP_SESSION_RULE = 'ledger.sleep_session.v1'
DECIMAL_POINT = money.NumberFormat('.', ())


def require_opt_in(enable_health) -> None:
    if enable_health is not True:
        raise PermissionError(REFUSAL)


def opt_in_record() -> dict:
    return {OPT_IN: True, 'processing': 'local_only'}


def exact_value(raw) -> str | None:
    """Exact decimal text of a numeric value written with a decimal point (else None)."""
    if raw is None:
        return None
    parsed = money.parse_number(str(raw), DECIMAL_POINT)
    return parsed['exact'] if parsed['status'] == 'ok' else None


def time_candidate(kind: str, raw: str | None, source: str, formats: list[str], *, metadata: dict | None = None,
                   two_digit_year_base: int | None = None) -> tuple[TimeCandidate | None, str | None]:
    """(candidate, problem) for a provider timestamp under explicit formats."""
    if raw is None or not str(raw).strip():
        return None, None
    parsed = timeparse.parse(str(raw), formats, two_digit_year_base=two_digit_year_base)
    if parsed['status'] != 'ok':
        return None, f"time_{parsed['status']}"
    meta = {'format': parsed['format'], **(metadata or {})}
    if 'year_rule' in parsed:
        meta['year_rule'] = parsed['year_rule']
    if parsed['offset_minutes'] is not None:
        return offset_candidate(kind, str(raw), source, parsed['naive'], parsed['offset_minutes'], parsed['precision'],
                                {**meta, 'zone_status': 'offset_stated_by_export'}), None
    return local_candidate(kind, str(raw), source, parsed['naive'], parsed['precision'],
                           {**meta, 'zone_status': 'export_zone_not_stated'}), None


def utc_wall_clock_candidate(kind: str, raw: str, source: str, formats: list[str], offset_raw: str | None,
                             metadata: dict | None = None) -> tuple[TimeCandidate | None, str | None]:
    """A UTC wall clock with a separate local offset column (``UTC+0100``) — as the mapping declares."""
    parsed = timeparse.parse(raw, formats)
    if parsed['status'] != 'ok':
        return None, f"time_{parsed['status']}"
    aware = parsed['naive'].replace(tzinfo=timezone.utc)
    meta = {'format': parsed['format'], 'time_basis': 'utc_wall_clock_with_offset_column', 'offset_raw': offset_raw,
            **(metadata or {})}
    offset = parse_offset_label(offset_raw)
    if offset is None:
        if offset_raw:
            meta['offset_status'] = 'unparsed'
        return TimeCandidate(kind, raw, source, utc=format_utc(aware), tz_status='explicit_offset', tz='UTC',
                             precision=parsed['precision'], metadata=meta), ('offset_column_unparsed' if offset_raw else None)
    local = (aware + timedelta(minutes=offset)).replace(tzinfo=None)
    sign = '+' if offset >= 0 else '-'
    hours, minutes = divmod(abs(offset), 60)
    return TimeCandidate(kind, raw, source, utc=format_utc(aware), local=local.isoformat(), tz_status='explicit_offset',
                         tz=f'{sign}{hours:02d}:{minutes:02d}', precision=parsed['precision'], metadata=meta), None


def parse_offset_label(text: str | None) -> int | None:
    """``UTC+0100`` / ``+01:00`` / ``GMT-0530`` → signed minutes (None when not an offset)."""
    if not text:
        return None
    match = re.fullmatch(r'\s*(?:UTC|GMT)?\s*([+-])(\d{2}):?(\d{2})\s*', str(text))
    if not match:
        return None
    hours, minutes = int(match.group(2)), int(match.group(3))
    if minutes >= 60 or hours * 60 + minutes > 24 * 60 - 1:
        return None
    return (hours * 60 + minutes) * (-1 if match.group(1) == '-' else 1)


def instant(candidate: TimeCandidate | None) -> tuple[str, datetime] | None:
    """Comparable instant: ('utc', naive UTC) when the zone is known, else ('local', wall clock)."""
    if candidate is None:
        return None
    if candidate.utc:
        return 'utc', datetime.fromisoformat(candidate.utc.replace('Z', '+00:00')).replace(tzinfo=None)
    if candidate.local:
        return 'local', datetime.fromisoformat(candidate.local)
    return None


def duration(start: TimeCandidate | None, end: TimeCandidate | None) -> dict:
    a, b = instant(start), instant(end)
    if a is None or b is None:
        return {'status': 'not_computable', 'reason': 'missing_endpoint'}
    if a[0] != b[0]:
        return {'status': 'not_computable', 'reason': 'endpoints_differ_in_zone_status'}
    seconds = (b[1] - a[1]).total_seconds()
    out = {'status': 'ok' if seconds >= 0 else 'negative', 'seconds': seconds,
           'basis': 'utc_difference' if a[0] == 'utc' else 'wall_clock_difference_zone_unknown'}
    if a[0] == 'local':
        out['caveat'] = 'a DST change between the endpoints would shift the true duration by the offset change'
    return out


def time_columns(prefix: str, candidate: TimeCandidate | None) -> dict:
    if candidate is None:
        return {f'{prefix}_raw': None, f'{prefix}_utc': None, f'{prefix}_local': None,
                f'{prefix}_tz_status': 'unknown' if prefix == 'start' else None, f'{prefix}_tz': None}
    return {f'{prefix}_raw': candidate.raw, f'{prefix}_utc': candidate.utc, f'{prefix}_local': candidate.local,
            f'{prefix}_tz_status': candidate.tz_status, f'{prefix}_tz': candidate.tz}


def sample_row(*, artifact_id: int, parser: str, parser_version: str, position: str, sample_type: str,
               sample_class: str, value_raw, unit: str | None, start: TimeCandidate | None, end: TimeCandidate | None,
               source_name: str | None, source_version: str | None, device_raw: str | None, basis: str,
               session_event_id: int | None = None, metadata: dict | None = None) -> dict:
    value_text = None if value_raw is None else str(value_raw)
    return {'artifact_id': artifact_id, 'parser': parser, 'parser_version': parser_version, 'source_position': position,
            'sample_type': sample_type, 'sample_class': sample_class, 'value_raw': value_text,
            'value_exact': exact_value(value_text), 'unit': unit, **time_columns('start', start),
            **time_columns('end', end), 'precision': (start.precision if start else 'unknown'),
            'source_name': source_name, 'source_version': source_version, 'device_raw': device_raw, 'basis': basis,
            'session_event_id': session_event_id, 'metadata_json': metadata or {}}


class SampleWriter:
    """Batched, idempotent sample inserts (one transaction per batch)."""

    def __init__(self):
        self.pending: list[dict] = []
        self.inserted = 0
        self.existing = 0

    def add(self, row: dict) -> None:
        self.pending.append(row)
        if len(self.pending) >= SAMPLE_BATCH:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        with session() as db:
            db.execute('BEGIN IMMEDIATE')
            result = store.insert_many(db, 'health_samples', self.pending)
        self.inserted += result['inserted']
        self.existing += result['existing']
        self.pending = []


def session_signal(provider: str, source: str | None, *, cls: str = 'passive', basis: str = 'provider_inference',
                   **extra) -> dict:
    return store.signal(cls, basis, provider, source, **extra)


def write_session(event: dict, candidates: list[TimeCandidate], samples: list[dict]) -> int:
    """Insert one session event with its candidates, then its samples (linked) in one transaction."""
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        event_id = store.add_event(db, candidates, **event)
        for offset in range(0, len(samples), SAMPLE_BATCH):
            store.insert_many(db, 'health_samples', [{**s, 'session_event_id': event_id}
                                                     for s in samples[offset:offset + SAMPLE_BATCH]])
    return event_id


def session_event(*, source_id: int, artifact_id: int, external_id: str, kind: str, activity_raw: str | None,
                  start: TimeCandidate | None, end: TimeCandidate | None, provider: str, source_name: str | None,
                  signal: dict, metadata: dict) -> dict:
    from ..importers.comm_common import legacy_instant
    span = duration(start, end)
    return {'source_id': source_id, 'artifact_id': artifact_id, 'event_type': 'health_session',
            'ts_start': legacy_instant(start), 'ts_end': legacy_instant(end), 'direction': None, 'contact_label': None,
            'phone_or_address': None, 'subject': f'{kind}: {activity_raw}' if activity_raw else kind, 'body': None,
            'thread_id': f'health:{provider}:{source_name or "unknown_source"}', 'external_id': external_id,
            'confidence': None,
            'metadata': {**metadata, 'session': {'kind': kind, 'activity_raw': activity_raw, 'span': span,
                                                 **metadata.get('session', {})},
                         'signal': signal, 'opt_in': opt_in_record()}}


def group_sleep(samples: list[dict], gap_s: int) -> list[list[dict]]:
    """Rule ``ledger.sleep_session.v1``: per (source, device), chronological; a sample starting at most
    ``gap_s`` after the current session's end joins it. Different sources are never merged."""
    groups: dict[tuple, list[dict]] = {}
    for sample in samples:
        start = instant(sample['start'])
        if start is None:
            continue
        groups.setdefault((sample.get('source') or '', sample.get('device') or '', start[0]), []).append(sample)
    sessions: list[list[dict]] = []
    for key in sorted(groups):
        items = sorted(groups[key], key=lambda s: (instant(s['start'])[1], s['position']))
        current: list[dict] = []
        current_end = None
        for sample in items:
            start = instant(sample['start'])[1]
            end = instant(sample['end'])[1] if instant(sample['end']) and instant(sample['end'])[0] == key[2] else start
            if current and start - current_end > timedelta(seconds=gap_s):
                sessions.append(current)
                current, current_end = [], None
            current.append(sample)
            current_end = end if current_end is None else max(current_end, end)
        if current:
            sessions.append(current)
    return sessions

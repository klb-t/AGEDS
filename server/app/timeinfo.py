"""Observed time values with explicit zone status, precision and origin.

A ``TimeCandidate`` records what a source said, not when something happened.
``utc`` is filled only when the offset is known from the source (or the source
uses an epoch). Wall-clock values without an offset keep ``local`` and
``tz_status='unknown'``; an operator-assumed zone is a separate status that can
be revised without touching the raw value. Daylight-saving folds and gaps under
an assumed zone are reported as ambiguity, never silently resolved.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIME_KINDS = (
    'sent', 'received', 'created', 'updated', 'start', 'end', 'file_mtime', 'exif',
    'filename_derived', 'export_time', 'observed', 'header_date', 'other',
)
TZ_STATUSES = ('explicit_offset', 'utc_epoch', 'assumed_zone', 'unknown')
PRECISIONS = ('year', 'month', 'day', 'hour', 'minute', 'second', 'millisecond', 'microsecond', 'unknown')


@dataclass(frozen=True)
class TimeCandidate:
    kind: str
    raw: str | None
    source: str
    utc: str | None = None
    local: str | None = None
    tz_status: str = 'unknown'
    tz: str | None = None
    precision: str = 'unknown'
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in TIME_KINDS:
            raise ValueError(f'unknown time kind: {self.kind}')
        if self.tz_status not in TZ_STATUSES:
            raise ValueError(f'unknown tz_status: {self.tz_status}')
        if self.precision not in PRECISIONS:
            raise ValueError(f'unknown precision: {self.precision}')
        if self.tz_status in ('explicit_offset', 'utc_epoch') and not self.utc:
            raise ValueError('an offset-bearing candidate requires utc')
        if self.tz_status == 'unknown' and self.utc:
            raise ValueError('utc cannot be derived for an unknown zone')

    def as_dict(self) -> dict:
        return asdict(self)


def format_utc(value: datetime) -> str:
    """ISO-8601 UTC with a trailing Z; microseconds only when present."""
    value = value.astimezone(timezone.utc)
    text = value.strftime('%Y-%m-%dT%H:%M:%S')
    if value.microsecond:
        text += f'.{value.microsecond:06d}'
    return text + 'Z'


def _precision_of_epoch(raw: str) -> str:
    if '.' in raw:
        decimals = len(raw.split('.', 1)[1])
        return 'millisecond' if decimals <= 3 else 'microsecond'
    return 'second'


def from_epoch(kind: str, raw: int | float | str, source: str, *, unit: str = 's', metadata: dict | None = None) -> TimeCandidate:
    """Epoch seconds/milliseconds; the raw spelling is preserved verbatim."""
    raw_text = raw if isinstance(raw, str) else json.dumps(raw)
    number = float(raw_text)
    if number != number or number in (float('inf'), float('-inf')):
        raise ValueError('non-finite epoch')
    seconds = number / 1000.0 if unit == 'ms' else number
    moment = datetime.fromtimestamp(0, timezone.utc) + timedelta(seconds=seconds)
    precision = 'millisecond' if unit == 'ms' else _precision_of_epoch(raw_text)
    return TimeCandidate(kind, raw_text, source, utc=format_utc(moment), tz_status='utc_epoch', tz='UTC',
                         precision=precision, metadata={'epoch_unit': unit, **(metadata or {})})


_ISO = re.compile(r'^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$')


def from_iso(kind: str, raw: str, source: str, *, metadata: dict | None = None) -> TimeCandidate:
    """Parse ISO-8601-like text; an absent offset stays a local wall-clock time."""
    match = _ISO.match(raw.strip())
    if not match:
        raise ValueError(f'not an ISO-like timestamp: {raw!r}')
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    precision = 'day' if hour is None else 'minute' if second is None else 'second'
    micro = 0
    if fraction:
        precision = 'millisecond' if len(fraction) <= 3 else 'microsecond'
        micro = int((fraction + '000000')[:6])
    naive = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0), micro)
    meta = dict(metadata or {})
    if fraction and len(fraction) > 6:
        meta['fraction_truncated_to_microseconds'] = True
    local_text = naive.isoformat()
    if offset is None:
        return TimeCandidate(kind, raw, source, local=local_text, tz_status='unknown', precision=precision, metadata=meta)
    if offset == 'Z':
        tzinfo, tz_name = timezone.utc, 'UTC'
    else:
        sign = 1 if offset[0] == '+' else -1
        digits = offset[1:].replace(':', '')
        delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        tzinfo, tz_name = timezone(sign * delta), offset[0] + digits[:2] + ':' + digits[2:]
    aware = naive.replace(tzinfo=tzinfo)
    return TimeCandidate(kind, raw, source, utc=format_utc(aware), local=local_text, tz_status='explicit_offset',
                         tz=tz_name, precision=precision, metadata=meta)


def assume_zone(candidate: TimeCandidate, zone: str, *, rule: str) -> TimeCandidate:
    """Derive a revisable UTC projection for a local time under an assumed zone.

    DST folds return the earlier instant and list both instants; DST gaps (a
    wall-clock time that never existed) keep ``utc`` empty and stay unknown.
    """
    if candidate.local is None:
        raise ValueError('candidate has no local wall-clock value')
    try:
        info = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f'unknown zone: {zone}') from exc
    naive = datetime.fromisoformat(candidate.local)
    first = naive.replace(tzinfo=info, fold=0)
    second = naive.replace(tzinfo=info, fold=1)
    meta = {**candidate.metadata, 'assumption_rule': rule, 'assumed_zone': zone}
    roundtrip = first.astimezone(timezone.utc).astimezone(info).replace(tzinfo=None)
    if roundtrip != naive:
        meta['dst'] = 'nonexistent_local_time'
        return TimeCandidate(candidate.kind, candidate.raw, candidate.source, local=candidate.local,
                             tz_status='unknown', tz=None, precision=candidate.precision, metadata=meta)
    if first.utcoffset() != second.utcoffset():
        meta['dst'] = 'ambiguous_fold'
        meta['utc_alternatives'] = [format_utc(first), format_utc(second)]
    return TimeCandidate(candidate.kind, candidate.raw, candidate.source, utc=format_utc(first), local=candidate.local,
                         tz_status='assumed_zone', tz=zone, precision=candidate.precision, metadata=meta)


def record_event_times(db: sqlite3.Connection, event_id: int, candidates: list[TimeCandidate]) -> list[int]:
    """Append candidates inside the caller's transaction; exact repeats are reused."""
    ids = []
    for c in candidates:
        metadata_json = json.dumps(c.metadata, ensure_ascii=False, sort_keys=True, allow_nan=False)
        existing = db.execute('SELECT * FROM event_times WHERE event_id=? AND kind=? AND source=? AND raw IS ?',
                              (event_id, c.kind, c.source, c.raw)).fetchone()
        if existing:
            if (existing['utc'], existing['local'], existing['tz_status'], existing['tz'], existing['precision'], existing['metadata_json']) != \
               (c.utc, c.local, c.tz_status, c.tz, c.precision, metadata_json):
                raise ValueError('time candidate conflicts with an earlier interpretation; use a new source/parser version')
            ids.append(int(existing['id']))
            continue
        cur = db.execute('''INSERT INTO event_times(event_id,kind,raw,utc,local,tz_status,tz,precision,source,metadata_json)
                            VALUES(?,?,?,?,?,?,?,?,?,?)''',
                         (event_id, c.kind, c.raw, c.utc, c.local, c.tz_status, c.tz, c.precision, c.source, metadata_json))
        ids.append(int(cur.lastrowid))
    return ids

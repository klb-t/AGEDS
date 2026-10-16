"""Calendar buckets in a stated IANA zone (DST-aware day lengths).

A day is ``[local midnight, next local midnight)`` converted to UTC epoch
seconds, so a spring-forward day lasts 23 h and a fall-back day 25 h. Weeks are
ISO weeks (Monday 00:00 local) and months start on the 1st at 00:00 local; both
are unions of days. Buckets cut by the analysis window are marked ``partial``
and keep their clipped length.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GRANULARITIES = ('day', 'week', 'month')
MAX_WINDOW_DAYS = 3700
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def zone_info(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name:
        raise ValueError('zone must be an IANA zone name such as Europe/Warsaw')
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f'unknown zone: {name}') from exc


def local_midnight(day: date, zone: ZoneInfo) -> float:
    return datetime.combine(day, time(0), tzinfo=zone).timestamp()


def parse_bound(text: str, zone: ZoneInfo) -> float:
    """``YYYY-MM-DD`` is local midnight in ``zone``; other values need an explicit offset or ``Z``."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('window bound is required')
    text = text.strip()
    if _DATE.match(text):
        return local_midnight(date.fromisoformat(text), zone)
    try:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'not a date or ISO timestamp: {text!r}') from exc
    if moment.tzinfo is None:
        raise ValueError(f'timestamp without offset is ambiguous: {text!r}; use a date or add an offset')
    return moment.timestamp()


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def local_date(epoch: float, zone: ZoneInfo) -> date:
    return datetime.fromtimestamp(epoch, zone).date()


def days(lo: float, hi: float, zone: ZoneInfo) -> list[dict]:
    """Days intersecting ``[lo, hi)`` with nominal and clipped lengths."""
    if not hi > lo:
        raise ValueError('window end must be after its start')
    first, last = local_date(lo, zone), local_date(hi - 1e-6, zone)
    if (last - first).days > MAX_WINDOW_DAYS:
        raise ValueError(f'window longer than {MAX_WINDOW_DAYS} days')
    out, d = [], first
    while d <= last:
        start, end = local_midnight(d, zone), local_midnight(d + timedelta(days=1), zone)
        a, b = max(start, lo), min(end, hi)
        if b > a:
            iso_year, iso_week, _ = d.isocalendar()
            out.append({'key': d.isoformat(), 'date': d, 'start': a, 'end': b, 'nominal_length_s': end - start,
                        'length_s': b - a, 'partial': (a, b) != (start, end),
                        'week': f'{iso_year}-W{iso_week:02d}', 'month': d.strftime('%Y-%m')})
        d += timedelta(days=1)
    return out


def buckets(day_list: list[dict], granularity: str) -> list[dict]:
    """Group days into ``granularity`` buckets; each lists its day indexes."""
    if granularity not in GRANULARITIES:
        raise ValueError(f'granularity must be one of {GRANULARITIES}')
    out: list[dict] = []
    for index, d in enumerate(day_list):
        key = d['key'] if granularity == 'day' else d[granularity]
        if out and out[-1]['key'] == key:
            b = out[-1]
            b['end'] = d['end']
            b['length_s'] += d['length_s']
            b['nominal_length_s'] += d['nominal_length_s']
            b['partial'] = b['partial'] or d['partial']
            b['days'].append(index)
        else:
            out.append({'key': key, 'start': d['start'], 'end': d['end'], 'length_s': d['length_s'],
                        'nominal_length_s': d['nominal_length_s'], 'partial': d['partial'], 'days': [index]})
    for b in out:
        if granularity == 'week':
            b['partial'] = b['partial'] or len(b['days']) != 7
        elif granularity == 'month':
            first = day_list[b['days'][0]]['date']
            next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
            b['partial'] = b['partial'] or len(b['days']) != (next_month - first.replace(day=1)).days
    return out

"""Integer microsecond instants and calendar units for the timeline projection.

The projection sorts by integer microseconds since 1970-01-01T00:00:00Z. A
wall-clock value without a zone is projected "as if UTC" only together with an
interval covering every real UTC offset, and that basis is always reported.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
US = 1_000_000
MIN_US = -62135596800 * US          # 0001-01-01T00:00:00Z
MAX_US = 253402300799 * US + 999999  # 9999-12-31T23:59:59.999999Z

_INPUT = re.compile(r'^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$')


class TimeInputError(ValueError):
    """A caller-provided window bound is not an accepted ISO instant."""


def to_us(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = moment - EPOCH
    return (delta.days * 86400 + delta.seconds) * US + delta.microseconds


def from_us(value: int) -> datetime:
    return EPOCH + timedelta(microseconds=value)


def iso(value: int | None) -> str | None:
    if value is None:
        return None
    if not MIN_US <= value <= MAX_US:
        return None
    moment = from_us(value)
    text = moment.strftime('%Y-%m-%dT%H:%M:%S')
    if moment.microsecond:
        text += f'.{moment.microsecond:06d}'
    return text + 'Z'


def utc_text_to_us(text: str) -> int:
    """Parse a stored UTC/offset ISO string (``...Z`` or ``+HH:MM``)."""
    moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('utc value lacks an offset')
    return to_us(moment)


def local_text_to_us(text: str) -> int:
    """Wall-clock ISO text read as if it were UTC (caller reports that basis)."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)
    return to_us(moment.replace(tzinfo=timezone.utc))


def parse_bound(text: str | None, *, end: bool = False) -> int | None:
    """Window bounds: ``2024``, ``2024-03``, ``2024-03-05``, full ISO with optional offset.

    A bound without an offset is read as UTC (the timeline axis is UTC). A
    partial ``to`` bound is exclusive at the start of the following unit.
    """
    if text is None or text == '':
        return None
    if not isinstance(text, str) or len(text) > 40:
        raise TimeInputError('time bound must be ISO text')
    match = _INPUT.match(text.strip())
    if not match:
        raise TimeInputError(f'not an ISO time bound: {text!r}')
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    try:
        naive = datetime(int(year), int(month or 1), int(day or 1), int(hour or 0), int(minute or 0),
                         int(second or 0), int((fraction or '0').ljust(6, '0')))
    except ValueError as exc:
        raise TimeInputError(str(exc)) from exc
    if offset in (None, 'Z'):
        tz = timezone.utc
    else:
        digits = offset[1:].replace(':', '')
        delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        tz = timezone(delta if offset[0] == '+' else -delta)
    try:
        value = to_us(naive.replace(tzinfo=tz))
    except OverflowError as exc:
        raise TimeInputError('time bound out of range') from exc
    if end and hour is None:
        # A date-only upper bound ("2024-03") names a whole unit; its exclusive
        # end is the start of the next unit. Bounds with a clock are literal.
        value = add_unit(value, 'day' if day else 'month' if month else 'year')
    return value


UNIT_US = {'minute': 60 * US, 'hour': 3600 * US, 'day': 86400 * US, 'week': 7 * 86400 * US,
           'second': US, 'millisecond': 1000, 'microsecond': 1}


def add_unit(value: int, unit: str, count: int = 1) -> int:
    """Add calendar months/years (UTC) or a fixed unit."""
    if unit in UNIT_US:
        return value + count * UNIT_US[unit]
    moment = from_us(value)
    if unit == 'year':
        months = 12 * count
    elif unit == 'month':
        months = count
    else:
        raise ValueError(f'unknown unit {unit}')
    total = moment.year * 12 + (moment.month - 1) + months
    year, month = divmod(total, 12)
    try:
        shifted = moment.replace(year=year, month=month + 1, day=1)
    except ValueError:
        return MAX_US if months > 0 else MIN_US
    return to_us(shifted)


def floor_unit(value: int, unit: str) -> int:
    """Start of the UTC calendar unit containing ``value`` (weeks start Monday)."""
    if unit in ('minute', 'hour', 'day'):
        size = UNIT_US[unit]
        return value - (value % size)
    if unit == 'week':
        monday = 4 * 86400 * US  # 1970-01-05 was a Monday
        size = UNIT_US['week']
        return value - ((value - monday) % size)
    moment = from_us(value)
    if unit == 'month':
        return to_us(moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    if unit == 'year':
        return to_us(moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0))
    raise ValueError(f'unknown unit {unit}')


def precision_span(start: int, precision: str) -> int:
    """Width of the unit named by a candidate's precision (0 for second or finer)."""
    if precision in ('minute', 'hour', 'day'):
        return UNIT_US[precision]
    if precision in ('month', 'year'):
        return add_unit(start, precision) - start
    return 0

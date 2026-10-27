"""Format-driven date/time parsing for profile data (no locale, no guessing).

A profile names the formats a column uses, e.g. ``DD.MM.YYYY``, ``YYYYMMDD``,
``MM/DD/YY HH:mm:ss`` or ``YYYY-MM-DD HH:mm:ss ZZ[ UTC]``. Tokens:

``YYYY`` ``YY`` (needs an explicit ``two_digit_year_base``) ``MM`` ``M`` ``DD``
``D`` ``HH`` ``H`` ``mm`` ``ss`` ``SSS`` (fraction, 1–9 digits) ``ZZ``
(``Z``, ``+hh:mm``, ``+hhmm``) ``A`` (AM/PM); ``[...]`` is literal text and a
space matches any run of white space. The first format that matches the whole
value wins and is recorded. Precision follows the tokens present.
"""
from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache

_TOKENS = (
    ('YYYY', r'(?P<Y>\d{4})'), ('SSS', r'(?P<f>\d{1,9})'), ('YY', r'(?P<y>\d{2})'), ('MM', r'(?P<M>\d{2})'),
    ('DD', r'(?P<D>\d{2})'), ('HH', r'(?P<h>\d{2})'), ('mm', r'(?P<m>\d{2})'), ('ss', r'(?P<s>\d{2})'),
    ('ZZ', r'(?P<z>Z|[+-]\d{2}:?\d{2})'), ('M', r'(?P<M>\d{1,2})'), ('D', r'(?P<D>\d{1,2})'),
    ('H', r'(?P<h>\d{1,2})'), ('A', r'(?P<a>[AaPp]\.?[Mm]\.?)'),
)


@lru_cache(maxsize=256)
def compile_format(fmt: str) -> tuple[re.Pattern, frozenset]:
    pattern, tokens, index = '', set(), 0
    while index < len(fmt):
        if fmt[index] == '[':
            close = fmt.find(']', index)
            if close < 0:
                raise ValueError(f'unterminated literal in format {fmt!r}')
            pattern += re.escape(fmt[index + 1:close])
            index = close + 1
            continue
        if fmt[index].isspace():
            while index < len(fmt) and fmt[index].isspace():
                index += 1
            pattern += r'\s+'
            continue
        for token, regex in _TOKENS:
            if fmt.startswith(token, index):
                if token in tokens or (token[0] in 'YMDHmsSZA' and any(t[0] == token[0] for t in tokens)):
                    raise ValueError(f'repeated field {token!r} in format {fmt!r}')
                pattern += regex
                tokens.add(token)
                index += len(token)
                break
        else:
            pattern += re.escape(fmt[index])
            index += 1
    return re.compile(r'\s*' + pattern + r'\s*'), frozenset(tokens)


def _precision(tokens: frozenset, fraction: str | None) -> str:
    if fraction:
        return 'millisecond' if len(fraction) <= 3 else 'microsecond'
    if 'ss' in tokens:
        return 'second'
    if 'mm' in tokens:
        return 'minute'
    if 'HH' in tokens or 'H' in tokens:
        return 'hour'
    if 'DD' in tokens or 'D' in tokens:
        return 'day'
    if 'MM' in tokens or 'M' in tokens:
        return 'month'
    return 'year'


def parse(text: str | None, formats: list[str], *, two_digit_year_base: int | None = None) -> dict:
    """Parse ``text`` with the first matching format; never raises for data problems."""
    if text is None or not str(text).strip():
        return {'status': 'missing'}
    value = str(text)
    for fmt in formats:
        regex, tokens = compile_format(fmt)
        match = regex.fullmatch(value)
        if not match:
            continue
        g = match.groupdict()
        meta = {'format': fmt}
        if g.get('Y') is not None:
            year = int(g['Y'])
        elif g.get('y') is not None:
            if two_digit_year_base is None:
                return {'status': 'two_digit_year_century_not_assumed', 'format': fmt}
            year = two_digit_year_base + int(g['y'])
            meta['year_rule'] = f'two_digit_year_base:{two_digit_year_base}'
        else:
            year = 1900
            meta['date_part'] = 'absent'
        month = int(g['M']) if g.get('M') is not None else 1
        day = int(g['D']) if g.get('D') is not None else 1
        hour = int(g['h']) if g.get('h') is not None else 0
        if g.get('a'):
            if not 1 <= hour <= 12:
                return {'status': 'invalid_clock_value', 'format': fmt}
            hour = hour % 12 + (12 if g['a'][0].lower() == 'p' else 0)
        minute = int(g['m']) if g.get('m') is not None else 0
        second = int(g['s']) if g.get('s') is not None else 0
        fraction = g.get('f')
        micro = int((fraction + '000000')[:6]) if fraction else 0
        if fraction and len(fraction) > 6:
            meta['fraction_truncated_to_microseconds'] = True
        try:
            naive = datetime(year, month, day, hour, minute, second, micro)
        except ValueError:
            return {'status': 'invalid_calendar_value', 'format': fmt}
        offset = None
        if g.get('z'):
            zone = g['z']
            if zone == 'Z':
                offset = 0
            else:
                digits = zone[1:].replace(':', '')
                hours, minutes = int(digits[:2]), int(digits[2:])
                if minutes >= 60 or hours * 60 + minutes > 24 * 60 - 1:
                    return {'status': 'offset_out_of_range', 'format': fmt, 'offset_raw': zone}
                offset = (hours * 60 + minutes) * (-1 if zone[0] == '-' else 1)
            meta['offset_raw'] = zone
        return {'status': 'ok', 'naive': naive, 'precision': _precision(tokens, fraction), 'offset_minutes': offset,
                'has_date': meta.get('date_part') != 'absent', 'has_time': bool(tokens & {'HH', 'H'}), **meta}
    return {'status': 'unparsed'}

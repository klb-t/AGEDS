"""Exact amounts: decimal text, currency and integer minor units — never floats.

An amount is parsed from the source's text under an explicit number format
(decimal separator, allowed thousands separators, negative notations). The
result keeps the raw text, the exact decimal string (scale as written), the
sign notation that was read and, when the currency's ISO 4217 exponent is
known, the integer number of minor units. Ambiguity (``1,234`` under an
``auto`` decimal policy) is reported with both readings instead of a guess.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

MINUS_SIGNS = '-−‒–'
SPACE_LIKE = '    '
NEGATIVE_FORMATS = ('leading_minus', 'trailing_minus', 'parentheses')
_DATA = Path(__file__).with_name('data') / 'currencies.v1.json'
_CURRENCIES: dict | None = None


def currencies() -> dict:
    global _CURRENCIES
    if _CURRENCIES is None:
        _CURRENCIES = json.loads(_DATA.read_text(encoding='utf-8'))
    return _CURRENCIES


@dataclass(frozen=True)
class NumberFormat:
    decimal: str = ','  # ',' | '.' | 'auto'
    thousands: tuple[str, ...] = ()
    negative: tuple[str, ...] = NEGATIVE_FORMATS

    @classmethod
    def from_spec(cls, spec: dict | None) -> 'NumberFormat':
        spec = spec or {}
        decimal = spec.get('decimal', 'auto')
        if decimal not in (',', '.', 'auto'):
            raise ValueError("number.decimal must be ',', '.' or 'auto'")
        thousands = tuple(spec.get('thousands', ()))
        if decimal in thousands:
            raise ValueError('the decimal separator cannot also be a thousands separator')
        negative = tuple(spec.get('negative', NEGATIVE_FORMATS))
        unknown = set(negative) - set(NEGATIVE_FORMATS)
        if unknown:
            raise ValueError(f'unknown negative formats: {sorted(unknown)}')
        return cls(decimal, thousands, negative)

    def as_dict(self) -> dict:
        return {'decimal': self.decimal, 'thousands': list(self.thousands), 'negative': list(self.negative)}


def _strip(text: str) -> str:
    return text.strip(SPACE_LIKE + '\t\r\n')


def _split_sign(text: str) -> tuple[str | None, str, str | None]:
    """(sign, body, notation) or (None, text, 'conflicting_signs')."""
    body = _strip(text)
    notations: list[tuple[str, str]] = []
    if len(body) >= 2 and body.startswith('(') and body.endswith(')'):
        notations.append(('parentheses', '-'))
        body = _strip(body[1:-1])
    if body and body[0] in MINUS_SIGNS + '+':
        notations.append(('leading_plus', '+') if body[0] == '+' else ('leading_minus', '-'))
        body = _strip(body[1:])
    if body and body[-1] in MINUS_SIGNS + '+':
        notations.append(('trailing_plus', '+') if body[-1] == '+' else ('trailing_minus', '-'))
        body = _strip(body[:-1])
    if len(notations) > 1:
        return None, body, 'conflicting_signs'
    if not notations:
        return None, body, None
    name, sign = notations[0]
    return sign, body, name


def _reading(body: str, decimal: str, thousands: tuple[str, ...]) -> tuple[str, str] | None:
    """(integer digits, fraction digits) when ``body`` is valid under this separator pair."""
    if not body or not body[0].isdigit() and body[0] != decimal:
        return None
    allowed = set('0123456789') | {decimal} | set(thousands)
    if any(ch not in allowed for ch in body):
        return None
    if body.count(decimal) > 1:
        return None
    integer, _, fraction = body.partition(decimal)
    if decimal in body and not fraction:
        return None
    if fraction and not fraction.isdigit():
        return None
    used = {ch for ch in integer if not ch.isdigit()}
    if used:
        if len(used) > 1:
            return None
        separator = used.pop()
        groups = integer.split(separator)
        if not groups[0] or len(groups[0]) > 3 or any(len(g) != 3 or not g.isdigit() for g in groups[1:]):
            return None
        integer = ''.join(groups)
    if not integer:
        integer = '0'
    if not integer.isdigit():
        return None
    return integer, fraction


def _exact(sign: str | None, integer: str, fraction: str) -> str:
    integer = integer.lstrip('0') or '0'
    text = integer + ('.' + fraction if fraction else '')
    if sign == '-' and Decimal(text) != 0:
        return '-' + text
    return text


def parse_number(raw: str | None, fmt: NumberFormat) -> dict:
    """Parse a decimal number written with the format's separators; exact text only."""
    if raw is None or not _strip(str(raw)):
        return {'status': 'missing', 'raw': raw}
    text = str(raw)
    sign, body, notation = _split_sign(text)
    result: dict = {'raw': text}
    if notation == 'conflicting_signs':
        return {**result, 'status': 'unparsed', 'reason': 'conflicting_sign_notations'}
    if notation in ('leading_minus', 'trailing_minus', 'parentheses') and notation not in fmt.negative:
        result['notation_not_in_profile'] = notation
    if fmt.decimal == 'auto':
        options = {',': tuple(t for t in ('.', "'") + tuple(SPACE_LIKE) if t != ','),
                   '.': tuple(t for t in (',', "'") + tuple(SPACE_LIKE) if t != '.')}
        readings = {}
        for decimal, thousands in options.items():
            parsed = _reading(body, decimal, thousands)
            if parsed is not None:
                readings[decimal] = _exact(sign, *parsed)
        if not readings:
            return {**result, 'status': 'unparsed', 'reason': 'not_a_number_under_any_separator_reading'}
        values = {Decimal(v) for v in readings.values()}
        if len(values) > 1:
            return {**result, 'status': 'ambiguous', 'reason': 'decimal_separator_ambiguous',
                    'readings': [{'decimal': d, 'exact': v} for d, v in sorted(readings.items())]}
        decimal = sorted(readings)[0] if len(readings) > 1 else next(iter(readings))
        exact = readings[decimal]
        result['decimal_inferred'] = decimal if len(readings) == 1 else 'equivalent_readings'
    else:
        parsed = _reading(body, fmt.decimal, fmt.thousands)
        if parsed is None:
            return {**result, 'status': 'unparsed', 'reason': 'not_a_number_under_profile_separators'}
        exact = _exact(sign, *parsed)
    if sign == '-' and Decimal(exact) == 0:
        result['negative_zero'] = True
    scale = len(exact.split('.', 1)[1]) if '.' in exact else 0
    return {**result, 'status': 'ok', 'exact': exact, 'scale': scale, 'sign_notation': notation}


def negate(exact: str) -> str:
    value = Decimal(exact)
    if value == 0:
        return exact.lstrip('-')
    return exact[1:] if exact.startswith('-') else '-' + exact


def is_negative(exact: str) -> bool:
    return Decimal(exact) < 0


def add_exact(values: list[str]) -> str:
    """Exact sum of decimal strings (scale = largest scale)."""
    total = sum((Decimal(v) for v in values), Decimal(0))
    scale = max([len(v.split('.', 1)[1]) if '.' in v else 0 for v in values] + [0])
    return format(total.quantize(Decimal(1).scaleb(-scale)), 'f') if scale else format(total, 'f')


def currency_code(raw: str | None) -> dict:
    """ISO 4217 alpha code from a column/attribute value or a mapped single-currency symbol."""
    if raw is None or not _strip(str(raw)):
        return {'status': 'missing', 'raw': raw, 'code': None}
    text = _strip(str(raw))
    if re.fullmatch(r'[A-Za-z]{3}', text):
        code = text.upper()
        result = {'status': 'ok', 'raw': raw, 'code': code, 'basis': 'iso_alpha_code'}
        if text != code:
            result['case_normalized'] = True
        return result
    symbols = currencies()['symbols']
    if text in symbols:
        if symbols[text] is None:
            return {'status': 'ambiguous', 'raw': raw, 'code': None, 'reason': 'currency_symbol_names_several_currencies'}
        return {'status': 'ok', 'raw': raw, 'code': symbols[text], 'basis': 'mapped_symbol'}
    return {'status': 'unparsed', 'raw': raw, 'code': None, 'reason': 'not_an_iso_alpha_code'}


def split_currency(text: str) -> tuple[str, str | None]:
    """Separate a leading/trailing currency code or symbol written inside an amount cell."""
    body = _strip(text)
    symbols = sorted(currencies()['symbols'], key=len, reverse=True)
    for pattern in (r'^([A-Za-z]{3})\s*(.+)$', r'^(.+?)\s*([A-Za-z]{3})$'):
        match = re.match(pattern, body)
        if match:
            first, second = match.groups()
            code, number = (first, second) if pattern.startswith('^([A') else (second, first)
            if re.search(r'\d', number):
                return number, code
    for symbol in symbols:
        if body.startswith(symbol) and re.search(r'\d', body[len(symbol):]):
            return body[len(symbol):], symbol
        if body.endswith(symbol) and re.search(r'\d', body[:-len(symbol)]):
            return body[:-len(symbol)], symbol
    return text, None


def minor_units(exact: str | None, code: str | None) -> dict:
    """Integer minor units under the ISO 4217 exponent, computed by code."""
    if exact is None or code is None:
        return {'minor': None, 'exponent': None, 'status': 'not_computed'}
    exponent = currencies()['exponents'].get(code)
    if exponent is None:
        return {'minor': None, 'exponent': None, 'status': 'currency_exponent_unknown'}
    scaled = Decimal(exact).scaleb(exponent)
    if scaled != scaled.to_integral_value():
        return {'minor': None, 'exponent': exponent, 'status': 'fraction_beyond_currency_exponent'}
    return {'minor': int(scaled), 'exponent': exponent, 'status': 'ok'}


def from_minor(minor: int, exponent: int) -> str:
    value = Decimal(minor).scaleb(-exponent)
    return format(value, 'f') if exponent else str(minor)

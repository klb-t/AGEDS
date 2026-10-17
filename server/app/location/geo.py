"""Coordinate parsing, validation, rounding and spherical distances (pure functions).

Every coordinate keeps the precision its source stated (number of decimal
places of the text, ``7`` for ``latitudeE7`` integers). Rounding to a coarser
stored precision is an explicit parameter; the rounding error is reported so a
geofence test can widen its uncertainty instead of silently pretending exact
positions. Distances use the haversine formula on a sphere of mean Earth radius
``EARTH_RADIUS_M`` (IUGG mean radius); that is a model choice with an error of up
to ~0.5 % against the ellipsoid, recorded in every result that uses it.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

EARTH_RADIUS_M = 6_371_008.8
DISTANCE_MODEL = 'haversine_sphere_r6371008.8m'
METERS_PER_DEGREE = math.pi * EARTH_RADIUS_M / 180.0
MAX_DECIMALS = 9
E7 = 10_000_000
INT32_SPAN = 2 ** 32

# "48.1234567°, 11.5°", "48.1,11.5", "geo:48.1,11.5", "geo:48.1,11.5;u=35" (RFC 5870 parameters ignored)
_PAIR = re.compile(r'^\s*(?:geo:)?\s*([+-]?\d{1,3}(?:\.\d+)?)\s*°?\s*,\s*([+-]?\d{1,3}(?:\.\d+)?)\s*°?\s*(?:[,;].*)?$',
                   re.IGNORECASE)


class CoordinateError(ValueError):
    """A coordinate that cannot be used; ``code`` is the issue code reported to the caller."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Coordinate:
    lat: float
    lon: float
    decimals: int  # precision the source stated (decimal places)
    basis: str  # e7_integer | decimal_text | float_value | geo_uri | degree_string


def decimals_of(text: str) -> int:
    """Decimal places stated by a numeric text (``'1.50'`` -> 2, ``'1.5e-3'`` -> 4, ``'12'`` -> 0)."""
    try:
        exponent = Decimal(text.strip()).as_tuple().exponent
    except (InvalidOperation, AttributeError):
        return 0
    if not isinstance(exponent, int):
        return 0
    return max(0, min(MAX_DECIMALS, -exponent))


def finite(value) -> float | None:
    """A finite float from an int/float/numeric text, else None (bool is not a number)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def check_range(lat: float, lon: float, where: str = 'coordinate') -> None:
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise CoordinateError('coordinate_not_finite', f'{where}: non-finite coordinate')
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise CoordinateError('coordinate_out_of_range', f'{where}: latitude/longitude out of range',
                              lat=repr(lat)[:32], lon=repr(lon)[:32])


def from_e7(lat_e7, lon_e7) -> Coordinate:
    """``latitudeE7``/``longitudeE7`` integers. Out-of-range values are refused, never corrected.

    Some exports are reported to contain values that look like a 32-bit overflow; the
    error then says so (``possible_int32_overflow``) but the value is not reinterpreted.
    """
    for value in (lat_e7, lon_e7):
        if value is None:
            raise CoordinateError('coordinate_missing', 'E7 coordinate missing')
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and not math.isfinite(value):
                raise CoordinateError('coordinate_not_finite', 'E7 coordinate is not finite')
            raise CoordinateError('coordinate_not_integer', 'E7 coordinate is not an integer', value=repr(value)[:32])
    if abs(lat_e7) > 90 * E7 or abs(lon_e7) > 180 * E7:
        overflow = abs(lat_e7 - INT32_SPAN) <= 90 * E7 or abs(lon_e7 - INT32_SPAN) <= 180 * E7
        raise CoordinateError('coordinate_out_of_range', 'E7 coordinate out of range', lat_e7=str(lat_e7)[:24],
                              lon_e7=str(lon_e7)[:24], possible_int32_overflow=overflow)
    return Coordinate(lat_e7 / E7, lon_e7 / E7, 7, 'e7_integer')


def from_values(lat, lon, *, lat_text: str | None = None, lon_text: str | None = None, basis: str = 'float_value') -> Coordinate:
    """Numbers (or numeric texts). ``*_text`` give the stated spelling for the precision."""
    la, lo = finite(lat), finite(lon)
    if la is None or lo is None:
        if lat is None or lon is None:
            raise CoordinateError('coordinate_missing', 'coordinate missing')
        texts = [v.strip().lower() for v in (lat, lon) if isinstance(v, str)]
        numbers = [v for v in (lat, lon) if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if any(t in ('nan', 'inf', '+inf', '-inf', 'infinity', '-infinity') for t in texts) or \
                any(not math.isfinite(float(v)) for v in numbers if isinstance(v, float)):
            raise CoordinateError('coordinate_not_finite', 'coordinate is not a finite number',
                                  lat=repr(lat)[:32], lon=repr(lon)[:32])
        raise CoordinateError('coordinate_unparsed', 'coordinate is not numeric', lat=repr(lat)[:32], lon=repr(lon)[:32])
    check_range(la, lo)
    texts = [t if t is not None else (v if isinstance(v, str) else _float_text(v)) for t, v in ((lat_text, lat), (lon_text, lon))]
    return Coordinate(la, lo, max(decimals_of(texts[0]), decimals_of(texts[1])), basis)


def _float_text(value) -> str:
    text = getattr(value, 'text', None)  # PreciseFloat keeps the JSON spelling
    return text if isinstance(text, str) else repr(value)


def parse_pair(text) -> Coordinate:
    """``"48.1°, 11.5°"`` (Timeline export), ``"geo:48.1,11.5"`` (iOS export) or ``"48.1,11.5"``."""
    if not isinstance(text, str):
        raise CoordinateError('coordinate_missing', 'coordinate text missing')
    if len(text) > 200:
        raise CoordinateError('coordinate_unparsed', 'coordinate text too long')
    match = _PAIR.match(text)
    if not match:
        raise CoordinateError('coordinate_unparsed', 'unrecognised coordinate text', raw=text[:64])
    basis = 'geo_uri' if text.strip().lower().startswith('geo:') else 'degree_string'
    return from_values(match.group(1), match.group(2), lat_text=match.group(1), lon_text=match.group(2), basis=basis)


def round_to(coordinate: Coordinate, store_decimals: int | None) -> tuple[float, float, int]:
    """Stored (lat, lon, decimals): never more decimals than the source stated."""
    decimals = coordinate.decimals if store_decimals is None else min(coordinate.decimals, store_decimals)
    return round(coordinate.lat, decimals), round(coordinate.lon, decimals), decimals


def rounding_error_m(lat: float, decimals: int, source_decimals: int | None = None) -> float:
    """Upper bound (m) of the displacement caused by rounding to ``decimals``.

    Zero when nothing was rounded away (``source_decimals`` known and not larger).
    Half a cell in each axis; the longitude half-cell shrinks with ``cos(lat)``.
    """
    if source_decimals is not None and source_decimals <= decimals:
        return 0.0
    half = 0.5 * 10.0 ** (-decimals)
    dy = half * METERS_PER_DEGREE
    dx = half * METERS_PER_DEGREE * abs(math.cos(math.radians(lat)))
    return math.hypot(dx, dy)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres; antimeridian and poles need no special case."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(max(0.0, a))))


def path_length_m(points) -> float:
    """Sum of haversine legs over ``[(lat, lon), ...]`` in the given order."""
    total, previous = 0.0, None
    for lat, lon in points:
        if previous is not None:
            total += haversine_m(previous[0], previous[1], lat, lon)
        previous = (lat, lon)
    return total


def normalize_lon(lon: float) -> float:
    """Into [-180, 180)."""
    return ((lon + 180.0) % 360.0) - 180.0


def bbox(points) -> dict | None:
    """Bounding box over ``[(lat, lon), ...]``; crosses the antimeridian when that is narrower.

    ``west > east`` means the box crosses the antimeridian.
    """
    lats, lons = [], []
    for lat, lon in points:
        lats.append(lat)
        lons.append(normalize_lon(lon) if lon != 180.0 else 180.0)
    if not lats:
        return None
    lons.sort()
    # Largest gap between consecutive longitudes (circularly): the box is its complement.
    gaps = [(lons[i + 1] - lons[i], i) for i in range(len(lons) - 1)]
    wrap_gap = lons[0] + 360.0 - lons[-1]
    widest = max(gaps, default=(-1.0, -1))
    if widest[0] > wrap_gap:
        west, east = lons[widest[1] + 1], lons[widest[1]]
    else:
        west, east = lons[0], lons[-1]
    return {'south': min(lats), 'north': max(lats), 'west': west, 'east': east, 'crosses_antimeridian': west > east}

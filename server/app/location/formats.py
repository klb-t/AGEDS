"""Format detection by bytes (bounded prefix) and one dispatcher for every location importer.

Detection never trusts the file name: a zip signature with a ``.kml`` member is
KMZ, ``.FIT`` at offset 8 is FIT, an XML root ``gpx``/``kml`` decides GPX/KML, and
JSON is Google (first top-level key, or an array of segment objects) or GeoJSON
(``"type": "FeatureCollection"``/``"Feature"``). Anything else is refused with the
probe result, so the operator can name the format explicitly.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from . import google

FORMATS = ('google', 'gpx', 'kml', 'geojson', 'fit')
PREFIX_BYTES = 65536


class UnknownFormat(ValueError):
    pass


def detect(path: Path) -> dict:
    with Path(path).open('rb') as handle:
        prefix = handle.read(PREFIX_BYTES)
    if prefix[:4] == b'PK\x03\x04':
        try:
            with zipfile.ZipFile(path) as archive:
                names = [i.filename for i in archive.infolist()[:10000]]
        except zipfile.BadZipFile:
            return {'format': None, 'reason': 'zip signature but not a readable zip'}
        if any(n.lower().endswith('.kml') for n in names):
            return {'format': 'kml', 'variant': 'kmz', 'reason': 'zip with a .kml member'}
        return {'format': None, 'reason': 'zip without a .kml member'}
    if len(prefix) >= 12 and prefix[8:12] == b'.FIT':
        return {'format': 'fit', 'reason': '.FIT signature at offset 8'}
    text = prefix.decode('utf-8', 'replace').lstrip('﻿ \t\r\n')
    if text.startswith('<'):
        body = re.sub(r'^(?:<\?.*?\?>|<!--.*?-->|<!DOCTYPE[^\[>]*(?:\[.*?\])?\s*>|\s)*', '', text, flags=re.S)
        root = re.match(r'<(?:[A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)', body)
        name = root.group(1).lower() if root else None
        if name in ('gpx', 'kml'):
            return {'format': name, 'reason': f'XML root element <{name}>'}
        return {'format': None, 'reason': f'XML root element {name!r} is not GPX or KML'}
    if text.startswith('{'):
        variant = google.detect_format(prefix)
        if variant != 'google_json_unrecognised':
            return {'format': 'google', 'variant': variant, 'reason': 'first top-level key of a Google location export'}
        if re.search(r'"type"\s*:\s*"(?:FeatureCollection|Feature)"', text):
            return {'format': 'geojson', 'reason': 'GeoJSON type member'}
        return {'format': None, 'reason': 'JSON object of an unrecognised layout'}
    if text.startswith('['):
        if re.search(r'"(?:startTime|timelinePath|visit|activity)"\s*:', text[:PREFIX_BYTES]):
            return {'format': 'google', 'variant': 'ios_location_history', 'reason': 'array of timeline segments'}
        return {'format': None, 'reason': 'JSON array of an unrecognised layout'}
    return {'format': None, 'reason': 'not XML, JSON, zip or FIT'}


def import_file(path: Path, *, fmt: str = 'auto', **kwargs) -> dict:
    """Import one location file with the importer of ``fmt`` (``auto`` = detected by bytes)."""
    path = Path(path)
    detected = detect(path) if fmt == 'auto' else {'format': fmt, 'reason': 'named by the operator'}
    chosen = detected.get('format')
    if chosen not in FORMATS:
        raise UnknownFormat(f'location format not recognised: {detected.get("reason")}; name it explicitly '
                            f'({", ".join(FORMATS)})')
    options = kwargs.pop('format_options', None) or {}
    if chosen == 'google':
        from ..importers.location import import_location_history_v2
        result = import_location_history_v2(path, provider_place_text=options.get('provider_place_text', 'keep'), **kwargs)
    elif chosen == 'gpx':
        from ..importers.gpx import import_gpx
        result = import_gpx(path, gpx_naive_time=options.get('gpx_naive_time', 'unknown'), **kwargs)
    elif chosen == 'kml':
        from ..importers.kml import import_kml
        result = import_kml(path, **kwargs)
    elif chosen == 'geojson':
        from ..importers.geojson import import_geojson
        result = import_geojson(path, options=options.get('geojson'), **kwargs)
    else:
        from ..importers.fit import import_fit
        result = import_fit(path, **kwargs)
    result['detection'] = detected
    return result

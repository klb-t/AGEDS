"""Minimal, bounded EXIF reader (JPEG APP1, bare TIFF, PNG ``eXIf``) in pure Python.

Reads only: ``DateTimeOriginal``, ``DateTimeDigitized`` (CreateDate),
``DateTime`` (ModifyDate), their ``OffsetTime*`` and ``SubSecTime*`` tags,
``Make``/``Model``/``Software`` and whether a GPS IFD exists (coordinates are
never decoded). Each EXIF time is a ``exif`` candidate: zone ``unknown`` unless
the matching offset tag is present. EXIF is editable metadata; it is an
observation of what the file claims, not the capture truth.
"""
from __future__ import annotations

import re
import struct
from datetime import datetime
from pathlib import Path

from ..db import session
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, add_event_with_times, local_candidate,
                          numeric_offset_minutes, offset_candidate, out_of_range_offset_candidate,
                          record_import_run, stored_path)

PARSER_NAME = 'exif_minimal'
PARSER_VERSION = '1'
_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
_IFD0_TAGS = {0x010F: 'Make', 0x0110: 'Model', 0x0131: 'Software', 0x0132: 'DateTime'}
_EXIF_TAGS = {0x9003: 'DateTimeOriginal', 0x9004: 'DateTimeDigitized', 0x9010: 'OffsetTime',
              0x9011: 'OffsetTimeOriginal', 0x9012: 'OffsetTimeDigitized', 0x9290: 'SubSecTime',
              0x9291: 'SubSecTimeOriginal', 0x9292: 'SubSecTimeDigitized'}
_TIMES = (('DateTimeOriginal', 'OffsetTimeOriginal', 'SubSecTimeOriginal'),
          ('DateTimeDigitized', 'OffsetTimeDigitized', 'SubSecTimeDigitized'),
          ('DateTime', 'OffsetTime', 'SubSecTime'))
_EXIF_DT = re.compile(r'^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})$')
_OFFSET = re.compile(r'^([+-])(\d{2}):(\d{2})$')


def _tiff(block: bytes, problems: list[str]) -> dict:
    """Parse IFD0, the EXIF sub-IFD and the presence of a GPS IFD from a TIFF block."""
    if len(block) < 8 or block[:2] not in (b'II', b'MM'):
        problems.append('tiff_header_invalid')
        return {}
    order = '<' if block[:2] == b'II' else '>'
    if struct.unpack(order + 'H', block[2:4])[0] != 42:
        problems.append('tiff_magic_invalid')
        return {}
    values: dict = {'byte_order': 'little' if order == '<' else 'big'}
    visited: set[int] = set()

    def read_ifd(offset: int, names: dict) -> dict[int, int]:
        pointers: dict[int, int] = {}
        if offset in visited or offset + 2 > len(block):
            problems.append('ifd_offset_invalid_or_loop')
            return pointers
        visited.add(offset)
        count = struct.unpack(order + 'H', block[offset:offset + 2])[0]
        if count > 512:
            problems.append('ifd_entry_count_over_limit')
            return pointers
        for i in range(count):
            entry = offset + 2 + i * 12
            if entry + 12 > len(block):
                problems.append('ifd_entry_truncated')
                break
            tag, kind, n = struct.unpack(order + 'HHI', block[entry:entry + 8])
            if tag in (0x8769, 0x8825) and kind in (4, 13):
                pointers[tag] = struct.unpack(order + 'I', block[entry + 8:entry + 12])[0]
                continue
            if tag not in names or kind not in _TYPE_SIZES:
                continue
            size = _TYPE_SIZES[kind] * n
            if size > 4096:
                problems.append(f'tag_value_too_large:{names[tag]}')
                continue
            start = entry + 8 if size <= 4 else struct.unpack(order + 'I', block[entry + 8:entry + 12])[0]
            raw = block[start:start + size]
            if len(raw) != size:
                problems.append(f'tag_value_out_of_bounds:{names[tag]}')
                continue
            if kind in (2, 7):
                values[names[tag]] = raw.split(b'\x00', 1)[0].decode('latin-1').strip()
        return pointers

    first = struct.unpack(order + 'I', block[4:8])[0]
    pointers = read_ifd(first, _IFD0_TAGS)
    values['gps_ifd_present'] = 0x8825 in pointers
    if 0x8769 in pointers:
        read_ifd(pointers[0x8769], _EXIF_TAGS)
    return values


def read_exif(path: Path, limits: CommLimits = DEFAULT_LIMITS) -> tuple[dict | None, list[str], str]:
    """Return (values or None, problems, container) reading at most ``max_exif_bytes``."""
    problems: list[str] = []
    with path.open('rb') as handle:
        head = handle.read(limits.max_exif_bytes)
    if head[:2] == b'\xff\xd8':
        position = 2
        while position + 4 <= len(head):
            if head[position] != 0xFF:
                problems.append('jpeg_marker_sync_lost')
                return None, problems, 'jpeg'
            marker = head[position + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                position += 2
                continue
            if marker in (0xDA, 0xD9):
                break
            length = struct.unpack('>H', head[position + 2:position + 4])[0]
            segment = head[position + 4:position + 2 + length]
            if len(segment) != length - 2:
                problems.append('jpeg_segment_truncated_or_beyond_read_limit')
                return None, problems, 'jpeg'
            if marker == 0xE1 and segment.startswith(b'Exif\x00\x00'):
                return _tiff(segment[6:], problems), problems, 'jpeg'
            position += 2 + length
        return None, problems, 'jpeg'
    if head[:4] in (b'II*\x00', b'MM\x00*'):
        return _tiff(head, problems), problems, 'tiff'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        position = 8
        while position + 8 <= len(head):
            length, kind = struct.unpack('>I4s', head[position:position + 8])
            data = head[position + 8:position + 8 + length]
            if kind == b'eXIf':
                if len(data) != length:
                    problems.append('png_exif_truncated_or_beyond_read_limit')
                    return None, problems, 'png'
                return _tiff(data, problems), problems, 'png'
            if kind == b'IEND':
                break
            position += 12 + length
        return None, problems, 'png'
    return None, ['not_jpeg_tiff_or_png'], 'unknown'


def exif_candidates(values: dict, problems: list[str]):
    candidates = []
    for name, offset_name, subsec_name in _TIMES:
        raw = values.get(name)
        if not raw:
            continue
        match = _EXIF_DT.match(raw)
        if not match:
            problems.append(f'exif_time_unparsed:{name}')
            continue
        try:
            naive = datetime(*map(int, match.groups()))
        except ValueError:
            problems.append(f'exif_time_invalid_calendar_value:{name}')
            continue
        precision = 'second'
        subsec = values.get(subsec_name)
        if subsec and subsec.isdigit():
            naive = naive.replace(microsecond=int((subsec + '000000')[:6]))
            precision = 'millisecond' if len(subsec) <= 3 else 'microsecond'
        meta = {'tag': name, 'offset_tag': offset_name, 'subsec_tag': subsec_name if subsec else None,
                'semantics': 'embedded_metadata_claim_editable'}
        offset = values.get(offset_name)
        offset_match = _OFFSET.match(offset or '')
        source = f'exif:{name}'
        if offset_match:
            sign, hours, minutes = offset_match.groups()
            total = numeric_offset_minutes(sign, hours, minutes)
            if total is None:  # e.g. +99:00 is not an offset: keep the wall clock, zone unknown
                problems.append(f'exif_offset_out_of_range:{offset_name}')
                candidates.append(out_of_range_offset_candidate('exif', raw, source, naive, precision, meta, offset))
                continue
            meta['offset_raw'] = offset
            candidates.append(offset_candidate('exif', raw, source, naive, total, precision, meta))
        else:
            if offset:
                problems.append(f'exif_offset_unparsed:{offset_name}')
            meta['zone_status'] = 'no_offset_tag_zone_unknown'
            candidates.append(local_candidate('exif', raw, source, naive, precision, meta))
    return candidates


def observe_artifact(artifact_id: int, *, limits: CommLimits = DEFAULT_LIMITS, issues: Issues | None = None) -> dict:
    """Read EXIF from an acquired artifact's stored copy; one event per artifact with EXIF times."""
    issues = issues or Issues(limits.max_issues)
    with session() as db:
        row = db.execute('SELECT id,source_id,original_name FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    if row is None:
        raise ValueError(f'artifact {artifact_id} does not exist')
    values, problems, container = read_exif(stored_path(artifact_id), limits)
    if values is None:
        for problem in problems:
            issues.add(problem, artifact_id=artifact_id)
        return {'artifact_id': artifact_id, 'status': 'no_exif', 'container': container, 'problems': problems}
    candidates = exif_candidates(values, problems)
    for problem in problems:
        issues.add(problem, artifact_id=artifact_id)
    if not candidates:
        return {'artifact_id': artifact_id, 'status': 'exif_without_time', 'container': container,
                'values': values, 'problems': problems}
    metadata = {'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                'locator': {'artifact_id': artifact_id, 'container': container},
                'exif': {k: v for k, v in values.items()}, 'gps': {'ifd_present': values.get('gps_ifd_present'),
                                                                   'coordinates_decoded': False},
                'semantics': 'time the embedded metadata claims; editable, not capture truth', 'issues': problems}
    event_id = add_event_with_times(
        candidates, source_id=row['source_id'], artifact_id=artifact_id, event_type='image_capture',
        subject=row['original_name'], external_id=f'{PARSER_NAME}:artifact:{artifact_id}', confidence=None,
        ts_start=None, metadata=metadata)
    return {'artifact_id': artifact_id, 'status': 'exif_time_observed', 'event_id': event_id,
            'container': container, 'candidates': len(candidates), 'problems': problems}


def import_images(paths: list[Path], *, label: str | None = None, collection_id: int | None = None,
                  limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """Acquire image files, then read EXIF from the stored copies."""
    issues = Issues(limits.max_issues)
    results = []
    for path in paths[:limits.max_records]:
        _, artifact_id = acquire(path, source_kind='image_files', source_label=label or 'Image files',
                                 locator=str(path), importer=PARSER_NAME, parser_version=PARSER_VERSION,
                                 collection_id=collection_id)
        results.append(observe_artifact(artifact_id, limits=limits, issues=issues))
    counts = {'images': len(results), 'with_time': sum(1 for r in results if r['status'] == 'exif_time_observed'),
              'without_exif': sum(1 for r in results if r['status'] == 'no_exif')}
    coverage = {'complete': len(paths) <= limits.max_records}
    record_import_run(artifact_id=None, importer=PARSER_NAME, parser_version=PARSER_VERSION, options={},
                      counts=counts, coverage=coverage, issues=issues)
    return {'parser_version': PARSER_VERSION, **counts, 'results': results, 'coverage': coverage,
            'issues': issues.as_dict()}

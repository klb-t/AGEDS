"""Document metadata (OOXML, ODF, PDF Info + XMP) → claims and time candidates — traces area.

Per artifact one ``document_metadata`` event (subject = file name) and one
``trace_document_metadata`` row:

* OOXML (DOCX/XLSX/PPTX and macro/template variants): ``docProps/core.xml``
  (``dc:creator``, ``cp:lastModifiedBy``, ``dcterms:created``/``modified`` W3CDTF,
  ``cp:revision``, ``cp:lastPrinted``, title/subject/keywords…) and
  ``docProps/app.xml`` (``Application``, ``AppVersion``, ``TotalTime`` minutes,
  ``Company``, ``Template``, counts);
* ODF: ``meta.xml`` (``meta:initial-creator``, ``dc:creator``,
  ``meta:creation-date``, ``dc:date``, ``meta:print-date``, ``meta:editing-cycles``,
  ``meta:editing-duration``, ``meta:generator``, statistics);
* PDF: the trailer's ``/Info`` dictionary (direct object, incremental updates —
  the last definition wins and earlier ones are listed — or inside an object
  stream) and the XMP packet (``xmp:CreateDate``/``ModifyDate``/``MetadataDate``,
  ``xmp:CreatorTool``, ``pdf:Producer``, ``dc:creator``). ``D:YYYYMMDDHHmmSSOHH'mm'``
  dates keep their stated precision (partial forms such as ``D:2020`` are year
  precision), a missing zone stays ``unknown``, an impossible offset keeps the
  wall clock with ``numeric_offset_out_of_range``.

Authors, companies and tools are **claims written into the file** (editable by
anyone), never identities: they stay out of event columns, bodies and the search
index. Created/modified times are claims too (``signal.class='declared'``).

Bounds (parser constants): ZIP members listed ≤ 10,000, a metadata member ≤ 4 MiB
read (actual decompressed bytes, so a zip bomb stops at the bound), PDF bytes
scanned ≤ ``max_pdf_bytes`` (256 MiB default), ≤ 64 inflated streams of ≤ 16 MiB,
XMP ≤ 1 MiB. XML with a DOCTYPE/ENTITY declaration is refused. Legacy OLE
(``.doc``/``.xls``) SummaryInformation and encrypted PDF strings are reported as
not supported.
"""
from __future__ import annotations

import re
import zipfile
import zlib
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from ..db import session
from ..traces.common import append_row, bounded, clean_text, dumps, signal, write_event
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, legacy_instant, local_candidate,
                          numeric_offset_minutes, offset_candidate, out_of_range_offset_candidate, record_import_run,
                          stored_path, walk_files)

PARSER_NAME = 'document_metadata'
PARSER_VERSION = '1'
MAX_ZIP_MEMBERS = 10_000
MAX_XML_MEMBER_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 256 * 1024 * 1024
MAX_INFLATED_STREAMS = 64
MAX_INFLATE_BYTES = 16 * 1024 * 1024
MAX_XMP_BYTES = 1024 * 1024
EXTENSIONS = {'.docx': 'ooxml', '.docm': 'ooxml', '.dotx': 'ooxml', '.xlsx': 'ooxml', '.xlsm': 'ooxml', '.xltx': 'ooxml',
              '.pptx': 'ooxml', '.pptm': 'ooxml', '.potx': 'ooxml', '.odt': 'odf', '.ods': 'odf', '.odp': 'odf',
              '.odg': 'odf', '.ott': 'odf', '.pdf': 'pdf', '.doc': 'ole', '.xls': 'ole', '.ppt': 'ole'}

NS = {'cp': 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties',
      'dc': 'http://purl.org/dc/elements/1.1/', 'dcterms': 'http://purl.org/dc/terms/',
      'ep': 'http://schemas.openxmlformats.org/officeDocument/2006/extended-properties',
      'office': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0', 'meta': 'urn:oasis:names:tc:opendocument:xmlns:meta:1.0',
      'x': 'adobe:ns:meta/', 'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#', 'xmp': 'http://ns.adobe.com/xap/1.0/',
      'pdf': 'http://ns.adobe.com/pdf/1.3/', 'xmpMM': 'http://ns.adobe.com/xap/1.0/mm/'}

# (field, kind, semantics) per time-bearing property
OOXML_TIMES = (('dcterms:created', 'created', 'document creation claim'), ('dcterms:modified', 'updated', 'last save claim'),
               ('cp:lastPrinted', 'other', 'last printed claim'))
OOXML_CLAIMS = ('dc:creator', 'cp:lastModifiedBy', 'dc:title', 'dc:subject', 'cp:keywords', 'dc:description',
                'cp:category', 'cp:contentStatus', 'dc:language', 'cp:version', 'cp:revision', 'dc:identifier')
APP_FIELDS = ('Application', 'AppVersion', 'TotalTime', 'Company', 'Manager', 'Template', 'Pages', 'Words', 'Characters',
              'Lines', 'Paragraphs', 'Slides', 'Notes', 'DocSecurity', 'HyperlinkBase')
ODF_TIMES = (('meta:creation-date', 'created', 'document creation claim'), ('dc:date', 'updated', 'last save claim'),
             ('meta:print-date', 'other', 'last printed claim'))
ODF_CLAIMS = ('meta:initial-creator', 'dc:creator', 'dc:title', 'dc:description', 'dc:subject', 'meta:keyword',
              'dc:language', 'meta:printed-by', 'meta:editing-cycles', 'meta:editing-duration', 'meta:generator')
PDF_TIMES = (('CreationDate', 'created', 'PDF Info creation claim'), ('ModDate', 'updated', 'PDF Info modification claim'))
PDF_CLAIMS = ('Author', 'Creator', 'Producer', 'Title', 'Subject', 'Keywords', 'Trapped')
XMP_TIMES = (('xmp:CreateDate', 'created', 'XMP creation claim'), ('xmp:ModifyDate', 'updated', 'XMP modification claim'),
             ('xmp:MetadataDate', 'other', 'XMP metadata change claim'))
XMP_CLAIMS = ('xmp:CreatorTool', 'pdf:Producer', 'pdf:Keywords', 'dc:creator', 'dc:title', 'xmpMM:DocumentID',
              'xmpMM:InstanceID')


class Unreadable(ValueError):
    pass


# --------------------------------------------------------------------- dates

_W3CDTF = re.compile(r'^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?)?)?'
                     r'\s*(Z|[+-]\d{2}:?\d{2})?$')
_PDF_DATE = re.compile(r"^(D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
                       r"(?:(Z)(?:\d{2}'?(?:\d{2}'?)?)?|([+-])(\d{2})(?:'?(\d{2})'?)?)?(.*)$", re.S)


def _naive(year, month, day, hour, minute, second, fraction=None):
    micro = int(((fraction or '') + '000000')[:6]) if fraction else 0
    return datetime(int(year), int(month or 1), int(day or 1), int(hour or 0), int(minute or 0), int(second or 0), micro)


def _precision(month, day, hour, minute, second, fraction) -> str:
    if fraction:
        return 'millisecond' if len(fraction) <= 3 else 'microsecond'
    for value, name in ((second, 'second'), (minute, 'minute'), (hour, 'hour'), (day, 'day'), (month, 'month')):
        if value is not None:
            return name
    return 'year'


def w3cdtf_candidate(kind: str, raw: str, source: str, meta: dict):
    """W3CDTF / ISO 8601 profile used by OOXML and ODF; returns (candidate, problem)."""
    match = _W3CDTF.match(raw.strip())
    if not match:
        return None, 'date_unparsed'
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    try:
        naive = _naive(year, month, day, hour, minute, second, fraction)
    except ValueError:
        return None, 'date_invalid_calendar_value'
    precision = _precision(month, day, hour, minute, second, fraction)
    meta = {**meta, 'format': 'w3cdtf'}
    if zone is None or hour is None:
        if zone is not None:
            meta['offset_raw_on_date_only_value'] = zone
        meta['zone_status'] = 'zone_not_stated' if zone is None else 'date_only_offset_not_applied'
        return local_candidate(kind, raw, source, naive, precision, meta), None
    if zone == 'Z':
        return offset_candidate(kind, raw, source, naive, 0, precision, meta, tz_label='UTC'), None
    digits = zone[1:].replace(':', '')
    total = numeric_offset_minutes(zone[0], digits[:2], digits[2:])
    if total is None:
        return out_of_range_offset_candidate(kind, raw, source, naive, precision, meta, zone), 'date_offset_out_of_range'
    return offset_candidate(kind, raw, source, naive, total, precision, meta), None


def pdf_date_candidate(kind: str, raw: str, source: str, meta: dict):
    """PDF date ``D:YYYYMMDDHHmmSSOHH'mm'`` with partial forms; returns (candidate, problems)."""
    problems = []
    match = _PDF_DATE.match(raw.strip())
    if not match or not match.group(2):
        return None, ['pdf_date_unparsed']
    prefix, year, month, day, hour, minute, second, zulu, sign, off_h, off_m, rest = match.groups()
    if not prefix:
        problems.append('pdf_date_without_d_prefix')
    if rest.strip():
        problems.append('pdf_date_trailing_text')
    try:
        naive = _naive(year, month, day, hour, minute, second)
    except ValueError:
        return None, problems + ['pdf_date_invalid_calendar_value']
    precision = _precision(month, day, hour, minute, second, None)
    meta = {**meta, 'format': 'pdf_date', 'trailing_text': rest.strip() or None}
    if hour is None or (zulu is None and sign is None):
        if zulu or sign:
            meta['offset_raw_on_date_only_value'] = (zulu or '') + (sign or '') + (off_h or '') + (off_m or '')
        meta['zone_status'] = 'zone_not_stated' if not (zulu or sign) else 'date_only_offset_not_applied'
        return local_candidate(kind, raw, source, naive, precision, meta), problems
    if zulu:
        return offset_candidate(kind, raw, source, naive, 0, precision, meta, tz_label='UTC'), problems
    total = numeric_offset_minutes(sign, off_h, off_m or 0)
    if total is None:
        return (out_of_range_offset_candidate(kind, raw, source, naive, precision, meta, f'{sign}{off_h}:{off_m or "00"}'),
                problems + ['pdf_date_offset_out_of_range'])
    return offset_candidate(kind, raw, source, naive, total, precision, meta), problems


def duration_seconds(text: str) -> int | None:
    """ISO 8601 duration ``PnDTnHnMnS`` (ODF ``meta:editing-duration``)."""
    match = re.fullmatch(r'P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?', text.strip())
    if not match or not any(match.groups()):
        return None
    years, months, days, hours, minutes, seconds = match.groups()
    if years or months:
        return None  # calendar-dependent length
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(float(seconds or 0))


# --------------------------------------------------------------------- XML

def _xml(data: bytes, problems: list[str]):
    if re.search(rb'<!DOCTYPE|<!ENTITY', data[:65536], re.I) or re.search(rb'<!ENTITY', data, re.I):
        problems.append('xml_doctype_or_entity_refused')
        return None
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError:
        problems.append('xml_not_well_formed')
        return None


def _qname(prefixed: str) -> str:
    prefix, local = prefixed.split(':', 1)
    return f'{{{NS[prefix]}}}{local}'


def _texts(root, names) -> dict:
    out = {}
    for name in names:
        element = root.find('.//' + _qname(name))
        if element is not None:
            text = ''.join(element.itertext()).strip()
            out[name] = clean_text(text)
    return out


# --------------------------------------------------------------------- containers

def _zip_member(archive: zipfile.ZipFile, name: str, problems: list[str]) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.flag_bits & 0x1:
        problems.append(f'zip_member_encrypted:{name}')
        return None
    if info.file_size > MAX_XML_MEMBER_BYTES:
        problems.append(f'zip_member_over_limit:{name}')
        return None
    try:
        with archive.open(info) as handle:
            data = handle.read(MAX_XML_MEMBER_BYTES + 1)
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError) as exc:
        problems.append(f'zip_member_unreadable:{name}:{type(exc).__name__}')
        return None
    if len(data) > MAX_XML_MEMBER_BYTES:
        problems.append(f'zip_member_over_limit:{name}')  # declared size lied (zip bomb shape)
        return None
    return data


def _times(values: dict, specs, family: str, problems: list[str]) -> tuple[list, dict]:
    candidates, raw = [], {}
    for field_name, kind, semantics in specs:
        value = values.get(field_name)
        if not value:
            continue
        source = f'{PARSER_NAME}@{PARSER_VERSION}:{family}:{field_name}'
        meta = {'field': field_name, 'semantics': semantics, 'claim': 'editable metadata written into the file'}
        if family == 'pdf_info':
            candidate, found = pdf_date_candidate(kind, value, source, meta)
            problems.extend(f'{p}:{field_name}' for p in found)
        else:
            candidate, problem = w3cdtf_candidate(kind, value, source, meta)
            if problem:
                problems.append(f'{problem}:{field_name}')
        raw[field_name] = {'raw': value, 'status': 'parsed' if candidate else 'unparsed',
                           'tz_status': candidate.tz_status if candidate else None,
                           'precision': candidate.precision if candidate else None}
        if candidate:
            candidates.append(candidate)
    return candidates, raw


def read_ooxml(path: Path, problems: list[str]) -> dict:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise Unreadable(f'zip_unreadable:{type(exc).__name__}') from exc
    with archive:
        names = archive.namelist()
        if len(names) > MAX_ZIP_MEMBERS:
            problems.append('zip_member_count_over_limit')
        result = {'format': 'ooxml', 'claims': {}, 'tools': {}, 'times_values': {}, 'raw': {}}
        core = _zip_member(archive, 'docProps/core.xml', problems)
        if core is None and 'docProps/core.xml' not in names:
            problems.append('ooxml_core_properties_missing')
        root = _xml(core, problems) if core else None
        if root is not None:
            values = _texts(root, [f for f, *_ in OOXML_TIMES] + list(OOXML_CLAIMS))
            result['times_values'] = {k: values[k] for k, *_ in OOXML_TIMES if k in values}
            result['claims'] = {k: values[k] for k in OOXML_CLAIMS if k in values}
            for field_name, *_ in OOXML_TIMES:
                element = root.find('.//' + _qname(field_name))
                if element is not None:
                    xsi = element.get('{http://www.w3.org/2001/XMLSchema-instance}type')
                    result['raw'][f'{field_name}@xsi:type'] = xsi
        app = _zip_member(archive, 'docProps/app.xml', problems)
        app_root = _xml(app, problems) if app else None
        if app_root is not None:
            for name in APP_FIELDS:
                element = app_root.find(f'{{{NS["ep"]}}}{name}')
                if element is not None and element.text is not None:
                    result['tools' if name in ('Application', 'AppVersion', 'Template', 'DocSecurity') else 'claims'][name] = \
                        clean_text(element.text.strip())
            if 'TotalTime' in result['claims']:
                total = result['claims']['TotalTime']
                result['claims']['TotalTime_minutes'] = int(total) if total.isdigit() else None
        result['members'] = len(names)
    return result


def read_odf(path: Path, problems: list[str]) -> dict:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise Unreadable(f'zip_unreadable:{type(exc).__name__}') from exc
    with archive:
        mimetype = _zip_member(archive, 'mimetype', problems)
        result = {'format': 'odf', 'claims': {}, 'tools': {}, 'times_values': {}, 'raw': {},
                  'mimetype': mimetype.decode('ascii', 'replace').strip() if mimetype else None}
        meta = _zip_member(archive, 'meta.xml', problems)
        if meta is None:
            problems.append('odf_meta_missing')
            return result
        root = _xml(meta, problems)
        if root is None:
            return result
        values = _texts(root, [f for f, *_ in ODF_TIMES] + list(ODF_CLAIMS))
        result['times_values'] = {k: values[k] for k, *_ in ODF_TIMES if k in values}
        result['claims'] = {k: values[k] for k in ODF_CLAIMS if k in values and k != 'meta:generator'}
        if 'meta:generator' in values:
            result['tools']['meta:generator'] = values['meta:generator']
        duration = values.get('meta:editing-duration')
        if duration:
            result['claims']['editing_duration_seconds'] = duration_seconds(duration)
        stats = root.find('.//' + _qname('meta:document-statistic'))
        if stats is not None:
            result['raw']['document-statistic'] = {k.split('}', 1)[-1]: v for k, v in stats.attrib.items()}
    return result


# --------------------------------------------------------------------- PDF

_OBJ = r'(?<![0-9])%d\s+%d\s+obj\b'


def pdf_string(data: bytes, start: int) -> tuple[bytes, int]:
    """A literal string ``( ... )`` with nesting and escapes; returns (bytes, end)."""
    out, depth, i = bytearray(), 1, start + 1
    escapes = {ord('n'): b'\n', ord('r'): b'\r', ord('t'): b'\t', ord('b'): b'\b', ord('f'): b'\f',
               ord('('): b'(', ord(')'): b')', ord('\\'): b'\\'}
    while i < len(data):
        ch = data[i]
        if ch == 0x5C and i + 1 < len(data):
            nxt = data[i + 1]
            if nxt in escapes:
                out += escapes[nxt]
                i += 2
            elif 0x30 <= nxt <= 0x37:
                digits = re.match(rb'[0-7]{1,3}', data[i + 1:i + 4]).group(0)
                out.append(int(digits, 8) & 0xFF)
                i += 1 + len(digits)
            elif nxt in (0x0D, 0x0A):
                i += 2 + (1 if nxt == 0x0D and i + 2 < len(data) and data[i + 2] == 0x0A else 0)
            else:
                out.append(nxt)
                i += 2
            continue
        if ch == 0x28:
            depth += 1
        elif ch == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
        out.append(ch)
        i += 1
    raise Unreadable('pdf_string_unterminated')


_PDFDOC = {0x18: '˘', 0x19: 'ˇ', 0x1a: 'ˆ', 0x1b: '˙', 0x1c: '˝', 0x1d: '˛', 0x1e: '˚',
           0x1f: '˜', 0x80: '•', 0x81: '†', 0x82: '‡', 0x83: '…', 0x84: '—', 0x85: '–',
           0x86: 'ƒ', 0x87: '⁄', 0x88: '‹', 0x89: '›', 0x8a: '−', 0x8b: '‰', 0x8c: '„',
           0x8d: '“', 0x8e: '”', 0x8f: '‘', 0x90: '’', 0x91: '‚', 0x92: '™', 0x93: 'ﬁ',
           0x94: 'ﬂ', 0x95: 'Ł', 0x96: 'Œ', 0x97: 'Š', 0x98: 'Ÿ', 0x99: 'Ž', 0x9a: 'ı',
           0x9b: 'ł', 0x9c: 'œ', 0x9d: 'š', 0x9e: 'ž', 0xa0: '€'}


def pdf_text(raw: bytes) -> tuple[str, str]:
    """PDF text string → (text, encoding): UTF-16BE BOM, UTF-8 BOM (PDF 2.0) or PDFDocEncoding."""
    if raw.startswith(b'\xfe\xff'):
        return raw[2:].decode('utf-16-be', errors='replace'), 'utf-16be'
    if raw.startswith(b'\xff\xfe'):
        return raw[2:].decode('utf-16-le', errors='replace'), 'utf-16le_nonstandard'
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw[3:].decode('utf-8', errors='replace'), 'utf-8'
    return ''.join(_PDFDOC.get(b, chr(b)) for b in raw), 'pdfdocencoding'


def pdf_dict(data: bytes, start: int, depth: int = 0) -> tuple[dict, int]:
    """Parse ``<< /Key value ... >>`` at ``start``; values are bytes, ('ref', n, g), ('name', s), numbers or dicts."""
    if data[start:start + 2] != b'<<':
        raise Unreadable('pdf_dict_expected')
    if depth > 16:
        raise Unreadable('pdf_dict_nesting_limit')
    i, out = start + 2, {}
    key = None
    while i < len(data):
        while i < len(data) and data[i] in b' \t\r\n\x0c\x00':
            i += 1
        if data[i:i + 1] == b'%':
            while i < len(data) and data[i] not in b'\r\n':
                i += 1
            continue
        if data[i:i + 2] == b'>>':
            return out, i + 2
        token, i = _pdf_value(data, i, depth)
        if key is None:
            if not (isinstance(token, tuple) and token[0] == 'name'):
                raise Unreadable('pdf_dict_key_not_a_name')
            key = token[1]
        else:
            out[key] = token
            key = None
    raise Unreadable('pdf_dict_unterminated')


def _pdf_value(data: bytes, i: int, depth: int):
    ch = data[i:i + 1]
    if ch == b'(':
        return pdf_string(data, i)
    if data[i:i + 2] == b'<<':
        return pdf_dict(data, i, depth + 1)
    if ch == b'<':
        end = data.find(b'>', i)
        if end < 0:
            raise Unreadable('pdf_hex_string_unterminated')
        digits = re.sub(rb'\s', b'', data[i + 1:end])
        if len(digits) % 2:
            digits += b'0'
        try:
            return bytes.fromhex(digits.decode('ascii')), end + 1
        except ValueError as exc:
            raise Unreadable('pdf_hex_string_invalid') from exc
    if ch == b'/':
        match = re.match(rb'/([^\s/<>\[\]()%{}]*)', data[i:i + 256])
        return ('name', match.group(1).decode('latin-1')), i + match.end()
    if ch == b'[':
        items, j = [], i + 1
        while j < len(data):
            while j < len(data) and data[j] in b' \t\r\n\x0c\x00':
                j += 1
            if data[j:j + 1] == b']':
                return ('array', items[:64]), j + 1
            value, j = _pdf_value(data, j, depth + 1)
            items.append(value)
            if len(items) > 4096:
                raise Unreadable('pdf_array_too_long')
        raise Unreadable('pdf_array_unterminated')
    match = re.match(rb'(\d+)\s+(\d+)\s+R\b', data[i:i + 32])
    if match:
        return ('ref', int(match.group(1)), int(match.group(2))), i + match.end()
    match = re.match(rb'[+-]?(?:\d+\.?\d*|\.\d+)|true|false|null', data[i:i + 64])
    if match:
        text = match.group(0)
        return text.decode('ascii'), i + match.end()
    raise Unreadable(f'pdf_token_unexpected_at_{i}')


def _object_dict(data: bytes, number: int, generation: int) -> list[tuple[int, dict]]:
    """Every direct definition of ``number generation obj`` that starts with a dict (file order)."""
    out = []
    for match in re.finditer((_OBJ % (number, generation)).encode('ascii'), data):
        j = match.end()
        while j < len(data) and data[j] in b' \t\r\n':
            j += 1
        try:
            if data[j:j + 2] == b'<<':
                out.append((match.start(), pdf_dict(data, j)[0]))
            elif data[j:j + 1] in (b'(', b'<'):
                out.append((match.start(), {'__value__': _pdf_value(data, j, 0)[0]}))
        except Unreadable:
            continue
    return out


def _stream_bytes(data: bytes, dict_end: int, length) -> bytes | None:
    match = re.compile(rb'\s*stream\r?\n').match(data, dict_end)
    if not match:
        return None
    start = match.end()
    if isinstance(length, str) and length.isdigit() and start + int(length) <= len(data):
        return data[start:start + int(length)]
    end = data.find(b'endstream', start)
    return data[start:end] if end > 0 else None


def _inflate(raw: bytes, problems: list[str]) -> bytes | None:
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(raw, MAX_INFLATE_BYTES)
    except zlib.error:
        problems.append('pdf_stream_inflate_failed')
        return None
    if inflater.unconsumed_tail:
        problems.append('pdf_stream_inflate_over_limit')
    return out


MAX_OBJECTS_SCANNED = 200_000


def _streams(data: bytes, kind: bytes, problems: list[str]):
    """Inflated contents of ``/Type /<kind>`` streams (bounded count and size)."""
    found = 0
    for scanned, match in enumerate(re.finditer(rb'(?<![0-9])(\d+)\s+(\d+)\s+obj\s*<<', data)):
        if scanned >= MAX_OBJECTS_SCANNED:
            problems.append('pdf_object_scan_limit')
            return
        start = match.end() - 2
        try:
            dictionary, end = pdf_dict(data, start)
        except Unreadable:
            continue
        if dictionary.get('Type') != ('name', kind.decode()):
            continue
        found += 1
        if found > MAX_INFLATED_STREAMS:
            problems.append('pdf_stream_count_over_limit')
            return
        raw = _stream_bytes(data, end, dictionary.get('Length'))
        if raw is None:
            continue
        filters = dictionary.get('Filter')
        if filters == ('name', 'FlateDecode'):
            raw = _inflate(raw, problems)
        elif filters is not None:
            problems.append('pdf_stream_filter_not_supported')
            continue
        if raw is not None:
            yield int(match.group(1)), dictionary, raw


def _from_object_streams(data: bytes, number: int, problems: list[str]) -> dict | None:
    for _, dictionary, raw in _streams(data, b'ObjStm', problems):
        count, first = dictionary.get('N'), dictionary.get('First')
        if not (isinstance(count, str) and count.isdigit() and isinstance(first, str) and first.isdigit()):
            continue
        header = raw[:int(first)].split()
        pairs = list(zip(header[0::2], header[1::2]))[:int(count)]
        for obj, offset in pairs:
            if obj.isdigit() and int(obj) == number and offset.isdigit():
                position = int(first) + int(offset)
                while position < len(raw) and raw[position] in b' \t\r\n':
                    position += 1
                try:
                    return pdf_dict(raw, position)[0]
                except Unreadable:
                    problems.append('pdf_info_in_object_stream_unparsed')
                    return None
    return None


def _resolve(data: bytes, value, problems: list[str]):
    if isinstance(value, tuple) and value[0] == 'ref':
        definitions = _object_dict(data, value[1], value[2])
        if definitions and '__value__' in definitions[-1][1]:
            return definitions[-1][1]['__value__']
        problems.append('pdf_indirect_value_not_resolved')
        return None
    return value


def read_pdf(path: Path, problems: list[str], limits_bytes: int = MAX_PDF_BYTES) -> dict:
    with path.open('rb') as handle:
        data = handle.read(limits_bytes + 1)
    if len(data) > limits_bytes:
        data = data[:limits_bytes]
        problems.append('pdf_scan_byte_limit')
    header = re.search(rb'%PDF-(\d\.\d)', data[:1024])
    if not header:
        raise Unreadable('pdf_header_missing')
    result = {'format': 'pdf', 'claims': {}, 'tools': {}, 'times_values': {}, 'raw': {'pdf_version': header.group(1).decode()},
              'xmp': {}}
    info_refs = []
    for match in re.finditer(rb'trailer\s*<<', data):
        try:
            trailer, _ = pdf_dict(data, match.end() - 2)
        except Unreadable:
            problems.append('pdf_trailer_unparsed')
            continue
        if isinstance(trailer.get('Info'), tuple) and trailer['Info'][0] == 'ref':
            info_refs.append(trailer['Info'])
        if 'Encrypt' in trailer:
            result['raw']['encrypted'] = True
    for _, dictionary, _ in _streams(data, b'XRef', problems):
        if isinstance(dictionary.get('Info'), tuple) and dictionary['Info'][0] == 'ref':
            info_refs.append(dictionary['Info'])
        if 'Encrypt' in dictionary:
            result['raw']['encrypted'] = True
    info = None
    if info_refs:
        _, number, generation = info_refs[-1]
        definitions = _object_dict(data, number, generation)
        if definitions:
            info = definitions[-1][1]
            if len(definitions) > 1:
                result['raw']['info_definitions'] = len(definitions)
                problems.append('pdf_info_redefined_by_incremental_update')
                earlier = []
                for _, item in definitions[:-1]:
                    earlier.append({k: pdf_text(v)[0][:200] for k, v in item.items() if isinstance(v, bytes)})
                result['raw']['earlier_info'] = earlier[:8]
        else:
            info = _from_object_streams(data, number, problems)
            if info is not None:
                result['raw']['info_location'] = 'object_stream'
        if info is None:
            problems.append('pdf_info_object_not_found')
    else:
        problems.append('pdf_info_not_referenced')
    if info:
        encodings = {}
        for key in [f for f, *_ in PDF_TIMES] + list(PDF_CLAIMS):
            value = _resolve(data, info.get(key), problems) if key in info else None
            if isinstance(value, bytes):
                if result['raw'].get('encrypted'):
                    result['raw'].setdefault('encrypted_strings_hex', {})[key] = value[:64].hex()
                    continue
                text, encoding = pdf_text(value)
                encodings[key] = encoding
                target = result['times_values'] if key in ('CreationDate', 'ModDate') else \
                    result['tools'] if key in ('Creator', 'Producer') else result['claims']
                target[key] = clean_text(text)
            elif isinstance(value, tuple) and value[0] == 'name':
                result['claims'][key] = value[1]
        result['raw']['string_encodings'] = encodings
        if result['raw'].get('encrypted'):
            problems.append('pdf_encrypted_strings_not_decoded')
    xmp = _xmp(data, problems)
    if xmp is not None:
        result['xmp'] = xmp
    return result


def _xmp(data: bytes, problems: list[str]) -> dict | None:
    packets = []
    for match in re.finditer(rb'<x:xmpmeta\b', data):
        end = data.find(b'</x:xmpmeta>', match.start())
        if end > 0:
            packets.append(data[match.start():end + len(b'</x:xmpmeta>')])
    if not packets:
        for _, _, raw in _streams(data, b'Metadata', problems):
            start = raw.find(b'<x:xmpmeta')
            end = raw.find(b'</x:xmpmeta>')
            if start >= 0 and end > start:
                packets.append(raw[start:end + len(b'</x:xmpmeta>')])
    if not packets:
        return None
    packet = packets[-1]
    if len(packet) > MAX_XMP_BYTES:
        problems.append('xmp_over_size_limit')
        return None
    root = _xml(packet, problems)
    if root is None:
        return None
    values = {}
    for description in root.iter(f'{{{NS["rdf"]}}}Description'):
        for attribute, value in description.attrib.items():
            for prefix, uri in NS.items():
                if attribute.startswith(f'{{{uri}}}'):
                    values.setdefault(f'{prefix}:{attribute.split("}", 1)[1]}', value)
        for child in description:
            for prefix, uri in NS.items():
                if child.tag.startswith(f'{{{uri}}}'):
                    name = f'{prefix}:{child.tag.split("}", 1)[1]}'
                    items = [''.join(li.itertext()).strip() for li in child.iter(f'{{{NS["rdf"]}}}li')]
                    values.setdefault(name, items if items else ''.join(child.itertext()).strip())
    if len(packets) > 1:
        problems.append('xmp_multiple_packets_last_used')
    return {k: clean_text(v) if isinstance(v, str) else [clean_text(x) for x in v][:32] for k, v in values.items()
            if k in {f for f, *_ in XMP_TIMES} | set(XMP_CLAIMS)}


# --------------------------------------------------------------------- observation

def detect_format(path: Path, name: str) -> str:
    with path.open('rb') as handle:
        head = handle.read(1024)
    if b'%PDF-' in head:
        return 'pdf'
    if head[:4] == b'PK\x03\x04':
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist()[:MAX_ZIP_MEMBERS])
        except (zipfile.BadZipFile, OSError):
            return EXTENSIONS.get(Path(name).suffix.lower(), 'zip_unreadable')
        if 'mimetype' in names and 'meta.xml' in names:
            return 'odf'
        if '[Content_Types].xml' in names:
            return 'ooxml'
        return EXTENSIONS.get(Path(name).suffix.lower(), 'zip_other')
    if head[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return 'ole'
    return EXTENSIONS.get(Path(name).suffix.lower(), 'unknown')


def observe_artifact(artifact_id: int, *, limits: CommLimits = DEFAULT_LIMITS, issues: Issues | None = None,
                     max_pdf_bytes: int = MAX_PDF_BYTES) -> dict:
    issues = issues or Issues(limits.max_issues)
    with session() as db:
        row = db.execute('SELECT id, source_id, original_name FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    if row is None:
        raise ValueError(f'artifact {artifact_id} does not exist')
    path = stored_path(artifact_id)
    name = row['original_name'] or path.name
    fmt = detect_format(path, name)
    problems: list[str] = []
    if fmt not in ('ooxml', 'odf', 'pdf'):
        code = 'ole_summary_information_not_supported' if fmt == 'ole' else f'document_format_not_supported:{fmt}'
        issues.add(code, artifact_id=artifact_id)
        return {'artifact_id': artifact_id, 'status': 'not_supported', 'format': fmt}
    status = 'parsed'
    try:
        reader = {'ooxml': read_ooxml, 'odf': read_odf}.get(fmt)
        result = reader(path, problems) if reader else read_pdf(path, problems, max_pdf_bytes)
    except Unreadable as exc:
        problems.append(str(exc))
        result, status = {'format': fmt, 'claims': {}, 'tools': {}, 'times_values': {}, 'raw': {}}, 'unreadable'
    family = {'ooxml': 'ooxml_core', 'odf': 'odf_meta', 'pdf': 'pdf_info'}[fmt]
    specs = {'ooxml': OOXML_TIMES, 'odf': ODF_TIMES, 'pdf': PDF_TIMES}[fmt]
    candidates, times_raw = _times(result.get('times_values') or {}, specs, family, problems)
    xmp = result.get('xmp') or {}
    if xmp:
        xmp_candidates, xmp_raw = _times(xmp, XMP_TIMES, 'xmp', problems)
        candidates += xmp_candidates
        times_raw.update({f'xmp/{k}': v for k, v in xmp_raw.items()})
        claims_xmp = {k: v for k, v in xmp.items() if k in XMP_CLAIMS}
    else:
        claims_xmp = {}
    if status == 'parsed' and any(p.startswith(('zip_member_', 'xml_', 'pdf_info_object_not_found', 'pdf_encrypted',
                                                'pdf_scan_byte_limit', 'pdf_stream_inflate', 'xmp_over',
                                                'pdf_object_scan_limit')) for p in problems):
        status = 'partial'
    elif status == 'parsed' and not candidates and not result.get('claims') and not result.get('tools') and not claims_xmp:
        status = 'no_metadata'
    claims, _ = bounded({**(result.get('claims') or {}), **({'xmp': claims_xmp} if claims_xmp else {})}, limits)
    tools, _ = bounded(result.get('tools') or {}, limits)
    raw, _ = bounded(result.get('raw') or {}, limits)
    metadata = {'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                'locator': {'artifact_id': artifact_id, 'format': fmt},
                'document': {'format': fmt, 'status': status, 'claim_fields': sorted(claims),
                             'tools': tools, 'times': times_raw},
                'signal': signal('declared', 'raw_sample', str(tools.get('Application') or tools.get('Producer')
                                                               or tools.get('meta:generator') or 'unknown')[:120],
                                 'file', note='metadata written into the file by software or a person; editable'),
                'semantics': 'authors and dates are claims of the file metadata, not identities or facts',
                'issues': problems}
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        created = next((c for c in candidates if c.kind == 'created' and c.utc), None)
        event_id = None
        if candidates or status in ('parsed', 'partial'):
            event_id = write_event(db, candidates, source_id=row['source_id'], artifact_id=artifact_id,
                                   event_type='document_metadata', ts_start=legacy_instant(created), subject=name,
                                   external_id=f'{PARSER_NAME}@{PARSER_VERSION}', confidence=None, metadata=metadata)
        row_id = append_row(db, 'trace_document_metadata',
                            {'artifact_id': artifact_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION},
                            {'event_id': event_id, 'format': fmt, 'status': status, 'claims_json': dumps(claims),
                             'times_json': dumps(times_raw), 'tools_json': dumps(tools), 'raw_json': dumps(raw),
                             'issues_json': dumps(problems)})
    for problem in problems:
        issues.add(problem, artifact_id=artifact_id)
    return {'artifact_id': artifact_id, 'status': status, 'format': fmt, 'event_id': event_id, 'row_id': row_id,
            'time_candidates': len(candidates), 'problems': problems}


def import_documents(path: Path, *, source_locator: str | None = None, original_name: str | None = None,
                     collection_id: int | None = None, label: str | None = None,
                     limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """One document or a folder of documents (by extension); originals are read only."""
    path = Path(path)
    issues = Issues(limits.max_issues)
    if path.is_dir():
        found = [p for p in walk_files(path, limits, issues) if p.suffix.lower() in EXTENSIONS]
        items = [(p, f'{source_locator}/{p.relative_to(path).as_posix()}' if source_locator else str(p), None)
                 for p in found]
    else:
        items = [(path, source_locator if source_locator is not None else str(path), original_name)]
    results = []
    complete = 'walk_entry_limit' not in issues.counts
    for file_path, locator, name in items:
        if len(results) >= limits.max_records:
            issues.add('record_limit', limit=limits.max_records)
            complete = False
            break
        _, artifact_id = acquire(file_path, source_kind='document_files', source_label=label or 'Documents',
                                 locator=locator, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                                 original_name=name, collection_id=collection_id)
        results.append(observe_artifact(artifact_id, limits=limits, issues=issues))
    counts = {'documents': len(results)}
    for result in results:
        counts[f'status_{result["status"]}'] = counts.get(f'status_{result["status"]}', 0) + 1
    coverage = {'complete': complete}
    record_import_run(artifact_id=results[0]['artifact_id'] if len(results) == 1 else None, importer=PARSER_NAME,
                      parser_version=PARSER_VERSION, options={}, counts=counts, coverage=coverage, issues=issues)
    return {'parser_version': PARSER_VERSION, **counts, 'results': results, 'coverage': coverage,
            'issues': issues.as_dict()}

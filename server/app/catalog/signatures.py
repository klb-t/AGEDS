"""Content type recognition: byte signatures over extensions.

The signature table is data (``data/signatures.json``, versioned). This module
only interprets the generic rule shapes and a few named refinements that need
structure (ISO-BMFF brands, Ogg codec header, EBML doctype, ID3 payload, ZIP
member names, OLE compound-file directory). Text classes (JSON, HTML, XML,
mbox, EML, vCard, iCalendar, CSV/TSV, plain text) are *heuristics* and are
labelled ``basis='heuristic'``.

A sniff result is an observation of leading bytes, never a validation of the
whole file. Both the name's claim (extension) and the bytes' claim are kept;
a disagreement is a recorded conflict, not a correction.
"""
from __future__ import annotations

import codecs
import json
import re
import struct
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

TABLE_PATH = Path(__file__).parent / 'data' / 'signatures.json'
SNIFF_BYTES = 64 * 1024


@dataclass
class Sniff:
    type: str | None
    basis: str  # 'signature' | 'container_members' | 'heuristic' | 'none'
    rule: str | None = None
    refine: str | None = None
    details: dict = field(default_factory=dict)


@lru_cache(maxsize=1)
def table() -> dict:
    data = json.loads(TABLE_PATH.read_text(encoding='utf-8'))
    if data.get('schema') != 'ageds.catalog-signatures/v1' or not data.get('version'):
        raise RuntimeError('unsupported signature table')
    types = data['types']
    for rule in data['rules']:
        if rule['type'] not in types:
            raise RuntimeError(f'signature rule {rule["id"]} names unknown type {rule["type"]}')
    ext_map: dict[str, str] = {}
    for name, spec in types.items():
        for ext in spec['extensions']:
            if ext in ext_map:
                raise RuntimeError(f'extension {ext} declared by {ext_map[ext]} and {name}')
            ext_map[ext] = name
    data['_ext_map'] = ext_map
    data['_compound'] = sorted((e for e in ext_map if '.' in e), key=len, reverse=True)
    group_of: dict[str, int] = {}
    for index, group in enumerate(data['compatible_groups']):
        for name in group:
            group_of.setdefault(name, index)
    data['_group_of'] = group_of
    return data


def version() -> str:
    return table()['version']


def type_info(name: str | None) -> dict:
    if not name:
        return {}
    return table()['types'].get(name, {})


def extension_of(name: str) -> tuple[str | None, str | None]:
    """Return (raw lower-case extension, type the extension claims or 'opaque' or None)."""
    lower = name.lower()
    data = table()
    for compound in data['_compound']:
        if lower.endswith('.' + compound) and len(lower) > len(compound) + 1:
            return compound, data['_ext_map'][compound]
    stem, dot, ext = lower.rpartition('.')
    if not dot or not stem.strip('.') or not ext:
        return None, None
    if ext in data['_ext_map']:
        return ext, data['_ext_map'][ext]
    if ext in data['opaque_extensions']:
        return ext, 'opaque'
    return ext, None


def _condition(prefix: bytes, cond: dict) -> bool:
    expected = bytes.fromhex(cond['hex']) if 'hex' in cond else cond['ascii'].encode('latin-1')
    at = cond['at']
    return prefix[at:at + len(expected)] == expected


def _frame_sync(prefix: bytes, at: int = 0) -> str | None:
    """MPEG audio (mp3) or ADTS (aac) frame header at offset ``at``."""
    if len(prefix) < at + 4 or prefix[at] != 0xFF or prefix[at + 1] & 0xE0 != 0xE0:
        return None
    version_bits = (prefix[at + 1] >> 3) & 3
    layer = (prefix[at + 1] >> 1) & 3
    if layer == 0:
        # ADTS: MPEG version bit, layer 00, sampling index must be valid.
        return 'aac' if (prefix[at + 1] & 0xF6) == 0xF0 and ((prefix[at + 2] >> 2) & 0xF) < 13 else None
    bitrate = prefix[at + 2] >> 4
    rate = (prefix[at + 2] >> 2) & 3
    if version_bits == 1 or bitrate in (0, 15) or rate == 3:
        return None
    return 'mp3'


def _ftyp(prefix: bytes, details: dict) -> str | None:
    data = table()
    brands_map = data['ftyp_brands']
    try:
        box_size = struct.unpack('>I', prefix[:4])[0]
    except struct.error:
        return None
    major = prefix[8:12].decode('latin-1')
    end = min(len(prefix), box_size if 16 <= box_size <= 4096 else 16)
    compatible = [prefix[i:i + 4].decode('latin-1') for i in range(16, end - 3, 4)]
    details.update(major_brand=major, compatible_brands=compatible)
    if major in brands_map:
        return brands_map[major]
    for brand in compatible:
        if brand in brands_map:
            details['brand_basis'] = 'compatible_brand'
            return brands_map[brand]
    return 'isobmff'


def _ogg(prefix: bytes, details: dict) -> str:
    for codec in table()['ogg_codecs']:
        if _condition(prefix, codec):
            details['codec_header'] = codec['type']
            return codec['type']
    details['codec_header'] = 'unknown'
    return 'ogg'


def _ebml(prefix: bytes, details: dict) -> str:
    window = prefix[:4096]
    # DocType element 0x4282, followed by a 1-byte size vint (0x80|len).
    index = window.find(b'\x42\x82')
    if index >= 0 and index + 3 <= len(window):
        size_byte = window[index + 2]
        if size_byte & 0x80:
            length = size_byte & 0x7F
            doctype = window[index + 3:index + 3 + length].decode('ascii', 'replace')
            details['doctype'] = doctype
            return table()['ebml_doctypes'].get(doctype, 'mkv')
    details['doctype'] = 'unknown'
    return 'mkv'


def _id3(prefix: bytes, details: dict) -> str:
    if len(prefix) < 10:
        details['payload'] = 'not_in_prefix'
        return 'mp3'
    size = 0
    for byte in prefix[6:10]:
        size = (size << 7) | (byte & 0x7F)
    start = 10 + size + (10 if prefix[5] & 0x10 else 0)
    details['id3_tag_bytes'] = start
    found = _frame_sync(prefix, start) if start < len(prefix) else None
    if found is None:
        details['payload'] = 'not_in_prefix' if start >= len(prefix) else 'no_frame_sync_after_tag'
        return 'mp3'
    details['payload'] = found
    return found


def sniff(prefix: bytes, *, complete: bool = False) -> Sniff:
    """Classify leading bytes using the signature table, then text heuristics.

    ``complete`` says the prefix is the whole content (enables full JSON parsing
    and the last-line rule of delimiter detection).
    """
    if not prefix:
        return Sniff(None, 'none', details={'empty': True})
    data = table()
    for rule in data['rules']:
        matched = False
        if 'all' in rule:
            matched = all(_condition(prefix, c) for c in rule['all'])
        elif 'any' in rule:
            matched = any(_condition(prefix, c) for c in rule['any'])
        elif 'search' in rule:
            needle = rule['search']['ascii'].encode('latin-1')
            position = prefix[:rule['search']['within']].find(needle)
            matched = position > 0
            if matched:
                found = Sniff(rule['type'], 'signature', rule['id'], details={'header_offset': position})
                return found
        elif rule.get('frame_sync'):
            kind = _frame_sync(prefix)
            if kind:
                return Sniff(kind, 'signature', rule['id'])
            continue
        if not matched:
            continue
        details: dict = {}
        kind = rule['type']
        refine = rule.get('refine')
        if refine == 'ftyp_brand':
            kind = _ftyp(prefix, details) or kind
        elif refine == 'ogg_codec':
            kind = _ogg(prefix, details)
        elif refine == 'ebml_doctype':
            kind = _ebml(prefix, details)
        elif refine == 'id3_payload':
            kind = _id3(prefix, details)
        pending = refine if refine in ('zip_members', 'cfb_directory') else None
        return Sniff(kind, 'signature', rule['id'], pending, details)
    return _text(prefix, complete)


# ---------------------------------------------------------------- refinements

def refine_zip(names: list[str], read_member: Callable[[str], bytes | None]) -> tuple[str, dict]:
    """Refine a ZIP container to an office/package type by its member names."""
    data = table()
    name_set = set(names)
    for rule in data['zip_member_rules']:
        if 'mimetype' in rule:
            if 'mimetype' in name_set:
                content = read_member('mimetype')
                if content is not None and content.strip() == rule['mimetype'].encode('ascii'):
                    return rule['type'], {'zip_rule': 'mimetype_member'}
            continue
        if not all(req in name_set for req in rule.get('requires', ())):
            continue
        prefixes = rule.get('any_prefix')
        if prefixes and not any(n.startswith(p) for n in names for p in prefixes):
            continue
        return rule['type'], {'zip_rule': 'member_names'}
    return 'zip', {}


def refine_cfb(read_at: Callable[[int, int], bytes], size: int, *, max_sectors: int = 256) -> tuple[str, dict]:
    """Read a bounded part of an OLE compound-file directory to name its streams."""
    header = read_at(0, 512)
    if len(header) < 512:
        return 'ole_cfb', {'cfb': 'truncated_header'}
    shift = struct.unpack_from('<H', header, 0x1E)[0]
    if shift not in (9, 12):
        return 'ole_cfb', {'cfb': 'unexpected_sector_shift'}
    sector = 1 << shift
    first_dir = struct.unpack_from('<I', header, 0x30)[0]
    difat = list(struct.unpack_from('<109I', header, 0x4C))
    fat_sectors = [s for s in difat if s < 0xFFFFFFFA][:max_sectors]

    def sector_bytes(number: int) -> bytes:
        offset = (number + 1) * sector
        if offset + sector > size:
            return b''
        return read_at(offset, sector)

    fat: list[int] = []
    for number in fat_sectors:
        raw = sector_bytes(number)
        if not raw:
            break
        fat.extend(struct.unpack(f'<{sector // 4}I', raw))
    names: list[str] = []
    current, seen = first_dir, set()
    while current < 0xFFFFFFFA and current not in seen and len(seen) < max_sectors:
        seen.add(current)
        raw = sector_bytes(current)
        if not raw:
            break
        for offset in range(0, sector, 128):
            entry = raw[offset:offset + 128]
            length = struct.unpack_from('<H', entry, 64)[0]
            if 2 <= length <= 64 and entry[66] in (1, 2, 5):
                names.append(entry[:length - 2].decode('utf-16-le', 'replace'))
        current = fat[current] if current < len(fat) else 0xFFFFFFFE
    details = {'cfb_streams_seen': names[:32], 'cfb_directory_sectors_read': len(seen)}
    for rule in table()['cfb_streams']:
        if any(n in rule.get('names', ()) for n in names) or \
                ('name_prefix' in rule and any(n.startswith(rule['name_prefix']) for n in names)):
            return rule['type'], details
    return 'ole_cfb', details


# ---------------------------------------------------------------- text heuristics

_BOMS = ((codecs.BOM_UTF8, 'utf-8-sig'), (codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
         (codecs.BOM_UTF16_LE, 'utf-16'), (codecs.BOM_UTF16_BE, 'utf-16'))
_HEADER_LINE = re.compile(r'^([A-Za-z0-9][A-Za-z0-9-]{0,76}):[ \t]')
_MBOX_FROM = re.compile(r'^From \S+ ')


def _decode_text(prefix: bytes) -> tuple[str | None, dict]:
    details: dict = {}
    encoding = 'utf-8'
    for bom, name in _BOMS:
        if prefix.startswith(bom):
            encoding = name
            details['bom'] = name
            break
    if encoding == 'utf-8' and b'\x00' in prefix:
        return None, details
    decoder = codecs.getincrementaldecoder(encoding)('strict')
    try:
        text = decoder.decode(prefix, final=False)
    except UnicodeDecodeError:
        try:
            text = prefix.decode('cp1252')
            details['encoding_guess'] = 'cp1252_fallback'
        except UnicodeDecodeError:
            return None, details
    else:
        details['encoding_guess'] = encoding
    text = text.lstrip('﻿')
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in '\r\n\t\f')
    if text and controls / len(text) > table()['text']['max_control_ratio']:
        return None, details
    return text, details


def _text(prefix: bytes, complete: bool) -> Sniff:
    text, details = _decode_text(prefix)
    if text is None:
        return Sniff(None, 'none', details={'binary': True, **details})
    spec = table()['text']
    stripped = text.lstrip()
    lines = text.splitlines()
    head_lower = stripped[:1024].lower()

    def found(kind: str, rule: str, **extra) -> Sniff:
        return Sniff(kind, 'heuristic', rule, details={**details, **extra})

    if not stripped:
        return found('txt', 'whitespace_only')
    if stripped.upper().startswith('BEGIN:VCARD'):
        return found('vcf', 'begin_vcard')
    if stripped.upper().startswith('BEGIN:VCALENDAR'):
        return found('ics', 'begin_vcalendar')
    if lines and _MBOX_FROM.match(lines[0]):
        following = [l for l in lines[1:12] if l.strip()]
        if following and sum(1 for l in following if _HEADER_LINE.match(l)) >= 1:
            return found('mbox', 'from_line_then_headers')
    if stripped[0] in '{[':
        if complete:
            try:
                json.loads(text)
                return found('json', 'json_parsed', json_valid=True)
            except ValueError:
                pass
        records = [l for l in lines[:20] if l.strip()]
        if len(records) >= 2 and all(l.lstrip().startswith('{') for l in records):
            valid = 0
            for line in records[:-1]:
                try:
                    json.loads(line)
                    valid += 1
                except ValueError:
                    break
            if valid >= 1:
                return found('jsonl', 'json_lines')
        return found('json', 'json_opening_bracket', json_valid=False if complete else 'not_checked')
    if head_lower.startswith('<!doctype html') or re.match(r'^(<!--.*?-->\s*)*<html[\s>]', head_lower, re.S):
        return found('html', 'html_tag')
    if stripped.startswith('<?xml') or re.match(r'^<[A-Za-z_][\w.:-]*[\s>/]', stripped):
        if '<svg' in head_lower:
            return found('svg', 'svg_root')
        if '<html' in head_lower:
            return found('html', 'html_in_xml')
        if stripped.startswith('<?xml'):
            return found('xml', 'xml_declaration')
        return found('xml', 'xml_element')
    header_names: list[str] = []
    for line in lines[:60]:
        if not line.strip():
            break
        match = _HEADER_LINE.match(line)
        if match:
            header_names.append(match.group(1).lower())
        elif not (line[:1].isspace() and header_names):
            header_names = []
            break
    if len(set(header_names) & set(spec['email_headers'])) >= spec['min_email_headers']:
        return found('eml', 'rfc822_header_block', headers=sorted(set(header_names))[:12])
    sample = [l for l in lines if l.strip()][:spec['delimiter_lines'] + 1]
    if not complete and len(sample) > 1:
        sample = sample[:-1]  # the last line of a cut prefix may be partial
    for delimiter, kind in (('\t', 'tsv'), (',', 'csv'), (';', 'csv')):
        counts = [line.count(delimiter) for line in sample]
        if len(counts) >= 2 and counts[0] >= 1 and len(set(counts)) == 1:
            return found(kind, 'consistent_delimiter', delimiter=delimiter, columns=counts[0] + 1)
    return found('txt', 'decodable_text')


# ---------------------------------------------------------------- name vs bytes

def relation(extension: str | None, extension_type: str | None, sniffed: Sniff) -> tuple[str, bool]:
    """Compare the name's claim with the bytes' claim; never pick a winner."""
    sniffed_type = sniffed.type
    if extension is None:
        return 'no_extension', False
    if sniffed_type is None:
        return 'unrecognized_content', False
    if extension_type == sniffed_type:
        return 'match', False
    if extension_type == 'opaque':
        return 'opaque_extension', True
    if extension_type is None:
        return 'unregistered_extension', sniffed.basis != 'heuristic'
    groups = table()['_group_of']
    if extension_type in groups and groups.get(extension_type) == groups.get(sniffed_type):
        return 'variant', sniffed.basis != 'heuristic'
    families = (type_info(extension_type).get('family'), type_info(sniffed_type).get('family'))
    text_like = {'text', 'email', 'contact', 'calendar'}
    if sniffed.basis == 'heuristic' and families[0] in text_like and families[1] in text_like:
        return 'text_variant', False
    return 'mismatch', True

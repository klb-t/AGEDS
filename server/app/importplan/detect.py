"""Detectors: catalog entries of pinned scans -> candidate import units.

Rules live in ``data/detectors.v1.json`` (schema ``ageds.importplan-detectors``,
versioned and hashed into every plan). Code implements only the generic rule
evaluation and a fixed set of named, bounded probes:

``xml_root``        first element name after the XML prolog (prefix only)
``json_keys``       top-level JSON kind plus key markers ``"key":`` in the prefix
``text_markers``    literal substrings in the prefix
``exif_marker``     JPEG APP1 ``Exif``, TIFF header or PNG ``eXIf`` in the prefix
``billing_header``  header row of a CSV/TSV prefix (or the first rows of an XLSX
                    copy within ``max_spreadsheet_probe_bytes``) against the
                    billing profiles' column aliases
``recording_name``  the recording-name patterns of the recording importer

Multi-file units are found first: chat exports (a directory or archive root
holding ``conversations*.json``, decided by element key markers, never by the
name alone when two vendors fit) and WhatsApp exports (a zip, or a directory,
with a chat ``.txt`` whose lines look like a WhatsApp chat). Their members are
not offered to single-file rules. Everything else is matched rule by rule; two
different primary importers make an item ambiguous and nothing is chosen.

Probes read at most ``prefix_bytes`` per entry and ``max_probe_bytes`` per plan;
a hit makes the plan coverage incomplete and the affected entries are listed as
not planned with the reason, never silently dropped.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from .. import db as dbmod
from ..catalog import archives
from ..evidence import IntegrityError
from .reader import EntryReader, ReadLimitError

DATA = Path(__file__).resolve().parent / 'data'
DEFAULT_DETECTORS = DATA / 'detectors.v1.json'
TYPE_OF_OPAQUE = 'opaque'
SAMPLES_PER_REASON = 20
ENTRY_COLUMNS = ('id', 'scan_id', 'collection_id', 'parent_entry_id', 'locator', 'depth', 'member_index', 'name',
                 'extension_type', 'sniffed_type', 'sniffed_mime', 'sniff_basis', 'family', 'size_bytes', 'sha256', 'hash_status',
                 'is_container', 'status')


from .limits import LIMIT_NAMES, PlanLimits  # noqa: E402,F401  (re-exported)


def load_detectors(path: Path | str | None = None) -> dict:
    raw = Path(path or DEFAULT_DETECTORS).read_bytes()
    table = json.loads(raw)
    if table.get('schema') != 'ageds.importplan-detectors' or not table.get('version'):
        raise ValueError('detector table must declare schema ageds.importplan-detectors and a version')
    for rule in table.get('files', []):
        if not rule.get('id') or not rule.get('candidates'):
            raise ValueError('every file rule needs an id and candidates')
        for candidate in rule['candidates']:
            if candidate.get('preference', 'primary') not in ('primary', 'alternative'):
                raise ValueError(f'rule {rule["id"]}: preference must be primary or alternative')
    table['_sha256'] = hashlib.sha256(raw).hexdigest()
    table['_path'] = str(Path(path or DEFAULT_DETECTORS))
    return table


def effective_type(entry: dict) -> str:
    if entry.get('sniffed_type'):
        return entry['sniffed_type']
    ext = entry.get('extension_type')
    if ext and ext != TYPE_OF_OPAQUE:
        return ext
    return 'unknown'


_OCCURRENCE = re.compile(r'!(\d+)$')


def split_member(locator: str) -> tuple[str | None, str]:
    """(container locator, path inside it) — ``None`` container for disk paths."""
    path = _OCCURRENCE.sub('', locator)
    index = path.rfind('!/')
    if index < 0:
        return None, path
    return path[:index], path[index + 2:]


def _type_fits(entry: dict, types: set) -> bool:
    """The effective type, or the extension's claim when the bytes were only classified heuristically as text.

    A CSV with a preamble line or an MBOX without a leading ``From`` line can be
    sniffed as plain text; the rule's structure probe then decides.
    """
    if not types or entry['_type'] in types:
        return True
    return entry.get('sniff_basis') in ('heuristic', 'none') and entry.get('extension_type') in types


def _dirname(path: str) -> str:
    return path.rsplit('/', 1)[0] if '/' in path else ''


def _basename(path: str) -> str:
    return path.rsplit('/', 1)[-1]


def _safe_component(component: str) -> str:
    if component in ('', '.', '..'):
        return '_' + {'': 'empty', '.': 'dot', '..': 'dotdot'}[component]
    return component.replace('\x00', '_')


def safe_relative(path: str) -> str:
    """A filesystem-safe relative path for materialization (the locator stays exact)."""
    return '/'.join(_safe_component(c) for c in path.split('/'))


@dataclass
class Item:
    unit_kind: str
    collection_id: int
    scan_id: int
    entry_id: int | None
    locator: str
    name: str
    effective_type: str | None
    candidates: list[dict]
    detection: dict
    members: list[tuple[int, str, str]]  # (entry_id, role, relative_path)
    default_params: dict = field(default_factory=dict)
    estimated_bytes: int = 0

    @property
    def key(self) -> str:
        return f'{self.unit_kind}:{self.scan_id}:{self.locator}'


class _Unplanned:
    def __init__(self):
        self.counts: Counter = Counter()
        self.by_type: Counter = Counter()
        self.samples: dict[str, list] = defaultdict(list)

    def add(self, reason: str, entry: dict, **detail):
        self.counts[reason] += 1
        self.by_type[(reason, effective_type(entry))] += 1
        if len(self.samples[reason]) < SAMPLES_PER_REASON:
            self.samples[reason].append({'entry_id': entry['id'], 'locator': entry['locator'], **detail})

    def as_dict(self) -> dict:
        return {'counts': dict(sorted(self.counts.items())),
                'by_type': [{'reason': r, 'type': t, 'entries': n} for (r, t), n in sorted(self.by_type.items())],
                'samples': dict(sorted(self.samples.items()))}


_XML_PROLOG = re.compile(rb'\s*(?:<\?.*?\?>|<!--.*?-->|<!DOCTYPE[^>]*>|\s)*', re.DOTALL)
_XML_ROOT = re.compile(rb'<([A-Za-z_][\w.:-]*)')


def _decode_prefix(prefix: bytes) -> str:
    if prefix.startswith(b'\xff\xfe') or prefix.startswith(b'\xfe\xff'):
        return prefix.decode('utf-16', errors='replace')
    return prefix.decode('utf-8', errors='replace').lstrip('﻿')


def probe_xml_root(prefix: bytes) -> dict:
    data = prefix[3:] if prefix.startswith(b'\xef\xbb\xbf') else prefix
    match = _XML_PROLOG.match(data)
    position = match.end() if match else 0
    root = _XML_ROOT.match(data, position)
    result = {'root': root.group(1).decode('ascii', 'replace').split(':')[-1].lower() if root else None,
              'doctype': b'<!DOCTYPE' in data[:position]}
    return result


def probe_json_keys(prefix: bytes, keys: list[str]) -> dict:
    text = _decode_prefix(prefix).lstrip()
    top = 'array' if text.startswith('[') else 'object' if text.startswith('{') else None
    found = sorted(k for k in keys if re.search(r'(?<!\\)"' + re.escape(k) + r'"\s*:', text))
    return {'top': top, 'keys': found}


def probe_exif(prefix: bytes) -> dict:
    if prefix[:2] == b'\xff\xd8':
        return {'container': 'jpeg', 'exif': b'Exif\x00\x00' in prefix}
    if prefix[:4] in (b'II*\x00', b'MM\x00*'):
        return {'container': 'tiff', 'exif': True}
    if prefix[:8] == b'\x89PNG\r\n\x1a\n':
        return {'container': 'png', 'exif': b'eXIf' in prefix}
    return {'container': None, 'exif': False}


class Detector:
    def __init__(self, reader: EntryReader, table: dict, limits: PlanLimits, options: dict):
        self.reader, self.table, self.limits, self.options = reader, table, limits, options
        self.prefix_bytes = int(table.get('probe', {}).get('prefix_bytes', 65536))
        self.unplanned = _Unplanned()
        self.incomplete: Counter = Counter()
        self.probe_bytes = 0
        self._prefix_cache: dict[int, bytes | None] = {}
        self._probe_cache: dict[tuple, object] = {}
        self._billing = None
        self._recording = None
        whatsapp = table.get('whatsapp', {})
        self.wa_name = re.compile(whatsapp.get('chat_name_regex', r'(?!)'), re.IGNORECASE)
        self.wa_line = re.compile(whatsapp.get('line_regex', r'(?!)'), re.MULTILINE)
        self.wa_min_lines = int(whatsapp.get('min_matching_lines', 1))
        self.contexts = [(c['id'], re.compile(c['locator_regex']) if c.get('locator_regex') else None,
                          re.compile(c['marker_name_regex']) if c.get('marker_name_regex') else None)
                         for c in table.get('contexts', [])]

    # ------------------------------------------------------------- reading
    def prefix(self, entry: dict) -> bytes | None:
        key = int(entry['id'])
        if key in self._prefix_cache:
            return self._prefix_cache[key]
        if self.probe_bytes + self.prefix_bytes > self.limits.max_probe_bytes:
            self.incomplete['limit:max_probe_bytes'] += 1
            self._prefix_cache[key] = None
            return None
        try:
            data = self.reader.read_prefix(key, self.prefix_bytes)
        except (IntegrityError, ReadLimitError, OSError, LookupError) as exc:
            self.incomplete['probe_failed'] += 1
            self._prefix_cache[key] = None
            entry.setdefault('_probe_error', f'{type(exc).__name__}: {exc}'[:300])
            return None
        self.probe_bytes += len(data)
        if len(self._prefix_cache) > 256:
            self._prefix_cache.clear()
        self._prefix_cache[key] = data
        return data

    def _cached(self, entry: dict, spec: dict, compute):
        key = (entry.get('sha256') or f'entry:{entry["id"]}', json.dumps(spec, sort_keys=True))
        if key not in self._probe_cache:
            self._probe_cache[key] = compute()
        return self._probe_cache[key]

    # ---------------------------------------------------------- name probes
    def billing_profiles(self) -> dict:
        if self._billing is None:
            from ..importers.billing import load_profiles
            path = self.options.get('billing_profiles_path')
            self._billing = load_profiles(Path(path) if path else None)
        return self._billing

    def recording_patterns(self) -> dict:
        if self._recording is None:
            from ..importers.recording_names import load_patterns
            path = self.options.get('recording_patterns_path')
            self._recording = load_patterns(Path(path) if path else None)
        return self._recording

    def _billing_rows(self, entry: dict, kind: str) -> list[list[str]] | None:
        if kind in ('csv', 'tsv'):
            prefix = self.prefix(entry)
            if prefix is None:
                return None
            text = _decode_prefix(prefix)
            lines = text.splitlines()[:21]
            if len(prefix) >= self.prefix_bytes and lines:
                lines = lines[:-1]  # the last line of a cut prefix may be partial
            rows = []
            for line in lines:
                delimiter = max(('\t', ';', ','), key=line.count)
                rows.append([cell.strip().strip('"') for cell in line.split(delimiter)])
            return rows
        if kind == 'xlsx':
            try:
                import openpyxl
            except ImportError:
                entry.setdefault('_probe_error', 'openpyxl unavailable: XLSX header not probed')
                return None
            size = entry.get('size_bytes') or 0
            if size > self.limits.max_spreadsheet_probe_bytes:
                entry.setdefault('_probe_error', 'XLSX larger than max_spreadsheet_probe_bytes: header not probed')
                self.incomplete['limit:max_spreadsheet_probe_bytes'] += 1
                return None
            if self.probe_bytes + size > self.limits.max_probe_bytes:
                self.incomplete['limit:max_probe_bytes'] += 1
                return None
            try:
                with self.reader.open(int(entry['id'])) as stream:
                    data = stream.read(self.limits.max_spreadsheet_probe_bytes + 1)
                self.probe_bytes += len(data)
                workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
                sheet = workbook.worksheets[0]
                rows = [['' if v is None else str(v) for v in row]
                        for row in sheet.iter_rows(min_row=1, max_row=20, values_only=True)]
                workbook.close()
                return rows
            except Exception as exc:  # a broken spreadsheet is a probe result, not a crash
                entry.setdefault('_probe_error', f'XLSX header probe failed: {type(exc).__name__}'[:300])
                return None
        return None

    def probe(self, entry: dict, spec: dict) -> tuple[bool, dict]:
        kind = spec['kind']
        if kind == 'recording_name':
            from ..importers.recording_names import parse_name
            parsed = parse_name(entry['name'], self.recording_patterns())
            primary = parsed.get('primary') or {}
            return parsed['status'] != 'unmatched', {'recording_name': parsed['status'],
                                                     'pattern_id': primary.get('pattern_id'),
                                                     'pattern_set_version': self.recording_patterns()['version']}
        if kind == 'billing_header':
            from ..importers.billing import _resolve_columns
            kind = effective_type(entry)
            if kind not in ('csv', 'tsv', 'xlsx'):
                kind = entry.get('extension_type')
            rows = self._cached(entry, spec, lambda: self._billing_rows(entry, kind))
            if rows is None:
                return False, {'billing_header': 'not_probed', 'error': entry.get('_probe_error')}
            matches = []
            for profile in self.billing_profiles()['profiles']:
                for position, row in enumerate(rows):
                    columns = _resolve_columns(profile, row)
                    if all(field in columns for field in profile['required']):
                        matches.append({'profile_id': profile['id'], 'header_row': position})
                        break
            return bool(matches), {'billing_profiles_matching_header': matches}
        prefix = self.prefix(entry)
        if prefix is None:
            return False, {'probe': kind, 'status': 'not_probed', 'error': entry.get('_probe_error')}
        if kind == 'xml_root':
            result = self._cached(entry, spec, lambda: probe_xml_root(prefix))
            return result['root'] in spec['equals'], {'xml_root': result['root'], 'doctype': result['doctype']}
        if kind == 'json_keys':
            wanted = sorted(set(spec.get('any', [])) | set(spec.get('all', [])))
            result = self._cached(entry, spec, lambda: probe_json_keys(prefix, wanted))
            ok = (spec.get('top') in (None, result['top'])
                  and (not spec.get('any') or any(k in result['keys'] for k in spec['any']))
                  and all(k in result['keys'] for k in spec.get('all', [])))
            if ok and spec.get('all_text'):
                text = _decode_prefix(prefix)
                ok = all(marker in text for marker in spec['all_text'])
            return ok, {'json_top': result['top'], 'json_keys': result['keys']}
        if kind == 'text_markers':
            text = _decode_prefix(prefix)
            found = [m for m in spec.get('all', []) if m in text]
            return len(found) == len(spec.get('all', [])), {'text_markers': found}
        if kind == 'exif_marker':
            result = probe_exif(prefix)
            return bool(result['exif']), {'exif_container': result['container'], 'exif_block_in_prefix': result['exif']}
        raise ValueError(f'unknown probe kind {kind!r}')

    # -------------------------------------------------------------- contexts
    def context_of(self, entry: dict, marked_containers: set, marked_dirs: set) -> list[str]:
        found = []
        container, path = split_member(entry['locator'])
        for context_id, locator_regex, _ in self.contexts:
            hit = bool(locator_regex and locator_regex.search(entry['locator']))
            if not hit:
                parent = entry.get('parent_entry_id')
                chain = entry.get('_chain', ())
                hit = any(c in marked_containers.get(context_id, ()) for c in chain) or \
                    (entry['scan_id'], container, _dirname(path)) in marked_dirs.get(context_id, ()) or \
                    (parent is not None and parent in marked_containers.get(context_id, ()))
            if hit:
                found.append(context_id)
        return found


def _load_entries(scan_ids: list[int], limits: PlanLimits) -> tuple[list[dict], bool]:
    with closing(dbmod.connect()) as db:
        marks = ','.join('?' * len(scan_ids))
        rows = db.execute(f'SELECT {",".join(ENTRY_COLUMNS)} FROM catalog_entries WHERE scan_id IN ({marks}) '
                          f'ORDER BY scan_id, id LIMIT ?', (*scan_ids, limits.max_entries + 1)).fetchall()
    entries = [dict(r) for r in rows[:limits.max_entries]]
    return entries, len(rows) > limits.max_entries


def detect(scan_ids: list[int], *, reader: EntryReader, table: dict, limits: PlanLimits, options: dict) -> dict:
    entries, truncated = _load_entries(scan_ids, limits)
    det = Detector(reader, table, limits, options)
    if truncated:
        det.incomplete['limit:max_entries'] += 1
    reader.prime(entries)
    by_id = {e['id']: e for e in entries}
    for entry in entries:
        chain, parent = [], entry['parent_entry_id']
        while parent is not None and parent in by_id and len(chain) < 64:
            chain.append(parent)
            parent = by_id[parent]['parent_entry_id']
        entry['_chain'] = tuple(chain)
        entry['_type'] = effective_type(entry)
        entry['_container'], entry['_path'] = split_member(entry['locator'])
    # (scan, parent container entry or None, member/disk path) indexes
    by_place: dict[tuple, list[dict]] = defaultdict(list)
    for entry in entries:
        by_place[(entry['scan_id'], entry['parent_entry_id'])].append(entry)
    dirs: dict[tuple, list[dict]] = defaultdict(list)
    for entry in entries:
        dirs[(entry['scan_id'], entry['parent_entry_id'], _dirname(entry['_path']))].append(entry)

    marked_containers: dict[str, set] = defaultdict(set)
    marked_dirs: dict[str, set] = defaultdict(set)
    for context_id, _, marker in det.contexts:
        if marker is None:
            continue
        for entry in entries:
            if marker.fullmatch(_basename(entry['_path'])):
                if entry['parent_entry_id'] is not None:
                    marked_containers[context_id].add(entry['parent_entry_id'])
                marked_dirs[context_id].add((entry['scan_id'], entry['_container'], _dirname(entry['_path'])))

    covered: set[int] = set()
    items: list[Item] = []

    def under(scan_id, parent_id, directory) -> list[dict]:
        prefix = directory + '/' if directory else ''
        return [e for e in by_place[(scan_id, parent_id)] if e['_path'].startswith(prefix)]

    def verifiable(entry) -> bool:
        return entry['status'] == 'ok' and bool(entry['sha256'])

    def unit_locator(scan_id, parent_id, directory) -> tuple[str, str, int | None]:
        if parent_id is None:
            return 'directory', directory or '.', None
        container = by_id[parent_id]
        if not directory:
            return 'archive', container['locator'], parent_id
        return 'directory', f'{container["locator"]}!/{directory}', None

    scan_collection = {e['scan_id']: e['collection_id'] for e in entries}

    def collection_of(scan_id):
        return scan_collection[scan_id]

    # ---------------------------------------------------------------- chat exports
    chat = table.get('chat_exports') or {}
    conv_name = re.compile(chat.get('conversation_name_regex', r'(?!)'))
    vendors = chat.get('vendors', [])
    vendor_patterns = {}
    for vendor in vendors:
        # The chat importers' mapping files are the authority for conversation file names.
        mapping = json.loads((Path(__file__).resolve().parents[1] / 'importers' / 'data' /
                              f'{vendor["mapping"]}_mapping.v1.json').read_text(encoding='utf-8'))
        files = mapping['files']
        vendor_patterns[vendor['importer']] = [re.compile(p) for p in (files.get('conversations'), files.get('shard')) if p]
    for (scan_id, parent_id, directory), siblings in sorted(dirs.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0, kv[0][2])):
        conv_files = [e for e in siblings if conv_name.fullmatch(_basename(e['_path'])) and e['is_container'] == 0]
        if not conv_files or any(e['id'] in covered for e in conv_files):
            continue
        unit_kind, locator, root_entry = unit_locator(scan_id, parent_id, directory)
        companions = sorted({_basename(e['_path']) for e in siblings} - {_basename(e['_path']) for e in conv_files})
        evidence, reasons = {}, defaultdict(list)
        name_fit = {}
        for vendor in vendors:
            key = vendor['importer']
            fits = [e for e in conv_files if any(p.fullmatch(_basename(e['_path'])) for p in vendor_patterns[key])]
            name_fit[key] = len(fits) == len(conv_files)
            companion_regex = re.compile(vendor.get('companion_name_regex') or r'(?!)')
            hits = [c for c in companions if companion_regex.fullmatch(c)]
            if hits:
                reasons[key].append(f'companion files: {", ".join(hits[:5])}')
            evidence[key] = {'companions': hits[:20], 'markers': []}
        probe_notes = []
        for conv in conv_files:
            if not verifiable(conv):
                probe_notes.append({'entry_id': conv['id'], 'status': conv['status'], 'probe': 'not_possible'})
                continue
            prefix = det.prefix(conv)
            if prefix is None:
                probe_notes.append({'entry_id': conv['id'], 'probe': 'not_probed', 'error': conv.get('_probe_error')})
                continue
            for vendor in vendors:
                found = probe_json_keys(prefix, vendor['element_key_markers'])
                if found['top'] == 'array' and found['keys']:
                    evidence[vendor['importer']]['markers'].append({'entry_id': conv['id'], 'keys': found['keys']})
        by_marker = [v['importer'] for v in vendors if evidence[v['importer']]['markers'] and name_fit[v['importer']]]
        by_name = [v['importer'] for v in vendors if name_fit[v['importer']]]
        for key in by_marker:
            reasons[key].insert(0, 'element key markers in conversations prefix: ' +
                                ', '.join(sorted({k for m in evidence[key]['markers'] for k in m['keys']})))
        if len(by_marker) == 1:
            chosen, ambiguous, basis = by_marker[0], False, 'structure_marker'
        elif len(by_marker) > 1:
            chosen, ambiguous, basis = None, True, 'conflicting_structure_markers'
        elif len(by_name) == 1:
            chosen, ambiguous, basis = by_name[0], False, 'file_name_pattern_only_structure_unconfirmed'
            reasons[chosen].insert(0, 'only this export format names conversation files like this; no structure marker in the prefix')
        else:
            chosen, ambiguous, basis = None, True, 'no_structure_marker'
        candidate_keys = by_marker if len(by_marker) > 1 else (by_name if chosen is None else [chosen])
        if not candidate_keys:
            for conv in conv_files:
                det.unplanned.add('chat_export_name_without_matching_format', conv)
            continue
        candidates = [{'importer': key, 'preference': 'primary',
                       'reasons': reasons[key] or ['conversation file name pattern of this export format']}
                      for key in candidate_keys]
        members_all = under(scan_id, parent_id, directory)
        members, skipped = [], []
        for entry in members_all:
            if entry['is_container'] and entry['_type'] in archives.CONTAINER_TYPES:
                pass  # a nested archive is an ordinary attachment candidate file
            if len(members) >= limits.max_unit_members:
                det.incomplete['limit:max_unit_members'] += 1
                skipped.append(entry)
                continue
            members.append(entry)
        rel_seen: set[str] = set()
        member_rows = []
        for entry in members:
            relative = entry['_path'][len(directory) + 1:] if directory else entry['_path']
            relative = safe_relative(relative)
            if relative in rel_seen:
                relative = f'{relative}~dup{entry["id"]}'
            rel_seen.add(relative)
            role = 'conversations' if entry in conv_files else 'attachment_candidate'
            member_rows.append((entry['id'], role, relative))
            covered.add(entry['id'])
        detection = {
            'unit': 'chat_export', 'materialize': 'members', 'basis': basis,
            'conversation_files': [{'entry_id': e['id'], 'locator': e['locator'], 'sha256': e['sha256'],
                                    'size_bytes': e['size_bytes'], 'status': e['status']} for e in conv_files],
            'vendor_evidence': evidence, 'probes': probe_notes,
            'members_not_verifiable': sum(1 for e in members if not verifiable(e)),
            'members_over_limit': len(skipped),
        }
        name = _basename(by_id[root_entry]['_path']) if root_entry else (_basename(directory) or locator)
        items.append(Item(unit_kind, collection_of(scan_id), scan_id, root_entry, locator, name, 'chat_export',
                          candidates, detection, member_rows,
                          estimated_bytes=sum(e['size_bytes'] or 0 for e in members)))
        items[-1].detection['ambiguous'] = ambiguous
        items[-1].detection['chosen'] = chosen

    # ---------------------------------------------------------------- WhatsApp
    def whatsapp_lines(entry) -> int:
        if not verifiable(entry):
            return 0
        prefix = det.prefix(entry)
        if prefix is None:
            return 0
        lines = _decode_prefix(prefix).splitlines()[:40]
        return sum(1 for line in lines if det.wa_line.match(line))

    for entry in entries:
        if entry['id'] in covered or entry['_type'] != 'zip' or not entry['is_container'] or not verifiable(entry):
            continue
        members = by_place[(entry['scan_id'], entry['id'])]
        txts = [m for m in members if '/' not in m['_path'] and m['_path'].lower().endswith('.txt')]
        named = [m for m in txts if det.wa_name.fullmatch(m['_path'])]
        probed = sorted(named or txts, key=lambda m: m['id'])[:limits.max_whatsapp_txt_probes_per_zip]
        chats = [m for m in probed if whatsapp_lines(m) >= det.wa_min_lines]
        if not chats:
            continue
        chat_names = sorted(m['_path'] for m in chats)
        default_params = {'chat_member': chat_names[0]} if len(chat_names) == 1 else {}
        reasons = [f'zip holds a WhatsApp-like chat text: {", ".join(chat_names[:3])}']
        if named:
            reasons.append('chat file name matches the WhatsApp export pattern')
        detection = {'unit': 'whatsapp_export', 'materialize': 'self', 'chat_members': chat_names,
                     'members': len(members), 'ambiguous': False, 'chosen': 'whatsapp_export'}
        for member in under(entry['scan_id'], entry['id'], ''):
            covered.add(member['id'])
        covered.add(entry['id'])
        items.append(Item('archive', entry['collection_id'], entry['scan_id'], entry['id'], entry['locator'], entry['name'],
                          'zip', [{'importer': 'whatsapp_export', 'preference': 'primary', 'reasons': reasons}],
                          detection, [(entry['id'], 'archive', safe_relative(_basename(entry['_path'])))],
                          default_params=default_params, estimated_bytes=entry['size_bytes'] or 0))

    for (scan_id, parent_id, directory), siblings in sorted(dirs.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0, kv[0][2])):
        candidates_txt = [e for e in siblings if e['id'] not in covered and det.wa_name.fullmatch(_basename(e['_path']))]
        chats = [e for e in candidates_txt if whatsapp_lines(e) >= det.wa_min_lines]
        if not chats:
            continue
        unit_kind, locator, root_entry = unit_locator(scan_id, parent_id, directory)
        if root_entry is not None:
            continue  # an archive root is handled by the zip pass above
        files = [e for e in siblings if e['id'] not in covered and not e['is_container']]
        chat_names = sorted(_basename(e['_path']) for e in chats)
        member_rows = [(e['id'], 'chat_text' if e in chats else 'media_candidate', safe_relative(_basename(e['_path'])))
                       for e in files]
        for e in files:
            covered.add(e['id'])
        items.append(Item('directory', collection_of(scan_id), scan_id, None, locator,
                          _basename(directory) or locator, 'whatsapp_folder',
                          [{'importer': 'whatsapp_export', 'preference': 'primary',
                            'reasons': [f'directory holds a WhatsApp chat text: {", ".join(chat_names[:3])}']}],
                          {'unit': 'whatsapp_export', 'materialize': 'siblings', 'chat_members': chat_names,
                           'members_not_verifiable': sum(1 for e in files if not verifiable(e)),
                           'ambiguous': False, 'chosen': 'whatsapp_export'},
                          member_rows, default_params={'chat_member': chat_names[0]} if len(chat_names) == 1 else {},
                          estimated_bytes=sum(e['size_bytes'] or 0 for e in files)))

    # ---------------------------------------------------------------- single files
    rules = []
    for rule in table.get('files', []):
        rules.append({**rule, '_loc': re.compile(rule['locator_regex']) if rule.get('locator_regex') else None,
                      '_not': re.compile(rule['locator_not_regex']) if rule.get('locator_not_regex') else None,
                      '_name': re.compile(rule['name_regex']) if rule.get('name_regex') else None,
                      '_types': set(rule.get('types') or [])})
    for entry in entries:
        if entry['id'] in covered:
            det.unplanned.counts['covered_by_unit'] += 1
            continue
        if entry['is_container'] and entry['_type'] in archives.CONTAINER_TYPES:
            det.unplanned.counts['archive_container_members_considered'] += 1
            continue
        matching = [r for r in rules if _type_fits(entry, r['_types'])
                    and (r['_loc'] is None or r['_loc'].search(entry['locator']))
                    and (r['_not'] is None or not r['_not'].search(entry['locator']))
                    and (r['_name'] is None or r['_name'].fullmatch(entry['name']))]
        if not matching:
            det.unplanned.add('no_detector_matched', entry)
            continue
        if not verifiable(entry):
            det.unplanned.add(f'entry_not_verifiable:{entry["status"]}', entry, hash_status=entry['hash_status'])
            continue
        hits, probes = [], {}
        for rule in matching:
            if rule.get('probe'):
                ok, observed = det.probe(entry, rule['probe'])
                probes[rule['id']] = observed
                if not ok:
                    continue
            hits.append(rule)
        if not hits:
            reason = 'probe_not_possible' if entry.get('_probe_error') else 'structure_probe_did_not_match'
            det.unplanned.add(reason, entry, rules=[r['id'] for r in matching], error=entry.get('_probe_error'))
            continue
        merged: dict[str, dict] = {}
        for rule in hits:
            for candidate in rule['candidates']:
                key = candidate['importer']
                slot = merged.setdefault(key, {'importer': key, 'preference': 'alternative', 'reasons': [], 'rules': []})
                if candidate.get('preference', 'primary') == 'primary':
                    slot['preference'] = 'primary'
                slot['reasons'].append(candidate['reason'])
                slot['rules'].append(rule['id'])
        primary = [c for c in merged.values() if c['preference'] == 'primary']
        candidates = sorted(merged.values(), key=lambda c: (c['preference'] != 'primary', c['importer']))
        ambiguous = len(primary) > 1
        default_params = {}
        billing = probes.get('billing_table')
        if billing and 'billing' in merged:
            matched = [m['profile_id'] for m in billing.get('billing_profiles_matching_header', [])]
            merged['billing']['profiles_matching_header'] = matched
            explicit = options.get('billing_profile_id')
            if explicit and explicit in matched:
                default_params['profile_id'] = explicit
        detection = {'unit': 'file', 'materialize': 'self', 'rules': [r['id'] for r in hits], 'probes': probes,
                     'ambiguous': ambiguous, 'chosen': primary[0]['importer'] if len(primary) == 1 else None}
        items.append(Item('file', entry['collection_id'], entry['scan_id'], entry['id'], entry['locator'], entry['name'],
                          entry['_type'], candidates, detection,
                          [(entry['id'], 'file', safe_relative(_basename(entry['_path'])))],
                          default_params=default_params, estimated_bytes=entry['size_bytes'] or 0))

    for item in items:
        root = by_id.get(item.entry_id) if item.entry_id else by_id.get(item.members[0][0]) if item.members else None
        item.detection['context'] = det.context_of(root, marked_containers, marked_dirs) if root else []
    items.sort(key=lambda i: (i.collection_id, i.scan_id, i.locator, i.unit_kind))
    coverage = {'entries_considered': len(entries), 'entries_truncated': truncated,
                'probe_bytes_read': det.probe_bytes, 'complete': not det.incomplete,
                'incomplete_reasons': dict(sorted(det.incomplete.items())),
                'not_planned': det.unplanned.as_dict()}
    return {'items': items, 'coverage': coverage, 'entries': by_id}

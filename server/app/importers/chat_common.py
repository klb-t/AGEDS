"""Shared machinery for AI chat export importers (ChatGPT, Claude.ai).

The importers keep four things apart:

* the preserved file (an ``artifacts`` row via ``ingest_file``; parsing reads
  the content-store snapshot, never the original path again);
* parser observations (``events`` rows with bounded raw fields, a JSON-pointer
  locator into the stored artifact and ``event_times`` candidates);
* logical identity across exports (``logical_records``/``logical_occurrences``
  in ``server.app.records``);
* interpretation, which is fixed by the parser version plus the versioned key
  mapping file (``importers/data/*_mapping.v1.json``). A different mapping file
  is a different interpretation with its own event identities; the earlier
  events stay.

Large files are not loaded whole: the top-level JSON array is split into its
elements by a byte scanner over a read-only ``mmap`` of the stored copy, and
each element (one conversation) is decoded separately under
``max_conversation_bytes``. Peak heap is therefore bounded by the largest
decoded conversation, not by the file size.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import os
import re
from bisect import bisect_left
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from ..db import session
from ..evidence import IntegrityError, add_event, ensure_source, ingest_file
from ..timeinfo import TimeCandidate, from_epoch, from_iso, record_event_times
from .. import records

DATA_DIR = Path(__file__).resolve().parent / 'data'
MAPPING_SCHEMA = 'ageds.chat_export_mapping'
MAPPING_MAX_BYTES = 1024 * 1024

# Payload-shaping bounds are part of the parser version: changing one changes
# stored event payloads, so it requires a new PARSER_VERSION, not a CLI flag.
BODY_MAX_CHARS = 4_000_000
RAW_FIELD_MAX_BYTES = 16 * 1024
CHILDREN_LISTED_MAX = 256
LIST_FIELD_MAX = 64
TIME_RAW_MAX_CHARS = 200
ASSET_SCAN_MAX_VALUES = 20_000

# Issue codes that mean "some source content was not interpreted".
PARTIAL_CODES = frozenset({
    'conversation_bytes_limit_exceeded', 'conversation_limit_reached', 'node_limit_reached',
    'truncated_json', 'malformed_json_structure', 'malformed_conversation_json',
    'conversation_not_recorded', 'trailing_data_after_array', 'non_object_element',
    'conversation_not_object', 'conversation_shape_not_recognized',
})
FAILED_CODES = frozenset({
    'empty_file', 'top_level_not_array', 'unsupported_encoding', 'file_too_large',
})


@dataclass(frozen=True)
class ChatImportLimits:
    """Run limits. They decide what is interpreted, never how (see BODY_MAX_CHARS)."""
    max_file_bytes: int = 8 * 1024 ** 3
    max_conversation_bytes: int = 64 * 1024 ** 2
    max_conversations_per_file: int = 1_000_000
    max_nodes_per_conversation: int = 200_000
    max_input_files: int = 100_000
    max_walk_depth: int = 8
    max_attachment_bytes: int = 4 * 1024 ** 3
    max_issues_listed: int = 1000

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f'{name} must be a positive integer')

    def as_dict(self) -> dict:
        return asdict(self)


class IssueLog:
    """Bounded list plus exact per-code counts; nothing is silently dropped."""

    def __init__(self, max_listed: int):
        self.items: list[dict] = []
        self.counts: Counter = Counter()
        self.max_listed = max_listed

    def add(self, code: str, locator: str | None = None, **detail) -> None:
        self.counts[code] += 1
        if len(self.items) < self.max_listed:
            item = {'code': code}
            if locator is not None:
                item['locator'] = storable_text(locator)
            item.update({k: _storable_value(v) for k, v in detail.items()})
            self.items.append(item)

    def codes(self) -> set[str]:
        return set(self.counts)

    def as_dict(self) -> dict:
        total = sum(self.counts.values())
        return {'issues': self.items, 'issue_counts': dict(sorted(self.counts.items())),
                'issues_truncated': total - len(self.items)}


# --- JSON decoding that keeps source spellings and duplicate keys -------------

class RawFloat(float):
    """A JSON number with fraction/exponent that remembers its exact spelling."""

    def __new__(cls, text: str):
        value = super().__new__(cls, text)
        value.raw = text
        return value


class JsonObject(dict):
    """First occurrence of a key wins; later duplicates stay in ``duplicates``."""
    __slots__ = ('duplicates',)


def _object_pairs(pairs):
    obj = JsonObject()
    duplicates = None
    for key, value in pairs:
        if key in obj:
            if duplicates is None:
                duplicates = []
            duplicates.append((key, value))
        else:
            obj[key] = value
    obj.duplicates = duplicates
    return obj


def _reject_constant(name: str):
    raise ValueError(f'non-standard JSON constant {name}')


def loads(data: bytes | str):
    return json.loads(data, object_pairs_hook=_object_pairs, parse_float=RawFloat,
                      parse_constant=_reject_constant)


def duplicate_keys(value) -> list[str]:
    if isinstance(value, JsonObject) and value.duplicates:
        return [key for key, _ in value.duplicates]
    return []


_TOKEN = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"|[\[\]{}]')
_NONWS = re.compile(rb'\S')
_NONSEP = re.compile(rb'[^\s,]')
_ITEM = re.compile(rb'[^\s,]+')


@dataclass
class Element:
    index: int
    start: int
    end: int
    status: str  # ok | conversation_bytes_limit_exceeded | non_object_element | truncated
    data: bytes | None = None


def iter_array_elements(buf, *, max_element_bytes: int, max_elements: int, issues: IssueLog,
                        file_label: str) -> Iterator[Element]:
    """Yield top-level array elements with byte ranges; never decodes the whole file.

    The scanner tracks strings (with escapes) and bracket nesting only. Indices
    follow JSON array positions, including scalar elements, so pointers stay exact.
    """
    size = len(buf)
    pos = 0
    if size >= 3 and buf[0:3] == b'\xef\xbb\xbf':
        pos = 3
        issues.add('utf8_bom_skipped', file_label)
    first = _NONWS.search(buf, pos)
    if first is None:
        issues.add('empty_file', file_label)
        return
    head = buf[first.start():first.start() + 4]
    if b'\x00' in head:
        issues.add('unsupported_encoding', file_label, detail='NUL bytes at start (UTF-16/32?)')
        return
    if buf[first.start()] != 0x5B:  # [
        issues.add('top_level_not_array', file_label, byte=first.start())
        return
    stack: list[int] = []
    index = 0
    element_start: int | None = None
    last = first.start() + 1
    closed = False

    def scalar_gap(begin: int, end: int):
        nonlocal index
        if _NONSEP.search(buf, begin, end) is None:
            return
        for item in _ITEM.finditer(buf, begin, end):
            issues.add('non_object_element', f'{file_label}#/{index}', byte=item.start())
            index += 1

    for token in _TOKEN.finditer(buf, first.start() + 1):
        start = token.start()
        char = buf[start]
        if not stack:
            if index >= max_elements and char != 0x5D:
                issues.add('conversation_limit_reached', file_label, limit=max_elements)
                return
            scalar_gap(last, start)
            if char == 0x22:  # string element
                issues.add('non_object_element', f'{file_label}#/{index}', byte=start)
                index += 1
                last = token.end()
            elif char in (0x7B, 0x5B):
                stack.append(char)
                element_start = start
            elif char == 0x5D:
                closed = True
                trailing = _NONWS.search(buf, token.end())
                if trailing is not None:
                    issues.add('trailing_data_after_array', file_label, byte=trailing.start())
                break
            else:
                issues.add('malformed_json_structure', file_label, byte=start)
                return
            continue
        if char == 0x22:
            continue
        if char in (0x7B, 0x5B):
            stack.append(char)
            continue
        opener = stack.pop()
        if (opener, char) not in ((0x7B, 0x7D), (0x5B, 0x5D)):
            issues.add('malformed_json_structure', f'{file_label}#/{index}', byte=start)
            return
        if stack:
            continue
        end = token.end()
        assert element_start is not None
        if opener == 0x5B:
            issues.add('non_object_element', f'{file_label}#/{index}', byte=element_start)
            yield Element(index, element_start, end, 'non_object_element')
        elif end - element_start > max_element_bytes:
            issues.add('conversation_bytes_limit_exceeded', f'{file_label}#/{index}',
                       bytes=end - element_start, limit=max_element_bytes,
                       byte_range=[element_start, end])
            yield Element(index, element_start, end, 'conversation_bytes_limit_exceeded')
        else:
            yield Element(index, element_start, end, 'ok', bytes(buf[element_start:end]))
        index += 1
        last = end
        element_start = None
    if not closed:
        issues.add('truncated_json', f'{file_label}#/{index}', byte=element_start if element_start is not None else size,
                   detail='end of file inside the top-level array')
        if element_start is not None:
            yield Element(index, element_start, size, 'truncated')


def decode_element(element: Element, issues: IssueLog, locator: str):
    """Decode one element; invalid UTF-8 is replaced and reported, never fatal."""
    try:
        return loads(element.data)
    except UnicodeDecodeError:
        issues.add('invalid_utf8_replaced', locator)
        try:
            return loads(element.data.decode('utf-8', errors='replace'))
        except (ValueError, RecursionError) as exc:
            issues.add('malformed_conversation_json', locator, detail=str(exc)[:200])
            return None
    except (ValueError, RecursionError) as exc:
        issues.add('malformed_conversation_json', locator, detail=str(exc)[:200])
        return None


# --- JSON pointers with a '*' wildcard ----------------------------------------

MISSING = object()


def escape_token(token) -> str:
    return str(token).replace('~', '~0').replace('/', '~1')


def _tokens(pointer: str) -> list[str]:
    if pointer == '':
        return []
    if not pointer.startswith('/'):
        raise ValueError(f'JSON pointer must start with "/": {pointer!r}')
    return [t.replace('~1', '/').replace('~0', '~') for t in pointer[1:].split('/')]


def expand(value, pointer: str, base: str = '') -> list[tuple[str, object]]:
    """All (concrete pointer, value) matches; ``*`` expands every list item/object value."""
    results = [(base, value)]
    for token in _tokens(pointer):
        nxt = []
        for where, current in results:
            if token == '*':
                if isinstance(current, list):
                    nxt.extend((f'{where}/{i}', item) for i, item in enumerate(current))
                elif isinstance(current, dict):
                    nxt.extend((f'{where}/{escape_token(k)}', v) for k, v in current.items())
            elif isinstance(current, dict):
                if token in current:
                    nxt.append((f'{where}/{escape_token(token)}', current[token]))
            elif isinstance(current, list) and token.isdigit():
                position = int(token)
                if position < len(current):
                    nxt.append((f'{where}/{position}', current[position]))
        results = nxt
        if not results:
            break
    return results


def first_of(value, pointers: list[str], base: str = '') -> tuple[object, str | None]:
    """First non-null match among alternative pointers (mapping order)."""
    for pointer in pointers or ():
        for where, found in expand(value, pointer, base):
            if found is not None:
                return found, where
    return None, None


def collect_text(value, specs, base: str = '') -> list[tuple[str, str]]:
    """Text pieces with their concrete pointers, following a mapping text spec.

    Spec forms: ``"/ptr"`` (strings at the pointer), ``{"each": "/ptr/*",
    "fields": [spec, ...]}`` (per element, relative specs) and ``{"json": "/ptr"}``
    (canonical JSON of any value). Non-string values at plain pointers are skipped.
    """
    out: list[tuple[str, str]] = []
    for spec in specs or ():
        if isinstance(spec, str):
            out.extend((found, where) for where, found in expand(value, spec, base) if isinstance(found, str))
        elif isinstance(spec, dict) and 'each' in spec:
            for where, item in expand(value, spec['each'], base):
                out.extend(collect_text(item, spec.get('fields', ['']), where))
        elif isinstance(spec, dict) and 'json' in spec:
            for where, found in expand(value, spec['json'], base):
                if found is not None:
                    out.append((canonical_json(found), where))
        else:
            raise ValueError(f'unsupported text spec: {spec!r}')
    return out


def iter_key_values(value, key: str, base: str = '', max_values: int = ASSET_SCAN_MAX_VALUES):
    """Iterative search for ``key`` anywhere below ``value`` (bounded work)."""
    stack = [(base, value)]
    seen = 0
    while stack:
        where, current = stack.pop()
        seen += 1
        if seen > max_values:
            return
        if isinstance(current, dict):
            items = list(current.items())
            for k, v in reversed(items):
                child = f'{where}/{escape_token(k)}'
                if k == key and isinstance(v, str):
                    yield child, v
                elif isinstance(v, (dict, list)):
                    stack.append((child, v))
        elif isinstance(current, list):
            for i in range(len(current) - 1, -1, -1):
                if isinstance(current[i], (dict, list)):
                    stack.append((f'{where}/{i}', current[i]))


# --- Canonical hashing and storable values -------------------------------------

def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def canonical_sha256(value) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8', 'surrogatepass')).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()


_SURROGATE = re.compile('[\ud800-\udfff]')


def storable_text(text):
    """SQLite needs valid UTF-8: lone surrogates (legal in JSON escapes) become U+FFFD."""
    if isinstance(text, str) and _SURROGATE.search(text):
        return _SURROGATE.sub('\ufffd', text)
    return text


def has_lone_surrogate(text) -> bool:
    return isinstance(text, str) and _SURROGATE.search(text) is not None


def _storable_value(value):
    if isinstance(value, str):
        return storable_text(value)
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            return '<unserializable>'
        return json.loads(_SURROGATE.sub('\ufffd', text)) if _SURROGATE.search(text) else value
    return value


def bounded_json(value, max_bytes: int = RAW_FIELD_MAX_BYTES) -> dict:
    """Raw value if its canonical form fits; otherwise size, hash and keys only."""
    try:
        text = canonical_json(value)
    except (ValueError, TypeError, RecursionError):
        return {'omitted': 'unserializable'}
    encoded = text.encode('utf-8', 'surrogatepass')
    if len(encoded) > max_bytes:
        out = {'omitted': 'size_limit', 'bytes': len(encoded), 'limit': max_bytes,
               'sha256': hashlib.sha256(encoded).hexdigest()}
        if isinstance(value, dict):
            out['keys'] = sorted(storable_text(str(k)) for k in value)[:200]
        return out
    if _SURROGATE.search(text):
        return {'value': json.loads(_SURROGATE.sub('\ufffd', text)), 'lone_surrogates_replaced': True}
    return {'value': value}


def bounded_body(pieces: list[str], joiner: str) -> tuple[str | None, dict]:
    if not pieces:
        return None, {'chars': 0, 'truncated': False}
    text = joiner.join(pieces)
    info = {'chars': len(text), 'truncated': False, 'sha256': text_sha256(text)}
    if len(text) > BODY_MAX_CHARS:
        text = text[:BODY_MAX_CHARS]
        info.update(truncated=True, stored_chars=BODY_MAX_CHARS)
    if has_lone_surrogate(text):
        text = storable_text(text)
        info['lone_surrogates_replaced'] = True
    return text, info


def scalar_or_none(value):
    """Columns take strings only; other JSON kinds stay in metadata."""
    return storable_text(value) if isinstance(value, str) else None


def bounded_list(values: list, limit: int = LIST_FIELD_MAX) -> dict:
    return {'items': [_storable_value(v) for v in values[:limit]], 'count': len(values),
            'truncated': len(values) > limit}


# --- Time candidates -------------------------------------------------------------

def _raw_time_text(value) -> str:
    if isinstance(value, RawFloat):
        return value.raw
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def time_candidate(kind: str, value, source: str, *, formats: list[str], zero_is_placeholder: bool) -> TimeCandidate | None:
    """Interpret one source time value under the mapping's declared formats.

    Missing values give no candidate. Unparsed values remain candidates with
    their raw text and ``tz_status='unknown'``; nothing is projected to UTC by guess.
    """
    if value is None:
        return None
    raw = _raw_time_text(value)
    meta: dict = {}
    if len(raw) > TIME_RAW_MAX_CHARS:
        meta.update(raw_truncated=True, raw_chars=len(raw), raw_sha256=text_sha256(raw))
        raw = raw[:TIME_RAW_MAX_CHARS]
    if has_lone_surrogate(raw):
        raw = storable_text(raw)
        meta['lone_surrogates_replaced'] = True
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not numeric and isinstance(value, str):
        try:
            float(value)
            numeric = bool(re.fullmatch(r'\s*-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\s*', value))
        except ValueError:
            numeric = False
    if 'epoch_seconds' in formats and numeric:
        try:
            if zero_is_placeholder and float(raw) == 0:
                return TimeCandidate(kind, raw, source, metadata={**meta, 'parse_status': 'zero_placeholder_not_projected',
                                                                   'policy': 'mapping.time.zero_is_placeholder'})
            return from_epoch(kind, raw.strip(), source, metadata={**meta, 'parse_status': 'parsed', 'format': 'epoch_seconds'})
        except (ValueError, OverflowError, OSError):
            pass
    if 'iso8601' in formats and isinstance(value, str):
        try:
            return from_iso(kind, value, source, metadata={**meta, 'parse_status': 'parsed', 'format': 'iso8601'})
        except (ValueError, OverflowError):
            pass
    return TimeCandidate(kind, raw, source, metadata={**meta, 'parse_status': 'unparsed', 'expected_formats': list(formats),
                                                       'value_type': type(value).__name__})


# --- Mapping files ------------------------------------------------------------------

@dataclass(frozen=True)
class ExportMapping:
    data: dict
    sha256: str
    path: str

    @property
    def namespace(self) -> str:
        return self.data['namespace']

    @property
    def version(self) -> str:
        return str(self.data['mapping_version'])

    def section(self, name: str) -> dict:
        return self.data.get(name) or {}


def load_mapping(path: Path | str, *, namespace: str, required: tuple[str, ...]) -> ExportMapping:
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) > MAPPING_MAX_BYTES:
        raise ValueError(f'mapping file exceeds {MAPPING_MAX_BYTES} bytes: {path}')
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f'mapping file is not valid JSON: {path}: {exc}') from exc
    if not isinstance(data, dict) or data.get('schema') != MAPPING_SCHEMA or data.get('schema_version') != 1:
        raise ValueError(f'mapping file must declare schema={MAPPING_SCHEMA!r} and schema_version=1: {path}')
    if data.get('namespace') != namespace:
        raise ValueError(f'mapping namespace {data.get("namespace")!r} does not match importer {namespace!r}')
    missing = [key for key in ('mapping_version', 'chatbot', 'files', 'time', *required) if key not in data]
    if missing:
        raise ValueError(f'mapping file lacks required keys {missing}: {path}')
    for key in ('conversations', 'shard'):
        pattern = data['files'].get(key)
        if pattern is not None:
            re.compile(pattern)
    return ExportMapping(data, hashlib.sha256(raw).hexdigest(), str(path))


# --- Inputs ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InputFile:
    path: Path
    locator: str
    name: str
    role: str  # conversations | conversations_shard | other
    shard_index: int | None = None


def classify(name: str, mapping: ExportMapping) -> tuple[str, int | None]:
    files = mapping.data['files']
    if files.get('conversations') and re.fullmatch(files['conversations'], name):
        return 'conversations', None
    if files.get('shard'):
        match = re.fullmatch(files['shard'], name)
        if match:
            index = match.groupdict().get('index')
            return 'conversations_shard', int(index) if index is not None else None
    return 'other', None


def discover_inputs(paths: list[Path], mapping: ExportMapping, limits: ChatImportLimits, issues: IssueLog,
                    *, locator_for: Callable[[Path], str] | None = None) -> tuple[list[InputFile], list[InputFile]]:
    """Conversation files come from the given files or a given directory's top level.

    Every other file under a given directory (bounded walk, no symlinks) is an
    attachment candidate. Nothing outside the given paths is consulted.
    """
    locate = locator_for or (lambda p: os.path.abspath(p))
    conversations: dict[str, InputFile] = {}
    others: dict[str, InputFile] = {}
    count = 0

    def add(path: Path, top_level: bool):
        nonlocal count
        count += 1
        if count > limits.max_input_files:
            if count == limits.max_input_files + 1:
                issues.add('input_file_limit_reached', str(path), limit=limits.max_input_files)
            return
        role, shard = classify(path.name, mapping)
        locator = locate(path)
        if role != 'other' and not top_level:
            issues.add('nested_conversations_file_ignored', locator)
            role, shard = 'other', None
        item = InputFile(path, locator, path.name, role, shard)
        (conversations if role != 'other' else others).setdefault(locator, item)

    for given in paths:
        given = Path(given)
        if given.is_symlink():
            issues.add('symlink_skipped', str(given))
            continue
        if given.is_file():
            add(given, True)
            continue
        if not given.is_dir():
            raise FileNotFoundError(f'input does not exist: {given}')
        base_depth = len(given.parts)
        for directory, subdirs, files in os.walk(given, followlinks=False):
            dpath = Path(directory)
            depth = len(dpath.parts) - base_depth
            subdirs.sort()
            kept = []
            for sub in subdirs:
                if (dpath / sub).is_symlink():
                    issues.add('symlink_skipped', str(dpath / sub))
                elif depth + 1 > limits.max_walk_depth:
                    issues.add('walk_depth_limit_reached', str(dpath / sub), limit=limits.max_walk_depth)
                else:
                    kept.append(sub)
            subdirs[:] = kept
            for name in sorted(files):
                path = dpath / name
                if path.is_symlink():
                    issues.add('symlink_skipped', str(path))
                    continue
                if path.is_file():
                    add(path, depth == 0)
    ordered = sorted(conversations.values(), key=lambda f: (f.role != 'conversations', f.shard_index or 0, f.name, f.locator))
    return ordered, sorted(others.values(), key=lambda f: (f.name, f.locator))


class AttachmentIndex:
    """Deterministic file-name match of referenced file IDs (rule from the mapping)."""

    def __init__(self, files: list[InputFile], rule: str | None):
        self.rule = rule
        self.files = sorted(files, key=lambda f: (f.name, f.locator))
        self.names = [f.name for f in self.files]
        self.digest = canonical_sha256(sorted([f.locator for f in self.files]))

    def candidates(self, file_id: str | None) -> list[InputFile]:
        if not self.rule or not file_id:
            return []
        pattern = re.compile(self.rule.replace('{file_id}', re.escape(file_id)))
        out = []
        position = bisect_left(self.names, file_id)
        while position < len(self.names) and self.names[position].startswith(file_id):
            if pattern.fullmatch(self.names[position]):
                out.append(self.files[position])
            position += 1
        return out


def tree_depths(parent_of: dict[str, str | None]) -> tuple[dict[str, int | None], set[str]]:
    """Depth per node from parent links; terminates on cycles (depth None).

    A node whose parent is null or absent from the map is a root (depth 0).
    Members of a cycle and their descendants get ``None``: unknown, not guessed.
    Linear in the number of nodes; no recursion.
    """
    depth: dict[str, int | None] = {}
    cycle_members: set[str] = set()
    for start in parent_of:
        if start in depth:
            continue
        chain: list[str] = []
        position: dict[str, int] = {}
        cursor = start
        base: int | None
        while True:
            if cursor in depth:
                base = depth[cursor]
                break
            if cursor in position:
                for member in chain[position[cursor]:]:
                    depth[member] = None
                    cycle_members.add(member)
                chain = chain[:position[cursor]]
                base = None
                break
            position[cursor] = len(chain)
            chain.append(cursor)
            parent = parent_of[cursor]
            if parent is None or parent not in parent_of:
                depth[cursor] = 0
                chain.pop()
                base = 0
                break
            cursor = parent
        for member in reversed(chain):
            base = None if base is None else base + 1
            depth[member] = base
    return depth, cycle_members


# --- Planning and writing one conversation -------------------------------------------

@dataclass
class PlannedRecord:
    record_kind: str
    container_id: str
    record_id: str
    locator: str
    content_sha256: str | None
    structure: dict


@dataclass
class PlannedRef:
    ref_kind: str
    ref: str
    file_id: str | None
    name: str | None
    locator: str
    metadata: dict = field(default_factory=dict)


@dataclass
class PlannedEvent:
    key: str
    event_type: str
    metadata: dict
    times: list[TimeCandidate] = field(default_factory=list)
    ts_start: str | None = None
    ts_end: str | None = None
    direction: str | None = None
    sender: str | None = None
    recipients: str | None = None
    contact_label: str | None = None
    subject: str | None = None
    body: str | None = None
    thread_id: str | None = None
    record: PlannedRecord | None = None
    refs: list[PlannedRef] = field(default_factory=list)


@dataclass
class ConversationPlan:
    events: list[PlannedEvent]
    structural_records: list[PlannedRecord]
    stats: Counter


@dataclass
class FileContext:
    namespace: str
    chatbot: str
    parser: str
    parser_version: str
    normalization_version: str
    mapping: ExportMapping
    artifact_id: int
    source_id: int
    file: InputFile
    limits: ChatImportLimits
    issues: IssueLog
    seen_containers: Counter = field(default_factory=Counter)

    @property
    def identity(self) -> str:
        return f'{self.parser}@{self.parser_version}/map{self.mapping.version}:{self.mapping.sha256[:12]}'

    @property
    def interpretation(self) -> str:
        return f'{self.identity}/{self.normalization_version}'

    def parser_metadata(self) -> dict:
        return {'name': self.parser, 'version': self.parser_version, 'mapping_version': self.mapping.version,
                'mapping_sha256': self.mapping.sha256, 'interpretation': self.interpretation}

    def time_source(self, path: str) -> str:
        return f'{self.namespace}:{self.file.name}#{path}'


Planner = Callable[[object, Element, FileContext], ConversationPlan | None]


def utc_of(candidates: list[TimeCandidate], kind: str) -> str | None:
    for candidate in candidates:
        if candidate.kind == kind and candidate.utc:
            return candidate.utc
    return None


def _resolve_refs(plan: ConversationPlan, index: AttachmentIndex, ctx: FileContext, run: 'ImportRun') -> dict:
    resolutions = {}
    for event in plan.events:
        for ref in event.refs:
            found = index.candidates(ref.file_id)
            if not found:
                resolutions[id(ref)] = ('unresolved', None, [])
                continue
            artifacts = []
            limited = False
            for candidate in found:
                artifact_id = run.ingest_attachment(candidate)
                limited = limited or artifact_id is None
                artifacts.append({'name': storable_text(candidate.name), 'locator': storable_text(candidate.locator),
                                  'artifact_id': artifact_id})
            if limited:
                resolutions[id(ref)] = ('not_ingested_limit', None, artifacts)
            elif len(found) == 1:
                resolutions[id(ref)] = ('resolved', artifacts[0]['artifact_id'], artifacts)
            else:
                resolutions[id(ref)] = ('ambiguous', None, artifacts)
    return resolutions


def write_plan(plan: ConversationPlan, ctx: FileContext, export_id: int, index: AttachmentIndex, run: 'ImportRun') -> dict:
    """One transaction per conversation: all of its rows, or none of them."""
    resolutions = _resolve_refs(plan, index, ctx, run)
    stats = Counter()
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        for event in plan.events:
            event_id = add_event(
                source_id=ctx.source_id, artifact_id=ctx.artifact_id, event_type=event.event_type,
                ts_start=event.ts_start, ts_end=event.ts_end, direction=event.direction,
                sender=event.sender, recipients=event.recipients, contact_label=event.contact_label,
                subject=event.subject, body=event.body, external_id=f'{ctx.identity}:{event.key}',
                thread_id=event.thread_id, confidence=None, metadata=event.metadata, db=db,
            )
            record_event_times(db, event_id, event.times)
            stats[event.event_type] += 1
            stats['time_candidates'] += len(event.times)
            if event.record is not None:
                records.add_occurrence(db, case_id=run.case_id, namespace=ctx.namespace, export_id=export_id,
                                       artifact_id=ctx.artifact_id, event_id=event_id,
                                       interpretation=ctx.interpretation,
                                       normalization_version=ctx.normalization_version, **asdict(event.record))
            for ref in event.refs:
                resolution, artifact_id, candidates = resolutions[id(ref)]
                records.add_attachment_ref(
                    db, event_id=event_id, export_id=export_id, ref_kind=ref.ref_kind, ref=ref.ref,
                    file_id=ref.file_id, name=ref.name, locator=ref.locator, resolution=resolution,
                    resolved_artifact_id=artifact_id, candidates=candidates, match_rule=index.rule or 'none',
                    available_files_sha256=index.digest, metadata=ref.metadata)
                stats[f'attachment_refs_{resolution}'] += 1
        for record in plan.structural_records:
            records.add_occurrence(db, case_id=run.case_id, namespace=ctx.namespace, export_id=export_id,
                                   artifact_id=ctx.artifact_id, event_id=None,
                                   interpretation=ctx.interpretation,
                                   normalization_version=ctx.normalization_version, **asdict(record))
    return stats


# --- Raw recovery ---------------------------------------------------------------------

def read_raw(stored_path: Path | str, byte_range: list[int], pointer: str):
    """Recover the raw JSON value an event was parsed from (exact stored bytes).

    ``pointer`` is relative to the element; a ``#dupN`` suffix selects the N-th
    later duplicate of the final key. Returns MISSING if it cannot be found.
    """
    start, end = byte_range
    with open(stored_path, 'rb') as handle:
        handle.seek(start)
        data = handle.read(end - start)
    try:
        value = loads(data)
    except UnicodeDecodeError:
        value = loads(data.decode('utf-8', errors='replace'))
    duplicate = 0
    if '#dup' in pointer:
        pointer, _, number = pointer.rpartition('#dup')
        duplicate = int(number)
    tokens = _tokens(pointer)
    current = value
    for position, token in enumerate(tokens):
        last = position == len(tokens) - 1
        if isinstance(current, dict):
            if last and duplicate:
                matches = [v for k, v in (current.duplicates or []) if k == token]
                if len(matches) < duplicate:
                    return MISSING
                return matches[duplicate - 1]
            if token not in current:
                return MISSING
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return MISSING
    return current


# --- Orchestration ---------------------------------------------------------------------

class ImportRun:
    def __init__(self, *, namespace: str, collection_id: int | None, limits: ChatImportLimits, issues: IssueLog,
                 parser: str, parser_version: str):
        self.namespace = namespace
        self.collection_id = collection_id
        self.limits = limits
        self.issues = issues
        self.parser = parser
        self.parser_version = parser_version
        self.case_id: int | None = None
        self.export_id: int | None = None
        self._attachments: dict[str, int | None] = {}

    def ingest_attachment(self, item: InputFile) -> int | None:
        if item.locator in self._attachments:
            return self._attachments[item.locator]
        artifact_id = None
        size = item.path.stat().st_size
        if size > self.limits.max_attachment_bytes:
            self.issues.add('attachment_not_ingested_limit', item.locator, bytes=size, limit=self.limits.max_attachment_bytes)
        else:
            source_id = ensure_source(f'{self.namespace}_export', f'{self.namespace} export', item.locator)
            artifact_id = ingest_file(item.path, source_id=source_id, source_locator=item.locator, original_name=item.name,
                                      metadata={'importer': self.parser, 'parser_version': self.parser_version,
                                                'role': 'chat_export_attachment_candidate',
                                                'media_type_status': 'extension_guess_not_verified'},
                                      collection_id=self.collection_id)
            with session() as db:
                db.execute('BEGIN IMMEDIATE')
                records.ensure_export_file(db, export_id=self.export_id, artifact_id=artifact_id, file_role='attachment',
                                           shard_index=None, file_name=item.name)
        self._attachments[item.locator] = artifact_id
        return artifact_id


def _coverage(codes: set[str]) -> str:
    if codes & FAILED_CODES:
        return 'failed'
    if codes & PARTIAL_CODES:
        return 'partial'
    return 'complete'


def run_import(paths: list[Path], *, mapping: ExportMapping, planner: Planner, parser: str, parser_version: str,
               normalization_version: str, export_label: str | None = None, collection_id: int | None = None,
               limits: ChatImportLimits | None = None, locator_for: Callable[[Path], str] | None = None) -> dict:
    limits = limits or ChatImportLimits()
    namespace = mapping.namespace
    run_issues = IssueLog(limits.max_issues_listed)
    conversation_files, other_files = discover_inputs(paths, mapping, limits, run_issues, locator_for=locator_for)
    if not conversation_files:
        raise ValueError(f'no {namespace} conversation files among the inputs (patterns: {mapping.data["files"]})')
    if export_label:
        # Fail before any acquisition is recorded when the label is bound elsewhere.
        records.check_export_binding(namespace=namespace, label=export_label, collection_id=collection_id)
    run = ImportRun(namespace=namespace, collection_id=collection_id, limits=limits, issues=run_issues,
                    parser=parser, parser_version=parser_version)
    ingested: list[tuple[InputFile, int, int, str]] = []
    skipped: list[dict] = []
    for item in conversation_files:
        size = item.path.stat().st_size
        if size > limits.max_file_bytes:
            run_issues.add('file_too_large', item.locator, bytes=size, limit=limits.max_file_bytes)
            skipped.append({'file_name': item.name, 'locator': item.locator, 'reason': 'file_too_large'})
            continue
        source_id = ensure_source(f'{namespace}_export', f'{namespace} export', item.locator)
        artifact_id = ingest_file(item.path, source_id=source_id, source_locator=item.locator, original_name=item.name,
                                  mime_type='application/json',
                                  metadata={'importer': parser, 'parser_version': parser_version, 'file_role': item.role,
                                            'shard_index': item.shard_index},
                                  collection_id=collection_id)
        with session() as db:
            row = db.execute('SELECT a.sha256, a.stored_path, s.case_id FROM artifacts a JOIN sources s ON s.id=? WHERE a.id=?',
                             (source_id, artifact_id)).fetchone()
        ingested.append((item, source_id, artifact_id, row['sha256']))
        if run.case_id is None:
            run.case_id = int(row['case_id'])
        elif run.case_id != int(row['case_id']):
            raise IntegrityError('export files resolve to different cases')
    if not ingested:
        raise ValueError('every conversation file exceeded max_file_bytes; nothing was imported')
    label_origin = 'user'
    if not export_label:
        digest = hashlib.sha256('\n'.join(sorted(h for *_, h in ingested)).encode()).hexdigest()
        export_label, label_origin = f'derived:{digest[:16]}', 'derived_from_file_hashes'
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        run.export_id = records.ensure_export(db, case_id=run.case_id, namespace=namespace, label=export_label,
                                              label_origin=label_origin, collection_id=collection_id)
        export_files = {}
        for item, _, artifact_id, _ in ingested:
            export_files[artifact_id] = records.ensure_export_file(
                db, export_id=run.export_id, artifact_id=artifact_id, file_role=item.role,
                shard_index=item.shard_index, file_name=item.name)
    index = AttachmentIndex(other_files, mapping.data.get('attachment_file_match'))
    file_results = []
    totals = Counter()
    for item, source_id, artifact_id, sha256 in ingested:
        issues = IssueLog(limits.max_issues_listed)
        ctx = FileContext(namespace, mapping.data['chatbot'], parser, parser_version, normalization_version, mapping,
                          artifact_id, source_id, item, limits, issues)
        stats = Counter()
        with session() as db:
            stored = db.execute('SELECT stored_path FROM artifacts WHERE id=?', (artifact_id,)).fetchone()['stored_path']
        with open(stored, 'rb') as handle:
            size = os.fstat(handle.fileno()).st_size
            if size == 0:
                issues.add('empty_file', item.name)
                elements = iter(())
                buf = None
            else:
                buf = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
                elements = iter_array_elements(buf, max_element_bytes=limits.max_conversation_bytes,
                                               max_elements=limits.max_conversations_per_file,
                                               issues=issues, file_label=item.name)
            try:
                for element in elements:
                    stats['elements'] += 1
                    if element.status != 'ok':
                        continue
                    value = decode_element(element, issues, f'{item.name}#/{element.index}')
                    if value is None:
                        continue
                    plan = planner(value, element, ctx)
                    if plan is None:
                        continue
                    try:
                        stats.update(write_plan(plan, ctx, run.export_id, index, run))
                        stats.update(plan.stats)
                        stats['conversations'] += 1
                    except (IntegrityError, ValueError) as exc:
                        issues.add('conversation_not_recorded', f'{item.name}#/{element.index}', detail=str(exc)[:300])
            finally:
                if buf is not None:
                    buf.close()
        coverage = _coverage(issues.codes())
        issue_dict = issues.as_dict()
        stats_dict = dict(sorted(stats.items()))
        with session() as db:
            db.execute('BEGIN IMMEDIATE')
            records.record_parse(db, export_file_id=export_files[artifact_id], interpretation=ctx.interpretation,
                                 parser=parser, parser_version=parser_version, mapping_version=mapping.version,
                                 mapping_sha256=mapping.sha256, normalization_version=normalization_version,
                                 limits=limits.as_dict(), coverage=coverage, stats=stats_dict, issues=issue_dict)
        totals.update(stats)
        file_results.append({'file_name': item.name, 'locator': item.locator, 'artifact_id': artifact_id, 'sha256': sha256,
                             'file_role': item.role, 'shard_index': item.shard_index, 'coverage': coverage,
                             'stats': stats_dict, **issue_dict})
    result = {
        'namespace': namespace, 'chatbot': mapping.data['chatbot'],
        'export': {'id': run.export_id, 'label': export_label, 'label_origin': label_origin, 'collection_id': collection_id},
        'interpretation': f'{parser}@{parser_version}/map{mapping.version}:{mapping.sha256[:12]}/{normalization_version}',
        'parser': {'name': parser, 'version': parser_version, 'mapping_version': mapping.version,
                   'mapping_sha256': mapping.sha256, 'normalization_version': normalization_version},
        'files': file_results, 'skipped_files': skipped,
        'attachment_candidates': len(other_files),
        'totals': dict(sorted(totals.items())),
        'coverage': 'complete' if all(f['coverage'] == 'complete' for f in file_results) and not skipped else 'partial',
        'run': run_issues.as_dict(),
        'time_zone_policy': 'source offsets/epochs only; local machine zone never consulted',
    }
    with session() as db:
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('chat_export_imported','record_export',?,?)",
                   (run.export_id, json.dumps({'namespace': namespace, 'label': export_label,
                                               'interpretation': result['interpretation'],
                                               'files': [{k: f[k] for k in ('artifact_id', 'coverage', 'stats', 'issue_counts')} for f in file_results],
                                               'limits': limits.as_dict()}, ensure_ascii=False)))
    return result

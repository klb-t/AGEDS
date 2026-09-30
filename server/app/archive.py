"""Inert SQLite envelopes for exact metadata-package roundtrips.

No live schema, jobs, source paths, URI dereferencing, migration or replay.
SQLite input is bounded, copied into memory, schema-checked and query-only.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Any

from .packages import TABLES, canonical_json, validate_metadata_package

ARCHIVE_SCHEMA = 'ageds.inert-metadata-archive/v1'
APPLICATION_ID = 0x41474541
_DDL = {
    'archive_envelope': 'CREATE TABLE archive_envelope (singleton INTEGER PRIMARY KEY CHECK(singleton=1), archive_schema TEXT NOT NULL, package_sha256 TEXT NOT NULL, envelope_json TEXT NOT NULL)',
    'archive_records': 'CREATE TABLE archive_records (table_name TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0), row_json TEXT NOT NULL, PRIMARY KEY(table_name, ordinal))',
}


@dataclass(frozen=True)
class ArchiveLimits:
    max_input_bytes: int = 32 * 1024 * 1024
    max_archive_bytes: int = 128 * 1024 * 1024
    max_rows: int = 100_000
    max_nodes: int = 2_000_000
    max_depth: int = 64

    def __post_init__(self):
        for value in vars(self).values():
            if type(value) is not int or value < 1:
                raise ValueError('Archive limits must be positive integers')


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'Duplicate JSON key: {key!r}')
        value[key] = item
    return value


def _constant(value):
    raise ValueError(f'Nonfinite JSON number: {value}')


def _check_shape(value: Any, limits: ArchiveLimits):
    stack, count = [(value, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > limits.max_nodes or depth > limits.max_depth:
            raise ValueError('JSON node/depth limit exceeded')
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                raise ValueError('JSON object keys must be strings')
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
        elif isinstance(node, float) and not math.isfinite(node):
            raise ValueError('Nonfinite JSON number')
        elif node is not None and type(node) not in (str, int, float, bool):
            raise ValueError('Non-JSON value')


def strict_json(raw: bytes | str, *, limits: ArchiveLimits | None = None) -> Any:
    limits = limits or ArchiveLimits()
    if len(raw.encode('utf-8') if isinstance(raw, str) else raw) > limits.max_input_bytes:
        raise ValueError('Metadata input exceeds size limit')
    try:
        # Force UTF-8: reject implicit UTF-16/32 detection by json.loads(bytes).
        text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        _check_shape(value, limits)
        canonical_json(value)  # Reject unpaired surrogate strings as well.
        return value
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid UTF-8 JSON or excessive nesting') from exc


def read_regular(path: Path, max_bytes: int) -> bytes:
    """Open a literal regular file, never a final symlink, pipe or URL."""
    if not hasattr(os, 'O_NOFOLLOW'):
        raise RuntimeError('Safe metadata reads require O_NOFOLLOW support')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError('Input must be a regular file within the size limit')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        after = os.fstat(descriptor)
        if len(raw) > max_bytes:
            raise ValueError('Input exceeds size limit')
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('Input changed while reading')
        return raw
    finally:
        os.close(descriptor)


def load_package(path: Path, *, limits: ArchiveLimits | None = None) -> Any:
    limits = limits or ArchiveLimits()
    return strict_json(read_regular(Path(path), limits.max_input_bytes), limits=limits)


def _verified(package: Any, limits: ArchiveLimits) -> bytes:
    _check_shape(package, limits)
    raw = canonical_json(package)
    if len(raw) > limits.max_input_bytes:
        raise ValueError('Metadata input exceeds size limit')
    if not isinstance(package, dict) or not isinstance(package.get('tables'), dict):
        raise ValueError('Package and tables must be objects')
    if set(package['tables']) != set(TABLES):
        raise ValueError('Archive requires the exact declared core table set')
    if any(not isinstance(rows, list) for rows in package['tables'].values()):
        raise ValueError('Table rows must be arrays')
    if sum(len(rows) for rows in package['tables'].values()) > limits.max_rows:
        raise ValueError('Metadata row limit exceeded')
    missing = package.get('missing_tables')
    if not isinstance(missing, list) or any(not isinstance(name, str) for name in missing) or len(set(missing)) != len(missing):
        raise ValueError('Missing tables must be unique names')
    if not isinstance(package.get('exported_at'), str) or not package['exported_at']:
        raise ValueError('Package must retain its exported_at string')
    result = validate_metadata_package(package)
    if not result['valid']:
        codes = ', '.join(item['code'] for item in result['errors'][:8])
        raise ValueError(f'Invalid metadata package: {codes}')
    return raw


def publish_new(raw: bytes, output: Path, *, max_bytes: int):
    """Publish completed bytes atomically and exclusively, never clobber."""
    output = Path(output)
    if len(raw) > max_bytes:
        raise ValueError('Output exceeds size limit')
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'Output already exists: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.ageds-archive-', suffix='.tmp', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)
    finally:
        os.unlink(temporary)


def _report(package: dict) -> dict:
    return {'archive_schema': ARCHIVE_SCHEMA, 'package_schema': package['schema'],
            'package_sha256': hashlib.sha256(canonical_json(package)).hexdigest(),
            'payload_sha256': package['integrity']['payload_sha256'],
            'row_count': sum(len(rows) for rows in package['tables'].values()),
            'inert': True, 'live_restore_supported': False, 'jobs_resumed': False,
            'source_bytes_included': False, 'replay_supported': False, 'signed': False}


def import_metadata_archive(package: Any, output: Path, *, limits: ArchiveLimits | None = None) -> dict:
    """Validate the whole package, then create a NEW inert SQLite archive."""
    limits = limits or ArchiveLimits()
    raw = _verified(package, limits)
    # Detach from mutable caller data after validation; keep every literal field.
    package = strict_json(raw, limits=limits)
    db = sqlite3.connect(':memory:')
    try:
        db.execute(f'PRAGMA application_id={APPLICATION_ID}')
        db.execute('PRAGMA user_version=1')
        for ddl in _DDL.values():
            db.execute(ddl)
        envelope = {key: value for key, value in package.items() if key != 'tables'}
        db.execute('INSERT INTO archive_envelope VALUES(1,?,?,?)',
                   (ARCHIVE_SCHEMA, hashlib.sha256(raw).hexdigest(), canonical_json(envelope).decode('utf-8')))
        for name in TABLES:
            db.executemany('INSERT INTO archive_records VALUES(?,?,?)',
                ((name, ordinal, canonical_json(row).decode('utf-8')) for ordinal, row in enumerate(package['tables'][name])))
        db.commit()
        archive_bytes = db.serialize()
    finally:
        db.close()
    if len(archive_bytes) > limits.max_archive_bytes:
        raise ValueError('Archive output exceeds size limit')
    restored = _decode_archive(archive_bytes, limits)
    if canonical_json(restored) != raw:
        raise ValueError('Archive roundtrip differs from the input package')
    publish_new(archive_bytes, Path(output), max_bytes=limits.max_archive_bytes)
    return _report(restored)


def _decode_archive(raw: bytes, limits: ArchiveLimits) -> dict:
    if len(raw) > limits.max_archive_bytes or not raw.startswith(b'SQLite format 3\0'):
        raise ValueError('Invalid or oversized SQLite archive')
    db = sqlite3.connect(':memory:')
    try:
        db.deserialize(raw)
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, limits.max_input_bytes)
        # Bound work on corrupt/untrusted databases, not merely file size.
        budget = [0]
        def progress():
            budget[0] += 1
            return int(budget[0] > 100_000)
        db.set_progress_handler(progress, 1000)
        if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or db.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('Not an AGEDS inert metadata archive')
        actual = db.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        expected_schema = [('table', name, ddl) for name, ddl in _DDL.items()]
        expected_schema.append(('index', 'sqlite_autoindex_archive_records_1', None))
        if sorted(actual) != sorted(expected_schema):
            raise ValueError('Unexpected archive schema; views, triggers and live tables are forbidden')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity check failed')
        headers = db.execute('SELECT singleton,archive_schema,package_sha256,envelope_json FROM archive_envelope').fetchall()
        if len(headers) != 1 or headers[0][0] != 1 or headers[0][1] != ARCHIVE_SCHEMA:
            raise ValueError('Invalid archive envelope')
        expected_hash, envelope_raw = headers[0][2:]
        if not isinstance(envelope_raw, str):
            raise ValueError('Archive envelope must be JSON text')
        package = strict_json(envelope_raw, limits=limits)
        if not isinstance(package, dict) or 'tables' in package:
            raise ValueError('Invalid archive envelope JSON')
        package['tables'] = {name: [] for name in TABLES}
        count, size = 0, len(envelope_raw.encode('utf-8'))
        for name, ordinal, row_raw in db.execute('SELECT table_name,ordinal,row_json FROM archive_records ORDER BY table_name,ordinal'):
            count += 1
            if count > limits.max_rows:
                raise ValueError('Metadata row limit exceeded')
            if name not in package['tables'] or type(ordinal) is not int or ordinal != len(package['tables'][name]) or not isinstance(row_raw, str):
                raise ValueError('Invalid archive row identity/order')
            size += len(row_raw.encode('utf-8'))
            if size > limits.max_input_bytes:
                raise ValueError('Metadata input exceeds size limit')
            package['tables'][name].append(strict_json(row_raw, limits=limits))
        package_raw = _verified(package, limits)
        if hashlib.sha256(package_raw).hexdigest() != expected_hash:
            raise ValueError('Archived package SHA-256 mismatch')
        return package
    finally:
        db.close()


def read_metadata_archive(path: Path, *, limits: ArchiveLimits | None = None) -> dict:
    limits = limits or ArchiveLimits()
    return _decode_archive(read_regular(Path(path), limits.max_archive_bytes), limits)


def export_metadata_archive(archive: Path, output: Path, *, limits: ArchiveLimits | None = None) -> dict:
    """Exact semantic/canonical export, preserving original export time/digests."""
    limits = limits or ArchiveLimits()
    package = read_metadata_archive(archive, limits=limits)
    publish_new(canonical_json(package), Path(output), max_bytes=limits.max_input_bytes)
    return _report(package)

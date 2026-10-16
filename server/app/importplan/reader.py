"""Read catalogued bytes (disk files and archive members) without touching sources.

The reader re-opens what a catalog scan observed: a disk file component-wise
without following symlinks (``catalog.ingest._open_disk``) or an archive member
by its container chain, member index and raw name. Containers are opened once
and kept in a small cache, so many members of one archive cost one directory
read. Nested containers are spooled to unnamed temporary files in
``spool_dir``, which must be outside every source root (checked by the caller).

Every read is bounded: prefixes by ``n``, copies by the catalogued size, spools
by the scan's ``max_nested_archive_bytes``. A member whose index or raw name no
longer matches the catalog, or whose bytes differ from the catalogued SHA-256,
raises :class:`IntegrityError`.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

from .. import db as dbmod
from ..catalog import archives
from ..catalog.ingest import _open_disk
from ..catalog.limits import CatalogLimits
from ..catalog.locator import parse_locator
from ..evidence import IntegrityError

CHUNK = 1024 * 1024


class ReadLimitError(ValueError):
    """A configured bound prevents reading the entry (reported, never silent)."""


class _Spool:
    def __init__(self, fileobj, size: int):
        self.fileobj, self.size = fileobj, size


class EntryReader:
    def __init__(self, spool_dir: Path, *, max_cached_containers: int = 4):
        self.spool_dir = Path(spool_dir)
        self.max_cached = max(1, max_cached_containers)
        self._entries: dict[int, dict] = {}
        self._scans: dict[int, dict] = {}
        self._containers: OrderedDict[int, tuple] = OrderedDict()
        self.bytes_read = 0

    # ------------------------------------------------------------------ rows
    def entry(self, entry_id: int) -> dict:
        if entry_id not in self._entries:
            with closing(dbmod.connect()) as db:
                row = db.execute('SELECT * FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
            if row is None:
                raise LookupError(f'catalog entry {entry_id} does not exist')
            self._entries[entry_id] = dict(row)
        return self._entries[entry_id]

    def prime(self, rows: list[dict]) -> None:
        for row in rows:
            self._entries.setdefault(int(row['id']), dict(row))

    def scan(self, scan_id: int) -> dict:
        if scan_id not in self._scans:
            with closing(dbmod.connect()) as db:
                row = dict(db.execute('SELECT * FROM catalog_scans WHERE id=?', (scan_id,)).fetchone())
            row['limits'] = CatalogLimits(**json.loads(row['limits_json']))
            self._scans[scan_id] = row
        return self._scans[scan_id]

    # ------------------------------------------------------------ containers
    def _container(self, entry: dict):
        key = int(entry['id'])
        if key in self._containers:
            self._containers.move_to_end(key)
            return self._containers[key]
        scan = self.scan(entry['scan_id'])
        limits = scan['limits']
        if entry['parent_entry_id'] is None:
            parsed = parse_locator(entry['locator'])
            handle = _open_disk(Path(scan['root_path']), scan['root_kind'], parsed['disk'])
            size = os.fstat(handle.fileno()).st_size
        else:
            handle = tempfile.TemporaryFile(dir=self.spool_dir)
            try:
                with self._open_member(entry) as stream:
                    size = self._copy(stream, handle, limits.max_nested_archive_bytes, 'max_nested_archive_bytes')
                handle.seek(0)
            except BaseException:
                handle.close()
                raise
        try:
            container = archives.open_container(handle, size, entry['sniffed_type'], entry['name'], limits)
            members = {m.index: m for m in container.members()}
        except archives.ContainerError as exc:
            handle.close()
            raise IntegrityError(f'container {entry["locator"]!r} cannot be opened: {exc.code}') from exc
        except archives.DAMAGE_ERRORS as exc:
            handle.close()
            raise IntegrityError(f'container {entry["locator"]!r} is unreadable: {type(exc).__name__}') from exc
        self._containers[key] = (container, members, handle)
        while len(self._containers) > self.max_cached:
            _, (old, _, old_handle) = self._containers.popitem(last=False)
            self._close(old, old_handle)
        return self._containers[key]

    @staticmethod
    def _close(container, handle):
        try:
            container.close()
        except archives.DAMAGE_ERRORS:
            pass
        handle.close()

    def close(self):
        while self._containers:
            _, (container, _, handle) = self._containers.popitem(last=False)
            self._close(container, handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- streams
    def _open_member(self, entry: dict):
        parent = self.entry(entry['parent_entry_id'])
        container, members, _ = self._container(parent)
        member = members.get(entry['member_index'])
        expected = parse_locator(entry['locator'])['members'][-1]['name']
        if member is None or member.raw_name != expected:
            raise IntegrityError(f'archive member {entry["locator"]!r} no longer matches the catalog entry')
        try:
            return _Closing(container.open(member))
        except archives.MemberError as exc:
            raise IntegrityError(f'archive member {entry["locator"]!r} cannot be read: {exc.code}') from exc

    def open(self, entry_id: int):
        """A readable stream of the entry's bytes (context manager)."""
        entry = self.entry(entry_id)
        if entry['parent_entry_id'] is None:
            scan = self.scan(entry['scan_id'])
            return _Closing(_open_disk(Path(scan['root_path']), scan['root_kind'], parse_locator(entry['locator'])['disk']))
        return self._open_member(entry)

    def read_prefix(self, entry_id: int, n: int) -> bytes:
        try:
            with self.open(entry_id) as stream:
                data = stream.read(n)
        except (IntegrityError, ReadLimitError):
            raise
        except archives.DAMAGE_ERRORS as exc:
            raise IntegrityError(f'entry {entry_id} unreadable: {type(exc).__name__}') from exc
        self.bytes_read += len(data)
        return data

    def _copy(self, stream, target, limit: int, limit_name: str) -> int:
        total = 0
        while chunk := stream.read(CHUNK):
            total += len(chunk)
            if total > limit:
                raise ReadLimitError(f'{limit_name} ({limit} bytes) exceeded')
            target.write(chunk)
        self.bytes_read += total
        return total

    def copy_to(self, entry_id: int, target: Path) -> tuple[str, int]:
        """Copy the entry to a new file and verify it against the catalogued SHA-256."""
        entry = self.entry(entry_id)
        if not entry['sha256']:
            raise IntegrityError(f'entry {entry["locator"]!r} has no catalogued hash; it cannot be verified')
        digest, total = hashlib.sha256(), 0
        expected_size = entry['size_bytes']
        try:
            with self.open(entry_id) as stream, open(target, 'xb') as out:
                while chunk := stream.read(CHUNK):
                    total += len(chunk)
                    if expected_size is not None and total > expected_size:
                        raise IntegrityError(f'{entry["locator"]!r} grew beyond its catalogued size; content changed since the catalog scan')
                    digest.update(chunk)
                    out.write(chunk)
        except (IntegrityError, ReadLimitError, FileExistsError, PermissionError):
            raise
        except archives.DAMAGE_ERRORS as exc:
            raise IntegrityError(f'{entry["locator"]!r} unreadable: {type(exc).__name__}: {exc}') from exc
        self.bytes_read += total
        if digest.hexdigest() != entry['sha256']:
            raise IntegrityError(f'{entry["locator"]!r}: content changed since the catalog scan (SHA-256 mismatch); nothing was imported')
        return entry['sha256'], total


class _Closing:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self.stream

    def __exit__(self, *exc):
        try:
            self.stream.close()
        except archives.DAMAGE_ERRORS:
            pass

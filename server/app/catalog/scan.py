"""Catalog scan: a read-only, bounded walk that records scan observations.

The filesystem traversal is the production scanner's ``_Scan.walk`` (lexical
order, no-follow descriptors, symlinks and special files skipped, entry/depth/
file limits). This module replaces only the per-file step: one pass that
hashes, sniffs the leading bytes and, for containers, streams archive members
recursively. Nothing is written to the source tree; nested archives are
spooled to memory or to an unnamed temporary file outside the source root.

A catalog scan is *not* an acquisition: no bytes are copied into the evidence
store and no ``source_observations`` row is written. See ``ingest.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import db as dbmod
from .. import scanner
from ..timeinfo import TimeCandidate, from_epoch
from . import archives, signatures
from .limits import LIMIT_NAMES, CatalogLimits  # noqa: F401 (re-exported for callers)
from .locator import disk_locator, member_locator

CATALOG_VERSION = '1.0.0'
CATALOG_SCHEMA = 'ageds.catalog/v1'

WALK_LIMIT_CODES = {'file_limit', 'total_entry_limit', 'directory_entry_limit', 'depth_limit'}
WALK_ERROR_CODES = {'directory_unreadable', 'entry_unreadable'}
SKIP_CODES = {'symlink_skipped', 'special_file_skipped'}


POLICY = {
    'read_only': True,
    'observation_kind': 'catalog_scan_observation_not_acquisition',
    'source_copies': 'none; nested archives spooled to memory or unnamed temp files outside the source root',
    'filesystem_walk': 'server.app.scanner walk (lexical order, no-follow descriptors)',
    'symlinks': 'never followed; recorded as skipped entries with their link text',
    'special_files': 'skipped',
    'type_basis': 'byte signature over extension; both kept; disagreement is a conflict, not a correction',
    'member_names': 'raw bytes preserved (hex when not plain UTF-8); never used as filesystem paths',
    'hash_scope': 'complete content bytes or none; never a partial digest',
    'filesystem_time': 'file_mtime from filesystem_stat (epoch, UTC); not an event time',
    'zip_dos_time': 'local wall clock of the archive writer, zone unknown, 2-second granularity',
    'tar_time': 'epoch seconds (UTC)',
    'document_containers': 'docx/xlsx/pptx/odt/ods/odp/epub/apk recognized by members; not expanded by default',
}


class _LimitHit(Exception):
    def __init__(self, code: str, **details):
        super().__init__(code)
        self.code, self.details = code, details


@dataclass
class _Node:
    origin: str
    depth: int
    locator: str
    raw_name: bytes
    name: str
    name_encoding: str
    flags: list[str]
    size: int | None
    compressed_size: int | None = None
    member_index: int | None = None
    parent_id: int | None = None
    times: list[TimeCandidate] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    read_bytes: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class _Writer:
    def __init__(self, db, scan_id: int, collection_id: int, case_id: int):
        self.db, self.scan_id, self.collection_id, self.case_id = db, scan_id, collection_id, case_id
        self.groups: dict[str, int] = {}
        self.commits = 0
        self.entries = 0

    def group(self, sha256: str, size: int | None) -> int:
        if sha256 in self.groups:
            return self.groups[sha256]
        self.db.execute('INSERT OR IGNORE INTO catalog_content_groups(case_id,sha256,size_bytes,first_scan_id) VALUES(?,?,?,?)',
                        (self.case_id, sha256, size, self.scan_id))
        group_id = int(self.db.execute('SELECT id FROM catalog_content_groups WHERE case_id=? AND sha256=?',
                                       (self.case_id, sha256)).fetchone()[0])
        self.groups[sha256] = group_id
        return group_id

    def entry(self, node: _Node, **fields) -> int:
        sha256 = fields.get('sha256')
        group_id = self.group(sha256, node.size) if sha256 else None
        row = {
            'scan_id': self.scan_id, 'collection_id': self.collection_id, 'parent_entry_id': node.parent_id,
            'locator': node.locator, 'depth': node.depth, 'origin': node.origin, 'member_index': node.member_index,
            'name': node.name, 'name_raw_hex': None if node.name_encoding in ('ascii', 'utf-8') else node.raw_name.hex(),
            'name_encoding': node.name_encoding, 'name_flags_json': _json(node.flags),
            'size_bytes': node.size, 'compressed_size': node.compressed_size, 'content_group_id': group_id,
            'issues_json': _json(node.issues), 'details_json': _json(node.details),
        }
        row.update({k: v for k, v in fields.items() if k not in ('issues', 'details')})
        row['issue_code'] = node.issues[0]['code'] if node.issues else None
        columns = ','.join(row)
        cursor = self.db.execute(f'INSERT INTO catalog_entries({columns}) VALUES({",".join("?" * len(row))})', tuple(row.values()))
        entry_id = int(cursor.lastrowid)
        for candidate in node.times:
            self.db.execute('''INSERT INTO catalog_entry_times(entry_id,kind,source,raw,utc,local,tz_status,tz,precision,metadata_json)
                               VALUES(?,?,?,?,?,?,?,?,?,?)''',
                            (entry_id, candidate.kind, candidate.source, candidate.raw, candidate.utc, candidate.local,
                             candidate.tz_status, candidate.tz, candidate.precision, _json(candidate.metadata)))
        self.entries += 1
        self._tick()
        return entry_id

    def update_entry(self, entry_id: int, node: _Node, **fields):
        fields = dict(fields)
        fields['issues_json'] = _json(node.issues)
        fields['details_json'] = _json(node.details)
        fields['issue_code'] = node.issues[0]['code'] if node.issues else None
        if 'sha256' in fields and fields['sha256']:
            fields['content_group_id'] = self.group(fields['sha256'], node.size)
        assignments = ','.join(f'{k}=?' for k in fields)
        self.db.execute(f'UPDATE catalog_entries SET {assignments} WHERE id=?', (*fields.values(), entry_id))
        self._tick()

    def issue(self, code: str, locator: str | None = None, entry_id: int | None = None, **details):
        self.db.execute('INSERT INTO catalog_issues(scan_id,entry_id,code,locator,details_json) VALUES(?,?,?,?,?)',
                        (self.scan_id, entry_id, code, locator, _json(details)))
        self._tick()

    def _tick(self):
        # Every write is its own short transaction. The engine reads and hashes
        # file content between writes; holding the write lock across that would
        # block workers, publications and token checks (review finding 4).
        self.commits += 1
        self.db.commit()


class _Walk(scanner._Scan):
    """Reuse the production scanner traversal; replace only the per-file step."""

    def __init__(self, root: Path, limits: scanner.ScanLimits, engine: '_Engine'):
        super().__init__(root, limits)
        self.engine = engine

    def file(self, directory_fd: int, name: str, rel: str, st):
        # The scanner checks max_files against this list's length.
        self.manifest['files'].append(None)
        self.manifest['coverage']['files_seen'] += 1
        self.engine.disk_file(directory_fd, name, rel, st)

    def issue(self, code: str, path: str | None = None, **details):
        super().issue(code, path, **details)
        self.engine.walk_issue(code, path, details)


class _Engine:
    def __init__(self, writer: _Writer, limits: CatalogLimits, root: Path, temp_root: Path):
        self.writer, self.limits, self.root, self.temp_root = writer, limits, root, temp_root
        self.hash_bytes = 0
        self.expanded_bytes = 0
        self.members_total = 0
        self.limit_hits: dict[str, int] = {}
        self.walk_issues: dict[str, int] = {}
        self.incomplete: dict[str, int] = {}

    # ------------------------------------------------------------ bookkeeping
    def _count(self, bucket: dict, code: str):
        bucket[code] = bucket.get(code, 0) + 1

    def _limit(self, node: _Node, code: str, **details):
        node.issues.append({'code': code, 'kind': 'limit', **details})
        self._count(self.limit_hits, code)

    def walk_issue(self, code: str, path: str | None, details: dict):
        self._count(self.walk_issues, code)
        raw_parts = [os.fsencode(p) for p in path.split('/')] if path else []
        locator = disk_locator(raw_parts) if raw_parts else None
        if code in SKIP_CODES and raw_parts:
            node = self._disk_node(raw_parts, locator, size=None)
            node.issues.append({'code': code})
            if code == 'symlink_skipped':
                try:
                    target = os.readlink(self.root.joinpath(*[os.fsdecode(p) for p in raw_parts]))
                    node.details['link_target'] = os.fsencode(target).decode('utf-8', 'replace')
                    node.details['link_target_raw_hex'] = os.fsencode(target).hex()
                except OSError as exc:
                    node.details['link_target_error'] = str(exc)
            self._insert_leaf(node, status='skipped', hash_status='not_read', sniff=None)
            return
        self.writer.issue(code, locator, **{k: v for k, v in details.items() if isinstance(v, (str, int, float, bool))})
        if code in WALK_LIMIT_CODES:
            self._count(self.limit_hits, code)
        else:
            self._count(self.incomplete, code)

    # ------------------------------------------------------------ nodes
    def _disk_node(self, raw_parts: list[bytes], locator: str, size: int | None) -> _Node:
        raw = raw_parts[-1]
        name, encoding, _ = archives.display_name(raw, utf8_flag=None)
        flags = [f for f in archives.name_flags(raw) if f not in ('absolute', 'parent_traversal')]
        return _Node('filesystem', 0, locator, raw, name, encoding, flags, size)

    def disk_file(self, directory_fd: int, name: str, rel: str, st):
        raw_parts = [os.fsencode(p) for p in rel.split('/')]
        node = self._disk_node(raw_parts, disk_locator(raw_parts), st.st_size)
        node.times.append(from_epoch('file_mtime', f'{st.st_mtime_ns // 10**9}.{st.st_mtime_ns % 10**9:09d}',
                                     'filesystem_stat', metadata={'semantics': 'filesystem_modification_not_event_time'}))
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except OSError as exc:
            node.issues.append({'code': 'file_unreadable', 'error': str(exc)})
            self._count(self.incomplete, 'file_unreadable')
            self._insert_leaf(node, status='unreadable', hash_status='error', sniff=None)
            return
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            identity = lambda s: (s.st_ino, s.st_dev, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if not stat.S_ISREG(before.st_mode) or identity(before) != identity(st):
                node.issues.append({'code': 'source_changed_before_read'})
                self._count(self.incomplete, 'source_changed_before_read')
                self._insert_leaf(node, status='unstable', hash_status='unstable', sniff=None)
                return
            entry_id = self._process(handle, node, disk_handle=handle)
            after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                node.issues.insert(0, {'code': 'source_changed_during_read'})
                self._count(self.incomplete, 'source_changed_during_read')
                self.writer.update_entry(entry_id, node, status='unstable', hash_status='unstable', sha256=None,
                                         content_group_id=None)

    # ------------------------------------------------------------ reading
    def _read(self, stream, size: int, node: _Node, account: bool) -> bytes:
        data = stream.read(size)
        if account and data:
            self.expanded_bytes += len(data)
            node.read_bytes += len(data)
            read = node.read_bytes
            if node.compressed_size is not None and read >= self.limits.ratio_check_min_bytes and \
                    read > self.limits.max_compression_ratio * max(node.compressed_size, 1):
                raise _LimitHit('compression_ratio', bytes_read=read, compressed_size=node.compressed_size)
            if self.expanded_bytes > self.limits.max_expanded_bytes:
                raise _LimitHit('max_expanded_bytes', bytes_read=read)
        return data

    def _process(self, stream, node: _Node, *, disk_handle=None, account: bool = False) -> int:
        """Hash + sniff one node, expand it if it is a container; returns the entry id.

        Depth: a disk file has depth 0, a member of a disk archive depth 1, and
        so on. A container at depth d is expanded only when d + 1 <= max_archive_depth.
        """
        is_disk = disk_handle is not None
        limits = self.limits
        status, hash_status, sha256 = 'ok', 'complete', None
        spool = None
        try:
            try:
                prefix = self._read(stream, limits.sniff_bytes, node, account)
            except _LimitHit as hit:
                self._limit(node, hit.code, **hit.details)
                return self._insert_leaf(node, status='limit_exceeded', hash_status='aborted_limit', sniff=None)
            except archives.DAMAGE_ERRORS + (NotImplementedError, RuntimeError) as exc:
                return self._member_failure(node, exc)
            complete = len(prefix) < limits.sniff_bytes or (node.size is not None and len(prefix) >= node.size)
            sniffed = signatures.sniff(prefix, complete=complete)
            is_archive = sniffed.type in archives.CONTAINER_TYPES
            depth_allows = node.depth + 1 <= limits.max_archive_depth
            wants_random_access = sniffed.refine == 'cfb_directory' or (is_archive and depth_allows)
            hasher = hashlib.sha256(prefix)
            total = len(prefix)
            if is_disk:
                remaining_budget = max(limits.max_total_hash_bytes - self.hash_bytes, 0)
                hash_limit = min(limits.max_hash_bytes, remaining_budget)
                limit_code = 'max_hash_bytes' if limits.max_hash_bytes <= remaining_budget else 'max_total_hash_bytes'
            else:
                hash_limit, limit_code = limits.max_member_hash_bytes, 'max_member_hash_bytes'
            declared_ratio_hit = (not is_disk and node.compressed_size is not None and node.size is not None
                                  and node.size >= limits.ratio_check_min_bytes
                                  and node.size > limits.max_compression_ratio * max(node.compressed_size, 1))
            if declared_ratio_hit:
                self._limit(node, 'compression_ratio_declared', size=node.size, compressed_size=node.compressed_size)
                status, hash_status = 'limit_exceeded', 'skipped_limit'
            elif node.size is not None and node.size > hash_limit:
                self._limit(node, limit_code, size=node.size, limit=hash_limit)
                status, hash_status = 'limit_exceeded', 'skipped_size_limit'
            else:
                if not is_disk and wants_random_access and (node.size or 0) <= limits.max_nested_archive_bytes:
                    spool = tempfile.SpooledTemporaryFile(max_size=limits.spool_memory_bytes, dir=self.temp_root)
                    spool.write(prefix)
                try:
                    while True:
                        chunk = self._read(stream, 1024 * 1024, node, account)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > hash_limit:
                            raise _LimitHit(limit_code + '_during_read', limit=hash_limit)
                        hasher.update(chunk)
                        if spool is not None:
                            if total > limits.max_nested_archive_bytes:
                                spool.close()
                                spool = None
                            else:
                                spool.write(chunk)
                    sha256 = hasher.hexdigest()
                except _LimitHit as hit:
                    self._limit(node, hit.code, **hit.details)
                    status, hash_status = 'limit_exceeded', 'aborted_limit'
                    if spool is not None:
                        spool.close()
                        spool = None
                except archives.DAMAGE_ERRORS + (NotImplementedError, RuntimeError) as exc:
                    if spool is not None:
                        spool.close()
                        spool = None
                    node.issues.append({'code': 'member_read_failed', 'error': f'{type(exc).__name__}: {exc}'})
                    self._count(self.incomplete, 'member_read_failed')
                    return self._insert_leaf(node, status='damaged', hash_status='error_damaged', sniff=sniffed,
                                             size_read=total)
            if is_disk:
                self.hash_bytes += total
            if sha256 is not None:
                if node.size is not None and node.size != total:
                    node.issues.append({'code': 'declared_size_mismatch', 'declared': node.size, 'read': total})
                    node.details['declared_size'] = node.size
                node.size = total
            if status != 'ok':
                self._count(self.incomplete, 'hash_not_complete')
            container_file = disk_handle if is_disk else spool
            if sniffed.refine == 'cfb_directory':
                refined = None
                if container_file is not None:
                    try:
                        refined, cfb_details = signatures.refine_cfb(lambda o, n: _read_at(container_file, o, n),
                                                                     _size_of(container_file))
                        node.details['cfb'] = cfb_details
                    except archives.DAMAGE_ERRORS as exc:
                        node.issues.append({'code': 'cfb_directory_unreadable', 'error': str(exc)})
                else:
                    node.details['cfb'] = {'refinement': 'not_performed_without_random_access'}
                return self._insert_leaf(node, status=status, hash_status=hash_status, sniff=sniffed, sha256=sha256,
                                         refined=refined)
            if is_archive:
                if not depth_allows:
                    self._limit(node, 'max_archive_depth', depth=node.depth)
                    self._count(self.incomplete, 'container_not_expanded')
                    return self._insert_leaf(node, status='limit_exceeded', hash_status=hash_status, sniff=sniffed,
                                             sha256=sha256, is_container=1)
                if container_file is None:
                    code = 'max_nested_archive_bytes' if (node.size or 0) > limits.max_nested_archive_bytes else 'container_not_expanded'
                    if code == 'max_nested_archive_bytes':
                        self._limit(node, code, size=node.size)
                    else:
                        node.issues.append({'code': code, 'reason': hash_status})
                    self._count(self.incomplete, 'container_not_expanded')
                    return self._insert_leaf(node, status='limit_exceeded', hash_status=hash_status, sniff=sniffed,
                                             sha256=sha256, is_container=1)
                return self._container(node, container_file, sniffed, status, hash_status, sha256)
            return self._insert_leaf(node, status=status, hash_status=hash_status, sniff=sniffed, sha256=sha256)
        finally:
            if spool is not None:
                spool.close()

    def _member_failure(self, node: _Node, exc: BaseException) -> int:
        if isinstance(exc, RuntimeError) and 'encrypt' in str(exc).lower():
            node.issues.append({'code': 'encrypted_member'})
            self._count(self.incomplete, 'encrypted_content')
            return self._insert_leaf(node, status='encrypted', hash_status='unavailable_encrypted', sniff=None)
        if isinstance(exc, NotImplementedError):
            node.issues.append({'code': 'compression_method_unsupported', 'error': str(exc)})
            self._count(self.incomplete, 'member_unsupported')
            return self._insert_leaf(node, status='unsupported', hash_status='unavailable_unsupported', sniff=None)
        node.issues.append({'code': 'member_read_failed', 'error': f'{type(exc).__name__}: {exc}'})
        self._count(self.incomplete, 'member_read_failed')
        return self._insert_leaf(node, status='damaged', hash_status='error_damaged', sniff=None)

    # ------------------------------------------------------------ entries
    def _type_fields(self, node: _Node, sniffed: signatures.Sniff | None, refined: str | None = None) -> dict:
        extension, extension_type = signatures.extension_of(node.name)
        if sniffed is None:
            sniffed = signatures.Sniff(None, 'none')
        if refined and refined != sniffed.type:
            sniffed = signatures.Sniff(refined, 'container_members' if sniffed.type == 'zip' else sniffed.basis,
                                       sniffed.rule, details={**sniffed.details, 'refined_from': sniffed.type})
        relation, conflict = signatures.relation(extension, extension_type, sniffed)
        info = signatures.type_info(sniffed.type or (extension_type if extension_type != 'opaque' else None))
        if sniffed.details:
            node.details['sniff'] = sniffed.details
        return {'extension': extension, 'extension_type': extension_type, 'sniffed_type': sniffed.type,
                'sniffed_mime': signatures.type_info(sniffed.type).get('mime'), 'sniff_basis': sniffed.basis,
                'sniff_rule': sniffed.rule, 'type_relation': relation, 'type_conflict': 1 if conflict else 0,
                'family': info.get('family')}

    def _insert_leaf(self, node: _Node, *, status: str, hash_status: str, sniff, sha256: str | None = None,
                     is_container: int = 0, refined: str | None = None, size_read: int | None = None) -> int:
        if size_read is not None:
            node.details['bytes_read_before_failure'] = size_read
        fields = self._type_fields(node, sniff, refined)
        return self.writer.entry(node, status=status, hash_status=hash_status, sha256=sha256,
                                 is_container=is_container, **fields)

    # ------------------------------------------------------------ containers
    def _container(self, node: _Node, fileobj, sniffed, status: str, hash_status: str, sha256: str | None) -> int:
        limits = self.limits
        size = _size_of(fileobj)
        try:
            container = archives.open_container(fileobj, size, sniffed.type, node.name, limits)
        except archives.ContainerError as error:
            node.issues.append({'code': error.code, **error.details})
            if error.status == 'limit_exceeded':
                self._count(self.limit_hits, error.code)
            self._count(self.incomplete, 'container_not_expanded')
            return self._insert_leaf(node, status=error.status, hash_status=hash_status, sniff=sniffed, sha256=sha256,
                                     is_container=1)
        try:
            container_status = status
            error = getattr(container, 'error', None)
            if error is not None:
                node.issues.append({'code': error.code, **{k: v for k, v in error.details.items() if k != 'declared_members'}})
                container_status = 'damaged'
                self._count(self.incomplete, 'container_damaged')
            refined = container.refined_type
            if sniffed.refine == 'zip_members' and isinstance(container, archives.ZipContainer):
                refined, zip_details = signatures.refine_zip(container.names(), container.read_small)
                node.details.update(zip_details)
            node.details['container'] = dict(container.details)
            expand = refined not in archives.DOCUMENT_CONTAINERS or limits.expand_document_containers
            entry_id = self._insert_leaf(node, status=container_status, hash_status=hash_status, sniff=sniffed,
                                         sha256=sha256, is_container=1 if expand else 0, refined=refined)
            if expand:
                final_status = self._expand(container, node, entry_id, container_status)
                self.writer.update_entry(entry_id, node, status=final_status)
            return entry_id
        finally:
            container.close()

    def _expand(self, container, parent: _Node, parent_id: int, status: str) -> str:
        limits = self.limits
        seen: dict[bytes, int] = {}
        counts = {'members_listed': 0, 'directories': 0}
        try:
            for member in container.members():
                if counts['members_listed'] >= limits.max_members_per_archive:
                    self._limit(parent, 'max_members_per_archive', listed=counts['members_listed'],
                                declared=container.declared_members)
                    status = 'limit_exceeded'
                    break
                if self.members_total >= limits.max_total_members:
                    self._limit(parent, 'max_total_members', listed=counts['members_listed'])
                    status = 'limit_exceeded'
                    break
                if member.kind == 'dir':
                    counts['directories'] += 1
                    continue
                occurrence = seen.get(member.raw_name, 0) + 1
                seen[member.raw_name] = occurrence
                flags = list(member.flags)
                if occurrence > 1:
                    flags.append('duplicate_name_in_archive')
                counts['members_listed'] += 1
                self.members_total += 1
                node = _Node(container.kind, parent.depth + 1, member_locator(parent.locator, member.raw_name, occurrence),
                             member.raw_name, member.name, member.name_encoding, flags, member.size,
                             member.compressed_size, member.index, parent_id, list(member.times), dict(member.details))
                if occurrence > 1:
                    node.details['occurrence'] = occurrence
                if self._member(container, member, node) == 'stop':
                    status = 'limit_exceeded'
                    break
        except archives.ContainerError as error:
            parent.issues.append({'code': error.code, **error.details})
            self._count(self.incomplete, 'container_damaged')
            status = 'damaged'
        finish = getattr(container, 'finish', None)
        if finish is not None and status not in ('limit_exceeded',):
            ending = finish()
            parent.details.setdefault('container', {}).update(ending)
            if ending.get('end_of_archive') != 'zero_blocks':
                parent.issues.append({'code': 'tar_end_marker_missing', **ending})
                self._count(self.incomplete, 'container_damaged')
                status = 'damaged'
        parent.details.setdefault('container', {}).update(counts)
        if status == 'limit_exceeded':
            self._count(self.incomplete, 'container_partially_listed')
        return status

    def _member(self, container, member: archives.Member, node: _Node) -> str | None:
        limits = self.limits
        if member.kind in ('symlink', 'hardlink', 'special'):
            node.issues.append({'code': f'{member.kind}_member_not_followed'})
            self._insert_leaf(node, status='skipped', hash_status='not_read', sniff=None)
            return None
        if member.encrypted:
            node.issues.append({'code': 'encrypted_member'})
            self._count(self.incomplete, 'encrypted_content')
            self._insert_leaf(node, status='encrypted', hash_status='unavailable_encrypted', sniff=None)
            return None
        reason = container.precheck(member)
        accounted = getattr(container, 'account', None)
        if reason is None and container.compressed_stream and isinstance(container, archives.TarContainer):
            if self.expanded_bytes + (member.size or 0) > limits.max_expanded_bytes:
                reason = 'max_expanded_bytes'
        if reason is not None:
            self._limit(node, reason, size=member.size)
            self._insert_leaf(node, status='limit_exceeded', hash_status='skipped_limit', sniff=None)
            # Listing further tar members would decompress past the limit.
            return 'stop' if isinstance(container, archives.TarContainer) else None
        if self.expanded_bytes >= limits.max_expanded_bytes:
            self._limit(node, 'max_expanded_bytes')
            self._insert_leaf(node, status='limit_exceeded', hash_status='skipped_limit', sniff=None)
            return None
        account = True
        if accounted is not None:
            charged = accounted(member)
            if charged:
                self.expanded_bytes += charged
                account = False
        try:
            stream = container.open(member)
        except archives.MemberError as error:
            node.issues.append({'code': error.code, **error.details})
            self._count(self.incomplete, 'encrypted_content' if error.status == 'encrypted' else f'member_{error.status}')
            hash_status = {'encrypted': 'unavailable_encrypted', 'unsupported': 'unavailable_unsupported'}.get(error.status, 'error_damaged')
            self._insert_leaf(node, status=error.status, hash_status=hash_status, sniff=None)
            return None
        try:
            self._process(stream, node, account=account)
        finally:
            try:
                stream.close()
            except archives.DAMAGE_ERRORS:
                pass
        return None


def _read_at(fileobj, offset: int, length: int) -> bytes:
    fileobj.seek(offset)
    return fileobj.read(length)


def _size_of(fileobj) -> int:
    position = fileobj.tell()
    fileobj.seek(0, os.SEEK_END)
    size = fileobj.tell()
    fileobj.seek(position)
    return size


def _validate_paths(source: Path, temp_root: Path):
    if _inside(temp_root, source):
        raise ValueError('temporary directory must be outside the scanned source root')
    for label, path in (('database', dbmod.settings.db_path), ('content store', dbmod.settings.store_dir)):
        resolved = Path(path).resolve(strict=False)
        if _inside(resolved, source):
            raise ValueError(f'the {label} must not be inside the scanned source root')


def run_catalog_scan(root: str | Path, collection_id: int, *, limits: CatalogLimits | None = None,
                     temp_dir: str | Path | None = None) -> dict:
    """Catalogue a directory (or one file such as an archive) read-only into a new scan."""
    limits = limits or CatalogLimits()
    requested = Path(root).expanduser()
    if requested.is_symlink():
        raise ValueError('Source root must not be a symlink')
    source = requested.resolve(strict=True)
    if source.is_dir():
        root_kind = 'directory'
    elif source.is_file():
        root_kind = 'file'
    else:
        raise ValueError('Source root must be a directory or a regular file')
    temp_root = Path(temp_dir or tempfile.gettempdir()).expanduser().resolve(strict=False)
    scan_root = source if root_kind == 'directory' else source.parent
    _validate_paths(source, temp_root)  # before creating anything
    temp_root.mkdir(parents=True, exist_ok=True)
    with closing(dbmod.connect()) as db:
        collection = db.execute('SELECT id, case_id FROM collections WHERE id=?', (collection_id,)).fetchone()
        if collection is None:
            raise ValueError('collection does not exist')
        cursor = db.execute('''INSERT INTO catalog_scans(case_id,collection_id,root_path,root_kind,status,started_at,
                               catalog_version,signature_table_version,scanner_version,limits_json,policy_json,adapters_json)
                               VALUES(?,?,?,?,'running',?,?,?,?,?,?,?)''',
                            (collection['case_id'], collection_id, str(source), root_kind, _now(),
                             f'{CATALOG_SCHEMA} {CATALOG_VERSION}', signatures.version(), scanner.SCANNER_VERSION,
                             _json(asdict(limits)), _json(POLICY), _json(archives.adapters())))
        scan_id = int(cursor.lastrowid)
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('catalog_scan_started','catalog_scan',?,?)",
                   (scan_id, _json({'collection_id': collection_id, 'root_kind': root_kind})))
        db.commit()
        # One short commit per entry: in WAL mode NORMAL syncs at checkpoints, not
        # per commit. A crash can lose the last entries of a 'running' scan only.
        db.execute('PRAGMA synchronous=NORMAL')
        writer = _Writer(db, scan_id, collection_id, int(collection['case_id']))
        engine = _Engine(writer, limits, scan_root, temp_root)
        try:
            if root_kind == 'directory':
                walk = _Walk(source, limits.scan_limits(), engine)
                root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    walk.walk(root_fd)
                finally:
                    os.close(root_fd)
                directories = walk.manifest['coverage']['directories_seen']
            else:
                parent_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    st = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
                    engine.disk_file(parent_fd, source.name, source.name, st)
                finally:
                    os.close(parent_fd)
                directories = 0
            coverage = _scan_coverage(db, scan_id, engine, directories)
            status = 'completed' if coverage['complete'] else 'partial'
            db.execute('UPDATE catalog_scans SET status=?, finished_at=?, coverage_json=? WHERE id=?',
                       (status, _now(), _json(coverage), scan_id))
            db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('catalog_scan_finished','catalog_scan',?,?)",
                       (scan_id, _json({'status': status, 'entries': coverage['entries']})))
            db.commit()
        except BaseException as exc:
            db.rollback()
            db.execute("UPDATE catalog_scans SET status='failed', finished_at=?, error=? WHERE id=?",
                       (_now(), f'{type(exc).__name__}: {exc}', scan_id))
            db.commit()
            raise
    return {'scan_id': scan_id, 'collection_id': collection_id, 'status': status, 'coverage': coverage}


def _scan_coverage(db, scan_id: int, engine: _Engine, directories: int) -> dict:
    by_status = {r[0]: r[1] for r in db.execute('SELECT status, COUNT(*) FROM catalog_entries WHERE scan_id=? GROUP BY status', (scan_id,))}
    conflicts = db.execute('SELECT COUNT(*) FROM catalog_entries WHERE scan_id=? AND type_conflict=1', (scan_id,)).fetchone()[0]
    entries = sum(by_status.values())
    containers = db.execute('SELECT COUNT(*) FROM catalog_entries WHERE scan_id=? AND is_container=1', (scan_id,)).fetchone()[0]
    reasons = dict(engine.incomplete)
    for code, count in engine.limit_hits.items():
        reasons[f'limit:{code}'] = count
    return {
        'complete': not reasons,
        'incomplete_reasons': reasons,
        'entries': entries,
        'by_status': by_status,
        'type_conflicts': conflicts,
        'containers': containers,
        'archive_members': engine.members_total,
        'directories_seen': directories,
        'hash_bytes_read': engine.hash_bytes,
        'expanded_bytes_read': engine.expanded_bytes,
        'limit_hits': engine.limit_hits,
        'walk_issues': engine.walk_issues,
    }

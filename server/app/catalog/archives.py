"""Read-only archive adapters with raw member names and header times.

Nothing here extracts to disk or interprets a member name as a path. Members
are streamed from the container file object. Every adapter reports the raw
name bytes, the decoding it used and anomalies (absolute, ``..``, backslash,
control characters, duplicates, non-UTF-8) as data.

Optional formats (7z via ``py7zr``, RAR via ``rarfile``) are used only when the
module is importable; otherwise :func:`open_container` raises
``ContainerError('unsupported', 'adapter_unavailable')``.
"""
from __future__ import annotations

import bz2
import gzip
import importlib
import io
import lzma
import struct
import tarfile
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from ..timeinfo import TimeCandidate, from_epoch

CONTAINER_TYPES = {'zip', 'tar', 'gz', 'bz2', 'xz', 'tar.gz', 'tar.bz2', 'tar.xz', '7z', 'rar'}
DOCUMENT_CONTAINERS = {'docx', 'xlsx', 'pptx', 'odt', 'ods', 'odp', 'epub', 'apk'}
DAMAGE_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, lzma.LZMAError, OSError, tarfile.TarError,
                 struct.error, ValueError, KeyError, IndexError, UnicodeDecodeError)


class ContainerError(Exception):
    """A container could not be opened or listed; ``status`` is the entry status."""

    def __init__(self, status: str, code: str, **details):
        super().__init__(code)
        self.status, self.code, self.details = status, code, details


class MemberError(Exception):
    def __init__(self, status: str, code: str, **details):
        super().__init__(code)
        self.status, self.code, self.details = status, code, details


@dataclass
class Member:
    index: int
    raw_name: bytes
    name: str
    name_encoding: str
    kind: str = 'file'  # file | dir | symlink | hardlink | special
    size: int | None = None
    compressed_size: int | None = None
    encrypted: bool = False
    times: list[TimeCandidate] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    handle: object = None


def adapters() -> dict:
    report = {}
    for kind, module in (('7z', 'py7zr'), ('rar', 'rarfile')):
        try:
            imported = importlib.import_module(module)
            report[kind] = {'module': module, 'available': True,
                            'version': str(getattr(imported, '__version__', 'unknown'))}
        except ImportError:
            report[kind] = {'module': module, 'available': False, 'reason': 'adapter_unavailable'}
    report['zip'] = {'module': 'zipfile', 'available': True}
    report['tar'] = {'module': 'tarfile', 'available': True}
    return report


# ---------------------------------------------------------------- names

def _valid_utf8(raw: bytes) -> bool:
    try:
        raw.decode('utf-8')
        return True
    except UnicodeDecodeError:
        return False


def name_flags(raw: bytes) -> list[str]:
    flags = []
    if not raw:
        flags.append('empty_name')
    if raw.startswith((b'/', b'\\')) or (len(raw) > 1 and raw[1:2] == b':' and raw[:1].isalpha()):
        flags.append('absolute')
    segments = raw.replace(b'\\', b'/').split(b'/')
    if b'..' in segments:
        flags.append('parent_traversal')
    if b'\\' in raw:
        flags.append('backslash')
    if any(b < 0x20 or b == 0x7F for b in raw):
        flags.append('control_chars')
    if not _valid_utf8(raw):
        flags.append('non_utf8')
    return flags


def display_name(raw: bytes, *, utf8_flag: bool | None) -> tuple[str, str, dict]:
    """Decode for display only. Returns (text, encoding label, alternatives)."""
    if raw.isascii():
        return raw.decode('ascii'), 'ascii', {}
    if utf8_flag:
        return raw.decode('utf-8', 'replace'), 'utf-8 (flag)', {}
    if utf8_flag is None:
        return raw.decode('utf-8', 'replace'), 'utf-8' if _valid_utf8(raw) else 'undecodable_bytes', {}
    cp437 = raw.decode('cp437')
    if _valid_utf8(raw):
        return raw.decode('utf-8'), 'utf-8 (unflagged)', {'cp437': cp437}
    return cp437, 'cp437 (zip default)', {}


# ---------------------------------------------------------------- time helpers

def _dos_time(date_time: tuple, source: str) -> TimeCandidate | None:
    year, month, day, hour, minute, second = date_time
    raw = f'{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}'
    meta = {'granularity_seconds': 2, 'zone': 'archive_writer_local_unknown'}
    if (year, month, day, hour, minute, second) == (1980, 1, 1, 0, 0, 0):
        meta['dos_epoch_placeholder'] = True
    try:
        local = datetime(year, month, day, hour, minute, second).isoformat()
    except ValueError:
        meta['invalid_calendar_value'] = True
        return TimeCandidate('file_mtime', raw, source, tz_status='unknown', precision='unknown', metadata=meta)
    return TimeCandidate('file_mtime', raw, source, local=local, tz_status='unknown', precision='second', metadata=meta)


def _zip_extra_times(extra: bytes) -> list[TimeCandidate]:
    found = []
    index = 0
    while index + 4 <= len(extra):
        header, length = struct.unpack_from('<HH', extra, index)
        body = extra[index + 4:index + 4 + length]
        index += 4 + length
        if header == 0x5455 and len(body) >= 5 and body[0] & 1:
            mtime = struct.unpack_from('<i', body, 1)[0]
            found.append(from_epoch('file_mtime', str(mtime), 'zip_extended_timestamp'))
        elif header == 0x000A and len(body) >= 32:
            offset = 4
            while offset + 4 <= len(body):
                tag, size = struct.unpack_from('<HH', body, offset)
                if tag == 1 and size >= 8:
                    filetime = struct.unpack_from('<Q', body, offset + 4)[0]
                    seconds = filetime // 10_000_000 - 11_644_473_600
                    fraction = filetime % 10_000_000
                    found.append(from_epoch('file_mtime', f'{seconds}.{fraction:07d}', 'zip_ntfs_timestamp',
                                            metadata={'filetime_raw': str(filetime)}))
                    break
                offset += 4 + size
    return found


# ---------------------------------------------------------------- ZIP

def _zip_directory_size(fileobj, size: int) -> tuple[int | None, int | None]:
    """Declared central-directory size and entry count from the (zip64) EOCD."""
    tail_len = min(size, 65535 + 22)
    fileobj.seek(size - tail_len)
    tail = fileobj.read(tail_len)
    position = tail.rfind(b'PK\x05\x06')
    if position < 0 or position + 22 > len(tail):
        return None, None
    entries, cd_size = struct.unpack_from('<HI', tail, position + 10)
    if entries == 0xFFFF or cd_size == 0xFFFFFFFF:
        locator = position - 20
        if locator >= 0 and tail[locator:locator + 4] == b'PK\x06\x07':
            record_offset = struct.unpack_from('<Q', tail, locator + 8)[0]
            if record_offset + 56 <= size:
                fileobj.seek(record_offset)
                record = fileobj.read(56)
                if record[:4] == b'PK\x06\x06':
                    entries = struct.unpack_from('<Q', record, 32)[0]
                    cd_size = struct.unpack_from('<Q', record, 40)[0]
    return cd_size, entries


class ZipContainer:
    kind = 'zip'
    compressed_stream = False

    def __init__(self, fileobj, size: int, limits):
        self.fileobj, self.size = fileobj, size
        cd_size, declared = _zip_directory_size(fileobj, size)
        self.declared_members = declared
        if cd_size is not None and cd_size > limits.max_zip_directory_bytes:
            raise ContainerError('limit_exceeded', 'max_zip_directory_bytes', declared_directory_bytes=cd_size,
                                 declared_members=declared)
        fileobj.seek(0)
        try:
            self.zf = zipfile.ZipFile(fileobj)
        except DAMAGE_ERRORS + (NotImplementedError, RuntimeError) as exc:
            raise ContainerError('damaged', 'zip_central_directory_unreadable', error=f'{type(exc).__name__}: {exc}',
                                 declared_members=declared) from exc
        self.refined_type = 'zip'
        self.details: dict = {'declared_members': declared}

    def names(self) -> list[str]:
        return [info.filename for info in self.zf.infolist()]

    def read_small(self, name: str, limit: int = 4096) -> bytes | None:
        for info in self.zf.infolist():
            if info.filename == name and not info.flag_bits & 1:
                try:
                    with self.zf.open(info) as handle:
                        return handle.read(limit)
                except DAMAGE_ERRORS + (NotImplementedError, RuntimeError):
                    return None
        return None

    def members(self) -> Iterator[Member]:
        for index, info in enumerate(self.zf.infolist()):
            utf8 = bool(info.flag_bits & 0x800)
            raw = info.orig_filename.encode('utf-8' if utf8 else 'cp437', 'surrogateescape')
            name, encoding, alternatives = display_name(raw, utf8_flag=utf8)
            flags = name_flags(raw)
            if not utf8 and not raw.isascii():
                flags.append('cp437_or_unflagged_utf8')
            kind = 'dir' if info.orig_filename.endswith('/') and info.file_size == 0 else 'file'
            times = [t for t in [_dos_time(info.date_time, 'zip_dos_datetime')] if t] + _zip_extra_times(info.extra)
            details = {'compress_type': info.compress_type, 'crc32': f'{info.CRC:08x}', 'flag_bits': info.flag_bits,
                       'create_system': info.create_system}
            if alternatives:
                details['name_alternatives'] = alternatives
            unix_mode = info.external_attr >> 16
            if info.create_system == 3 and (unix_mode & 0o170000) == 0o120000:
                kind = 'symlink'
            yield Member(index, raw, name, encoding, kind, info.file_size, info.compress_size,
                         bool(info.flag_bits & 1), times, flags, details, info)

    def open(self, member: Member):
        try:
            return self.zf.open(member.handle)
        except NotImplementedError as exc:
            raise MemberError('unsupported', 'compression_method_unsupported', compress_type=member.handle.compress_type) from exc
        except RuntimeError as exc:
            raise MemberError('encrypted', 'encrypted_member', error=str(exc)) from exc
        except DAMAGE_ERRORS as exc:
            raise MemberError('damaged', 'member_unreadable', error=f'{type(exc).__name__}: {exc}') from exc

    def precheck(self, member: Member):
        return None

    def close(self):
        self.zf.close()


class _RecoveredStream(io.RawIOBase):
    """Stream one member located by its local header (central directory lost)."""

    def __init__(self, fileobj, offset: int, csize: int, method: int, crc: int, total: int):
        self.fileobj, self.position, self.remaining = fileobj, offset, csize
        self.method, self.crc, self.total = method, crc, total
        self.running_crc = 0
        self.inflater = zlib.decompressobj(-15) if method == 8 else None
        self.buffer = b''
        self.done = False

    def readable(self):
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = 1 << 30
        while len(self.buffer) < size and not self.done:
            chunk_size = min(self.remaining, 256 * 1024)
            if chunk_size <= 0:
                if self.inflater is not None:
                    self.buffer += self.inflater.flush()
                self.done = True
                if self.running_crc_of(self.buffer) != self.crc:
                    raise zipfile.BadZipFile('Bad CRC-32 for recovered member')
                break
            self.fileobj.seek(self.position)
            raw = self.fileobj.read(chunk_size)
            if len(raw) < chunk_size:
                raise EOFError('member data truncated')
            self.position += len(raw)
            self.remaining -= len(raw)
            self.buffer += self.inflater.decompress(raw) if self.inflater is not None else raw
        out, self.buffer = self.buffer[:size], self.buffer[size:]
        self.running_crc = zlib.crc32(out, self.running_crc)
        return out

    def running_crc_of(self, pending: bytes) -> int:
        return zlib.crc32(pending, self.running_crc)


class RecoveredZipContainer:
    """Bounded local-header walk for a ZIP whose central directory is unreadable."""
    kind = 'zip'
    compressed_stream = False

    def __init__(self, fileobj, size: int, limits, error: ContainerError):
        self.fileobj, self.size, self.limits = fileobj, size, limits
        self.refined_type = 'zip'
        self.declared_members = error.details.get('declared_members')
        self.details = {'recovery': 'local_headers', 'central_directory_error': error.details.get('error')}

    def names(self):
        return []

    def members(self) -> Iterator[Member]:
        offset, index, seen = 0, 0, 0
        while offset + 30 <= self.size:
            self.fileobj.seek(offset)
            header = self.fileobj.read(30)
            if header[:4] != b'PK\x03\x04':
                break
            (_, _, flags, method, mtime, mdate, crc, csize, usize, nlen, xlen) = struct.unpack('<IHHHHHIIIHH', header)
            raw = self.fileobj.read(nlen)
            extra = self.fileobj.read(xlen)
            data_offset = offset + 30 + nlen + xlen
            utf8 = bool(flags & 0x800)
            name, encoding, alternatives = display_name(raw, utf8_flag=utf8)
            date_time = ((mdate >> 9) + 1980, (mdate >> 5) & 0xF, mdate & 0x1F, mtime >> 11, (mtime >> 5) & 0x3F, (mtime & 0x1F) * 2)
            times = [t for t in [_dos_time(date_time, 'zip_dos_datetime_local_header')] if t] + _zip_extra_times(extra)
            details = {'recovered_from': 'local_header', 'local_header_offset': offset, 'compress_type': method,
                       'crc32': f'{crc:08x}', 'flag_bits': flags}
            if alternatives:
                details['name_alternatives'] = alternatives
            kind = 'dir' if raw.endswith(b'/') and usize == 0 else 'file'
            member = Member(index, raw, name, encoding, kind, usize, csize, bool(flags & 1), times, name_flags(raw), details,
                            (data_offset, csize, method, crc, usize))
            if flags & 0x08 and csize == 0:
                member.details['size_unknown'] = 'data_descriptor_without_sizes'
                member.handle = None
                yield member
                break
            yield member
            index += 1
            seen += 1
            offset = data_offset + csize

    def open(self, member: Member):
        if member.handle is None:
            raise MemberError('damaged', 'member_size_unknown')
        data_offset, csize, method, crc, usize = member.handle
        if method not in (0, 8):
            raise MemberError('unsupported', 'compression_method_unsupported', compress_type=method)
        if data_offset + csize > self.size:
            raise MemberError('damaged', 'member_data_truncated', missing_bytes=data_offset + csize - self.size)
        return _RecoveredStream(self.fileobj, data_offset, csize, method, crc, usize)

    def precheck(self, member):
        return None

    def close(self):
        pass


# ---------------------------------------------------------------- TAR and single streams

_TAR_MODES = {'tar': ('r:', None), 'gz': ('r:gz', 'gzip'), 'bz2': ('r:bz2', 'bz2'), 'xz': ('r:xz', 'xz')}
_TAR_TYPES = {'tar': 'tar', 'gz': 'tar.gz', 'bz2': 'tar.bz2', 'xz': 'tar.xz'}


class TarContainer:
    kind = 'tar'

    def __init__(self, fileobj, size: int, compression: str, limits):
        self.size, self.limits = size, limits
        mode, _ = _TAR_MODES[compression]
        self.compressed_stream = compression != 'tar'
        fileobj.seek(0)
        self.tar = tarfile.open(fileobj=fileobj, mode=mode, encoding='utf-8', errors='surrogateescape')
        self.refined_type = _TAR_TYPES[compression]
        self.declared_members = None
        self.details = {'compression': compression}
        self.traversed = 0

    def names(self):
        return []

    def members(self) -> Iterator[Member]:
        index = 0
        while True:
            try:
                info = self.tar.next()
            except tarfile.ReadError as exc:
                raise ContainerError('damaged', 'tar_header_unreadable', error=str(exc), members_listed=index) from exc
            except DAMAGE_ERRORS as exc:
                raise ContainerError('damaged', 'tar_stream_unreadable', error=f'{type(exc).__name__}: {exc}',
                                     members_listed=index) from exc
            if info is None:
                return
            raw = info.name.encode('utf-8', 'surrogateescape')
            name, encoding, _ = display_name(raw, utf8_flag=None)
            kind = ('file' if info.isreg() else 'dir' if info.isdir() else 'symlink' if info.issym()
                    else 'hardlink' if info.islnk() else 'special')
            times = []
            try:
                times.append(from_epoch('file_mtime', repr(info.mtime) if isinstance(info.mtime, float) else str(int(info.mtime)),
                                        'tar_header', metadata={'zero_epoch': True} if not info.mtime else None))
            except (ValueError, OverflowError):
                pass
            details = {'tar_type': info.type.decode('latin-1', 'replace'), 'mode': oct(info.mode)}
            if kind in ('symlink', 'hardlink'):
                details['link_target'] = info.linkname.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')
            if info.pax_headers:
                details['pax_keys'] = sorted(info.pax_headers)[:16]
            yield Member(index, raw, name, encoding, kind, info.size, None, False, times, name_flags(raw), details, info)
            index += 1

    def finish(self) -> dict:
        """Check the end-of-archive marker; tarfile ends silently on a truncated header."""
        try:
            self.tar.fileobj.seek(self.tar.offset)
            marker = self.tar.fileobj.read(1024)
        except DAMAGE_ERRORS as exc:
            return {'end_of_archive': 'stream_error', 'error': f'{type(exc).__name__}: {exc}'}
        if marker == b'\0' * 1024:
            return {'end_of_archive': 'zero_blocks'}
        return {'end_of_archive': 'missing', 'bytes_after_last_member': len(marker)}

    def precheck(self, member: Member):
        """Skipping or reading a member of a compressed tar decompresses it."""
        if not self.compressed_stream or member.kind != 'file':
            return None
        projected = self.traversed + (member.size or 0)
        if projected >= self.limits.ratio_check_min_bytes and projected > self.limits.max_compression_ratio * max(self.size, 1):
            return 'compression_ratio'
        return None

    def account(self, member: Member) -> int:
        if self.compressed_stream and member.kind == 'file':
            self.traversed += member.size or 0
            return member.size or 0
        return 0

    def open(self, member: Member):
        try:
            handle = self.tar.extractfile(member.handle)
        except DAMAGE_ERRORS as exc:
            raise MemberError('damaged', 'member_unreadable', error=f'{type(exc).__name__}: {exc}') from exc
        if handle is None:
            raise MemberError('unsupported', 'not_a_regular_member')
        return handle

    def close(self):
        self.tar.close()


def _gzip_header(fileobj) -> dict:
    fileobj.seek(0)
    head = fileobj.read(10)
    if len(head) < 10:
        return {}
    flags = head[3]
    info: dict = {'mtime': struct.unpack_from('<I', head, 4)[0], 'os': head[9]}
    if flags & 4:
        extra_len = struct.unpack('<H', fileobj.read(2))[0]
        fileobj.read(extra_len)
    if flags & 8:
        name = bytearray()
        while len(name) < 4096:
            byte = fileobj.read(1)
            if not byte or byte == b'\x00':
                break
            name += byte
        info['fname'] = bytes(name)
    return info


class StreamContainer:
    """A single compressed stream (gzip/bz2/xz) that is not a tar: one member."""
    compressed_stream = True

    def __init__(self, fileobj, size: int, compression: str, container_name: str, limits):
        self.fileobj, self.size, self.limits = fileobj, size, limits
        self.kind = compression
        self.refined_type = compression
        self.declared_members = 1
        self.details: dict = {'single_stream': True}
        self.header = _gzip_header(fileobj) if compression == 'gz' else {}
        fileobj.seek(0)
        stem = container_name
        for suffix in ('.gz', '.gzip', '.bz2', '.xz', '.z'):
            if stem.lower().endswith(suffix):
                stem = stem[:-len(suffix)]
                break
        self.fallback_name = stem or 'stream'

    def names(self):
        return []

    def members(self) -> Iterator[Member]:
        raw = self.header.get('fname') or self.fallback_name.encode('utf-8', 'surrogateescape')
        if self.header.get('fname'):
            name, encoding = raw.decode('latin-1'), 'latin-1 (gzip FNAME)'
            source = 'gzip_fname'
        else:
            name, encoding = self.fallback_name, 'derived_from_container_name'
            source = 'container_name_without_suffix'
        times = []
        if self.header.get('mtime'):
            times.append(from_epoch('file_mtime', str(self.header['mtime']), 'gzip_header'))
        yield Member(0, raw, name, encoding, 'file', None, self.size, False, times, name_flags(raw), {'name_source': source}, None)

    def precheck(self, member):
        return None

    def open(self, member):
        self.fileobj.seek(0)
        opener = {'gz': gzip.GzipFile, 'bz2': bz2.BZ2File, 'xz': lzma.LZMAFile}[self.kind]
        return opener(fileobj=self.fileobj, mode='rb') if self.kind == 'gz' else opener(self.fileobj, mode='rb')

    def close(self):
        pass


# ---------------------------------------------------------------- optional adapters

class SevenZipContainer:  # pragma: no cover - exercised only when py7zr is installed
    kind = '7z'
    compressed_stream = True

    def __init__(self, fileobj, size: int, limits, module):
        self.limits = limits
        fileobj.seek(0)
        self.archive = module.SevenZipFile(fileobj, mode='r')
        if self.archive.needs_password():
            raise ContainerError('encrypted', 'encrypted_archive')
        self.infos = self.archive.list()
        self.refined_type = '7z'
        self.declared_members = len(self.infos)
        self.details = {'adapter': 'py7zr'}

    def names(self):
        return []

    def members(self):
        for index, info in enumerate(self.infos):
            raw = info.filename.encode('utf-8', 'surrogateescape')
            name, encoding, _ = display_name(raw, utf8_flag=None)
            times = []
            moment = getattr(info, 'creationtime', None)
            if isinstance(moment, datetime):
                aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
                times.append(from_epoch('file_mtime', repr(aware.timestamp()), '7z_header'))
            yield Member(index, raw, name, encoding, 'dir' if info.is_directory else 'file', info.uncompressed,
                         getattr(info, 'compressed', None), False, times, name_flags(raw), {}, info)

    def precheck(self, member):
        if (member.size or 0) > self.limits.spool_memory_bytes:
            return 'adapter_reads_member_into_memory'
        return None

    def open(self, member):
        self.archive.reset()
        data = self.archive.read(targets=[member.handle.filename])
        return io.BytesIO(data[member.handle.filename].read())

    def close(self):
        self.archive.close()


class RarContainer:  # pragma: no cover - exercised only when rarfile is installed
    kind = 'rar'
    compressed_stream = True

    def __init__(self, fileobj, size: int, limits, module):
        fileobj.seek(0)
        self.archive = module.RarFile(fileobj)
        if self.archive.needs_password():
            raise ContainerError('encrypted', 'encrypted_archive')
        self.refined_type = 'rar'
        self.infos = self.archive.infolist()
        self.declared_members = len(self.infos)
        self.details = {'adapter': 'rarfile'}

    def names(self):
        return []

    def members(self):
        for index, info in enumerate(self.infos):
            raw = info.filename.encode('utf-8', 'surrogateescape')
            name, encoding, _ = display_name(raw, utf8_flag=None)
            times = [t for t in [_dos_time(info.date_time, 'rar_header_local')] if t] if info.date_time else []
            yield Member(index, raw, name, encoding, 'dir' if info.is_dir() else 'file', info.file_size,
                         info.compress_size, bool(info.needs_password()), times, name_flags(raw), {}, info)

    def precheck(self, member):
        return None

    def open(self, member):
        try:
            return self.archive.open(member.handle)
        except Exception as exc:  # rarfile raises its own hierarchy (e.g. missing unrar tool)
            raise MemberError('unsupported', 'rar_member_unreadable', error=f'{type(exc).__name__}: {exc}') from exc

    def close(self):
        self.archive.close()


def open_container(fileobj, size: int, sniffed_type: str, container_name: str, limits):
    """Open a container by its *sniffed* type; extensions are never trusted."""
    if sniffed_type == 'zip' or sniffed_type in DOCUMENT_CONTAINERS:
        try:
            return ZipContainer(fileobj, size, limits)
        except ContainerError as error:
            if error.status != 'damaged':
                raise
            recovered = RecoveredZipContainer(fileobj, size, limits, error)
            recovered.error = error
            return recovered
    if sniffed_type in ('tar', 'gz', 'bz2', 'xz', 'tar.gz', 'tar.bz2', 'tar.xz'):
        compression = {'tar.gz': 'gz', 'tar.bz2': 'bz2', 'tar.xz': 'xz'}.get(sniffed_type, sniffed_type)
        try:
            return TarContainer(fileobj, size, compression, limits)
        except tarfile.ReadError as exc:
            if compression == 'tar':
                raise ContainerError('damaged', 'tar_unreadable', error=str(exc)) from exc
            return StreamContainer(fileobj, size, compression, container_name, limits)
        except DAMAGE_ERRORS as exc:
            raise ContainerError('damaged', 'compressed_stream_unreadable', error=f'{type(exc).__name__}: {exc}') from exc
    if sniffed_type in ('7z', 'rar'):
        module_name = 'py7zr' if sniffed_type == '7z' else 'rarfile'
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ContainerError('unsupported', 'adapter_unavailable', adapter=module_name) from exc
        cls = SevenZipContainer if sniffed_type == '7z' else RarContainer
        try:
            return cls(fileobj, size, limits, module)
        except ContainerError:
            raise
        except Exception as exc:
            raise ContainerError('damaged', f'{sniffed_type}_unreadable', error=f'{type(exc).__name__}: {exc}') from exc
    raise ContainerError('unsupported', 'not_a_container')

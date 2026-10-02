"""Bounded verified binary reader for a decoder's seek/read file-like protocol.

Reuses the descriptor/chunk identity primitive exercised by HTTP media serving.
This is a trusted Python adapter boundary, not a sandbox against introspection.
"""
from __future__ import annotations

import hashlib
import io
import operator
import os
import threading

from .verified_media import (CHUNK_SIZE, MAX_CHUNKS, MAX_MEDIA_BYTES,
                             MediaIntegrityError, MediaLimitError,
                             MediaUnavailable, VerifiedMedia)

MAX_READ_BYTES = 1024 * 1024
MAX_POSITION = 2**63 - 1


class VerifiedReader(io.RawIOBase):
    """Seekable, read-only input retaining one verified descriptor until close.

    Required store_root selects anchored no-follow traversal. Reads never expose
    unverified partial data to the caller; readinto also leaves its buffer intact
    if any covering chunk fails. Unbounded reads fail when they exceed the cap.
    """
    def __init__(self, path, expected_sha256, expected_size, *, store_root):
        super().__init__()
        self._lock = threading.RLock()
        self._media = None
        self._position = 0
        self._failed = False
        if store_root is None:
            raise MediaUnavailable('a configured content store is required')
        self._media = VerifiedMedia(path, expected_sha256, expected_size, store_root=store_root)

    def _require_open(self):
        if self.closed or self._media is None:
            raise ValueError('I/O operation on closed verified reader')

    def readable(self):
        with self._lock:
            self._require_open()
            return True

    def seekable(self):
        with self._lock:
            self._require_open()
            return True

    def writable(self):
        with self._lock:
            self._require_open()
            return False

    def fileno(self):
        raise io.UnsupportedOperation('verified reader does not expose a descriptor')

    def tell(self):
        with self._lock:
            self._require_open()
            return self._position

    def seek(self, offset, whence=os.SEEK_SET):
        with self._lock:
            self._require_open()
            offset, whence = operator.index(offset), operator.index(whence)
            if whence == os.SEEK_SET:
                position = offset
            elif whence == os.SEEK_CUR:
                position = self._position + offset
            elif whence == os.SEEK_END:
                position = self._media.size + offset
            else:
                raise ValueError('unsupported seek origin')
            if position < 0:
                raise ValueError('negative seek position')
            if position > MAX_POSITION:
                raise OverflowError('seek position exceeds signed 64-bit range')
            self._position = position
            return position

    def read(self, size=-1):
        with self._lock:
            if self._failed:
                raise MediaIntegrityError('verified reader previously failed')
            try:
                return self._read(size)
            except (MediaIntegrityError, MediaUnavailable, MediaLimitError, OSError):
                self._failed = True
                raise

    def _read(self, size=-1):
        with self._lock:
            self._require_open()
            size = -1 if size is None else operator.index(size)
            remaining = max(0, self._media.size - self._position)
            if size < 0:
                size = remaining
            if size > MAX_READ_BYTES:
                raise MediaLimitError('decoder read exceeds the verified buffer limit')
            length = min(size, remaining)
            if length == 0:
                return b''
            start, end = self._position, self._position + length
            buffer = bytearray()
            first = start // self._media.chunk_size
            last = (end - 1) // self._media.chunk_size
            for index in range(first, last + 1):
                chunk = self._media.read_chunk(index)
                offset = index * self._media.chunk_size
                buffer.extend(chunk[max(0, start - offset):min(len(chunk), end - offset)])
            result = bytes(buffer)
            self._position = end
            return result

    def readall(self):
        return self.read(-1)

    def readline(self, size=-1):
        raise io.UnsupportedOperation('verified decoder reader does not implement line reads')

    def readlines(self, hint=-1):
        raise io.UnsupportedOperation('verified decoder reader does not implement line reads')

    def readinto(self, target):
        with self._lock:
            self._require_open()
            with memoryview(target) as view:
                if view.readonly:
                    raise TypeError('readinto target must be writable')
                with view.cast('B') as byte_view:
                    data = self.read(len(byte_view))
                    byte_view[:len(data)] = data
                    return len(data)

    def verify_unchanged(self):
        """Recheck all initial chunks/whole digest on the same retained fd.

        Position is unchanged. This catches current edits even in bytes the
        decoder never requested. It does not promise immutability after return.
        """
        with self._lock:
            self._require_open()
            if self._failed:
                raise MediaIntegrityError('verified reader previously failed')
            try:
                digest = hashlib.sha256()
                count = len(self._media.chunk_digests) // 32
                for index in range(count):
                    digest.update(self._media.read_chunk(index))
                if (os.fstat(self._media.fd).st_size != self._media.size or
                        digest.hexdigest() != self._media.sha256):
                    raise MediaIntegrityError('stored content changed during decoder processing')
            except (MediaIntegrityError, MediaUnavailable, OSError):
                self._failed = True
                raise

    def close(self):
        # RawIOBase's finalizer calls close even after partial construction.
        lock = getattr(self, '_lock', None)
        if lock is None:
            super().close()
            return
        with lock:
            try:
                if self._media is not None:
                    self._media.close()
            finally:
                super().close()

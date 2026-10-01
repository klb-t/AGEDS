"""Serve only buffered bytes verified against an initially verified descriptor.

Preparation is synchronous and belongs in a worker thread, never the event loop.
No media copies, locator resolution, background jobs or source writes are made.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote

import anyio
from starlette.responses import Response, StreamingResponse

CHUNK_SIZE = 256 * 1024
MAX_CHUNKS = 16_384
MAX_MEDIA_BYTES = CHUNK_SIZE * MAX_CHUNKS


class MediaUnavailable(ValueError):
    """The configured regular file cannot be opened safely."""


class MediaIntegrityError(ValueError):
    """Recorded byte identity is missing or does not match buffered content."""


class MediaLimitError(ValueError):
    """Media exceeds this bounded serving profile."""


def _open(path, store_root=None):
    for candidate in (path, store_root):
        if candidate is not None:
            try:
                raw = os.fspath(candidate)
            except TypeError:
                raise MediaUnavailable('stored content pathname is invalid') from None
            nul = b'\x00' if isinstance(raw, bytes) else '\x00'
            if nul in raw:
                raise MediaUnavailable('stored content pathname is invalid')
    if not all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_NONBLOCK', 'O_DIRECTORY', 'pread')):
        raise MediaUnavailable('safe descriptor reads are unavailable')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if store_root is None:
        return os.open(path, flags)
    # Lexical containment and no-follow traversal avoid a resolve/open race.
    if '..' in Path(path).parts or '..' in Path(store_root).parts:
        raise MediaUnavailable('parent traversal is not permitted')
    root = Path(os.path.abspath(store_root))
    candidate = Path(os.path.abspath(path))
    try:
        parts = candidate.relative_to(root).parts
    except ValueError:
        raise MediaUnavailable('stored content is outside configured store') from None
    if not parts:
        raise MediaUnavailable('stored content is not a regular file')
    directory = os.open(root.anchor, flags | os.O_DIRECTORY)
    try:
        for part in root.parts[1:] + parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], flags, dir_fd=directory)
    finally:
        os.close(directory)


class VerifiedMedia:
    """One retained descriptor and compact SHA256 digests for bounded chunks."""
    def __init__(self, path, expected_sha256, expected_size, *, store_root=None):
        self.fd = None
        self.chunk_digests = bytearray()
        if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
            raise MediaIntegrityError('recorded SHA256 is unavailable or invalid')
        if type(expected_size) is not int or expected_size < 0:
            raise MediaIntegrityError('recorded byte count is unavailable or invalid')
        if expected_size > MAX_MEDIA_BYTES or (expected_size + CHUNK_SIZE - 1) // CHUNK_SIZE > MAX_CHUNKS:
            raise MediaLimitError('stored content exceeds the verified serving limit')
        self.size = expected_size
        self.sha256 = expected_sha256
        self.chunk_size = CHUNK_SIZE
        try:
            self.fd = _open(path, store_root)
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode):
                raise MediaUnavailable('stored content is not a regular file')
            if info.st_size != self.size:
                raise MediaIntegrityError('stored content byte count differs')
            digest = hashlib.sha256()
            for offset in range(0, self.size, self.chunk_size):
                chunk = self._read_exact(offset, min(self.chunk_size, self.size - offset))
                digest.update(chunk)
                self.chunk_digests.extend(hashlib.sha256(chunk).digest())
            if os.fstat(self.fd).st_size != self.size or digest.hexdigest() != expected_sha256:
                raise MediaIntegrityError('stored content failed SHA256 verification')
        except OSError:
            self.close()
            raise MediaUnavailable('stored content is unavailable') from None
        except BaseException:
            self.close()
            raise

    def _read_exact(self, offset, size):
        # pread can return short regular-file reads; each allocation is bounded.
        chunks = bytearray()
        while len(chunks) < size:
            part = os.pread(self.fd, size - len(chunks), offset + len(chunks))
            if not part:
                raise MediaIntegrityError('stored content was truncated')
            chunks.extend(part)
        return bytes(chunks)

    def read_chunk(self, index):
        if self.fd is None:
            raise MediaUnavailable('verified descriptor is closed')
        count = len(self.chunk_digests) // 32
        if type(index) is not int or not 0 <= index < count:
            raise MediaIntegrityError('invalid verified chunk index')
        if os.fstat(self.fd).st_size != self.size:
            raise MediaIntegrityError('stored content byte count changed during serving')
        offset = index * self.chunk_size
        chunk = self._read_exact(offset, min(self.chunk_size, self.size - offset))
        if hashlib.sha256(chunk).digest() != self.chunk_digests[index * 32:(index + 1) * 32]:
            raise MediaIntegrityError('stored content changed during serving')
        return chunk

    def close(self):
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            os.close(descriptor)

    def __del__(self):
        # Fallback for response construction that is never handed to ASGI.
        try:
            self.close()
        except OSError:
            pass


def _range(raw, size):
    if not isinstance(raw, str) or len(raw) > 256:
        raise ValueError('invalid range')
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', raw.strip())
    if not match or not any(match.groups()) or size == 0:
        raise ValueError('invalid or unsatisfiable range')
    left, right = match.groups()
    if left:
        start = int(left)
        end = min(int(right), size - 1) if right else size - 1
        if start >= size or end < start:
            raise ValueError('unsatisfiable range')
    else:
        suffix = int(right)
        if suffix == 0:
            raise ValueError('unsatisfiable range')
        start, end = max(0, size - suffix), size - 1
    return start, end


class VerifiedMediaResponse(StreamingResponse):
    def __init__(self, media, start, end, *, status_code, headers, media_type):
        self.media = media
        self.start, self.end = start, end
        super().__init__(self._body(), status_code=status_code, headers=headers, media_type=media_type)

    async def _body(self):
        if self.end < self.start:
            return
        first, last = self.start // self.media.chunk_size, self.end // self.media.chunk_size
        for index in range(first, last + 1):
            # Default abandon_on_cancel=False shields the owned descriptor read.
            chunk = await anyio.to_thread.run_sync(self.media.read_chunk, index)
            offset = index * self.media.chunk_size
            yield chunk[max(0, self.start - offset):min(len(chunk), self.end - offset + 1)]

    def close(self):
        self.media.close()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A failed send/disconnect also owns cleanup; no reliance on an
            # exhausted generator or a background callback running successfully.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.media.close)


def verified_media_response(path, expected_sha256, expected_size, *, media_type=None,
                            filename=None, range_header=None, head=False,
                            if_range=None, store_root=None):
    """Prepare a response in a worker thread, with full initial verification.

    Single bytes ranges only; invalid/multiple ranges return 416. HEAD ignores
    ranges. An If-Range value unequal to our strong ETag selects the full body.
    No response is issued for unknown expected sizes or failed initial integrity.
    """
    if media_type is not None and (not isinstance(media_type, str) or
            any(ord(char) < 32 or ord(char) > 126 for char in media_type)):
        raise MediaIntegrityError('recorded media type is not a safe HTTP header')
    if filename is not None:
        try:
            disposition = "attachment; filename*=utf-8''" + quote(str(filename), safe='')
        except UnicodeError:
            raise MediaIntegrityError('recorded filename is not valid UTF-8') from None
    media = VerifiedMedia(path, expected_sha256, expected_size, store_root=store_root)
    try:
        etag = '"sha256:' + media.sha256 + '"'
        headers = {'accept-ranges': 'bytes', 'etag': etag, 'cache-control': 'no-store'}
        if filename is not None:
            headers['content-disposition'] = disposition
        mime = media_type or 'application/octet-stream'
        start, end, status_code = 0, media.size - 1, 200
        if not head and range_header is not None and (if_range is None or if_range == etag):
            try:
                start, end = _range(range_header, media.size)
            except ValueError:
                media.close()
                headers['content-range'] = 'bytes */' + str(media.size)
                return Response(status_code=416, headers=headers)
            status_code = 206
            headers['content-range'] = f'bytes {start}-{end}/{media.size}'
        headers['content-length'] = str(max(0, end - start + 1))
        if head:
            media.close()
            return Response(status_code=status_code, headers=headers, media_type=mime)
        return VerifiedMediaResponse(media, start, end, status_code=status_code,
                                     headers=headers, media_type=mime)
    except BaseException:
        media.close()
        raise

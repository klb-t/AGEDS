"""Shared worker plumbing for media job kinds other than transcription.

Inputs are read only through ``VerifiedReader`` (content-store bytes checked
against the recorded SHA-256 before and after processing); outputs are
published only through the fenced ``jobs.publish_result``.
"""
from __future__ import annotations

import math

from .. import worker as _worker
from ..db import session
from ..verified_reader import CHUNK_SIZE, MAX_MEDIA_BYTES, MAX_READ_BYTES, VerifiedReader


class AdapterUnavailable(RuntimeError):
    """An optional local engine is not installed or not usable; the reason is explicit."""


class UnsupportedInput(RuntimeError):
    """The input type is outside what this adapter can process here."""


def load_artifact(artifact_id: int) -> dict:
    with session() as db:
        row = db.execute('SELECT * FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    if row is None:
        raise RuntimeError('artifact missing')
    return dict(row)


def verification_record() -> dict:
    return {
        'initial_full_sha256': 'not_completed', 'final_same_descriptor': 'not_performed',
        'input_mode': 'verified_seekable_filelike',
        'limits': {'max_media_bytes': MAX_MEDIA_BYTES, 'max_read_bytes': MAX_READ_BYTES,
                   'verification_chunk_bytes': CHUNK_SIZE},
        'worker_path_reopen': False, 'worker_media_copy': False,
        'limitations': ['byte identity does not establish authenticity or output accuracy'],
    }


def input_provenance(artifact: dict, verification: dict) -> dict:
    return {'input_hash_recorded': artifact['sha256'] or 'unknown', 'input_size_recorded': artifact['size_bytes'],
            'source_id': artifact['source_id'], 'source_locator': artifact['source_locator'] or 'unknown',
            'input_mode': 'verified_seekable_filelike', 'input_verification': verification}


def open_verified(artifact: dict) -> VerifiedReader:
    return VerifiedReader(artifact['stored_path'], artifact['sha256'], artifact['size_bytes'],
                          store_root=_worker.settings.store_dir)


def read_all(reader: VerifiedReader, limit: int) -> bytes:
    """Read the verified stream into memory, refusing inputs above ``limit`` bytes."""
    size = reader.seek(0, 2)
    reader.seek(0)
    if size > limit:
        raise UnsupportedInput(f'input is {size} bytes; recipe limit max_input_bytes is {limit}')
    buffer = bytearray()
    while len(buffer) < size:
        chunk = reader.read(min(MAX_READ_BYTES, size - len(buffer)))
        if not chunk:
            break
        buffer.extend(chunk)
    return bytes(buffer)


def finite_seconds(value) -> float | None:
    value = _worker._json_number(value)
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return float(value)
    return None

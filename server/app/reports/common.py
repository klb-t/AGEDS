"""Shared, dependency-free helpers for reports (no database or config import)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

CANONICALIZATION = 'sorted-keys-compact-utf8-no-nan/v1'
SHA256_RE = re.compile(r'[0-9a-f]{64}')


class ReportInputError(ValueError):
    """Invalid report type, parameter or prose slot (HTTP 422, CLI exit 2)."""


class ReportOutputRefused(ValueError):
    """The requested output location is not allowed (HTTP 403/422, CLI exit 2)."""


class ReportNotFound(ValueError):
    """Unknown report id (HTTP 404)."""


def canonical_json(value: Any) -> bytes:
    """Same canonical form as ``packages.canonical_json`` (rows hash identically)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sha256_json(value: Any, domain: str = '') -> str:
    return hashlib.sha256(domain.encode('utf-8') + canonical_json(value)).hexdigest()


def row_sha256(row: dict) -> str:
    """Digest of a ``SELECT *`` row as a dict; equal to hashing the same row of
    a metadata package export (same canonicalization)."""
    return sha256_json(row)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def epoch_to_utc(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    value = datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = value.strftime('%Y-%m-%dT%H:%M:%S')
    if value.microsecond:
        text += f'.{value.microsecond:06d}'.rstrip('0')
    return text + 'Z'


def parse_utc_bound(value: str | None, name: str) -> str | None:
    """Accept an offset-bearing ISO time or a bare date (UTC midnight).

    A wall-clock time without an offset is refused: the host zone is never used.
    """
    if value is None or value == '':
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ReportInputError(f'{name} must be an ISO-8601 string')
    text = value.strip()
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
            parsed = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ReportInputError(f'{name} is not an ISO-8601 time: {value!r}') from exc
    if parsed.tzinfo is None:
        raise ReportInputError(f'{name} needs an explicit offset or Z (the host zone is never assumed)')
    return parsed.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def has_numeral(text: str) -> bool:
    """Any digit or other numeric character (Unicode ``isnumeric``)."""
    return any(char.isnumeric() for char in text)


def pointer_token(key: Any) -> str:
    return str(key).replace('~', '~0').replace('/', '~1')


def resolve_pointer(document: Any, pointer: str) -> Any:
    """RFC 6901 JSON Pointer resolution (used by tests and verification)."""
    if pointer == '':
        return document
    if not pointer.startswith('/'):
        raise KeyError(pointer)
    value = document
    for raw in pointer[1:].split('/'):
        token = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list):
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise KeyError(pointer)
    return value


BUNDLE_DIR = 'citations'
BUNDLE_MANIFEST = 'citation-bundle.json'


def packet_name(artifact_id: int, anchor_id: int) -> str:
    """File name of one citation packet inside a bundle (unique per pair)."""
    return f'packets/a{int(artifact_id)}-c{int(anchor_id)}.json'

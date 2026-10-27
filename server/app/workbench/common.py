"""Shared validation, errors and audit helpers for the workbench package."""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from datetime import datetime, timezone

ACTOR = 'workbench'
MAX_SQL_INTEGER = 2**63 - 1
MAX_NAME_CHARS = 200
MAX_NOTE_CHARS = 20_000


class WorkbenchInputError(ValueError):
    """Invalid input; maps to HTTP 422."""


class WorkbenchNotFound(LookupError):
    """A referenced workbench object or evidence row does not exist; HTTP 404."""


class WorkbenchConflict(RuntimeError):
    """The operation conflicts with existing state; HTTP 409."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit(db: sqlite3.Connection, action: str, object_kind: str, object_id: int | None, details: dict) -> int:
    cur = db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES (?,?,?,?,?)',
                     (ACTOR, action, object_kind, object_id, json.dumps(details, ensure_ascii=False, sort_keys=True, allow_nan=False)))
    return int(cur.lastrowid)


def _has_control(value: str, *, allow_newlines: bool) -> bool:
    for char in value:
        if allow_newlines and char in '\n\t\r':
            continue
        if unicodedata.category(char) == 'Cc':
            return True
    return False


def clean_text(value, field: str, *, max_chars: int = MAX_NOTE_CHARS, required: bool = False) -> str | None:
    """Validate free text without rewriting it; blank optional text becomes None."""
    if value is None:
        if required:
            raise WorkbenchInputError(f'{field} is required')
        return None
    if not isinstance(value, str):
        raise WorkbenchInputError(f'{field} must be text')
    if '\x00' in value or _has_control(value, allow_newlines=True):
        raise WorkbenchInputError(f'{field} must not contain control characters')
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeError as error:
        raise WorkbenchInputError(f'{field} must be valid UTF-8 text') from error
    if len(value) > max_chars:
        raise WorkbenchInputError(f'{field} exceeds {max_chars} characters')
    if not value.strip():
        if required:
            raise WorkbenchInputError(f'{field} cannot be blank')
        return None
    return value


def clean_name(value, field: str = 'name', *, max_chars: int = MAX_NAME_CHARS) -> str:
    """A single-line label kept exactly as typed; ambiguous whitespace is rejected, not trimmed."""
    text = clean_text(value, field, max_chars=max_chars, required=True)
    if any(char in text for char in '\n\r\t') or _has_control(text, allow_newlines=False):
        raise WorkbenchInputError(f'{field} must be a single line without control characters')
    if text != text.strip():
        raise WorkbenchInputError(f'{field} must not start or end with whitespace')
    return text


def positive_id(value, field: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_SQL_INTEGER:
        raise WorkbenchInputError(f'{field} must be an integer from 1 to {MAX_SQL_INTEGER}')
    return value


def loads_object(raw: str | None) -> dict:
    """Stored JSON of a workbench row; malformed values are reported, not repaired."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {'_stored_json_invalid': True}
    return value if isinstance(value, dict) else {'_stored_json_not_object': True}

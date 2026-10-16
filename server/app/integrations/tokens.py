"""Local access tokens for the read API.

A token is ``ageds_<key>.<secret>``. The database keeps ``key`` (public lookup
identifier), a random ``salt`` and ``sha256(salt || secret)``; the secret is
printed once by the CLI and never stored or logged. Verification compares the
recomputed digest with :func:`hmac.compare_digest`. Scopes are plain strings
(``read``, ``propose``). Revocation and expiry are timestamps; rows are kept.

``token_required_everywhere()`` reads ``EW_REQUIRE_TOKEN`` at request time so an
operator can turn global enforcement on without editing settings code. The
read API routes always require a token regardless of that switch.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from ..db import session

TOKEN_PREFIX = 'ageds_'
KNOWN_SCOPES = ('read', 'propose')
MAX_LABEL_CHARS = 200
MAX_TOKEN_CHARS = 256
KEY_BYTES = 6
SECRET_BYTES = 32
SALT_BYTES = 16


class TokenError(ValueError):
    """Invalid token request input (label, scopes, expiry, identity)."""


class TokenRejected(PermissionError):
    """Authentication failed; ``reason`` is a stable machine-readable code."""

    def __init__(self, reason: str, *, status: int = 401):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def token_required_everywhere() -> bool:
    return os.environ.get('EW_REQUIRE_TOKEN', '').strip().lower() in ('1', 'true', 'yes', 'on')


def _digest(salt: str, secret: str) -> str:
    return hashlib.sha256((salt + secret).encode('utf-8')).hexdigest()


def _scopes(scopes) -> list[str]:
    if not isinstance(scopes, (list, tuple)) or not scopes:
        raise TokenError('at least one scope is required')
    result = []
    for scope in scopes:
        if not isinstance(scope, str) or scope not in KNOWN_SCOPES:
            raise TokenError(f'unknown scope; known scopes: {", ".join(KNOWN_SCOPES)}')
        if scope not in result:
            result.append(scope)
    return result


def _audit(db: sqlite3.Connection, action: str, token_id: int | None, details: dict, *, actor: str = 'system') -> None:
    db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,'api_token',?,?)",
               (actor, action, token_id, json.dumps(details, ensure_ascii=False, sort_keys=True)))


def token_payload(row) -> dict:
    """Public projection: never includes salt or digest."""
    return {
        'id': row['id'], 'token_key': row['token_key'], 'label': row['label'], 'client_id': row['client_id'],
        'scopes': json.loads(row['scopes_json']), 'created_at': row['created_at'], 'expires_at': row['expires_at'],
        'revoked_at': row['revoked_at'], 'last_used_at': row['last_used_at'], 'use_count': row['use_count'],
        'status': 'revoked' if row['revoked_at'] else 'active',
    }


def create_token(label: str, scopes=('read',), *, client_id: str | None = None,
                 expires_in_days: int | None = None, actor: str = 'cli') -> dict:
    """Create a token; the returned ``token`` value exists only in this result."""
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_CHARS:
        raise TokenError('label must be nonblank text up to 200 characters')
    if client_id is not None and (not isinstance(client_id, str) or not client_id.strip() or len(client_id) > MAX_LABEL_CHARS):
        raise TokenError('client_id must be nonblank text up to 200 characters')
    if expires_in_days is not None and (type(expires_in_days) is not int or expires_in_days < 1 or expires_in_days > 36500):
        raise TokenError('expires_in_days must be an integer from 1 to 36500')
    scope_list = _scopes(list(scopes))
    created = utc_now()
    expires = None
    if expires_in_days is not None:
        expires = (datetime.fromisoformat(created.replace('Z', '+00:00')) + timedelta(days=expires_in_days)).isoformat().replace('+00:00', 'Z')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        for _ in range(8):
            key = secrets.token_hex(KEY_BYTES)
            if not db.execute('SELECT 1 FROM api_tokens WHERE token_key=?', (key,)).fetchone():
                break
        else:
            raise TokenError('could not allocate a unique token key')
        secret = secrets.token_urlsafe(SECRET_BYTES)
        salt = secrets.token_hex(SALT_BYTES)
        cur = db.execute('''INSERT INTO api_tokens(token_key,salt,secret_sha256,label,client_id,scopes_json,created_at,expires_at)
                            VALUES(?,?,?,?,?,?,?,?)''',
                         (key, salt, _digest(salt, secret), label.strip(), client_id.strip() if client_id else None,
                          json.dumps(scope_list), created, expires))
        token_id = int(cur.lastrowid)
        _audit(db, 'token_create', token_id, {'label': label.strip(), 'client_id': client_id, 'scopes': scope_list,
                                              'expires_at': expires, 'token_key': key}, actor=actor)
        row = db.execute('SELECT * FROM api_tokens WHERE id=?', (token_id,)).fetchone()
    result = token_payload(row)
    result['token'] = f'{TOKEN_PREFIX}{key}.{secret}'
    result['shown_once'] = True
    return result


def list_tokens() -> list[dict]:
    with session() as db:
        return [token_payload(row) for row in db.execute('SELECT * FROM api_tokens ORDER BY id')]


def revoke_token(token_id: int, *, actor: str = 'cli', note: str | None = None) -> dict:
    if type(token_id) is not int or token_id < 1:
        raise TokenError('token id must be a positive integer')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM api_tokens WHERE id=?', (token_id,)).fetchone()
        if not row:
            raise TokenError('token not found')
        if row['revoked_at']:
            raise TokenError('token already revoked')
        now = utc_now()
        db.execute('UPDATE api_tokens SET revoked_at=? WHERE id=?', (now, token_id))
        _audit(db, 'token_revoke', token_id, {'token_key': row['token_key'], 'note': note}, actor=actor)
        return token_payload(db.execute('SELECT * FROM api_tokens WHERE id=?', (token_id,)).fetchone())


def parse_bearer(header_value: str | None) -> str | None:
    """Extract the token from an ``Authorization`` header, or None."""
    if not header_value or not isinstance(header_value, str):
        return None
    parts = header_value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return None
    return parts[1].strip() or None


def verify_token(token: str | None, *, scope: str | None = 'read', touch: bool = True) -> dict:
    """Return the token record for a valid bearer with ``scope``; else raise.

    Rejections are audited with the public key only. Expired, revoked and
    unknown tokens all yield HTTP 401 reasons; a valid token lacking the scope
    yields 403. ``scope=None`` checks validity only (global enforcement mode).
    The digest comparison is constant-time for a given key.
    """
    if scope is not None and scope not in KNOWN_SCOPES:
        raise TokenError('unknown scope requested by route')
    if not token:
        raise TokenRejected('missing_token')
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or not token.startswith(TOKEN_PREFIX) or '.' not in token:
        raise TokenRejected('malformed_token')
    key, _, secret = token[len(TOKEN_PREFIX):].partition('.')
    if not key or not secret or any(c not in '0123456789abcdef' for c in key):
        raise TokenRejected('malformed_token')
    rejection: TokenRejected | None = None
    # The audit row must survive a rejection, so the decision is made inside the
    # transaction and the exception is raised after commit.
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM api_tokens WHERE token_key=?', (key,)).fetchone()
        now = utc_now()
        if row is None:
            # Burn comparable work so an unknown key is not distinguishable by timing alone.
            hmac.compare_digest(_digest('0' * 2 * SALT_BYTES, secret), '0' * 64)
            _audit(db, 'token_rejected', None, {'token_key': key, 'reason': 'unknown_key'})
            rejection = TokenRejected('unknown_token')
        elif not hmac.compare_digest(_digest(row['salt'], secret), row['secret_sha256']):
            _audit(db, 'token_rejected', row['id'], {'token_key': key, 'reason': 'secret_mismatch'})
            rejection = TokenRejected('unknown_token')
        elif row['revoked_at']:
            _audit(db, 'token_rejected', row['id'], {'token_key': key, 'reason': 'revoked'})
            rejection = TokenRejected('revoked_token')
        elif row['expires_at'] and row['expires_at'] <= now:
            _audit(db, 'token_rejected', row['id'], {'token_key': key, 'reason': 'expired'})
            rejection = TokenRejected('expired_token')
        elif scope is not None and scope not in json.loads(row['scopes_json']):
            _audit(db, 'token_rejected', row['id'], {'token_key': key, 'reason': 'scope', 'required': scope})
            rejection = TokenRejected('insufficient_scope', status=403)
        elif touch:
            db.execute('UPDATE api_tokens SET last_used_at=?, use_count=use_count+1 WHERE id=?', (now, row['id']))
            row = db.execute('SELECT * FROM api_tokens WHERE id=?', (row['id'],)).fetchone()
        result = None if rejection else token_payload(row)
    if rejection:
        raise rejection
    return result

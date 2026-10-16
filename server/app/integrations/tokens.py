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
import logging
import os
import secrets
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .. import db as dbmod
from ..db import session

TOKEN_PREFIX = 'ageds_'
KNOWN_SCOPES = ('read', 'propose')
MAX_LABEL_CHARS = 200
MAX_TOKEN_CHARS = 256
KEY_BYTES = 6
SECRET_BYTES = 32
SALT_BYTES = 16
# Review finding 8: a valid token is verified read-only; ``last_used_at`` and
# ``use_count`` are written at most once per interval per token and process
# (uses in between are counted in memory and added on the next write).
TOUCH_INTERVAL_SECONDS = 60
# Rejections are counted per UTC hour, public key and reason; the first one of
# each writes an audit row. Past this many keys in one hour further keys are
# pooled under ``*`` so unauthenticated clients cannot grow the tables per request.
MAX_REJECTION_KEYS_PER_BUCKET = 25
_PENDING_USES: dict[tuple[str, int], int] = {}
_PENDING_LOCK = threading.Lock()
log = logging.getLogger(__name__)


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


def _record_rejection(key: str, reason: str, token_id: int | None, now: str, details: dict | None = None) -> None:
    """Count one rejection; only the first per hour, key and reason writes an audit row."""
    bucket = now[:13]
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        found = db.execute('SELECT id FROM api_token_rejections WHERE bucket=? AND token_key=? AND reason=?',
                           (bucket, key, reason)).fetchone()
        if found is None and db.execute('SELECT COUNT(*) FROM api_token_rejections WHERE bucket=?',
                                        (bucket,)).fetchone()[0] >= MAX_REJECTION_KEYS_PER_BUCKET:
            key, token_id, details = '*', None, {'pooled': 'per-hour key cap reached'}
            found = db.execute('SELECT id FROM api_token_rejections WHERE bucket=? AND token_key=? AND reason=?',
                               (bucket, key, reason)).fetchone()
        if found is not None:
            db.execute('UPDATE api_token_rejections SET count=count+1, last_seen_at=? WHERE id=?', (now, found['id']))
            return
        db.execute('INSERT INTO api_token_rejections(bucket,token_key,reason,token_id,count,first_seen_at,last_seen_at) '
                   'VALUES(?,?,?,?,1,?,?)', (bucket, key, reason, token_id, now, now))
        _audit(db, 'token_rejected', token_id, {'token_key': key, 'reason': reason, **(details or {}),
                                                'counted_in': f'api_token_rejections bucket {bucket}'})


def _touch(row, now: str):
    """Lazy ``last_used_at``/``use_count`` update; never blocks or fails a valid request."""
    entry = (str(dbmod.settings.db_path), int(row['id']))
    with _PENDING_LOCK:
        _PENDING_USES[entry] = _PENDING_USES.get(entry, 0) + 1
        pending = _PENDING_USES[entry]
    last = row['last_used_at']
    if last:
        try:
            age = (datetime.fromisoformat(now.replace('Z', '+00:00'))
                   - datetime.fromisoformat(last.replace('Z', '+00:00'))).total_seconds()
        except ValueError:
            age = TOUCH_INTERVAL_SECONDS
        if age < TOUCH_INTERVAL_SECONDS:
            return row
    try:
        with closing(dbmod.connect()) as db:
            db.execute('PRAGMA busy_timeout=200')
            db.execute('UPDATE api_tokens SET last_used_at=?, use_count=use_count+? WHERE id=?', (now, pending, row['id']))
            db.commit()
            fresh = db.execute('SELECT * FROM api_tokens WHERE id=?', (row['id'],)).fetchone()
    except sqlite3.OperationalError as exc:  # busy: keep the uses pending for the next write
        log.debug('token use bookkeeping deferred: %s', exc)
        return row
    with _PENDING_LOCK:
        _PENDING_USES[entry] = _PENDING_USES.get(entry, 0) - pending
    return fresh


def verify_token(token: str | None, *, scope: str | None = 'read', touch: bool = True) -> dict:
    """Return the token record for a valid bearer with ``scope``; else raise.

    Expired, revoked and unknown tokens yield HTTP 401 reasons; a valid token
    lacking the scope yields 403. ``scope=None`` checks validity only (global
    enforcement mode). The digest comparison is constant-time for a given key.
    Verification reads without a write transaction; rejections are counted in
    ``api_token_rejections`` (the first per hour/key/reason is audited) and the
    use of a valid token is recorded lazily (``TOUCH_INTERVAL_SECONDS``).
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
    with closing(dbmod.connect()) as db:
        row = db.execute('SELECT * FROM api_tokens WHERE token_key=?', (key,)).fetchone()
    now = utc_now()
    if row is None:
        # Burn comparable work so an unknown key is not distinguishable by timing alone.
        hmac.compare_digest(_digest('0' * 2 * SALT_BYTES, secret), '0' * 64)
        _record_rejection(key, 'unknown_key', None, now)
        raise TokenRejected('unknown_token')
    if not hmac.compare_digest(_digest(row['salt'], secret), row['secret_sha256']):
        _record_rejection(key, 'secret_mismatch', row['id'], now)
        raise TokenRejected('unknown_token')
    if row['revoked_at']:
        _record_rejection(key, 'revoked', row['id'], now)
        raise TokenRejected('revoked_token')
    if row['expires_at'] and row['expires_at'] <= now:
        _record_rejection(key, 'expired', row['id'], now)
        raise TokenRejected('expired_token')
    if scope is not None and scope not in json.loads(row['scopes_json']):
        _record_rejection(key, 'scope', row['id'], now, {'required': scope})
        raise TokenRejected('insufficient_scope', status=403)
    if touch:
        row = _touch(row, now)
    return token_payload(row)

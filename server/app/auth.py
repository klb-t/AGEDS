"""Identity for HTTP callers (package P3): per-principal API tokens.

Authentication only names a principal; what it may see is decided by grants
in authz.py. Tokens are random, shown once, and stored as a salted hash with
the hashing parameters kept in data. A token is `<method uid>.<secret>`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3

from .assignments import new_uid
from datetime import datetime, timedelta, timezone

from .authz import _time, get_setting, utc_now

KDF = 'sha256-salted-v1'


def _digest(salt: str, secret: str) -> str:
    return hashlib.sha256((salt + secret).encode('utf-8')).hexdigest()


def issue_token(db: sqlite3.Connection, principal_id: int, *, label: str | None = None,
                actor: str | None = None, kind: str = 'token', expires_at: str | None = None) -> str:
    if not db.execute('SELECT 1 FROM principals WHERE id=?', (principal_id,)).fetchone():
        raise ValueError('principal does not exist')
    uid, secret, salt = new_uid(), secrets.token_urlsafe(32), secrets.token_hex(16)
    cur = db.execute('''INSERT INTO auth_methods(uid,principal_id,kind,label,secret_salt,secret_hash,kdf,kdf_params_json,expires_at)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                     (uid, principal_id, kind, label, salt, _digest(salt, secret), KDF,
                      json.dumps({'encoding': 'utf-8'}), expires_at))
    db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)',
               (actor or 'system', 'token_issued' if kind == 'token' else 'session_started', 'auth_method', cur.lastrowid,
                json.dumps({'uid': uid, 'principal_id': principal_id, 'kind': kind, 'expires_at': expires_at})))
    return f'{uid}.{secret}'


SESSION_HOURS_SEED = 12


def start_session(db: sqlite3.Connection, principal_id: int) -> tuple[str, int]:
    """A browser session: its own short-lived secret, never a copy of the API token."""
    hours = get_setting(db, 'auth.session_hours', SESSION_HOURS_SEED)
    hours = hours if isinstance(hours, (int, float)) and hours > 0 else SESSION_HOURS_SEED
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    return issue_token(db, principal_id, label='browser session', kind='session', expires_at=expires), int(hours * 3600)


def end_session(db: sqlite3.Connection, token: str | None) -> bool:
    """Revoke a browser session server-side; API tokens are never revoked by logout."""
    if not isinstance(token, str) or token.count('.') != 1:
        return False
    uid = token.split('.')[0]
    row = db.execute("SELECT kind FROM auth_methods WHERE uid=? AND revoked_at IS NULL", (uid,)).fetchone()
    if row is None or row['kind'] != 'session' or principal_for_token(db, token) is None:
        return False
    revoke_token(db, uid, actor='logout')
    return True


def revoke_token(db: sqlite3.Connection, method_uid: str, *, actor: str) -> None:
    db.execute('UPDATE auth_methods SET revoked_at=? WHERE uid=? AND revoked_at IS NULL', (utc_now(), method_uid))
    db.execute('INSERT INTO audit_log(actor,action,object_kind,details_json) VALUES(?,?,?,?)',
               (actor, 'token_revoked', 'auth_method', json.dumps({'uid': method_uid})))


def principal_for_token(db: sqlite3.Connection, token: str | None) -> int | None:
    if not isinstance(token, str) or token.count('.') != 1 or len(token) > 512:
        return None
    uid, secret = token.split('.')
    row = db.execute('''SELECT m.principal_id, m.secret_salt, m.secret_hash, m.kdf, m.expires_at, p.status FROM auth_methods m
                        JOIN principals p ON p.id=m.principal_id
                        WHERE m.uid=? AND m.kind IN ('token','session') AND m.revoked_at IS NULL''', (uid,)).fetchone()
    if row is None or row['kdf'] != KDF or row['status'] != 'active':
        return None
    if row['expires_at'] is not None and _time(row['expires_at']) <= datetime.now(timezone.utc):
        return None
    if not hmac.compare_digest(_digest(row['secret_salt'], secret), row['secret_hash']):
        return None
    return int(row['principal_id'])

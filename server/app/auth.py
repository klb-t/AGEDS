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
from .authz import utc_now

KDF = 'sha256-salted-v1'


def _digest(salt: str, secret: str) -> str:
    return hashlib.sha256((salt + secret).encode('utf-8')).hexdigest()


def issue_token(db: sqlite3.Connection, principal_id: int, *, label: str | None = None,
                actor: str | None = None) -> str:
    if not db.execute('SELECT 1 FROM principals WHERE id=?', (principal_id,)).fetchone():
        raise ValueError('principal does not exist')
    uid, secret, salt = new_uid(), secrets.token_urlsafe(32), secrets.token_hex(16)
    cur = db.execute('''INSERT INTO auth_methods(uid,principal_id,kind,label,secret_salt,secret_hash,kdf,kdf_params_json)
                        VALUES(?,?,'token',?,?,?,?,?)''',
                     (uid, principal_id, label, salt, _digest(salt, secret), KDF, json.dumps({'encoding': 'utf-8'})))
    db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)',
               (actor or 'system', 'token_issued', 'auth_method', cur.lastrowid,
                json.dumps({'uid': uid, 'principal_id': principal_id})))
    return f'{uid}.{secret}'


def revoke_token(db: sqlite3.Connection, method_uid: str, *, actor: str) -> None:
    db.execute('UPDATE auth_methods SET revoked_at=? WHERE uid=? AND revoked_at IS NULL', (utc_now(), method_uid))
    db.execute('INSERT INTO audit_log(actor,action,object_kind,details_json) VALUES(?,?,?,?)',
               (actor, 'token_revoked', 'auth_method', json.dumps({'uid': method_uid})))


def principal_for_token(db: sqlite3.Connection, token: str | None) -> int | None:
    if not isinstance(token, str) or token.count('.') != 1 or len(token) > 512:
        return None
    uid, secret = token.split('.')
    row = db.execute('''SELECT m.principal_id, m.secret_salt, m.secret_hash, m.kdf, p.status FROM auth_methods m
                        JOIN principals p ON p.id=m.principal_id
                        WHERE m.uid=? AND m.kind='token' AND m.revoked_at IS NULL''', (uid,)).fetchone()
    if row is None or row['kdf'] != KDF or row['status'] != 'active':
        return None
    if not hmac.compare_digest(_digest(row['secret_salt'], secret), row['secret_hash']):
        return None
    return int(row['principal_id'])

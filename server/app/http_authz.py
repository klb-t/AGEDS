"""HTTP gate (package P3): every route declares exactly one guard.

`auth.mode=legacy_open` keeps today's behaviour for the owner's local work
and is announced in a response header. In `tokens` mode a request names a
principal by token (X-AGEDS-Token header, Bearer authorization or the
ageds_token cookie) and each guard asks authz before the route reads data.
Installation-wide routes (global search before P4, whole-database exports
before P5, ingest, scans, annotations without an author) require coverage of
the installation scope; per-artifact routes require visibility of that
artifact. A denial for an absent or hidden object is identical unless
`denial.reveal_existence` says otherwise.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3

from fastapi import Depends, HTTPException, Request

from .auth import principal_for_token
from .authz import ContentGate, _row, get_setting
from . import db as database


@contextmanager
def _read():
    """Authorization only reads; a missing database is never created here."""
    try:
        connection = database.connect(read_only=True)
    except sqlite3.Error as exc:
        raise HTTPException(503, 'Evidence database is unavailable.') from exc
    try:
        yield connection
    except sqlite3.Error as exc:
        raise HTTPException(503, 'Evidence database is unavailable.') from exc
    finally:
        connection.close()

TOKEN_HEADER = 'x-ageds-token'
TOKEN_COOKIE = 'ageds_token'


@dataclass(frozen=True)
class Access:
    mode: str
    principal_id: int | None

    @property
    def open(self) -> bool:
        return self.mode == 'legacy_open'

    def gate(self, db) -> ContentGate | None:
        """None means legacy_open: callers keep the unfiltered behaviour."""
        return None if self.open else ContentGate(db, self.principal_id)


def _token(request: Request) -> str | None:
    if request.headers.get(TOKEN_HEADER):
        return request.headers[TOKEN_HEADER]
    authorization = request.headers.get('authorization', '')
    if authorization[:7].lower() == 'bearer ':
        return authorization[7:].strip()
    return request.cookies.get(TOKEN_COOKIE)


def _resolve(request: Request) -> Access:
    with _read() as db:
        mode = get_setting(db, 'auth.mode', 'legacy_open')
        if mode == 'legacy_open':
            warning = db.execute('''SELECT 1 FROM grants g JOIN scopes s ON s.id=g.scope_id
                                    WHERE g.revoked_at IS NULL AND s.kind<>'installation' LIMIT 1''').fetchone()
            request.state.ageds_auth = ('legacy_open', 'recipient_grants_unenforced' if warning else None)
            return Access('legacy_open', None)
        request.state.ageds_auth = (str(mode), None)
        if mode != 'tokens':
            # An unknown mode is kept in data and admits nobody.
            raise HTTPException(401, 'authentication required')
        principal = principal_for_token(db, _token(request))
    if principal is None:
        raise HTTPException(401, 'authentication required')
    return Access('tokens', principal)


def _deny(db, kind: str, object_id: int) -> HTTPException:
    reveal = get_setting(db, 'denial.reveal_existence', 'never')
    exists = _row(db, kind, object_id) is not None
    if reveal == 'always' and exists:
        return HTTPException(403, f'{kind} not permitted')
    if reveal == 'category_only' and exists:
        return HTTPException(403, 'not permitted')
    return HTTPException(404, 'not available')


def _mark(dependency, description: str):
    dependency._ageds_guard = description
    return dependency


def authenticated():
    """Any known principal; for routes that read no stored data."""
    def guard(request: Request) -> Access:
        return _resolve(request)
    return Depends(_mark(guard, 'authenticated'))


def installation(capability: str):
    def guard(request: Request) -> Access:
        access = _resolve(request)
        if not access.open:
            with _read() as db:
                if not ContentGate(db, access.principal_id).installation(capability):
                    raise HTTPException(403, 'not permitted')
        return access
    return Depends(_mark(guard, f'installation:{capability}'))


def scoped(capability: str):
    """Route filters its rows through Access.gate(); global access not required."""
    def guard(request: Request) -> Access:
        return _resolve(request)
    return Depends(_mark(guard, f'scoped:{capability}'))


def artifact(capability: str, parameter: str = 'artifact_id'):
    def guard(request: Request) -> Access:
        access = _resolve(request)
        if access.open:
            return access
        raw = request.path_params.get(parameter)
        try:
            object_id = int(raw)
        except (TypeError, ValueError):
            # Path validation answers with 422 whether or not anything exists.
            return access
        with _read() as db:
            if not ContentGate(db, access.principal_id).allows('artifact', object_id, capability):
                raise _deny(db, 'artifact', object_id)
        return access
    return Depends(_mark(guard, f'artifact:{capability}'))


async def auth_headers(request: Request, call_next):
    response = await call_next(request)
    state = getattr(request.state, 'ageds_auth', None)
    if state is not None:
        response.headers['X-AGEDS-Auth-Mode'] = state[0]
        if state[1]:
            response.headers['X-AGEDS-Auth-Warning'] = state[1]
    return response

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
            # Without accounts every request acts as the owner: only local clients may do that.
            if not _legacy_client_allowed(db, request):
                raise HTTPException(403, 'legacy_open accepts only local clients; use auth.mode=tokens')
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


LEGACY_OPEN_CLIENTS_SEED = ('127.0.0.0/8', '::1/128')


def _legacy_client_allowed(db, request: Request) -> bool:
    import ipaddress
    import os
    entries = list(LEGACY_OPEN_CLIENTS_SEED)
    entries += [e.strip() for e in os.environ.get('EW_LEGACY_OPEN_CLIENTS', '').split(',') if e.strip()]
    extra = get_setting(db, 'http.legacy_open_clients', [])
    entries += [str(e) for e in extra] if isinstance(extra, list) else []
    host = request.client.host if request.client else ''
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in entries  # a named test client must be listed literally
    for entry in entries:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


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


def public():
    """Pages that read no stored data (the advocate shell and its login form)."""
    def guard(request: Request) -> None:
        return None
    return Depends(_mark(guard, 'public'))


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


# --- Host and origin checks (every mode) -------------------------------------
# DNS rebinding: a page whose name resolves to this server must not read it, so the Host
# header must name a configured host. CSRF: a state-changing request from another site
# is refused (Sec-Fetch-Site, else Origin vs Host). Clients without these headers (CLI,
# Android, curl) are not browsers on another site and pass.
import os
from urllib.parse import urlsplit
from fastapi.responses import JSONResponse

DEFAULT_ALLOWED_HOSTS = ('127.0.0.1', 'localhost', '::1')
UNSAFE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


def _host_name(value: str) -> str:
    value = value.strip().lower()
    if value.startswith('['):
        return value[1:value.find(']')] if ']' in value else value
    return value.rsplit(':', 1)[0] if value.count(':') == 1 else value


def allowed_hosts() -> set:
    hosts = set(DEFAULT_ALLOWED_HOSTS)
    hosts |= {h.strip().lower() for h in os.environ.get('EW_ALLOWED_HOSTS', '').split(',') if h.strip()}
    try:
        connection = database.connect(read_only=True)
        try:
            extra = get_setting(connection, 'http.allowed_hosts', [])
        finally:
            connection.close()
        if isinstance(extra, list):
            hosts |= {str(h).strip().lower() for h in extra if str(h).strip()}
    except sqlite3.Error:
        pass
    return hosts


async def host_and_origin(request: Request, call_next):
    hosts = allowed_hosts()
    host = _host_name(request.headers.get('host', ''))
    if '*' not in hosts and host not in hosts:
        return JSONResponse(status_code=400, content={'detail': 'host not allowed'})
    if request.method in UNSAFE_METHODS:
        fetch_site = request.headers.get('sec-fetch-site')
        if fetch_site in ('cross-site', 'same-site'):
            return JSONResponse(status_code=403, content={'detail': 'cross-site request refused'})
        origin = request.headers.get('origin')
        if origin and origin != 'null':
            parts = urlsplit(origin)
            if parts.netloc.lower() != request.headers.get('host', '').lower():
                return JSONResponse(status_code=403, content={'detail': 'cross-origin request refused'})
        elif origin == 'null':
            return JSONResponse(status_code=403, content={'detail': 'opaque-origin request refused'})
    return await call_next(request)

"""HTTP routes owned by the integrations area.

``/api/read/v1/*`` (read scope) and ``/api/integrations/v1/*`` (propose scope
for writes) always require ``Authorization: Bearer <token>``. Their error model
is ``{"detail": {"code": "...", "message": "..."}}``; 401 carries
``WWW-Authenticate: Bearer``. The human pages under ``/integrations`` follow the
rest of the workbench UI (trusted network).

``install_app`` adds one middleware (``api.include_routers`` calls it): a Host
allowlist against DNS rebinding (review 2 finding 5), a CSRF guard for every
state-changing request (review finding 6; Origin checked against the allowlist)
and the opt-in global enforcement (``EW_REQUIRE_TOKEN=1``): every route except
the login form and static assets then needs a valid token (bearer header or the
browser session cookie from ``POST /integrations/session``), without scope
checks (review 2 finding 6: HTML pages and report files included).
"""
from __future__ import annotations

import html
import ipaddress
import os
import sqlite3
from http.cookies import CookieError, SimpleCookie
from typing import Annotated
from urllib.parse import urlsplit

from anyio import to_thread
from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query, Request
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..integrations import intents, loom_graph, proposals, read_api, tokens
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/integrations', 'Integracje')]
MAX_SQL_INTEGER = 2**63 - 1
SqlPathId = Annotated[int, PathParameter(ge=1, le=MAX_SQL_INTEGER)]
MAX_BODY_BYTES = 32 * 1024 * 1024
_OWN_PREFIXES = ('/api/read/v1/', '/api/integrations/v1/')


def _error(status: int, code: str, message: str, *, headers: dict | None = None) -> HTTPException:
    return HTTPException(status, {'code': code, 'message': message}, headers=headers)


def _rejection(exc: tokens.TokenRejected) -> HTTPException:
    messages = {'missing_token': 'Authorization: Bearer <token> is required.', 'malformed_token': 'Token is malformed.',
                'unknown_token': 'Token is unknown.', 'revoked_token': 'Token was revoked.', 'expired_token': 'Token has expired.',
                'insufficient_scope': 'Token lacks the required scope.'}
    headers = {'WWW-Authenticate': 'Bearer'} if exc.status == 401 else None
    return _error(exc.status, exc.reason, messages.get(exc.reason, exc.reason), headers=headers)


def require_scope(scope: str):
    def dependency(request: Request) -> dict:
        try:
            return tokens.verify_token(tokens.parse_bearer(request.headers.get('authorization')), scope=scope)
        except tokens.TokenRejected as exc:
            raise _rejection(exc) from exc
        except (OSError, sqlite3.Error) as exc:
            raise _error(503, 'database_unavailable', 'Evidence database is unavailable for authentication.') from exc
    return dependency


ReadToken = Annotated[dict, Depends(require_scope('read'))]
ProposeToken = Annotated[dict, Depends(require_scope('propose'))]


def _map_read_errors(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except read_api.ReadInputError as exc:
        raise _error(422, 'invalid_input', str(exc)) from exc
    except read_api.ReadNotFound as exc:
        raise _error(404, 'not_found', str(exc)) from exc
    except read_api.ReadLimitError as exc:
        raise _error(413, 'read_limit', str(exc)) from exc
    except read_api.ReadStoredError as exc:
        raise _error(409, 'stored_data_invalid', str(exc)) from exc
    except read_api.ReadWorkLimitError as exc:
        raise _error(503, 'work_limit', 'Read work budget exceeded; narrow the request.') from exc
    except (OSError, sqlite3.Error) as exc:
        raise _error(503, 'database_unavailable', 'Evidence database is unavailable for read-only access.') from exc


# --- read contract -----------------------------------------------------------
@router.get('/api/read/v1/contract')
def read_contract(token: ReadToken):
    return read_api.contract()


@router.get('/api/read/v1/search')
def read_search(token: ReadToken, q: str, limit: int = Query(read_api.LIMITS['search']['default_limit'], ge=1, le=read_api.LIMITS['search']['max_limit'])):
    return _map_read_errors(read_api.read_search, q, limit)


@router.get('/api/read/v1/timeline')
def read_timeline(token: ReadToken, from_: str | None = Query(None, alias='from'), to: str | None = None,
                  event_type: str | None = None, phone: str | None = None, direction: str | None = None,
                  thread_id: str | None = None, artifact_id: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER),
                  source_id: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER),
                  limit: int = Query(read_api.TIMELINE_DEFAULT_LIMIT, ge=1, le=read_api.TIMELINE_MAX_LIMIT), cursor: str | None = None):
    filters = {name: value for name, value in (('event_type', event_type), ('phone', phone), ('direction', direction), ('thread_id', thread_id),
                                               ('artifact_id', artifact_id), ('source_id', source_id)) if value is not None}
    return _map_read_errors(read_api.read_timeline, from_=from_, to=to, filters=filters, limit=limit, cursor=cursor)


@router.get('/api/read/v1/artifacts/{artifact_id}')
def read_artifact(token: ReadToken, artifact_id: SqlPathId):
    return _map_read_errors(read_api.read_artifact, artifact_id)


@router.get('/api/read/v1/artifacts/{artifact_id}/citations/{anchor_id}/packet')
def read_citation_packet(token: ReadToken, artifact_id: SqlPathId, anchor_id: SqlPathId):
    payload = _map_read_errors(read_api.read_citation_packet, artifact_id, anchor_id)
    return Response(content=payload, media_type='application/json', headers={'Cache-Control': 'no-store'})


@router.get('/api/read/v1/reports')
def read_reports(token: ReadToken):
    return _map_read_errors(read_api.read_reports)


@router.get('/api/read/v1/graph/export')
def read_graph_export(token: ReadToken, from_: str | None = Query(None, alias='from'), to: str | None = None, include_text: bool = True,
                      max_nodes: int = Query(loom_graph.MAX_NODES, ge=1, le=10_000_000), max_edges: int = Query(loom_graph.MAX_EDGES, ge=1, le=10_000_000)):
    try:
        return _map_read_errors(loom_graph.export_graph, from_=from_, to=to, include_text=include_text, max_nodes=max_nodes, max_edges=max_edges)
    except loom_graph.GraphInputError as exc:
        raise _error(422, 'invalid_input', str(exc)) from exc


@router.post('/api/read/v1/intents/execute')
def read_intent_execute(token: ReadToken, intent: dict = Body(...)):
    try:
        return _map_read_errors(intents.execute_intent, intent)
    except intents.IntentError as exc:
        raise _error(422, 'invalid_intent', str(exc)) from exc


@router.get('/api/read/v1/intents/schema')
def read_intent_schema(token: ReadToken):
    return intents.INTENT_SCHEMA


@router.post('/api/read/v1/intents/interpretive/validate')
def read_interpretive_validate(token: ReadToken, answer: dict = Body(...)):
    try:
        return intents.label_interpretive_answer(answer)
    except intents.InterpretiveError as exc:
        raise _error(422, 'invalid_interpretive_answer', str(exc)) from exc


# --- integrations: proposals and hypotheses ------------------------------------
@router.post('/api/integrations/v1/proposals', status_code=201)
def integration_proposal_create(token: ProposeToken, payload: dict = Body(...)):
    try:
        return proposals.submit_proposal(payload, client_id=token['client_id'] or f'token:{token["id"]}', token_id=token['id'])
    except proposals.ProposalNotFound as exc:
        raise _error(404, 'not_found', str(exc)) from exc
    except proposals.ProposalError as exc:
        raise _error(422, 'invalid_proposal', str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise _error(503, 'database_unavailable', 'Evidence database is unavailable.') from exc


@router.get('/api/integrations/v1/proposals')
def integration_proposal_list(token: ReadToken, status: str | None = None, limit: int = Query(200, ge=1, le=10_000)):
    try:
        return proposals.list_proposals(status=status, client_id=token['client_id'] or f'token:{token["id"]}', limit=limit)
    except proposals.ProposalError as exc:
        raise _error(422, 'invalid_input', str(exc)) from exc


@router.post('/api/integrations/v1/hypotheses', status_code=201)
def integration_hypotheses_import(token: ProposeToken, document: dict = Body(...)):
    try:
        return loom_graph.import_hypotheses(document, origin=token['client_id'] or f'token:{token["id"]}')
    except loom_graph.GraphInputError as exc:
        raise _error(422, 'invalid_hypotheses', str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise _error(503, 'database_unavailable', 'Evidence database is unavailable.') from exc


@router.get('/api/integrations/v1/hypotheses')
def integration_hypotheses_list(token: ReadToken, status: str | None = None, limit: int = Query(200, ge=1, le=10_000)):
    try:
        return loom_graph.list_hypotheses(status=status, limit=limit, origin=token['client_id'] or f'token:{token["id"]}')
    except loom_graph.GraphInputError as exc:
        raise _error(422, 'invalid_input', str(exc)) from exc


# --- human pages ----------------------------------------------------------------
@router.get('/integrations', response_class=HTMLResponse)
def integrations_page(request: Request):
    tokens.flush_rejection_counts()  # lazily counted repeats of rejected tokens (review 2 P2)
    return templates.TemplateResponse(request, 'integrations.html', {
        'tokens': tokens.list_tokens(), 'proposals': proposals.list_proposals(limit=500),
        'hypotheses': loom_graph.list_hypotheses(limit=500), 'require_everywhere': tokens.token_required_everywhere(),
    })


@router.post('/integrations/proposals/{proposal_id}/decide')
def integrations_decide_proposal(proposal_id: SqlPathId, decision: str = Form(...), decided_by: str = Form('ui'), note: str = Form('')):
    try:
        proposals.decide_proposal(proposal_id, decision, decided_by=decided_by, note=note or None)
    except proposals.ProposalNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except proposals.ProposalError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse('/integrations', status_code=303)


@router.post('/integrations/hypotheses/{hypothesis_id}/decide')
def integrations_decide_hypothesis(hypothesis_id: SqlPathId, decision: str = Form(...), decided_by: str = Form('ui'), note: str = Form('')):
    try:
        loom_graph.decide_hypothesis(hypothesis_id, decision, decided_by=decided_by, note=note or None)
    except loom_graph.HypothesisNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except loom_graph.GraphInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse('/integrations', status_code=303)


@router.post('/integrations/tokens/{token_id}/revoke')
def integrations_revoke_token(token_id: SqlPathId):
    try:
        tokens.revoke_token(token_id, actor='ui')
    except tokens.TokenError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse('/integrations', status_code=303)


# --- browser session (EW_REQUIRE_TOKEN) ------------------------------------------------
SESSION_COOKIE = 'ageds_token'
_SESSION_PATHS = ('/integrations/session', '/integrations/session/logout')


def _safe_next(value: str | None) -> str:
    """A local path to return to after login; anything else (other hosts, schemes) goes to /integrations."""
    if (not isinstance(value, str) or not value.startswith('/') or value.startswith(('//', '/\\')) or len(value) > 2000
            or any(ord(c) < 32 or c == '\\' for c in value)):
        return '/integrations'
    return value


def login_page(next_path: str = '/integrations', *, message: str | None = None, status_code: int = 200) -> HTMLResponse:
    """The only HTML served without a token under ``EW_REQUIRE_TOKEN``: a form, no evidence data."""
    note = f'<p class="muted">{html.escape(message)}</p>' if message else ''
    body = ('<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"><title>AGEDS — logowanie</title>'
            '<link rel="stylesheet" href="/static/app.css"></head><body><main><section class="panel">'
            '<h1>Wymagany token</h1><p>Serwer działa z <code>EW_REQUIRE_TOKEN</code>: strony, pliki raportów i '
            '<code>/api/*</code> wymagają lokalnego tokenu. Podaj token, aby ustawić ciasteczko sesji (HttpOnly, '
            f'SameSite=Strict).</p>{note}<form method="post" action="/integrations/session" class="citation-controls">'
            f'<input type="hidden" name="next" value="{html.escape(_safe_next(next_path), quote=True)}">'
            '<input name="token" type="password" autocomplete="off" aria-label="Token" placeholder="ageds_…">'
            '<button>Zaloguj</button></form></section></main></body></html>')
    return HTMLResponse(body, status_code=status_code, headers={'Cache-Control': 'no-store'})


@router.get('/integrations/session', response_class=HTMLResponse)
def integrations_session_form(next: str | None = Query(None, max_length=2000)):
    """Login form only (no data); reachable without a token."""
    return login_page(next or '/integrations')


@router.post('/integrations/session')
def integrations_session_login(token: str = Form(...), next: str | None = Form(None)):
    """Same-origin browser session: the token is checked and kept in an HttpOnly, SameSite=Strict cookie."""
    try:
        tokens.verify_token(token.strip(), scope=None)
    except tokens.TokenRejected as exc:
        return login_page(next or '/integrations', message=f'Token odrzucony: {exc.reason}.', status_code=401)
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(SESSION_COOKIE, token.strip(), httponly=True, samesite='strict', path='/')
    return response


@router.post('/integrations/session/logout')
def integrations_session_logout():
    response = RedirectResponse('/integrations', status_code=303)
    response.delete_cookie(SESSION_COOKIE, path='/', httponly=True, samesite='strict')
    return response


# --- CSRF guard and opt-in global enforcement ----------------------------------------------
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


def _headers(scope) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, value in scope.get('headers', []):
        found.setdefault(name.decode('latin-1').lower(), value.decode('latin-1'))
    return found


def _netloc(value: str) -> str | None:
    """``host[:port]`` of an absolute http(s) URL with the default port removed, else None."""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or '@' in parsed.netloc:
        return None
    netloc = parsed.netloc.lower()
    default = ':80' if parsed.scheme == 'http' else ':443'
    return netloc[:-len(default)] if netloc.endswith(default) else netloc


def _host_name(value: str | None) -> str | None:
    """Lower-case host of a ``Host`` header or netloc without the port (IPv6 without brackets); None if malformed."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text or any(c in text for c in '/@\\ \t'):
        return None
    if text.startswith('['):
        end = text.find(']')
        rest = text[end + 1:]
        if end < 0 or (rest and not (rest.startswith(':') and rest[1:].isdigit())):
            return None
        return text[1:end]
    host, sep, port = text.rpartition(':')
    if not sep:
        return text
    if not port.isdigit() or not host or ':' in host:
        return None
    return host


def _trusted_origins() -> set[str]:
    return {n for n in (_netloc(o) for o in os.environ.get('EW_TRUSTED_ORIGINS', '').split(',') if o.strip()) if n}


def host_allowed(host_header: str | None) -> bool:
    """Host allowlist against DNS rebinding (review 2 finding 5).

    Allowed: ``localhost``; any IP literal (``127.0.0.1``, ``[::1]``, the Android emulator's
    ``10.0.2.2``, a LAN address) — a rebinding page always sends the attacker's *hostname*,
    never an IP literal; the hosts of ``EW_TRUSTED_ORIGINS``; and ``EW_TRUSTED_HOSTS``
    (comma-separated names, ``*.suffix`` wildcards, or ``*`` to switch the check off).
    A request without ``Host`` (HTTP/1.0 tools) is not a browser request and passes.
    """
    if host_header is None:
        return True
    host = _host_name(host_header)
    if host is None:
        return False
    if host == 'localhost':
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    for entry in os.environ.get('EW_TRUSTED_HOSTS', '').split(','):
        entry = entry.strip().lower()
        if entry == '*' or (entry.startswith('*.') and host.endswith(entry[1:])) or (entry and _host_name(entry) == host):
            return True
    return host in {_host_name(n) for n in _trusted_origins()}


def _trusted(value: str, host: str | None) -> bool:
    """``Origin``/``Referer`` names an allowlisted host and is this server (same netloc), or a trusted origin."""
    origin = _netloc(value)
    if origin is None:
        return False
    if origin in _trusted_origins():
        return True
    own = (host or '').strip().lower()
    for default in (':80', ':443'):
        if own.endswith(default):
            own = own[:-len(default)]
    return origin == own and host_allowed(origin)


def csrf_refusal(method: str, headers: dict[str, str]) -> str | None:
    """Why a state-changing browser request is refused as cross-site, or None (review finding 6).

    Applies to every non-GET/HEAD/OPTIONS request without an ``Authorization``
    header (a header a cross-site page cannot send without a CORS preflight,
    which this server never grants). ``Sec-Fetch-Site: cross-site`` is refused;
    otherwise ``Origin`` (or, without it, ``Referer``) must name an allowlisted host
    (``host_allowed``) that is this server (same netloc as ``Host``, itself
    allowlisted before) or an origin in ``EW_TRUSTED_ORIGINS`` (review 2 finding 5).
    Requests without these headers (CLI, curl, scripts) are allowed.
    """
    if method in SAFE_METHODS or 'authorization' in headers:
        return None
    if headers.get('sec-fetch-site', '').strip().lower() == 'cross-site':
        return 'cross-site request (Sec-Fetch-Site)'
    host = headers.get('host')
    if 'origin' in headers:
        return None if _trusted(headers['origin'], host) else 'Origin is not this server'
    if 'referer' in headers and not _trusted(headers['referer'], host):
        return 'Referer is not this server'
    return None


OPEN_PATHS = ('/favicon.ico',)
OPEN_PREFIXES = ('/static/',)


def _needs_token(path: str, method: str) -> bool:
    """Under ``EW_REQUIRE_TOKEN`` every route needs a token except the login form and static assets.

    Review 2 finding 6: HTML pages render evidence (search snippets, report files, the
    integrations page), so they are not exempt; the empty-shell pages are protected too, since
    their data calls need the same token and nothing is gained by leaving them open.
    """
    if path.startswith(_OWN_PREFIXES):
        return False  # scoped bearer checks in the routes
    if path in _SESSION_PATHS or path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
        return False  # the login form itself, CSS/JS/icons
    return True


def _session_token(headers: dict[str, str]) -> str | None:
    if 'cookie' not in headers:
        return None
    jar = SimpleCookie()
    try:
        jar.load(headers['cookie'])
    except CookieError:
        return None
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel is not None else None


class RequireTokenMiddleware:
    """Pure ASGI: CSRF guard always; token enforcement when ``EW_REQUIRE_TOKEN`` is on.

    Every request first passes the Host allowlist (``host_allowed``). With
    enforcement every request except the login form and static assets needs a
    valid token, sent as ``Authorization: Bearer`` or as the HttpOnly session
    cookie set by ``POST /integrations/session``; a browser page request without
    one gets the login form (401). Routes owned by this area verify scoped bearer
    tokens themselves and are skipped here. Streaming responses are untouched
    because the middleware never wraps ``send``.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            method = scope.get('method', 'GET').upper()
            headers = _headers(scope)
            if not host_allowed(headers.get('host')):
                response = JSONResponse({'detail': {'code': 'untrusted_host', 'message': (
                    'Host header is not allowed (DNS rebinding guard): use localhost or an IP address, or list the '
                    'name in EW_TRUSTED_HOSTS / EW_TRUSTED_ORIGINS')}}, status_code=400)
                await response(scope, receive, send)
                return
            refusal = csrf_refusal(method, headers)
            if refusal is not None:
                response = JSONResponse({'detail': {'code': 'cross_site_request', 'message': refusal}}, status_code=403)
                await response(scope, receive, send)
                return
            path = scope.get('path', '')
            if tokens.token_required_everywhere() and _needs_token(path, method):
                header = headers.get('authorization')
                bearer = tokens.parse_bearer(header) if header is not None else _session_token(headers)
                try:
                    await to_thread.run_sync(lambda: tokens.verify_token(bearer, scope=None))
                except tokens.TokenRejected as exc:
                    if method in ('GET', 'HEAD') and not path.startswith(('/api/', '/export/')) \
                            and 'authorization' not in headers:
                        query = scope.get('query_string', b'').decode('latin-1')
                        response = login_page(path + (f'?{query}' if query else ''), status_code=401,
                                              message=None if exc.reason == 'missing_token' else f'Token odrzucony: {exc.reason}.')
                        response.headers['WWW-Authenticate'] = 'Bearer'
                    else:
                        response = JSONResponse({'detail': {'code': exc.reason, 'message': 'A valid local token is required (EW_REQUIRE_TOKEN).'}},
                                                status_code=exc.status, headers={'WWW-Authenticate': 'Bearer'})
                    await response(scope, receive, send)
                    return
                except (OSError, sqlite3.Error):
                    response = JSONResponse({'detail': {'code': 'database_unavailable', 'message': 'Evidence database is unavailable for authentication.'}}, status_code=503)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def install_app(app) -> None:
    app.add_middleware(RequireTokenMiddleware)

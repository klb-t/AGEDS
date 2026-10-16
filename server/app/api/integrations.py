"""HTTP routes owned by the integrations area.

``/api/read/v1/*`` (read scope) and ``/api/integrations/v1/*`` (propose scope
for writes) always require ``Authorization: Bearer <token>``. Their error model
is ``{"detail": {"code": "...", "message": "..."}}``; 401 carries
``WWW-Authenticate: Bearer``. The human pages under ``/integrations`` follow the
rest of the workbench UI (trusted network).

``install_app`` adds one middleware (``api.include_routers`` calls it): a CSRF
guard for every state-changing request (review finding 6) and the opt-in global
enforcement (``EW_REQUIRE_TOKEN=1``): every other ``/api/*`` and ``/export/*``
route and every state-changing route then needs a valid token (bearer header
or the browser session cookie from ``POST /integrations/session``), without
scope checks.
"""
from __future__ import annotations

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


@router.post('/integrations/session')
def integrations_session_login(token: str = Form(...)):
    """Same-origin browser session: the token is checked and kept in an HttpOnly, SameSite=Strict cookie."""
    try:
        tokens.verify_token(token.strip(), scope=None)
    except tokens.TokenRejected as exc:
        return HTMLResponse(f'<p>Token odrzucony: {exc.reason}.</p><p><a href="/integrations">Wróć</a></p>', status_code=401)
    response = RedirectResponse('/integrations', status_code=303)
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


def _trusted(value: str, host: str | None) -> bool:
    origin = _netloc(value)
    if origin is None:
        return False
    own = (host or '').strip().lower()
    for default in (':80', ':443'):
        if own.endswith(default):
            own = own[:-len(default)]
    if origin == own:
        return True
    extra = [o.strip() for o in os.environ.get('EW_TRUSTED_ORIGINS', '').split(',') if o.strip()]
    return origin in {_netloc(o) for o in extra}


def csrf_refusal(method: str, headers: dict[str, str]) -> str | None:
    """Why a state-changing browser request is refused as cross-site, or None (review finding 6).

    Applies to every non-GET/HEAD/OPTIONS request without an ``Authorization``
    header (a header a cross-site page cannot send without a CORS preflight,
    which this server never grants). ``Sec-Fetch-Site: cross-site`` is refused;
    otherwise ``Origin`` (or, without it, ``Referer``) must name this server
    (``Host``) or an origin in ``EW_TRUSTED_ORIGINS``. Requests without these
    headers (CLI, curl, scripts) are allowed.
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


def _needs_token(path: str, method: str) -> bool:
    if path.startswith(_OWN_PREFIXES) or path in _SESSION_PATHS:
        return False  # scoped bearer checks in the routes / the login form itself
    return path.startswith(('/api/', '/export/')) or method not in SAFE_METHODS


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

    With enforcement every ``/api/*`` and ``/export/*`` request and every
    state-changing request needs a valid token, sent as ``Authorization: Bearer``
    or as the HttpOnly session cookie set by ``POST /integrations/session``.
    Routes owned by this area verify scoped bearer tokens themselves and are
    skipped here. Streaming responses are untouched because the middleware
    never wraps ``send``.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            method = scope.get('method', 'GET').upper()
            headers = _headers(scope)
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

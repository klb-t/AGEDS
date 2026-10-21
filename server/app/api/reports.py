"""HTTP routes owned by the reports area.

Report creation over HTTP writes only below the operator-configured private
directory ``EW_REPORTS_DIR`` (plus an optional relative subdirectory); without
it HTTP creation is disabled (403) and the CLI remains available. Files are
served only for registered reports, only when listed in a manifest whose bytes
still match the register digest, and only after their own SHA-256 matches.
The HTML preview is served with a sandboxing Content-Security-Policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi import Path as PathParameter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..reports import definitions
from ..reports.common import ReportInputError, ReportNotFound, ReportOutputRefused
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/reports', 'Raporty')]
MAX_SQL_INTEGER = 2**63 - 1
SqlPathId = Annotated[int, PathParameter(ge=1, le=MAX_SQL_INTEGER)]
REPORTS_DIR_ENV = 'EW_REPORTS_DIR'
MAX_SERVED_BYTES = 256 * 1024 * 1024
_SUBDIR = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*')
PREVIEW_CSP = ("sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
               "form-action 'none'; frame-ancestors 'self'")


def reports_root() -> Path | None:
    value = os.environ.get(REPORTS_DIR_ENV, '').strip()
    return Path(value) if value else None


def _output_dir(subdir: str | None) -> Path:
    root = reports_root()
    if root is None:
        raise HTTPException(403, f'report output over HTTP is disabled: the operator must configure {REPORTS_DIR_ENV} '
                                 '(or use the report-create CLI with --output)')
    if not root.is_absolute():
        raise HTTPException(403, f'{REPORTS_DIR_ENV} must be an absolute path')
    subdir = (subdir or '').strip().strip('/')
    if subdir and (not _SUBDIR.fullmatch(subdir) or '..' in subdir.split('/')):
        raise HTTPException(422, 'output_subdir must be a relative path of letters, digits, _ . -')
    return root / subdir if subdir else root


def _create(report_type: str, params: dict, subdir: str | None) -> dict:
    from ..reports.service import create_report
    output = _output_dir(subdir)
    try:
        return create_report(report_type, params, output)
    except ReportInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ReportOutputRefused as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc


def _registered(report_id: int) -> dict:
    from ..reports.service import registered
    try:
        return registered(report_id)
    except ReportNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get('/api/reports/types')
def api_report_types():
    return definitions.describe_types()


@router.get('/api/reports')
def api_reports(report_type: str | None = Query(None, alias='type', max_length=64), limit: int = Query(100, ge=1, le=1000)):
    from ..reports.service import listing
    return {'reports': listing(report_type=report_type, limit=limit), 'output_enabled': reports_root() is not None}


@router.post('/api/reports', status_code=201)
def api_report_create(body: dict = Body(...)):
    unknown = sorted(set(body) - {'type', 'params', 'output_subdir'})
    if unknown or not isinstance(body.get('type'), str):
        raise HTTPException(422, f'expected {{"type", "params", "output_subdir"}}; unknown: {unknown}')
    params = body.get('params') or {}
    if not isinstance(params, dict):
        raise HTTPException(422, 'params must be an object')
    return _create(body['type'], params, body.get('output_subdir'))


@router.get('/api/reports/{report_id}')
def api_report(report_id: SqlPathId):
    return _registered(report_id)


@router.get('/api/reports/{report_id}/verify')
def api_report_verify(report_id: SqlPathId):
    from ..reports.service import verify_registered
    try:
        return verify_registered(report_id)
    except ReportNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


def _verified_file(report_id: int, relative: str) -> tuple[bytes, dict]:
    from ..reports.service import report_file
    try:
        path, entry = report_file(report_id, relative)
    except ReportNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f'report files are not readable: {exc}') from exc
    if entry['size_bytes'] > MAX_SERVED_BYTES:
        raise HTTPException(413, 'report file exceeds the serving limit')
    from ..reports.bundle_consumer import read_regular
    try:
        data = read_regular(path, MAX_SERVED_BYTES)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f'report file is not readable: {exc}') from exc
    if hashlib.sha256(data).hexdigest() != entry['sha256'] or len(data) != entry['size_bytes']:
        raise HTTPException(409, 'report file differs from its manifest; not served')
    return data, entry


@router.get('/reports/{report_id}/files/{relative:path}')
def report_download(report_id: SqlPathId, relative: str):
    data, entry = _verified_file(report_id, relative)
    name = f'ageds-report-{report_id}-' + relative.replace('/', '_')
    return Response(content=data, media_type=entry['media_type'], headers={
        'Content-Disposition': f'attachment; filename="{name}"', 'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': PREVIEW_CSP})


@router.get('/reports/{report_id}/view/{relative:path}')
def report_view(report_id: SqlPathId, relative: str):
    """Inline view for the sandboxed preview; relative links inside the report resolve here."""
    data, entry = _verified_file(report_id, relative)
    media_type = entry['media_type'] + ('; charset=utf-8' if entry['media_type'].startswith(('text/', 'application/json')) else '')
    return Response(content=data, media_type=media_type, headers={
        'Content-Security-Policy': PREVIEW_CSP, 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
        'Referrer-Policy': 'no-referrer', 'Content-Disposition': 'inline'})


@router.get('/reports/{report_id}/manifest')
def report_manifest(report_id: SqlPathId):
    from ..reports.bundle_consumer import read_regular
    row = _registered(report_id)
    try:
        data = read_regular(Path(row['output_dir']) / row['manifest_name'], 64 * 1024 * 1024)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f'manifest is not readable: {exc}') from exc
    if hashlib.sha256(data).hexdigest() != row['manifest_sha256']:
        raise HTTPException(409, 'manifest differs from the registered SHA-256; not served')
    return Response(content=data, media_type='application/json', headers={
        'Content-Disposition': f'attachment; filename="ageds-report-{report_id}-manifest.json"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


def _page(request: Request, *, prefill: dict | None = None, error: str | None = None, status: int = 200):
    from ..reports.service import listing
    return templates.TemplateResponse(request, 'reports/page_index.html', {
        'reports': listing(limit=200), 'types': definitions.describe_types(), 'output_enabled': reports_root() is not None,
        'reports_dir_env': REPORTS_DIR_ENV, 'prefill': prefill or {}, 'error': error,
    }, status_code=status)


@router.get('/reports', response_class=HTMLResponse)
def reports_page(request: Request):
    return _page(request)


@router.get('/reports/new', response_class=HTMLResponse)
def reports_new(request: Request, from_: str | None = Query(None, alias='from', max_length=64),
                to: str | None = Query(None, max_length=64), report_type: str = Query('timeline', alias='type', max_length=64),
                counterparty: str | None = Query(None, max_length=500), topic: str | None = Query(None, max_length=500)):
    """Prefilled creation form; the timeline view links here with its range."""
    if report_type not in definitions.TYPES:
        report_type = 'timeline'
    return _page(request, prefill={'type': report_type, 'from': from_ or '', 'to': to or '',
                                   'counterparty': counterparty or '', 'topic': topic or ''})


def _form_params(form) -> tuple[str, dict, str | None]:
    report_type = str(form.get('type') or '')
    extra = str(form.get('params_json') or '').strip()
    try:
        params = json.loads(extra) if extra else {}
    except ValueError as exc:
        raise ReportInputError(f'additional parameters are not valid JSON: {exc}') from exc
    if not isinstance(params, dict):
        raise ReportInputError('additional parameters must be a JSON object')
    for name in ('from', 'to', 'counterparty', 'topic', 'assume_zone', 'assumption_rule', 'title', 'language', 'group_by'):
        value = str(form.get(name) or '').strip()
        if value:
            params[name] = value
    for name in ('collection_id', 'budget_run_id', 'hypothesis_id', 'max_rows', 'quote_chars', 'min_gap_days'):
        value = str(form.get(name) or '').strip()
        if value:
            try:
                params[name] = int(value)
            except ValueError as exc:
                raise ReportInputError(f'{name} must be an integer') from exc
    for name in ('event_types', 'anchor_ids'):
        value = str(form.get(name) or '').strip()
        if value:
            items = [item.strip() for item in value.split(',') if item.strip()]
            try:
                params[name] = [int(item) for item in items] if name == 'anchor_ids' else items
            except ValueError as exc:
                raise ReportInputError('anchor_ids must be comma-separated integers') from exc
    questions = [line.strip() for line in str(form.get('open_questions') or '').splitlines() if line.strip()]
    if questions:
        params['open_questions'] = questions
    if form.get('include_hidden_chat_parts'):
        params['include_hidden_chat_parts'] = True
    formats = [str(item) for item in form.getlist('formats')]
    if formats:
        params['formats'] = sorted(set(formats) | set(definitions.REQUIRED_FORMATS), key=definitions.FORMATS.index)
    return report_type, params, str(form.get('output_subdir') or '').strip() or None


@router.post('/reports/create')
async def reports_create(request: Request):
    form = await request.form()
    prefill = {key: str(form.get(key) or '') for key in ('type', 'from', 'to', 'counterparty', 'topic')}
    try:
        report_type, params, subdir = _form_params(form)
        result = await run_in_threadpool(_create, report_type, params, subdir)
    except ReportInputError as exc:
        return _page(request, prefill=prefill, error=str(exc), status=422)
    except HTTPException as exc:
        return _page(request, prefill=prefill, error=str(exc.detail), status=exc.status_code)
    return RedirectResponse(f'/reports/{result["report_id"]}', status_code=303)


@router.get('/reports/{report_id}', response_class=HTMLResponse)
def report_page(request: Request, report_id: SqlPathId):
    from ..reports.service import verify_registered
    row = _registered(report_id)
    verification = verify_registered(report_id)
    files = []
    if verification['digest_valid']:
        from ..reports.bundle_consumer import read_regular, strict_json
        try:
            manifest = strict_json(read_regular(Path(row['output_dir']) / row['manifest_name'], 64 * 1024 * 1024))
            files = manifest.get('files', [])
        except (OSError, ValueError):
            files = []
    return templates.TemplateResponse(request, 'reports/page_detail.html', {
        'report': row, 'verification': verification, 'files': files})

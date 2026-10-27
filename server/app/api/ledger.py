"""HTTP routes owned by the ledger area (financial, health and transport records).

Page ``/ledger`` (nav „Rejestry”) shows statements with their code-computed
balance checks, exact totals from integer minor units, entries (account numbers
masked for display by default; ``?mask=0`` shows them raw) and health sessions.
Imports: ``POST /api/ledger/import/{kind}`` (upload, private temporary copy) and
``POST /api/ledger/import/{kind}/path`` (confined to ``EW_SCAN_ROOTS``), kinds
``tabular``, ``camt053``, ``mt940``, ``health``. Health imports return 403
without ``enable_health``; conflicts 409; invalid input 422. Token enforcement
and CSRF checks come from the shared middleware (``api/integrations.py``).
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from ..evidence import IntegrityError, temporary_copy_acquisitions
from ..ledger import queries
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/ledger', 'Rejestry')]
PREFIX = '/api/ledger'
KINDS = ('tabular', 'camt053', 'mt940', 'health')


class PathImport(BaseModel):
    rootId: int = Field(ge=0)
    relativePath: str = Field(min_length=1, max_length=4096)
    label: str | None = Field(default=None, max_length=200)
    collectionId: int | None = None
    profile: str | None = Field(default=None, max_length=200)
    encoding: str | None = Field(default=None, max_length=40)
    twoDigitYearBase: int = Field(default=2000, ge=1900, le=2100)
    enableHealth: bool = False
    format: str = Field(default='auto', max_length=20)


def _run(kind: str, path: Path, *, locator: str, original_name: str | None, label: str | None,
         collection_id: int | None, profile: str | None = None, encoding: str | None = None,
         two_digit_year_base: int = 2000, enable_health: bool = False, fmt: str = 'auto') -> dict:
    common = {'source_locator': locator, 'original_name': original_name, 'collection_id': collection_id, 'label': label}
    try:
        if kind == 'tabular':
            from ..importers.tabular_profiles import import_tabular
            return import_tabular(path, profile_id=profile or None, encoding=encoding or None, **common)
        if kind == 'camt053':
            from ..importers.camt053 import import_camt053
            return import_camt053(path, **common)
        if kind == 'mt940':
            from ..importers.mt940 import import_mt940
            return import_mt940(path, two_digit_year_base=two_digit_year_base, encoding=encoding or None, **common)
        if kind == 'health':
            from ..commands.ledger import HEALTH_FORMATS, run_health
            if fmt not in HEALTH_FORMATS:
                raise ValueError(f'format must be one of {HEALTH_FORMATS}')
            return run_health(path, fmt, enable_health=enable_health is True, **common)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    raise HTTPException(404, 'unknown import kind')


@router.post(PREFIX + '/import/{kind}')
def upload_import(kind: str, file: UploadFile = File(...), label: str | None = Form(None),
                  collection_id: int | None = Form(None), profile: str | None = Form(None),
                  encoding: str | None = Form(None), two_digit_year_base: int = Form(2000),
                  enable_health: bool = Form(False), format: str = Form('auto')):
    if kind not in KINDS:
        raise HTTPException(404, 'unknown import kind')
    if kind == 'health' and enable_health is not True:
        raise HTTPException(403, 'health import needs enable_health (special-category data, local processing only)')
    name = Path(file.filename or 'upload').name
    with tempfile.TemporaryDirectory(prefix='ageds-upload-') as directory:
        path = Path(directory) / ('upload' + ''.join(Path(name).suffixes)[-20:])
        with path.open('wb') as handle:
            shutil.copyfileobj(file.file, handle)
        with temporary_copy_acquisitions():
            return _run(kind, path, locator=f'upload:ledger:{kind}:{name}', original_name=name, label=label,
                        collection_id=collection_id, profile=profile, encoding=encoding,
                        two_digit_year_base=two_digit_year_base, enable_health=enable_health, fmt=format)


@router.post(PREFIX + '/import/{kind}/path')
def path_import(kind: str, request: PathImport):
    if kind not in KINDS:
        raise HTTPException(404, 'unknown import kind')
    if kind == 'health' and request.enableHealth is not True:
        raise HTTPException(403, 'health import needs enableHealth (special-category data, local processing only)')
    from .imports_comm import _resolve
    target = _resolve(request.rootId, request.relativePath)
    if target.is_dir():
        raise HTTPException(422, 'this importer expects a file')
    return _run(kind, target, locator=str(target), original_name=None, label=request.label,
                collection_id=request.collectionId, profile=request.profile, encoding=request.encoding,
                two_digit_year_base=request.twoDigitYearBase, enable_health=request.enableHealth, fmt=request.format)


@router.get(PREFIX + '/overview')
def overview():
    return queries.overview()


@router.get(PREFIX + '/profiles')
def profiles():
    return queries.profiles()


@router.get(PREFIX + '/entries')
def entries(artifact_id: int | None = None, record_kind: str | None = Query(None, max_length=40),
            limit: int = Query(100, ge=1, le=1000), after_id: int = Query(0, ge=0), mask_accounts: bool = False):
    return queries.entries(artifact_id=artifact_id, record_kind=record_kind, limit=limit, after_id=after_id,
                           mask_accounts=mask_accounts)


@router.get(PREFIX + '/totals')
def totals(artifact_id: int | None = None):
    return queries.totals(artifact_id=artifact_id)


@router.get(PREFIX + '/statements')
def statements(artifact_id: int | None = None):
    return queries.statements(artifact_id=artifact_id)


@router.get(PREFIX + '/health/sessions')
def health_sessions(artifact_id: int | None = None, limit: int = Query(100, ge=1, le=1000)):
    return queries.health_sessions(artifact_id=artifact_id, limit=limit)


@router.get('/ledger')
def ledger_page(request: Request, mask: int = Query(1, ge=0, le=1), kind: str | None = Query(None, max_length=40),
                after: int = Query(0, ge=0)):
    masked = mask == 1
    listing = queries.entries(record_kind=kind or None, limit=100, after_id=after, mask_accounts=masked)
    statement_rows = queries.statements()['items']
    if masked:
        from ..ledger.store import mask_account
        for row in statement_rows:
            row['account_raw'] = mask_account(row['account_raw'])
    context = {'overview': queries.overview(), 'statements': statement_rows, 'totals': queries.totals()['rows'],
               'entries': listing, 'sessions': queries.health_sessions(limit=50)['items'],
               'profiles': queries.profiles(), 'masked': masked, 'kind': kind or '', 'after': after}
    return templates.TemplateResponse(request, 'ledger.html', context)

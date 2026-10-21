"""HTTP routes owned by the traces area (personal-data exports, photo/document metadata).

Uploads go to a private temporary directory under their sanitized file name
(name rules such as Google Photos sidecar names depend on it), are acquired into
the store and deleted. Server-side path imports are confined to the operator's
``EW_SCAN_ROOTS`` (same rule as the other import routes). Opt-in data classes
(location, browser history) return 403 unless the request enables them. The API
has no authentication of its own; see HANDOFF.md (read-API tokens).
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..evidence import IntegrityError, temporary_copy_acquisitions
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/traces', 'Ślady')]
PREFIX = '/api/traces'


class PathImport(BaseModel):
    rootId: int = Field(ge=0)
    relativePath: str = Field(min_length=1, max_length=4096)
    label: str | None = Field(default=None, max_length=200)
    collectionId: int | None = None
    enableLocation: bool = False
    coordinateDecimals: int | None = Field(default=None, ge=0, le=8)
    enableBrowserHistory: bool = False
    ownerName: str | None = Field(default=None, max_length=300)
    ownerId: str | None = Field(default=None, max_length=300)
    exportLabel: str | None = Field(default=None, max_length=200)
    sessionGapMinutes: int | None = Field(default=None, ge=1, le=1440)


def _run(kind: str, path: Path, **options) -> dict:
    from ..traces import dispatch
    if kind not in dispatch.KINDS:
        raise HTTPException(404, 'unknown trace import kind')
    try:
        return {'kind': kind, **dispatch.run(kind, path, **options)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get(PREFIX + '/kinds')
def kinds():
    from ..traces import dispatch
    return {'kinds': dispatch.describe()}


@router.post(PREFIX + '/import/{kind}')
def upload_import(kind: str, file: UploadFile = File(...), label: str | None = Form(None),
                  collection_id: int | None = Form(None), enable_location: bool = Form(False),
                  coordinate_decimals: int | None = Form(None), enable_browser_history: bool = Form(False),
                  owner_name: str | None = Form(None), owner_id: str | None = Form(None),
                  export_label: str | None = Form(None)):
    from ..importers.comm_common import safe_name
    from ..traces import dispatch
    if kind not in dispatch.KINDS:
        raise HTTPException(404, 'unknown trace import kind')
    name = safe_name(file.filename, 'upload')
    with tempfile.TemporaryDirectory(prefix='ageds-traces-upload-') as directory:
        path = Path(directory) / name
        with path.open('wb') as handle:
            shutil.copyfileobj(file.file, handle)
        options = {'label': label, 'collection_id': collection_id, 'enable_location': enable_location,
                   'coordinate_decimals': coordinate_decimals, 'enable_browser_history': enable_browser_history,
                   'owner_name': owner_name, 'owner_id': owner_id, 'export_label': export_label,
                   'source_locator': f'upload:traces:{kind}:{name}'}
        if 'original_name' in dispatch.KINDS[kind].accepts:
            options['original_name'] = name
        with temporary_copy_acquisitions():  # the private copy's mtime is the upload instant
            return _run(kind, path, **options)


def _resolve(root_id: int, relative_path: str) -> Path:
    if not settings.scan_roots:
        raise HTTPException(403, 'path imports disabled: operator must configure EW_SCAN_ROOTS')
    if not 0 <= root_id < len(settings.scan_roots):
        raise HTTPException(404, 'source root not found')
    relative = Path(relative_path)
    if relative.is_absolute() or '..' in relative.parts:
        raise HTTPException(422, 'relativePath must stay within the configured source root')
    root = settings.scan_roots[root_id].resolve()
    target = root / relative
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise HTTPException(422, 'source path escapes configured root')
    if not target.exists():
        raise HTTPException(404, 'source path not found')
    return target


@router.post(PREFIX + '/import/{kind}/path')
def path_import(kind: str, request: PathImport):
    target = _resolve(request.rootId, request.relativePath)
    return _run(kind, target, label=request.label, collection_id=request.collectionId,
                enable_location=request.enableLocation, coordinate_decimals=request.coordinateDecimals,
                enable_browser_history=request.enableBrowserHistory, owner_name=request.ownerName,
                owner_id=request.ownerId, export_label=request.exportLabel,
                session_gap_minutes=request.sessionGapMinutes)


@router.get(PREFIX + '/summary')
def summary():
    from ..traces import queries
    return queries.summary()


@router.get(PREFIX + '/geotags')
def geotags(limit: int = Query(100, ge=1, le=1000), after_id: int = Query(0, ge=0)):
    from ..traces import queries
    return queries.geotag_rows(limit=limit, after_id=after_id)


@router.get(PREFIX + '/photo-sidecars')
def photo_sidecars(limit: int = Query(100, ge=1, le=1000), after_id: int = Query(0, ge=0),
                   include_people: bool = False):
    from ..traces import queries
    return queries.photo_sidecars(limit=limit, after_id=after_id, include_people=include_people)


@router.get(PREFIX + '/documents')
def documents(limit: int = Query(100, ge=1, le=1000), after_id: int = Query(0, ge=0)):
    from ..traces import queries
    return queries.documents(limit=limit, after_id=after_id)


@router.get(PREFIX + '/browser-visits')
def browser_visits(limit: int = Query(100, ge=1, le=1000), after_id: int = Query(0, ge=0)):
    from ..traces import queries
    return queries.browser_visits(limit=limit, after_id=after_id)


@router.get('/traces', response_class=HTMLResponse)
def traces_page(request: Request):
    from ..traces import dispatch, queries
    return templates.TemplateResponse(request, 'traces.html', {'summary': queries.summary(),
                                                               'kinds': dispatch.describe()})

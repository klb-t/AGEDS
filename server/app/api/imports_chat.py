"""HTTP upload endpoints for AI chat exports and the logical dedup report.

Uploads are copied to a private temporary directory (bounded size), imported
under a stable ``upload:<namespace>:<file name>`` locator and removed. Extra
uploaded files are attachment candidates for ChatGPT file-ID references.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

router = APIRouter()
NAV: list[tuple[str, str]] = []

MAX_UPLOAD_BYTES = 4 * 1024 ** 3
COPY_CHUNK = 1024 * 1024


def _safe_name(filename: str | None) -> str:
    name = Path(filename or '').name
    if not name or name in ('.', '..') or len(name.encode('utf-8')) > 255:
        raise HTTPException(422, 'each upload needs a plain file name')
    return name


def _receive(files: list[UploadFile], directory: Path) -> list[Path]:
    paths = []
    for upload in files:
        name = _safe_name(upload.filename)
        target = directory / name
        if target.exists():
            raise HTTPException(422, f'duplicate upload file name: {name}')
        written = 0
        with target.open('wb') as out:
            while chunk := upload.file.read(COPY_CHUNK):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f'upload exceeds {MAX_UPLOAD_BYTES} bytes: {name}')
                out.write(chunk)
        paths.append(target)
    return paths


def _import(namespace: str, files: list[UploadFile], export_label: str, collection_id: int | None) -> dict:
    from ..evidence import IntegrityError, temporary_copy_acquisitions
    if namespace == 'chatgpt':
        from ..importers.chatgpt import import_chatgpt_export as run
    else:
        from ..importers.claude_ai import import_claude_export as run
    directory = Path(tempfile.mkdtemp(prefix='ageds-chat-upload-'))
    try:
        paths = _receive(files, directory)
        try:
            with temporary_copy_acquisitions():  # the private copies' mtimes are the upload instant
                return run(paths, export_label=export_label or None, collection_id=collection_id,
                           locator_for=lambda p: f'upload:{namespace}:{p.name}')
        except IntegrityError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@router.post('/api/imports/chatgpt')
def import_chatgpt_upload(files: list[UploadFile] = File(...), export_label: str = Form(''),
                          collection_id: int | None = Form(None)):
    return _import('chatgpt', files, export_label, collection_id)


@router.post('/api/imports/claude')
def import_claude_upload(files: list[UploadFile] = File(...), export_label: str = Form(''),
                         collection_id: int | None = Form(None)):
    return _import('claude', files, export_label, collection_id)


@router.get('/api/imports/chat/dedup-report')
def chat_dedup_report(namespace: str | None = Query(None), conversation_id: str | None = Query(None),
                      interpretation: str | None = Query(None), changes_only: bool = Query(False),
                      limit: int = Query(100, ge=1, le=10_000), offset: int = Query(0, ge=0)):
    from ..records.report import dedup_report
    try:
        return dedup_report(namespace=namespace, conversation_id=conversation_id, interpretation=interpretation,
                            changes_only=changes_only, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

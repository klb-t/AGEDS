"""HTTP routes for communication, Takeout and media importers (task B part 2).

Uploads are written to a private temporary file, acquired into the store and
deleted. Server-side path imports are limited to the operator's configured
``EW_SCAN_ROOTS`` (same rule as ``/api/source-scans``). Opt-in importers return
403 unless their flag is set in the request. The API has no authentication;
operate it on a trusted network (see HANDOFF.md).
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from ..config import settings
from ..db import session
from ..evidence import IntegrityError

router = APIRouter()
NAV: list[tuple[str, str]] = []
PREFIX = '/api/imports/comm'
KINDS = ('email', 'ics', 'vcf', 'calls-xml', 'billing', 'takeout-activity', 'location-history', 'browser-history',
         'whatsapp-export', 'exif')


class PathImport(BaseModel):
    rootId: int = Field(ge=0)
    relativePath: str = Field(min_length=1, max_length=4096)
    label: str | None = Field(default=None, max_length=200)
    collectionId: int | None = None
    enableBrowserHistory: bool = False
    enableLocationHistory: bool = False
    coarseDecimals: int = Field(default=2, ge=0, le=4)
    profile: str | None = Field(default=None, max_length=200)
    chatMember: str | None = Field(default=None, max_length=1024)


class NamesIn(BaseModel):
    names: list[str] = Field(min_length=1, max_length=10000)
    label: str | None = Field(default=None, max_length=200)


def _run(kind: str, path: Path, *, locator: str, original_name: str | None, label: str | None,
         collection_id: int | None, enable_browser_history: bool = False, enable_location_history: bool = False,
         coarse_decimals: int = 2, profile: str | None = None, chat_member: str | None = None) -> dict:
    from ..importers import (billing, browser_history, call_log, email_mbox, exif, ics, location, takeout_activity,
                             vcf, whatsapp_media)
    common = {'source_locator': locator, 'original_name': original_name, 'collection_id': collection_id,
              'label': label}
    try:
        if kind == 'email':
            with path.open('rb') as handle:
                is_mbox = handle.read(5) == b'From '
            return (email_mbox.import_mbox if is_mbox else email_mbox.import_eml)(path, **common)
        if kind == 'ics':
            return ics.import_ics(path, **common)
        if kind == 'vcf':
            return vcf.import_vcf(path, **common)
        if kind == 'calls-xml':
            return call_log.import_calls_xml(path, **common)
        if kind == 'billing':
            return billing.import_billing(path, profile_id=profile, **common)
        if kind == 'takeout-activity':
            return takeout_activity.import_my_activity(path, enable_browser_history=enable_browser_history, **common)
        if kind == 'location-history':
            return location.import_location_history(path, enable_location=enable_location_history,
                                                    coarse_decimals=coarse_decimals, **common)
        if kind == 'browser-history':
            return browser_history.import_browser_history(path, enable_browser_history=enable_browser_history, **common)
        if kind == 'whatsapp-export':
            return whatsapp_media.import_whatsapp_export(path, chat_label=label, chat_member=chat_member,
                                                         source_locator=locator, collection_id=collection_id)
        if kind == 'exif':
            from ..importers.comm_common import acquire
            _, artifact_id = acquire(path, source_kind='image_files', source_label=label or 'Image files',
                                     locator=locator, importer=exif.PARSER_NAME, parser_version=exif.PARSER_VERSION,
                                     original_name=original_name, collection_id=collection_id)
            return exif.observe_artifact(artifact_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    raise HTTPException(404, 'unknown import kind')


# Registered before the generic upload route so the literal path wins.
@router.post(PREFIX + '/recording-names')
def recording_names_from_list(request: NamesIn):
    from ..importers import recording_names
    return recording_names.observe_names(request.names, label=request.label, locator_prefix='api-names')


@router.post(PREFIX + '/{kind}')
def upload_import(kind: str, file: UploadFile = File(...), label: str | None = Form(None),
                        collection_id: int | None = Form(None), enable_browser_history: bool = Form(False),
                        enable_location_history: bool = Form(False), coarse_decimals: int = Form(2),
                        profile: str | None = Form(None), chat_member: str | None = Form(None)):
    if kind not in KINDS:
        raise HTTPException(404, 'unknown import kind')
    name = Path(file.filename or 'upload').name
    with tempfile.TemporaryDirectory(prefix='ageds-upload-') as directory:
        path = Path(directory) / ('upload' + ''.join(Path(name).suffixes)[-20:])
        with path.open('wb') as handle:
            shutil.copyfileobj(file.file, handle)
        return _run(kind, path, locator=f'upload:{kind}:{name}', original_name=name, label=label,
                    collection_id=collection_id, enable_browser_history=enable_browser_history,
                    enable_location_history=enable_location_history, coarse_decimals=coarse_decimals,
                    profile=profile, chat_member=chat_member)


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


@router.post(PREFIX + '/{kind}/path')
def path_import(kind: str, request: PathImport):
    if kind not in KINDS and kind != 'recording-names':
        raise HTTPException(404, 'unknown import kind')
    target = _resolve(request.rootId, request.relativePath)
    if kind == 'recording-names':
        from ..importers import recording_names
        return recording_names.observe_paths([target], label=request.label)
    if target.is_dir() and kind != 'whatsapp-export':
        raise HTTPException(422, 'this importer expects a file')
    return _run(kind, target, locator=str(target), original_name=None, label=request.label,
                collection_id=request.collectionId, enable_browser_history=request.enableBrowserHistory,
                enable_location_history=request.enableLocationHistory, coarse_decimals=request.coarseDecimals,
                profile=request.profile, chat_member=request.chatMember)


@router.get(PREFIX + '/calendar/{event_id}/occurrences')
def calendar_occurrences(event_id: int, max_instances: int = Query(100, ge=1, le=500), until: str | None = None):
    from ..importers import ics
    try:
        return ics.expand_recurrence(event_id, max_instances=max_instances, until=until)
    except ValueError as exc:
        raise HTTPException(404 if 'does not exist' in str(exc) else 422, str(exc)) from exc


@router.get(PREFIX + '/events/{event_id}/media-references')
def media_references(event_id: int):
    with session() as db:
        if not db.execute('SELECT 1 FROM events WHERE id=?', (event_id,)).fetchone():
            raise HTTPException(404, 'event not found')
        rows = db.execute('''SELECT r.*, a.sha256, a.original_name AS artifact_name FROM event_media_references r
                             LEFT JOIN artifacts a ON a.id=r.artifact_id WHERE r.event_id=? ORDER BY r.id''',
                          (event_id,)).fetchall()
    return [{**{k: row[k] for k in row.keys() if not k.endswith('_json')},
             'locator': json.loads(row['locator_json']), 'metadata': json.loads(row['metadata_json'])} for row in rows]


@router.get(PREFIX + '/events/{event_id}/times')
def event_times(event_id: int):
    with session() as db:
        rows = db.execute('SELECT * FROM event_times WHERE event_id=? ORDER BY id', (event_id,)).fetchall()
    return [{**{k: row[k] for k in row.keys() if k != 'metadata_json'}, 'metadata': json.loads(row['metadata_json'])}
            for row in rows]


@router.get(PREFIX + '/contacts')
def contacts(limit: int = Query(100, ge=1, le=1000), after_id: int = Query(0, ge=0)):
    with session() as db:
        rows = db.execute('SELECT * FROM contact_observations WHERE id>? ORDER BY id LIMIT ?', (after_id, limit + 1)).fetchall()
        values = {}
        for row in rows[:limit]:
            values[row['id']] = [dict(v) for v in db.execute(
                'SELECT value_kind,position,raw_value,normalized_candidate,types_json FROM contact_observation_values '
                'WHERE contact_observation_id=? ORDER BY value_kind,position', (row['id'],))]
    items = [{**{k: row[k] for k in row.keys() if not k.endswith('_json')},
              'name_parts': json.loads(row['name_parts_json']), 'locator': json.loads(row['locator_json']),
              'metadata': json.loads(row['metadata_json']), 'values': values[row['id']]} for row in rows[:limit]]
    return {'items': items, 'has_more': len(rows) > limit, 'semantics': 'address-book observations; not identities'}


@router.get(PREFIX + '/recording-names')
def recording_name_observations(limit: int = Query(100, ge=1, le=1000), after_id: int = Query(0, ge=0)):
    with session() as db:
        rows = db.execute('SELECT * FROM recording_name_observations WHERE id>? ORDER BY id LIMIT ?',
                          (after_id, limit + 1)).fetchall()
    items = [{**{k: row[k] for k in row.keys() if not k.endswith('_json')},
              'groups': json.loads(row['groups_json']), 'hypotheses': json.loads(row['hypotheses_json']),
              'alternatives': json.loads(row['alternatives_json']), 'metadata': json.loads(row['metadata_json'])}
             for row in rows[:limit]]
    return {'items': items, 'has_more': len(rows) > limit,
            'semantics': 'file-name observations; number/name/direction are hypotheses'}

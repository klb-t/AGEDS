"""HTTP routes owned by the workbench area.

JSON under ``/api/workbench/*``; error model ``{"detail": {"code", "message"}}``
(422 invalid_input, 404 not_found, 409 conflict, 503 work_limit or
database_unavailable). Human pages under ``/workbench`` use POST forms with a
303 redirect back to a local path. ``/api/search`` is not changed.

Importing this module registers the i18n helpers (``t``, ``t_nav``, ...) on the
shared Jinja environment, so every template can use them.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query, Request
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..db import session
from ..search import SearchQueryError, SearchWorkLimitError
from ..web import templates
from ..workbench import bookmarks, folders, i18n, knowledge, saved, tags, theme
from ..workbench.common import WorkbenchConflict, WorkbenchInputError, WorkbenchNotFound, loads_object
from ..workbench.layered_search import layered_search_page
from ..workbench.layers import DERIVED_KIND_LAYERS, LAYER_NAMES, legend

i18n.install(templates.env)


def _template_knowledge(artifact_id):
    """Template helper: the computed summary or None; an error never breaks the evidence page."""
    try:
        return knowledge.artifact_knowledge(artifact_id)
    except (WorkbenchInputError, WorkbenchNotFound, OSError, sqlite3.Error):
        logging.getLogger(__name__).warning('knowledge summary unavailable for artifact %s', artifact_id, exc_info=True)
        return None


def _template_folders():
    try:
        return folders.list_folders()
    except (OSError, sqlite3.Error):
        logging.getLogger(__name__).warning('folder list unavailable', exc_info=True)
        return []


templates.env.globals.update(wb_artifact_knowledge=_template_knowledge, wb_folder_choices=_template_folders)

def _reject_cross_site_writes(request: Request) -> None:
    """Cheap CSRF guard for workbench writes: refuse browser requests marked cross-site.

    Browsers send ``Sec-Fetch-Site``/``Origin`` on form posts and fetches; local
    clients (CLI, scripts, tests) usually send neither and are unaffected. This
    complements, and does not replace, a trusted network or ``EW_REQUIRE_TOKEN``.
    """
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return
    site = request.headers.get('sec-fetch-site')
    origin = request.headers.get('origin')
    host = request.headers.get('host', '')
    if site == 'cross-site' or (origin and origin != 'null' and urlsplit(origin).netloc != host) or origin == 'null':
        raise HTTPException(403, {'code': 'cross_site_write', 'message': 'Workbench writes must come from this origin.'})


router = APIRouter(dependencies=[Depends(_reject_cross_site_writes)])
NAV: list[tuple[str, str]] = [('/workbench', 'Warsztat'), ('/workbench/folders', 'Teczki'), ('/workbench/bookmarks', 'Zakładki')]
MAX_SQL_INTEGER = 2**63 - 1
SqlPathId = Annotated[int, PathParameter(ge=1, le=MAX_SQL_INTEGER)]
KEYS_FILE = Path(__file__).resolve().parents[1] / 'static' / 'workbench-keys.json'


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {'code': code, 'message': message})


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (WorkbenchInputError, SearchQueryError) as exc:
        raise _error(422, 'invalid_input', str(exc)) from exc
    except WorkbenchNotFound as exc:
        raise _error(404, 'not_found', str(exc)) from exc
    except WorkbenchConflict as exc:
        raise _error(409, 'conflict', str(exc)) from exc
    except SearchWorkLimitError as exc:
        raise _error(503, 'work_limit', 'Search work limit exceeded; narrow the query or the layers.') from exc
    except (OSError, sqlite3.Error) as exc:
        raise _error(503, 'database_unavailable', 'Evidence database or workbench index is unavailable.') from exc


# --- layered search -------------------------------------------------------------
@router.get('/api/workbench/search')
def api_layered_search(q: str, layer: list[str] = Query(default=[]), limit: int = Query(100, ge=1, le=200)):
    return _call(layered_search_page, q, layer or None, limit)


@router.get('/api/workbench/layers')
def api_layers():
    return {'layers': legend(), 'derived_kind_layers': DERIVED_KIND_LAYERS}


# --- saved searches -------------------------------------------------------------
@router.get('/api/workbench/saved-searches')
def api_saved_list():
    return {'items': _call(saved.list_saved_searches), 'results_stored': False}


@router.post('/api/workbench/saved-searches', status_code=201)
def api_saved_create(body: dict = Body(...)):
    return _call(saved.create_saved_search, body.get('name'), body.get('query'), body.get('layers'),
                 body.get('limit', 100), body.get('note'))


@router.get('/api/workbench/saved-searches/{saved_id}')
def api_saved_get(saved_id: SqlPathId):
    return _call(saved.get_saved_search, saved_id)


@router.patch('/api/workbench/saved-searches/{saved_id}')
def api_saved_update(saved_id: SqlPathId, body: dict = Body(...)):
    allowed = {'name', 'query', 'layers', 'limit', 'note'}
    if set(body) - allowed:
        raise _error(422, 'invalid_input', f'unknown fields: {sorted(set(body) - allowed)}')
    return _call(saved.update_saved_search, saved_id, **body)


@router.delete('/api/workbench/saved-searches/{saved_id}')
def api_saved_delete(saved_id: SqlPathId):
    return _call(saved.delete_saved_search, saved_id)


@router.post('/api/workbench/saved-searches/{saved_id}/run')
def api_saved_run(saved_id: SqlPathId):
    return _call(saved.run_saved_search, saved_id)


# --- tags -----------------------------------------------------------------------
@router.get('/api/workbench/tags')
def api_tags():
    return _call(tags.list_tags)


@router.post('/api/workbench/tags/apply')
def api_tags_apply(body: dict = Body(...)):
    action = body.get('action', 'add')
    if action not in ('add', 'remove'):
        raise _error(422, 'invalid_input', 'action must be add or remove')
    return _call(tags.apply_tag, body.get('tag'), body.get('targets'), remove=action == 'remove')


@router.post('/api/workbench/tags/{tag_id}/rename')
def api_tags_rename(tag_id: SqlPathId, body: dict = Body(...)):
    return _call(tags.rename_tag, tag_id, body.get('name'))


@router.post('/api/workbench/tags/merge')
def api_tags_merge(body: dict = Body(...)):
    return _call(tags.merge_tags, body.get('sources'), body.get('target'))


# --- folders --------------------------------------------------------------------
@router.get('/api/workbench/folders')
def api_folders():
    return {'items': _call(folders.list_folders)}


@router.post('/api/workbench/folders', status_code=201)
def api_folder_create(body: dict = Body(...)):
    return _call(folders.create_folder, body.get('name'), body.get('parent_id'), body.get('note'), body.get('position'))


@router.get('/api/workbench/folders/{folder_id}')
def api_folder_get(folder_id: SqlPathId):
    return _call(folders.get_folder, folder_id)


@router.patch('/api/workbench/folders/{folder_id}')
def api_folder_update(folder_id: SqlPathId, body: dict = Body(...)):
    allowed = {'name', 'note', 'parent_id', 'position'}
    if set(body) - allowed:
        raise _error(422, 'invalid_input', f'unknown fields: {sorted(set(body) - allowed)}')
    return _call(folders.update_folder, folder_id, **body)


@router.delete('/api/workbench/folders/{folder_id}')
def api_folder_delete(folder_id: SqlPathId, recursive: bool = False):
    return _call(folders.delete_folder, folder_id, recursive=recursive)


@router.post('/api/workbench/folders/{folder_id}/items', status_code=201)
def api_folder_add(folder_id: SqlPathId, body: dict = Body(...)):
    return _call(folders.add_item, folder_id, body.get('kind'), body.get('id'), body.get('note'), body.get('position'))


@router.patch('/api/workbench/folders/{folder_id}/items/{entry_id}')
def api_folder_item_update(folder_id: SqlPathId, entry_id: SqlPathId, body: dict = Body(...)):
    if set(body) - {'note'}:
        raise _error(422, 'invalid_input', 'only note can be changed; membership references are fixed')
    return _call(folders.update_item, folder_id, entry_id, **body)


@router.delete('/api/workbench/folders/{folder_id}/items/{entry_id}')
def api_folder_item_remove(folder_id: SqlPathId, entry_id: SqlPathId):
    return _call(folders.remove_item, folder_id, entry_id)


@router.post('/api/workbench/folders/{folder_id}/order')
def api_folder_order(folder_id: SqlPathId, body: dict = Body(...)):
    return _call(folders.reorder_items, folder_id, body.get('order'))


@router.get('/api/workbench/folders/{folder_id}/export')
def api_folder_export(folder_id: SqlPathId, format: str = Query('json', pattern='^(json|csv)$'), recursive: bool = False):
    payload, media_type = _call(folders.export_folder, folder_id, format, recursive=recursive)
    return Response(content=payload, media_type=media_type, headers={
        'Content-Disposition': f'attachment; filename="ageds-folder-{folder_id}.{format}"', 'Cache-Control': 'no-store'})


# --- bookmarks ------------------------------------------------------------------
@router.get('/api/workbench/bookmarks')
def api_bookmarks(from_: str | None = Query(None, alias='from'), to: str | None = None, ref_kind: str | None = None,
                  ref_id: int | None = Query(None, ge=1, le=MAX_SQL_INTEGER), include_unzoned: bool | None = None,
                  limit: int = Query(bookmarks.DEFAULT_PAGE, ge=1, le=bookmarks.MAX_BOOKMARKS_PAGE)):
    return _call(bookmarks.list_bookmarks, from_=from_, to=to, ref_kind=ref_kind, ref_id=ref_id,
                 include_unzoned=include_unzoned, limit=limit)


@router.post('/api/workbench/bookmarks', status_code=201)
def api_bookmark_create(body: dict = Body(...)):
    allowed = {'start', 'end', 'label', 'note', 'ref', 'view'}
    if set(body) - allowed:
        raise _error(422, 'invalid_input', f'unknown fields: {sorted(set(body) - allowed)}')
    return _call(bookmarks.create_bookmark, body.get('start'), body.get('end'), label=body.get('label'),
                 note=body.get('note'), ref=body.get('ref'), view=body.get('view'))


@router.get('/api/workbench/bookmarks/{bookmark_id}')
def api_bookmark_get(bookmark_id: SqlPathId):
    return _call(bookmarks.get_bookmark, bookmark_id)


@router.patch('/api/workbench/bookmarks/{bookmark_id}')
def api_bookmark_update(bookmark_id: SqlPathId, body: dict = Body(...)):
    allowed = {'start', 'end', 'label', 'note', 'ref', 'view'}
    if set(body) - allowed:
        raise _error(422, 'invalid_input', f'unknown fields: {sorted(set(body) - allowed)}')
    return _call(bookmarks.update_bookmark, bookmark_id, **body)


@router.delete('/api/workbench/bookmarks/{bookmark_id}')
def api_bookmark_delete(bookmark_id: SqlPathId):
    return _call(bookmarks.delete_bookmark, bookmark_id)


# --- knowledge, jump, citation text, i18n, theme ---------------------------------
@router.get('/api/workbench/artifacts/{artifact_id}/knowledge')
def api_knowledge(artifact_id: SqlPathId):
    return _call(knowledge.artifact_knowledge, artifact_id)


@router.get('/api/workbench/jump')
def api_jump(q: str):
    return _call(knowledge.quick_jump, q)


@router.get('/api/workbench/citations/{anchor_id}/text')
def api_citation_text(request: Request, anchor_id: SqlPathId, lang: str | None = None):
    language = lang if lang in i18n.LANGUAGES else i18n.request_language(request)[0]
    return _call(knowledge.citation_text, anchor_id, language)


@router.get('/api/workbench/i18n')
def api_i18n(request: Request):
    lang, source = i18n.request_language(request)
    return {'lang': lang, 'source': source, 'languages': list(i18n.LANGUAGES), 'default': i18n.DEFAULT_LANGUAGE,
            'precedence': ['query ?lang= (this request only)', f'cookie {i18n.COOKIE}', 'Accept-Language', i18n.DEFAULT_LANGUAGE],
            'catalog_url': f'/static/i18n/{lang}.json'}


@router.get('/api/workbench/theme/contrast')
def api_theme_contrast():
    return theme.contrast_report()


# --- HTML pages -------------------------------------------------------------------
def _local_path(value: str | None, default: str) -> str:
    if not value or not value.startswith('/') or value.startswith('//') or '\\' in value or any(ord(c) < 32 for c in value):
        return default
    return value


def _redirect(target: str, **params) -> RedirectResponse:
    clean = {k: v for k, v in params.items() if v is not None}
    if clean:
        target += ('&' if '?' in target else '?') + urlencode(clean)
    return RedirectResponse(target, status_code=303)


def _form_call(back: str, function, *args, **kwargs):
    """Run a form action; on a typed error redirect back with a visible message instead of a bare error page."""
    try:
        return function(*args, **kwargs), None
    except (WorkbenchInputError, SearchQueryError) as exc:
        return None, _redirect(back, wb_error='invalid_input', wb_detail=str(exc)[:500])
    except WorkbenchNotFound as exc:
        return None, _redirect(back, wb_error='not_found', wb_detail=str(exc)[:500])
    except WorkbenchConflict as exc:
        return None, _redirect(back, wb_error='conflict', wb_detail=str(exc)[:500])
    except SearchWorkLimitError:
        return None, _redirect(back, wb_error='work_limit')


def _audit_summary(audit_id: int | None) -> dict | None:
    if audit_id is None:
        return None
    with session() as db:
        row = db.execute("SELECT id, ts, action, object_kind, object_id, details_json FROM audit_log WHERE id=? AND actor='workbench'",
                         (audit_id,)).fetchone()
    if row is None:
        return None
    details = loads_object(row['details_json'])
    targets = details.get('targets') or {}
    return {'id': row['id'], 'ts': row['ts'], 'action': row['action'], 'object_kind': row['object_kind'], 'object_id': row['object_id'],
            'tag': details.get('tag'), 'changed': {k: len(v.get('changed', [])) for k, v in targets.items()},
            'unchanged': {k: len(v.get('unchanged', [])) for k, v in targets.items()},
            'changed_ids': {k: v.get('changed', [])[:50] for k, v in targets.items()}}


def _return_to(request: Request) -> str:
    """Current page as a local redirect target, without one-shot message parameters."""
    kept = [(k, v) for k, v in request.query_params.multi_items() if k not in ('done', 'wb_error', 'wb_detail', 'saved', 'ran', 'added', 'deleted')]
    return request.url.path + ('?' + urlencode(kept) if kept else '')


def _messages(request: Request) -> dict:
    params = request.query_params
    done = params.get('done')
    return {'error': params.get('wb_error'), 'error_detail': params.get('wb_detail'),
            'done': _audit_summary(int(done)) if done and done.isdigit() and len(done) < 19 else None}


@router.get('/workbench', response_class=HTMLResponse)
def workbench_page(request: Request, q: str = '', layer: list[str] = Query(default=[]), limit: int = Query(50, ge=1, le=200)):
    page, error, status = None, None, 200
    if q:
        try:
            page = layered_search_page(q, layer or None, limit)
        except SearchQueryError as exc:
            error, status = ('invalid_query', str(exc)), 422
        except SearchWorkLimitError:
            error, status = ('work_limit', ''), 503
    try:
        keys = json.loads(KEYS_FILE.read_text('utf-8'))
    except (OSError, ValueError):
        keys = {'bindings': [], 'reserved': []}
    context = {'q': q, 'selected_layers': [l for l in layer if l in LAYER_NAMES], 'layer_names': LAYER_NAMES, 'limit': limit,
               'page': page, 'search_error': error, 'saved': saved.list_saved_searches(), 'tags': tags.list_tags(),
               'folders': folders.list_folders(), 'messages': _messages(request), 'keys': keys,
               'return_to': _return_to(request)}
    return templates.TemplateResponse(request, 'workbench.html', context, status_code=status)


@router.post('/workbench/saved')
def workbench_saved_create(name: str = Form(...), q: str = Form(...), layer: list[str] = Form(default=[]), limit: int = Form(50),
                           note: str = Form('')):
    back = '/workbench?' + urlencode([('q', q), *[('layer', l) for l in layer], ('limit', limit)])
    result, failure = _form_call(back, saved.create_saved_search, name, q, layer or None, limit, note or None)
    return failure or _redirect(back, saved=result['id'])


@router.post('/workbench/saved/{saved_id}/run')
def workbench_saved_run(saved_id: SqlPathId):
    result, failure = _form_call('/workbench', saved.run_saved_search, saved_id)
    if failure:
        return failure
    search = result['saved_search']
    return _redirect('/workbench?' + urlencode([('q', search['query']), *[('layer', l) for l in search['layers']],
                                                ('limit', search['limit'])]), ran=saved_id)


@router.post('/workbench/saved/{saved_id}/delete')
def workbench_saved_delete(saved_id: SqlPathId):
    result, failure = _form_call('/workbench', saved.delete_saved_search, saved_id)
    return failure or _redirect('/workbench', done=result['audit_id'])


@router.post('/workbench/tags/apply')
def workbench_tags_apply(tag: str = Form(...), target: list[str] = Form(default=[]), extra_targets: str = Form(''),
                         action: str = Form('add'), return_to: str = Form('/workbench')):
    back = _local_path(return_to, '/workbench')
    targets = list(target) + [part for part in extra_targets.replace(',', ' ').split() if part]
    if action not in ('add', 'remove'):
        return _redirect(back, wb_error='invalid_input', wb_detail='action must be add or remove')
    result, failure = _form_call(back, tags.apply_tag, tag, targets, remove=action == 'remove', channel='html_form')
    return failure or _redirect(back, done=result['audit_id'])


@router.post('/workbench/tags/{tag_id}/rename')
def workbench_tag_rename(tag_id: SqlPathId, name: str = Form(...)):
    result, failure = _form_call('/workbench#tags', tags.rename_tag, tag_id, name, channel='html_form')
    return failure or _redirect('/workbench', done=result['audit_id'])


@router.post('/workbench/tags/merge')
def workbench_tag_merge(source: list[int] = Form(default=[]), target: int = Form(...)):
    result, failure = _form_call('/workbench', tags.merge_tags, source, target, channel='html_form')
    return failure or _redirect('/workbench', done=result['audit_id'])


@router.get('/workbench/folders', response_class=HTMLResponse)
def workbench_folders_page(request: Request):
    return templates.TemplateResponse(request, 'workbench_folders.html', {
        'folders': folders.list_folders(), 'messages': _messages(request)})


@router.post('/workbench/folders')
def workbench_folder_create(name: str = Form(...), parent_id: str = Form(''), note: str = Form(''), return_to: str = Form('/workbench/folders')):
    back = _local_path(return_to, '/workbench/folders')
    parent = int(parent_id) if parent_id.strip().isdigit() and len(parent_id.strip()) < 19 else None
    result, failure = _form_call(back, folders.create_folder, name, parent, note or None)
    return failure or _redirect(f'/workbench/folders/{result["id"]}')


@router.get('/workbench/folders/{folder_id}', response_class=HTMLResponse)
def workbench_folder_page(request: Request, folder_id: SqlPathId):
    folder = _call(folders.get_folder, folder_id)
    return templates.TemplateResponse(request, 'workbench_folder.html', {
        'folder': folder, 'all_folders': folders.list_folders(), 'item_kinds': folders.ITEM_KINDS, 'messages': _messages(request)})


@router.post('/workbench/folders/{folder_id}/update')
def workbench_folder_update(folder_id: SqlPathId, name: str = Form(...), note: str = Form('')):
    back = f'/workbench/folders/{folder_id}'
    _, failure = _form_call(back, folders.update_folder, folder_id, name=name, note=note or None)
    return failure or _redirect(back)


@router.post('/workbench/folders/{folder_id}/delete')
def workbench_folder_delete(folder_id: SqlPathId, recursive: str = Form('')):
    back = f'/workbench/folders/{folder_id}'
    result, failure = _form_call(back, folders.delete_folder, folder_id, recursive=recursive == '1')
    return failure or _redirect('/workbench/folders', deleted=len(result['deleted_folders']))


@router.post('/workbench/folders/{folder_id}/items')
def workbench_folder_add(folder_id: SqlPathId, kind: str = Form(...), item_id: str = Form(...), note: str = Form(''),
                         return_to: str = Form('')):
    back = _local_path(return_to, f'/workbench/folders/{folder_id}')
    try:
        ident = int(item_id.strip().lstrip('#'))
    except ValueError:
        return _redirect(back, wb_error='invalid_input', wb_detail='item id must be a positive integer')
    _, failure = _form_call(back, folders.add_item, folder_id, kind, ident, note or None)
    return failure or _redirect(back, added=f'{kind}:{ident}')


@router.post('/workbench/folder-items')
def workbench_folder_add_any(folder_id: str = Form(...), kind: str = Form(...), item_id: str = Form(...), note: str = Form(''),
                             return_to: str = Form('/workbench/folders')):
    back = _local_path(return_to, '/workbench/folders')
    try:
        folder, ident = int(folder_id), int(item_id.strip().lstrip('#'))
    except ValueError:
        return _redirect(back, wb_error='invalid_input', wb_detail='folder and item ids must be positive integers')
    _, failure = _form_call(back, folders.add_item, folder, kind, ident, note or None)
    return failure or _redirect(back, added=f'{kind}:{ident}')


@router.post('/workbench/folders/{folder_id}/items/{entry_id}/remove')
def workbench_folder_remove(folder_id: SqlPathId, entry_id: SqlPathId):
    back = f'/workbench/folders/{folder_id}'
    result, failure = _form_call(back, folders.remove_item, folder_id, entry_id)
    return failure or _redirect(back, done=result['audit_id'])


@router.post('/workbench/folders/{folder_id}/items/{entry_id}/move')
def workbench_folder_move(folder_id: SqlPathId, entry_id: SqlPathId, direction: str = Form(...)):
    back = f'/workbench/folders/{folder_id}'
    _, failure = _form_call(back, folders.move_item, folder_id, entry_id, -1 if direction == 'up' else 1)
    return failure or _redirect(back)


@router.post('/workbench/folders/{folder_id}/items/{entry_id}/note')
def workbench_folder_note(folder_id: SqlPathId, entry_id: SqlPathId, note: str = Form('')):
    back = f'/workbench/folders/{folder_id}'
    _, failure = _form_call(back, folders.update_item, folder_id, entry_id, note=note or None)
    return failure or _redirect(back)


@router.get('/workbench/bookmarks', response_class=HTMLResponse)
def workbench_bookmarks_page(request: Request):
    listing = bookmarks.list_bookmarks(limit=bookmarks.MAX_BOOKMARKS_PAGE)
    return templates.TemplateResponse(request, 'workbench_bookmarks.html', {
        'listing': listing, 'ref_kinds': bookmarks.REF_KINDS, 'messages': _messages(request)})


@router.post('/workbench/bookmarks')
def workbench_bookmark_create(start: str = Form(...), end: str = Form(''), label: str = Form(''), note: str = Form(''),
                              ref_kind: str = Form(''), ref_id: str = Form(''), ref_version_id: str = Form(''),
                              ref_segment_index: str = Form('')):
    back = '/workbench/bookmarks'
    ref = None
    if ref_kind:
        def number(value):
            value = value.strip()
            return int(value) if value.isdigit() and len(value) < 19 else None
        ref = {'kind': ref_kind, 'id': number(ref_id)}
        if ref_kind == 'transcript_segment':
            ref |= {'version_id': number(ref_version_id), 'segment_index': number(ref_segment_index)}
    result, failure = _form_call(back, bookmarks.create_bookmark, start, end or None, label=label or None, note=note or None, ref=ref)
    return failure or _redirect(back + f'#bookmark-{result["id"]}')


@router.post('/workbench/bookmarks/{bookmark_id}/delete')
def workbench_bookmark_delete(bookmark_id: SqlPathId):
    result, failure = _form_call('/workbench/bookmarks', bookmarks.delete_bookmark, bookmark_id)
    return failure or _redirect('/workbench/bookmarks', done=result['audit_id'])


@router.get('/workbench/artifacts/{artifact_id}', response_class=HTMLResponse)
def workbench_artifact_page(request: Request, artifact_id: SqlPathId):
    summary = _call(knowledge.artifact_knowledge, artifact_id)
    return templates.TemplateResponse(request, 'workbench_artifact.html', {'k': summary, 'folders': folders.list_folders(),
                                                                           'messages': _messages(request)})

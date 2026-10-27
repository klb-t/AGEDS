"""The owner's pages: start (upload → plan → originals → ASR/OCR), the report, and settings.

Server-rendered forms that redirect back (POST → 303), so the path works without the CLI and
without client code. Behind the feature-router guard: reads need installation read_text, every
POST needs installation administer (the owner). See ``server/app/owner.py``.
"""
from __future__ import annotations

import json
import shutil
from contextlib import closing
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import authz, owner, providers
from ..db import connect, session
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/start', 'Start pracy'), ('/report', 'Raport'), ('/settings', 'Ustawienia')]

CONSENT_PROVIDERS = {
    'vertex': 'Vertex AI (Gemini): transkrypcja, OCR, odpowiedzi rzecznika',
    'google_tts': 'Cloud Text-to-Speech: czytanie odpowiedzi na głos',
}
DATA_POLICY = {'public': False, 'training': False, 'provider_cache': 'enabled'}


def _active_consent(db, provider: str) -> bool:
    return db.execute('SELECT 1 FROM provider_consents WHERE provider_name=? AND revoked_at IS NULL LIMIT 1',
                      (provider,)).fetchone() is not None


def _presets(kind: str) -> list[dict]:
    from ..media.recipe import load_preset, preset_ids
    out = []
    for recipe_id in sorted(preset_ids(kind)):
        preset, _ = load_preset(recipe_id)
        out.append({'id': recipe_id, 'description': preset.get('description', '')})
    return out


def _back(path: str, **message) -> RedirectResponse:
    query = '&'.join(f'{k}={quote(str(v))}' for k, v in message.items() if v is not None)
    return RedirectResponse(f'{path}{"?" + query if query else ""}', status_code=303)


# ---------------------------------------------------------------- start

@router.get('/start', response_class=HTMLResponse)
def start_page(request: Request, error: str | None = Query(None, max_length=500)):
    with session() as db:
        consent = _active_consent(db, 'vertex')
        provider = authz.get_setting(db, 'advocate.provider')
    return templates.TemplateResponse(request, 'owner_start.html', {
        'batches': owner.recent_batches(), 'error': error, 'limit_bytes': owner.max_upload_bytes(),
        'vertex_ready': bool(consent) and bool(provider)})


@router.post('/start/upload')
def start_upload(files: list[UploadFile] = File(...), label: str = Form(''), browser_times: str = Form('{}')):
    try:
        times = json.loads(browser_times or '{}')
    except ValueError:
        times = {}
    batch = owner.new_batch_dir()
    written, used, budget = [], set(), [owner.max_upload_bytes()]
    try:
        for upload in files:
            if upload.filename:
                written.append(owner.write_upload(batch, upload.filename, upload.file, used, budget))
        if not written:
            raise owner.OwnerError('no file was uploaded')
    except (owner.OwnerError, OSError) as exc:
        shutil.rmtree(batch, ignore_errors=True)  # nothing refers to a batch that never got a collection
        return _back('/start', error=str(exc)[:300])
    try:
        started = owner.start_batch(batch, written, label=label.strip() or None,
                                    browser_times=times if isinstance(times, dict) else {})
    except (owner.OwnerError, ValueError, OSError) as exc:  # the files stay; the error is shown
        return _back('/start', error=f'{type(exc).__name__}: {str(exc)[:300]}')
    return _back(f'/start/batch/{started["collection_id"]}', done='uploaded')


@router.get('/start/batch/{collection_id}', response_class=HTMLResponse)
def batch_page(request: Request, collection_id: int, done: str | None = Query(None, max_length=40),
               error: str | None = Query(None, max_length=500)):
    try:
        view = owner.batch_view(collection_id)
    except Exception as exc:  # unknown collection
        raise HTTPException(404, 'batch not found') from exc
    if view['collection'].get('acquisition_method') != 'browser_upload':
        raise HTTPException(404, 'not an upload batch')
    with session() as db:
        ready = _active_consent(db, 'vertex')
    from ..media.recipe import default_recipe_id
    return templates.TemplateResponse(request, 'owner_batch.html', {
        **view, 'done': done, 'error': error, 'vertex_consent': ready,
        'recipes': {'transcribe': default_recipe_id('asr'), 'ocr': default_recipe_id('ocr')}})


@router.post('/start/batch/{collection_id}/originals')
def batch_originals(collection_id: int):
    try:
        result = owner.keep_originals(collection_id)
    except Exception as exc:
        return _back(f'/start/batch/{collection_id}', error=f'{type(exc).__name__}: {str(exc)[:200]}')
    return _back(f'/start/batch/{collection_id}', done=f'kept:{result["ingested"]}')


@router.post('/start/batch/{collection_id}/process')
def batch_process(collection_id: int):
    try:
        result = owner.process_new(collection_id)
    except Exception as exc:
        return _back(f'/start/batch/{collection_id}', error=f'{type(exc).__name__}: {str(exc)[:200]}')
    return _back(f'/start/batch/{collection_id}', done=f'queued:{len(result["queued"])}')


# ---------------------------------------------------------------- report

@router.get('/report', response_class=HTMLResponse)
def report_page(request: Request):
    from ..pilot_report import collect
    with closing(connect(read_only=True)) as db:
        report = collect(db)
    return templates.TemplateResponse(request, 'owner_report.html', {'r': report})


# ---------------------------------------------------------------- settings

@router.get('/settings', response_class=HTMLResponse)
def settings_page(request: Request, done: str | None = Query(None, max_length=60),
                  error: str | None = Query(None, max_length=500)):
    from ..media.recipe import default_recipe_id
    with session() as db:
        consents = [dict(r) for r in db.execute(
            'SELECT uid, provider_name, scope_id, basis_text, granted_by, granted_at, revoked_at FROM provider_consents ORDER BY id DESC')]
        state = {'provider': authz.get_setting(db, 'advocate.provider'),
                 'voice': authz.get_setting(db, 'advocate.voice'),
                 'budget': authz.get_setting(db, 'advocate.daily_budget_eur', 1.0),
                 'spent': round(providers.spent_today_eur(db), 4),
                 'calls': [dict(r) for r in db.execute(
                     'SELECT purpose, provider_name, model, status, cost_eur, started_at FROM provider_calls ORDER BY id DESC LIMIT 15')]}
    presets = {kind: _presets(kind) for kind in ('asr', 'ocr')}
    return templates.TemplateResponse(request, 'owner_settings.html', {
        'consents': consents, 'providers_known': CONSENT_PROVIDERS, 'done': done, 'error': error, **state,
        'presets': presets, 'defaults': {kind: default_recipe_id(kind) for kind in ('asr', 'ocr')},
        'upload_limit_gib': round(owner.max_upload_bytes() / 1024 ** 3, 3)})


@router.post('/settings/consent')
def settings_consent(provider: str = Form(...), basis: str = Form(...)):
    if provider not in CONSENT_PROVIDERS:
        return _back('/settings', error='unknown provider')
    if not basis.strip():
        return _back('/settings', error='podaj podstawę zgody (np. „R100 właściciel 2026-10-25”)')
    with session() as db:
        scope = authz.create_scope(db, 'installation', {}, actor='owner-web')
        providers.record_consent(db, provider, scope, granted_by='owner-web', basis_text=basis.strip()[:1000],
                                 data_policy=DATA_POLICY)
    return _back('/settings', done=f'consent:{provider}')


@router.post('/settings/consent/{uid}/revoke')
def settings_revoke(uid: str):
    with session() as db:
        if not db.execute('SELECT 1 FROM provider_consents WHERE uid=?', (uid,)).fetchone():
            return _back('/settings', error='consent not found')
        providers.revoke_consent(db, uid, actor='owner-web')
    return _back('/settings', done='revoked')


@router.post('/settings/budget')
def settings_budget(eur: str = Form(...)):
    try:
        value = round(float(eur.replace(',', '.')), 2)
    except ValueError:
        return _back('/settings', error='budżet musi być liczbą')
    if not 0 <= value <= 1000:
        return _back('/settings', error='budżet od 0 do 1000 EUR dziennie')
    with session() as db:
        authz.set_setting(db, 'advocate.daily_budget_eur', value, actor='owner-web')
    return _back('/settings', done='budget')


@router.post('/settings/provider')
def settings_provider(state: str = Form(...)):
    with session() as db:
        authz.set_setting(db, 'advocate.provider', {'type': 'vertex_gemini', 'name': 'vertex'} if state == 'on' else None,
                          actor='owner-web')
    return _back('/settings', done='provider')


@router.post('/settings/voice')
def settings_voice(state: str = Form(...)):
    with session() as db:
        authz.set_setting(db, 'advocate.voice', {'enabled': True} if state == 'on' else None, actor='owner-web')
    return _back('/settings', done='voice')


@router.post('/settings/upload-limit')
def settings_upload_limit(gib: str = Form(...)):
    try:
        value = float(gib.replace(',', '.'))
    except ValueError:
        return _back('/settings', error='limit musi być liczbą (GiB)')
    if not 0.001 <= value <= 1024:
        return _back('/settings', error='limit od 0,001 do 1024 GiB')
    with session() as db:
        authz.set_setting(db, owner.UPLOAD_LIMIT_SETTING, int(value * 1024 ** 3), actor='owner-web')
    return _back('/settings', done='upload-limit')


@router.post('/settings/recipes')
def settings_recipes(asr: str = Form(...), ocr: str = Form(...)):
    from ..media.recipe import DEFAULT_RECIPE_SETTING, RecipeError, effective_recipe
    try:
        effective_recipe(asr, kind='asr')
        effective_recipe(ocr, kind='ocr')
    except (RecipeError, ValueError) as exc:
        return _back('/settings', error=str(exc)[:200])
    with session() as db:
        authz.set_setting(db, DEFAULT_RECIPE_SETTING.format(kind='asr'), asr, actor='owner-web')
        authz.set_setting(db, DEFAULT_RECIPE_SETTING.format(kind='ocr'), ocr, actor='owner-web')
    return _back('/settings', done='recipes')

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

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

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
        out.append({'id': recipe_id, 'description': preset.get('description', ''), 'availability': owner.preset_status(recipe_id)})
    return out


def _back(path: str, **message) -> RedirectResponse:
    query = '&'.join(f'{k}={quote(str(v))}' for k, v in message.items() if v is not None)
    return RedirectResponse(f'{path}{"?" + query if query else ""}', status_code=303)


# ---------------------------------------------------------------- start

@router.get('/start', response_class=HTMLResponse)
def start_page(request: Request, error: str | None = Query(None, max_length=500)):
    with session() as db:
        consent = _active_consent(db, 'vertex')
    return templates.TemplateResponse(request, 'owner_start.html', {
        'batches': owner.recent_batches(), 'error': error, 'limit_bytes': owner.max_upload_bytes(),
        'vertex_ready': consent})


@router.post('/start/upload')
async def start_upload(request: Request):
    # Size and free space are checked from the header before the body is read: the parser would
    # otherwise spool a too-large upload to disk first and refuse it only at the end.
    try:
        length = int(request.headers['content-length'])
    except (KeyError, ValueError):
        length = None
    refusal = await run_in_threadpool(owner.upload_refusal, length)
    if refusal:
        return _back('/start', error=refusal)
    async with request.form(max_files=10000, max_fields=10) as form:
        files = [f for f in form.getlist('files') if isinstance(f, StarletteUploadFile)]
        label, browser_times = form.get('label') or '', form.get('browser_times') or '{}'
        return await run_in_threadpool(_start_upload, files, label if isinstance(label, str) else '',
                                       browser_times if isinstance(browser_times, str) else '{}')


def _start_upload(files: list, label: str, browser_times: str) -> RedirectResponse:
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
    recipes = {'transcribe': default_recipe_id('asr'), 'ocr': default_recipe_id('ocr')}
    return templates.TemplateResponse(request, 'owner_batch.html', {
        **view, 'done': done, 'error': error, 'vertex_consent': ready, 'recipes': recipes,
        'preset_status': {kind: owner.preset_status(recipe) for kind, recipe in recipes.items()}})


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
    from ..reports.outputs import pdf_status
    return templates.TemplateResponse(request, 'owner_report.html', {'r': report, 'pdf': pdf_status()})


@router.get('/report/download/{fmt}')
def report_download(request: Request, fmt: str):
    """The report as one self-contained file (no scripts, no external assets) for a recipient."""
    import tempfile
    from datetime import datetime, timezone
    from pathlib import Path
    from fastapi.responses import FileResponse, Response
    from ..pilot_report import collect
    from ..reports.outputs import pdf_status, write_pdf
    if fmt not in ('html', 'pdf'):
        raise HTTPException(404, 'format must be html or pdf')
    if fmt == 'pdf' and pdf_status()['status'] != 'available':
        raise HTTPException(409, 'PDF output is not available in this installation: ' + pdf_status()['reason'])
    with closing(connect(read_only=True)) as db:
        report = collect(db)
    static = Path(__file__).resolve().parents[1] / 'static'
    css = (static / 'app.css').read_text() + '\n' + (static / 'owner.css').read_text()
    html = templates.get_template('owner_report.html').render(
        {'request': request, 'r': report, 'standalone': True, 'inline_css': css})
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    headers = {'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; img-src data:"}
    if fmt == 'html':
        headers['Content-Disposition'] = f'attachment; filename="ageds-raport-{stamp}.html"'
        return Response(html, media_type='text/html; charset=utf-8', headers=headers)
    staging = Path(tempfile.mkdtemp(prefix='ageds-report-pdf-'))
    (staging / 'report.html').write_text(html, encoding='utf-8')
    result = write_pdf(staging)
    if result.get('status') != 'written':
        raise HTTPException(500, f"PDF was not written: {result.get('reason', 'unknown')}")
    from starlette.background import BackgroundTask
    import shutil as _shutil
    return FileResponse(staging / 'report.pdf', media_type='application/pdf', filename=f'ageds-raport-{stamp}.pdf',
                        headers={'Cache-Control': 'no-store'},
                        background=BackgroundTask(_shutil.rmtree, staging, ignore_errors=True))


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
    with session() as db:
        recipients = _recipients(db)
        cases = [dict(r) for r in db.execute('SELECT id, name FROM cases ORDER BY id')]
        tokens_mode = authz.get_setting(db, 'auth.mode', 'legacy_open') == 'tokens'
    return templates.TemplateResponse(request, 'owner_settings.html', {
        'consents': consents, 'providers_known': CONSENT_PROVIDERS, 'done': done, 'error': error, **state,
        'presets': presets, 'defaults': {kind: default_recipe_id(kind) for kind in ('asr', 'ocr')},
        'default_status': {kind: owner.preset_status(default_recipe_id(kind)) for kind in ('asr', 'ocr')},
        'upload_limit_gib': round(owner.max_upload_bytes() / 1024 ** 3, 3),
        'recipients': recipients, 'cases': cases, 'tokens_mode': tokens_mode})


def _recipients(db) -> list[dict]:
    """Principals other than the owner, with their grants and invitation tokens (never secrets)."""
    owner_uid = authz.get_setting(db, 'installation.owner_principal_uid')
    out = []
    for p in db.execute("SELECT id, uid, display_name, kind, created_at FROM principals ORDER BY id DESC"):
        if p['uid'] == owner_uid:
            continue
        grants = [dict(g) | {'capabilities': json.loads(g['capabilities_json'])} for g in db.execute(
            '''SELECT g.uid, g.capabilities_json, g.granted_at, g.valid_to, g.revoked_at, s.kind AS scope_kind
               FROM grants g JOIN scopes s ON s.id=g.scope_id WHERE g.principal_id=? ORDER BY g.id''', (p['id'],))]
        tokens = [dict(t) for t in db.execute(
            "SELECT uid, label, created_at, expires_at, revoked_at FROM auth_methods WHERE principal_id=? AND kind='token' ORDER BY id",
            (p['id'],))]
        if grants or tokens:
            out.append({**dict(p), 'grants': grants, 'tokens': tokens})
    return out


@router.post('/settings/invite')
def settings_invite(request: Request, name: str = Form(''), case_ids: list[int] = Form([]), mode: str = Form('snapshot'),
                    read_bytes: str = Form(''), expires_days: str = Form('')):
    import os
    from ..sharing import invite_recipient
    with session() as db:
        if authz.get_setting(db, 'auth.mode', 'legacy_open') != 'tokens':
            return _back('/settings', error='zaproszenia działają tylko w trybie tokenów (auth.mode=tokens)')
        try:
            days = int(expires_days) if expires_days.strip() else None
            if days is not None and not 1 <= days <= 3650:
                raise ValueError('ważność od 1 do 3650 dni')
            base = os.environ.get('AGEDS_PUBLIC_URL') or str(request.base_url)
            result = invite_recipient(db, name=name, case_ids=case_ids, mode=mode, read_bytes=read_bytes == 'on',
                                      expires_days=days, base_url=base, actor='owner-web')
        except ValueError as exc:
            db.rollback()
            return _back('/settings', error=str(exc)[:200])
    # The token is shown once, in this response only: never in a URL, a redirect or a cache.
    response = templates.TemplateResponse(request, 'owner_invited.html', {'r': result, 'name': name.strip()})
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@router.post('/settings/grant/{uid}/revoke')
def settings_revoke_grant(uid: str):
    with session() as db:
        row = db.execute('SELECT id FROM grants WHERE uid=? AND revoked_at IS NULL', (uid,)).fetchone()
        if row is None:
            return _back('/settings', error='grant not found')
        authz.revoke_grant(db, row['id'], revoked_by='owner-web')
    return _back('/settings', done='grant-revoked')


@router.post('/settings/token/{uid}/revoke')
def settings_revoke_token(uid: str):
    from .. import auth
    with session() as db:
        row = db.execute("SELECT p.uid AS owner FROM auth_methods m JOIN principals p ON p.id=m.principal_id WHERE m.uid=?",
                         (uid,)).fetchone()
        if row is None:
            return _back('/settings', error='token not found')
        if row['owner'] == authz.get_setting(db, 'installation.owner_principal_uid'):
            return _back('/settings', error='tokenów właściciela nie cofa się tutaj (zablokowałoby to dostęp)')
        auth.revoke_token(db, uid, actor='owner-web')
    return _back('/settings', done='token-revoked')


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
    for chosen in (asr, ocr):
        state = owner.preset_status(chosen)
        if state['status'] == 'unavailable':
            return _back('/settings', error=f"{chosen} nie może działać w tej instalacji: {'; '.join(state['missing'])}"[:300])
    with session() as db:
        authz.set_setting(db, DEFAULT_RECIPE_SETTING.format(kind='asr'), asr, actor='owner-web')
        authz.set_setting(db, DEFAULT_RECIPE_SETTING.format(kind='ocr'), ocr, actor='owner-web')
    return _back('/settings', done='recipes')

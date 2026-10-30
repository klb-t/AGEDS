"""The owner's path in the browser: upload → catalogue → import plan → originals → ASR/OCR → report.

Nothing here adds a new import rule; it chains the existing ones:

* An upload batch is written under ``<data dir>/incoming/<batch>/`` (a server-side copy, never
  modified afterwards). A collection is created for it with ``acquisition_method='browser_upload'``
  and the browser's declared ``lastModified`` per file. The files' own times on the server are the
  upload times, and the collection says so.
* The batch is catalogued read-only (archives expanded as usual) and an import plan is created
  from that scan; the plan is reviewed and run on the existing ``/import`` page.
* Entries that no plan item claims (recordings, images, the export archives themselves, …) can be
  kept as originals through the catalog ingest, with their SHA-256 verified.
* ASR/OCR jobs are queued with the default presets (``/settings``) for originals of the batch
  without a text version; the worker processes them, ``/media/queue`` shows progress.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbmod
from .db import session

AUDIO_PREFIXES = ('audio/', 'video/')
DOCUMENT_MIMES = ('application/pdf',)
IMAGE_PREFIX = 'image/'
MAX_UPLOAD_BYTES = int(os.environ.get('EW_MAX_UPLOAD_BYTES', str(8 * 1024 ** 3)))  # preset when no setting
UPLOAD_LIMIT_SETTING = 'upload.max_bytes'  # chosen by the owner on /settings


def max_upload_bytes() -> int:
    """The owner's limit for one upload batch (setting), else the operator's environment preset."""
    from . import authz
    with session() as db:
        value = authz.get_setting(db, UPLOAD_LIMIT_SETTING)
    return value if type(value) is int and value > 0 else MAX_UPLOAD_BYTES


class OwnerError(ValueError):
    """The request cannot be done as asked; nothing was changed by the failing step."""


def incoming_root() -> Path:
    return Path(dbmod.settings.data_dir) / 'incoming'


def safe_filename(name: str | None, used: set[str]) -> str:
    """A plain file name for the server copy: no path parts, no control characters, unique in the batch."""
    base = Path(str(name or '')).name.replace('\\', '_').replace(':', '_')  # no drive letters or streams
    base = ''.join(ch for ch in unicodedata.normalize('NFC', base) if unicodedata.category(ch)[0] != 'C').strip()
    base = re.sub(r'^\.+', '', base)[:200] or 'upload'
    candidate, n = base, 1
    stem, suffix = os.path.splitext(base)
    while candidate.lower() in used:
        n += 1
        candidate = f'{stem} ({n}){suffix}'
    used.add(candidate.lower())
    return candidate


def new_batch_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = incoming_root() / f'{stamp}-{secrets.token_hex(4)}'
    path.mkdir(parents=True, mode=0o700)
    return path


UPLOAD_SPACE_RESERVE = int(os.environ.get('EW_UPLOAD_SPACE_RESERVE_BYTES', str(2 * 1024 ** 3)))


def upload_refusal(content_length: int | None) -> str | None:
    """Why a request body of ``content_length`` bytes must be refused before it is read, else None.

    The multipart parser spools each file to the temp directory before the copy into the batch, so
    one upload needs its size twice (once if both lie on different file systems), plus a reserve
    that keeps the database and the content store writable.
    """
    import tempfile
    if content_length is None:
        return None
    limit = max_upload_bytes()
    if content_length > limit + (1 << 20):  # multipart framing and the small fields
        return (f'paczka ma {content_length / 1024 ** 3:.2f} GiB, limit to {limit / 1024 ** 3:.2f} GiB '
                f'(Ustawienia → limit wgrywania)')
    need: dict[int, list] = {}
    for path in (incoming_root(), Path(tempfile.gettempdir())):
        path.mkdir(parents=True, exist_ok=True)
        stat = os.statvfs(path)
        entry = need.setdefault(os.stat(path).st_dev, [stat.f_bavail * stat.f_frsize, 0])
        entry[1] += content_length
    for free, wanted in need.values():
        if wanted + UPLOAD_SPACE_RESERVE > free:
            return (f'za mało miejsca na serwerze: wgranie {content_length / 1024 ** 3:.2f} GiB potrzebuje '
                    f'{(wanted + UPLOAD_SPACE_RESERVE) / 1024 ** 3:.2f} GiB wolnego, jest {free / 1024 ** 3:.2f} GiB')
    return None


def write_upload(batch: Path, name: str, stream, used: set[str], budget: list[int]) -> dict:
    """Copy one uploaded stream into the batch; ``budget`` holds the bytes still allowed."""
    import hashlib
    target = batch / safe_filename(name, used)
    digest, size = hashlib.sha256(), 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as out:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > budget[0]:
                out.close()
                target.unlink()
                raise OwnerError('upload exceeds the batch limit (Ustawienia → limit wgrywania)')
            digest.update(chunk)
            out.write(chunk)
    budget[0] -= size
    os.chmod(target, 0o400)  # the server copy is never changed afterwards
    return {'name': target.name, 'uploaded_name': name, 'bytes': size, 'sha256': digest.hexdigest()}


def start_batch(batch: Path, files: list[dict], *, label: str | None, browser_times: dict | None,
                actor: str = 'owner') -> dict:
    """Collection + read-only catalogue scan + import plan for one uploaded batch."""
    from .catalog import store
    from .catalog.scan import run_catalog_scan
    from .importplan import plan
    if not files:
        raise OwnerError('no file was uploaded')
    declared = {}
    for item in files:
        value = (browser_times or {}).get(item['uploaded_name'])
        if isinstance(value, (int, float)) and 0 < value < 32503680000000:
            declared[item['name']] = datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    collection = store.create_collection(
        label or f'Wgrane w przeglądarce {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC', actor=actor,
        acquisition_method='browser_upload', operator=actor,
        notes='file times on the server are upload times, not the times of the original files',
        metadata={'batch_dir': batch.name, 'files': files,
                  'browser_last_modified': declared,
                  'browser_last_modified_basis': 'File.lastModified declared by the uploading browser; not verified'})
    scan = run_catalog_scan(batch, collection['id'])
    created = plan.create_plan(collection_ids=[collection['id']], actor=actor)
    return {'collection_id': collection['id'], 'scan_id': scan['scan_id'], 'plan_id': created['plan']['id']}


def _latest_plan(db, collection_id: int):
    for row in db.execute('SELECT id, collection_ids_json FROM import_plans ORDER BY id DESC'):
        if collection_id in json.loads(row['collection_ids_json']):
            return row['id']
    return None


def unclaimed_entries(db, collection_id: int, plan_id: int | None) -> list[dict]:
    """Entries of the collection's current scans that no plan item claims and that have bytes to keep."""
    claimed = set()
    if plan_id is not None:
        claimed = {r[0] for r in db.execute('''SELECT entry_id FROM import_plan_items WHERE plan_id=? AND entry_id IS NOT NULL
                                               UNION SELECT m.entry_id FROM import_plan_item_members m
                                               JOIN import_plan_items i ON i.id=m.item_id WHERE i.plan_id=?''',
                                            (plan_id, plan_id))}
    scan = db.execute("SELECT MAX(id) FROM catalog_scans WHERE collection_id=? AND status IN ('completed','partial')",
                      (collection_id,)).fetchone()[0]
    if scan is None:
        return []
    rows = db.execute('''SELECT e.id, e.locator, e.name, e.size_bytes, e.sniffed_mime, e.family, e.sha256,
                                (SELECT COUNT(*) FROM catalog_entry_artifacts l WHERE l.entry_id=e.id) AS kept
                         FROM catalog_entries e WHERE e.scan_id=? AND e.status='ok' AND e.sha256 IS NOT NULL ORDER BY e.id''',
                      (scan,)).fetchall()
    return [dict(r) for r in rows if r['id'] not in claimed]


def keep_originals(collection_id: int, *, actor: str = 'owner') -> dict:
    from .catalog import ingest
    with session() as db:
        plan_id = _latest_plan(db, collection_id)
        todo = [e['id'] for e in unclaimed_entries(db, collection_id, plan_id) if not e['kept']]
    if not todo:
        return {'ingested': 0, 'entries': 0}
    result = ingest.ingest_entries(todo, actor=actor)
    return {'ingested': result['ingested'], 'entries': len(todo),
            'not_ingested': [r for r in result['results'] if r['status'] != 'ingested']}


def collection_artifacts(db, collection_id: int) -> list[dict]:
    rows = db.execute('''SELECT DISTINCT a.id, a.original_name, a.mime_type, a.size_bytes, a.sha256
                         FROM source_observations o JOIN artifacts a ON a.id=o.artifact_id
                         WHERE o.collection_id=? ORDER BY a.id''', (collection_id,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item['texts'] = [dict(t) for t in db.execute(
            'SELECT id, kind, model, created_at FROM derived_text WHERE artifact_id=? ORDER BY id', (row['id'],))]
        item['jobs'] = [dict(j) for j in db.execute(
            'SELECT id, kind, status, error, created_at, finished_at FROM jobs WHERE artifact_id=? ORDER BY id', (row['id'],))]
        item['processing'] = processing_kind(item['mime_type'])
        # The same bytes acquired earlier (another provenance) may already have text versions.
        item['same_bytes_texts'] = [dict(t) for t in db.execute(
            '''SELECT d.id, d.kind, d.artifact_id FROM derived_text d JOIN artifacts a ON a.id=d.artifact_id
               WHERE a.sha256=? AND a.id<>? ORDER BY d.id''', (row['sha256'], row['id']))] if row['sha256'] else []
        out.append(item)
    return out


def processing_kind(mime: str | None) -> str | None:
    mime = mime or ''
    if mime.startswith(AUDIO_PREFIXES):
        return 'transcribe'
    if mime.startswith(IMAGE_PREFIX) or mime in DOCUMENT_MIMES:
        return 'ocr'
    return None


def process_new(collection_id: int) -> dict:
    """Queue ASR/OCR with the default presets for the batch's originals that have no text and no open job."""
    from .media.queue import enqueue_media_job
    from .media.recipe import default_recipe_id
    queued, skipped = [], []
    recipes = {'transcribe': default_recipe_id('asr'), 'ocr': default_recipe_id('ocr')}
    status = {kind: preset_status(recipe) for kind, recipe in recipes.items()}
    with session() as db:
        artifacts = collection_artifacts(db, collection_id)
    for item in artifacts:
        kind = item['processing']
        if kind is None:
            continue
        if status[kind]['status'] == 'unavailable':  # a job that can only fail is not queued
            skipped.append({'artifact_id': item['id'], 'reason': 'preset unavailable here',
                            'recipe': recipes[kind], 'missing': status[kind]['missing']})
            continue
        text_kind = 'transcript' if kind == 'transcribe' else 'ocr'
        if any(t['kind'] == text_kind for t in item['texts']) or any(
                j['kind'] == kind and j['status'] in ('queued', 'running') for j in item['jobs']):
            skipped.append({'artifact_id': item['id'], 'reason': 'has text or an open job'})
            continue
        earlier = [t for t in item['same_bytes_texts'] if t['kind'] == text_kind]
        if earlier:  # no second paid call for bytes already processed under another acquisition
            skipped.append({'artifact_id': item['id'], 'reason': 'the same bytes already have this text',
                            'derived_text_ids': [t['id'] for t in earlier]})
            continue
        result = enqueue_media_job(kind, item['id'])
        queued.append({'artifact_id': item['id'], 'job_id': result['job_id'], 'kind': kind})
    return {'queued': queued, 'skipped': skipped, 'recipes': recipes, 'preset_status': status}


def batch_view(collection_id: int) -> dict:
    from .catalog import store
    from .importplan import plan as planmod
    collection = store.get_collection(collection_id)
    with session() as db:
        plan_id = _latest_plan(db, collection_id)
        unclaimed = unclaimed_entries(db, collection_id, plan_id)
        artifacts = collection_artifacts(db, collection_id)
        scan = db.execute('SELECT * FROM catalog_scans WHERE collection_id=? ORDER BY id DESC LIMIT 1',
                          (collection_id,)).fetchone()
    summary = planmod.summary(plan_id) if plan_id is not None else None
    return {'collection': collection, 'plan_id': plan_id, 'plan_summary': summary, 'unclaimed': unclaimed,
            'artifacts': artifacts, 'scan': dict(scan) if scan else None,
            'coverage': json.loads(scan['coverage_json'] or '{}') if scan and 'coverage_json' in scan.keys() else {}}


def recent_batches(limit: int = 20) -> list[dict]:
    with session() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, label, created_at FROM collections WHERE acquisition_method='browser_upload' ORDER BY id DESC LIMIT ?",
            (limit,))]


def preset_status(recipe_id: str) -> dict:
    """Whether a preset can run in this installation: ``ok``, ``partial`` (runs, part missing) or ``unavailable``.

    Probed locally, without network or inference: engines, optional packages, and for Vertex a
    switched-on provider with an active consent. Shown on /settings and checked before queueing.
    """
    import importlib.util
    from .media.capabilities import media_capabilities
    from .media.recipe import RecipeError, effective_recipe
    try:
        record = effective_recipe(recipe_id)
    except (RecipeError, ValueError):
        try:
            record = effective_recipe(recipe_id, kind='ocr')
        except (RecipeError, ValueError) as exc:
            return {'status': 'unavailable', 'missing': [str(exc)[:200]]}
    effective = record['effective']
    caps = media_capabilities()
    missing, partial = [], []
    engine = str(effective.get('engine') or '')
    strategy = effective.get('strategy')
    uses_vertex = strategy == 'vertex_gemini' or engine.startswith('vertex-gemini')
    if uses_vertex:  # media jobs need the consent; the provider switch governs the advocate only
        from . import providers
        with session() as db:
            consent = providers.has_covering_consent(db, 'vertex')
        if not consent:
            missing.append('brak aktywnej zgody na Vertex z aktualną polityką danych (Ustawienia → Zgody)')
    if strategy in ('auto_per_file', 'per_segment_language') and caps['asr_faster_whisper']['status'] != 'importable':
        missing.append('faster-whisper nie jest zainstalowany')
    if engine.startswith('tesseract') or engine == 'tesseract-cli' or (record['kind'] == 'ocr' and not uses_vertex):
        if caps['ocr_tesseract']['status'] != 'available':
            missing.append('Tesseract nie jest zainstalowany w tej instalacji')
    reference = (effective.get('model_options') or {}).get('timing_reference') if effective.get('word_timestamps') else None
    if reference:
        engines = [reference['engine']] if reference['engine'] != 'hybrid' else [reference['primary']['engine'], 'fill:' + reference['fill']['engine']]
        for name in engines:
            fill = name.startswith('fill:')
            name = name.removeprefix('fill:')
            if name == 'faster-whisper' and caps['asr_faster_whisper']['status'] != 'importable':
                partial.append('faster-whisper brak: czasy słów niedostępne (tekst Gemini zostaje)')
            if name == 'mms-forced-align' and importlib.util.find_spec('torchaudio') is None:
                partial.append('torch/torchaudio brak (requirements-align.txt): '
                               + ('uzupełnianie wymuszonym wyrównaniem pominięte' if fill else 'czasy słów niedostępne, a wywołanie Gemini i tak jest płatne'))
    status = 'unavailable' if missing else 'partial' if partial else 'ok'
    return {'status': status, 'missing': missing, 'partial': partial}

"""Profile -> explain -> promote for catalog descriptions.

* **profile**: deterministic statistics of a collection, computed by code and
  stored append-only with its SHA-256 (identical profiles are reused).
* **explain**: a provider adapter turns the profile into prose. The model may
  only write placeholders such as ``{{totals.entries}}``; the pipeline resolves
  them from the profile, so every number, date and identifier comes from code.
  Digits written by the model are flagged. The result is a *hypothesis*: one run
  row per attempt, one annotation row, and one line appended to a sidecar
  ``<output>/collection-<id>.annotations.ndjson`` outside every source root.
* **promote/reject**: an explicit human decision, recorded append-only with an
  audit entry and a decision line in the same sidecar.

Providers: ``none`` (default; nothing is generated) and ``fake`` (deterministic,
for tests). Any other provider must be registered by code and, when it sends
data off the machine, needs ``allow_external=True`` for that single run; the
opt-in, provider, model and parameters are recorded. No real model is called
by this module.

The sidecar is AGEDS's own format (``ageds.catalog-annotations/v1``) with
``format_compat: "unknown"``: the UCAF/chatarchive specification was not
available, so no compatibility is claimed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import db as dbmod
from .coverage import coverage_report
from .export import _check_output_dir
from .store import NotFound

PROFILE_SCHEMA = 'ageds.catalog-profile/v1'
SIDECAR_FORMAT = 'ageds.catalog-annotations/v1'
PROMPT_TEMPLATE_VERSION = 'catalog-describe-prompt/v1'
PROMPT_TEMPLATE = (
    'Opisz kolekcję materiału źródłowego w 2–4 zdaniach po polsku. Nie wpisuj samodzielnie żadnych liczb, dat '
    'ani identyfikatorów: używaj wyłącznie placeholderów {{ścieżka.w.profilu}}, które pipeline rozwiąże z profilu. '
    'Opis jest hipotezą do weryfikacji przez człowieka.\nPROFIL:\n'
)
_PLACEHOLDER = re.compile(r'\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}')


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_profile(collection_id: int, *, scan_ids: list[int] | None = None, min_gap_days: int = 30) -> dict:
    """Deterministic profile (no labels: private names stay out of anything a provider might receive)."""
    report = coverage_report(collection_id, scan_ids=scan_ids, min_gap_days=min_gap_days)
    collection = report['collections'][0]
    types = {}
    for item in collection['types']:
        types[item['type']] = {
            'family': item['family'], 'entries': item['entries'], 'bytes': item['bytes'],
            'type_conflicts': item['type_conflicts'],
            'statuses': {k: v for k, v in item['by_status'].items() if v},
            'time_bases': [{'source': b['source'], 'tz_status': b['tz_status'], 'first': b['first'], 'last': b['last'],
                            'observed_days': b['observed_days'], 'gaps': len(b['gaps'])} for b in item['time_bases']],
        }
    largest = max(collection['types'], key=lambda t: (t['entries'], t['type']), default=None)
    with closing(dbmod.connect()) as db:
        selected = collection['scan_ids']
        marks = ','.join('?' * len(selected)) or 'NULL'
        depth = db.execute(f'SELECT MAX(depth) FROM catalog_entries WHERE scan_id IN ({marks})', selected).fetchone()[0]
        shared = db.execute(f'''SELECT COUNT(DISTINCT e.content_group_id) FROM catalog_entries e
                WHERE e.scan_id IN ({marks}) AND e.content_group_id IN (
                  SELECT content_group_id FROM catalog_entries WHERE collection_id <> ? AND content_group_id IS NOT NULL)''',
                            (*selected, collection_id)).fetchone()[0]
    profile = {
        'schema': PROFILE_SCHEMA, 'collection_id': collection_id, 'scan_ids': selected, 'min_gap_days': min_gap_days,
        'totals': collection['totals'], 'type_count': len(types), 'types': types,
        'largest_type': {'name': largest['type'], 'entries': largest['entries']} if largest else None,
        'max_archive_depth_seen': depth or 0, 'content_groups_shared_with_other_collections': shared,
        'scan_complete': all(s['complete'] for s in collection['scans']) if collection['scans'] else None,
    }
    return profile


def store_profile(profile: dict) -> tuple[int, str]:
    text = _canonical(profile)
    digest = _sha(text)
    with closing(dbmod.connect()) as db:
        row = db.execute('SELECT id FROM catalog_profiles WHERE collection_id=? AND profile_sha256=? ORDER BY id LIMIT 1',
                         (profile['collection_id'], digest)).fetchone()
        if row:
            return int(row['id']), digest
        cursor = db.execute('INSERT INTO catalog_profiles(collection_id,scan_ids_json,parameters_json,profile_json,profile_sha256) VALUES(?,?,?,?,?)',
                            (profile['collection_id'], _canonical(profile['scan_ids']), _canonical({'min_gap_days': profile['min_gap_days']}),
                             text, digest))
        db.commit()
        return int(cursor.lastrowid), digest


# ---------------------------------------------------------------- providers

@dataclass(frozen=True)
class ProviderResult:
    text: str
    model: str
    model_version: str
    params: dict


class FakeProvider:
    """Deterministic test double: writes only placeholders, never numbers."""
    name = 'fake'
    external = False

    def describe(self, profile: dict, prompt: str, params: dict) -> ProviderResult:
        text = ('Kolekcja #{{collection_id}} zawiera {{totals.entries}} wpisów w {{type_count}} typach; '
                'najliczniejszy typ to {{largest_type.name}} ({{largest_type.entries}}). '
                'Konflikty typu: {{totals.type_conflicts}}, luki czasowe: {{totals.gaps}}.')
        if params.get('style') == 'with_digits':
            text += ' Około 12 plików wygląda na duplikaty.'
        if params.get('style') == 'unresolved':
            text += ' {{no.such.path}}'
        return ProviderResult(text, 'fake-describer', '1', dict(params))


PROVIDERS: dict[str, object] = {'fake': FakeProvider()}


def register_provider(provider) -> None:
    """Register an adapter (``name``, ``external``, ``describe``). Used by future real adapters."""
    if provider.name in ('none',) or not hasattr(provider, 'describe'):
        raise ValueError('invalid provider adapter')
    PROVIDERS[provider.name] = provider


def resolve(text: str, profile: dict) -> tuple[str, dict, list[dict]]:
    placeholders, warnings = {}, []

    def lookup(path: str):
        value = profile
        for part in path.split('.'):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise KeyError(path)
        return value

    def substitute(match):
        path = match.group(1)
        try:
            value = lookup(path)
        except KeyError:
            warnings.append({'code': 'unresolved_placeholder', 'path': path})
            return match.group(0)
        placeholders[path] = value
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    resolved = _PLACEHOLDER.sub(substitute, text)
    outside = _PLACEHOLDER.sub('', text)
    if re.search(r'\d', outside):
        warnings.append({'code': 'model_wrote_digits', 'detail': 'numbers must come from placeholders resolved by code'})
    return resolved, placeholders, warnings


def _append_line(path: Path, record: dict):
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def sidecar_path(output_dir: str | Path, collection_id: int) -> Path:
    directory = _check_output_dir(Path(output_dir))
    return directory / f'collection-{collection_id}.annotations.ndjson'


def explain(collection_id: int, *, provider: str = 'none', output_dir: str | Path | None = None,
            allow_external: bool = False, params: dict | None = None, scan_ids: list[int] | None = None,
            min_gap_days: int = 30, actor: str = 'user') -> dict:
    profile = build_profile(collection_id, scan_ids=scan_ids, min_gap_days=min_gap_days)
    profile_id, profile_sha = store_profile(profile)
    if provider == 'none':
        return {'status': 'not_run', 'provider': 'none', 'profile_id': profile_id, 'profile_sha256': profile_sha,
                'profile': profile, 'note': 'no provider selected; deterministic profile only'}
    adapter = PROVIDERS.get(provider)
    if adapter is None:
        raise ValueError(f'provider unavailable: {provider}')
    if adapter.external and not allow_external:
        raise PermissionError(f'provider {provider} sends data off this machine; explicit per-run opt-in is required')
    if output_dir is None:
        raise ValueError('output_dir is required for a sidecar')
    target = sidecar_path(output_dir, collection_id)
    params = dict(params or {})
    prompt = PROMPT_TEMPLATE + _canonical(profile)
    prompt_sha = _sha(prompt)
    run_uid = str(uuid.uuid4())
    opt_in = {'allow_external': bool(allow_external), 'external': bool(adapter.external), 'actor': actor, 'at': _now()}
    try:
        result = adapter.describe(profile, prompt, params)
        status, error = 'completed', None
    except Exception as exc:  # provider failures are recorded as a failed run, not hidden
        result, status, error = None, 'failed', f'{type(exc).__name__}: {exc}'
    with closing(dbmod.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            cursor = db.execute('''INSERT INTO catalog_description_runs(run_uid,collection_id,profile_id,provider,model,model_version,
                    params_json,external,opt_in_json,prompt_template_version,prompt_sha256,input_profile_sha256,status,error,sidecar_path)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                (run_uid, collection_id, profile_id, provider, result.model if result else 'unknown',
                                 result.model_version if result else 'unknown', _canonical(result.params if result else params),
                                 1 if adapter.external else 0, _canonical(opt_in), PROMPT_TEMPLATE_VERSION, prompt_sha, profile_sha,
                                 status, error, str(target)))
            run_id = int(cursor.lastrowid)
            annotation = None
            if result is not None:
                resolved, placeholders, warnings = resolve(result.text, profile)
                annotation_id = int(db.execute('''INSERT INTO catalog_annotations(run_id,collection_id,target_kind,target_id,model_text,
                        resolved_text,placeholders_json,warnings_json) VALUES(?,?,?,?,?,?,?,?)''',
                                               (run_id, collection_id, 'collection', collection_id, result.text, resolved,
                                                _canonical(placeholders), _canonical(warnings))).lastrowid)
                annotation = {
                    'format': SIDECAR_FORMAT, 'format_compat': 'unknown', 'record': 'hypothesis', 'status': 'hypothesis',
                    'annotation_id': annotation_id, 'run_id': run_uid, 'collection_id': collection_id,
                    'target': {'kind': 'collection', 'id': collection_id}, 'provider': provider, 'model': result.model,
                    'model_version': result.model_version, 'params': result.params, 'external': bool(adapter.external),
                    'opt_in': opt_in, 'prompt_template_version': PROMPT_TEMPLATE_VERSION, 'prompt_sha256': prompt_sha,
                    'input_profile_sha256': profile_sha, 'profile_id': profile_id, 'scan_ids': profile['scan_ids'],
                    'model_text': result.text, 'resolved_text': resolved, 'placeholders': placeholders,
                    'warnings': warnings, 'created_at': _now(),
                }
            db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)",
                       (actor, 'catalog_explain', 'catalog_description_run', run_id,
                        _canonical({'provider': provider, 'status': status, 'collection_id': collection_id})))
            if annotation is not None:
                _append_line(target, annotation)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'status': status, 'run_id': run_id, 'run_uid': run_uid, 'error': error, 'sidecar': str(target),
            'profile_id': profile_id, 'profile_sha256': profile_sha, 'annotation': annotation}


def decide(annotation_id: int, decision: str, *, actor: str, reason: str | None = None) -> dict:
    if decision not in ('promoted', 'rejected'):
        raise ValueError('decision must be promoted or rejected')
    if not actor or not actor.strip():
        raise ValueError('a human actor is required for a decision')
    with closing(dbmod.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('''SELECT a.id, a.collection_id, r.run_uid, r.sidecar_path FROM catalog_annotations a
                    JOIN catalog_description_runs r ON r.id=a.run_id WHERE a.id=?''', (annotation_id,)).fetchone()
            if row is None:
                raise NotFound('annotation does not exist')
            decision_id = int(db.execute('INSERT INTO catalog_annotation_decisions(annotation_id,decision,actor,reason) VALUES(?,?,?,?)',
                                         (annotation_id, decision, actor, reason)).lastrowid)
            db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)',
                       (actor, f'catalog_annotation_{decision}', 'catalog_annotation', annotation_id,
                        _canonical({'decision_id': decision_id, 'reason': reason})))
            record = {'format': SIDECAR_FORMAT, 'format_compat': 'unknown', 'record': 'decision', 'annotation_id': annotation_id,
                      'run_id': row['run_uid'], 'collection_id': row['collection_id'], 'decision': decision, 'actor': actor,
                      'reason': reason, 'decided_at': _now()}
            if row['sidecar_path']:
                _append_line(Path(row['sidecar_path']), record)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'decision_id': decision_id, **record}


def list_annotations(collection_id: int | None = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    with closing(dbmod.connect()) as db:
        sql = '''SELECT a.*, r.provider, r.model, r.model_version, r.run_uid, r.prompt_sha256, r.input_profile_sha256,
                 (SELECT decision FROM catalog_annotation_decisions d WHERE d.annotation_id=a.id ORDER BY d.id DESC LIMIT 1) AS decision
                 FROM catalog_annotations a JOIN catalog_description_runs r ON r.id=a.run_id'''
        params: list = []
        if collection_id is not None:
            sql += ' WHERE a.collection_id=?'
            params.append(collection_id)
        rows = db.execute(sql + ' ORDER BY a.id DESC LIMIT ?', (*params, limit)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['placeholders'] = json.loads(item.pop('placeholders_json'))
        item['warnings'] = json.loads(item.pop('warnings_json'))
        item['current_status'] = item['decision'] or 'hypothesis'
        result.append(item)
    return result

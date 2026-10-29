"""Stored, versioned import plans and human review.

A plan pins catalog scans, the detector table (version and SHA-256) and the
planner limits. Items and their member entries are immutable. Review appends a
plan version with per-item decisions (``proposed``/``approved``/``skipped``,
importer, parameters, opt-ins); the state of an item at version V is its latest
decision at or below V, else the item's defaults. Execution results are item
attempts; the displayed status adds ``done``/``failed``/``refused`` (and the
transient ``running``) for approved items.

Ambiguous items have no importer until a person picks one; opt-in importers
(location, browser history) cannot be approved until their opt-in is set;
billing needs an explicit profile. Bulk approval skips every blocked item and
says why.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .. import db as dbmod
from ..catalog import store
from . import PLANNER_VERSION
from .adapters import IMPORTERS
from .detect import PlanLimits, detect, load_detectors
from .reader import EntryReader

DECISION_STATUSES = ('proposed', 'approved', 'skipped')
DISPLAY_STATUSES = ('proposed', 'approved', 'skipped', 'running', 'done', 'failed', 'refused')
MAX_PAGE = 500
NotFound = store.NotFound


class Conflict(ValueError):
    """The plan changed since the version the caller reviewed."""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def config_sha256(importer: str | None, parameters: dict, opt_ins: dict) -> str:
    payload = _json({'importer': importer, 'parameters': parameters, 'opt_ins': opt_ins})
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


# ----------------------------------------------------------- temp directories

def source_roots() -> list[Path]:
    roots = {Path(p).resolve(strict=False) for p in dbmod.settings.scan_roots}
    with closing(dbmod.connect()) as db:
        for row in db.execute('SELECT DISTINCT root_path, root_kind FROM catalog_scans'):
            path = Path(row['root_path']).resolve(strict=False)
            roots.add(path if row['root_kind'] == 'directory' else path.parent)
    return sorted(roots)


def work_parent(temp_dir: str | Path | None = None) -> Path:
    """The parent of temporary directories; refused inside any catalogued or configured source root."""
    parent = Path(temp_dir or Path(dbmod.settings.data_dir) / 'importplan-work').expanduser().resolve(strict=False)
    for root in source_roots():
        if parent == root or root in parent.parents:
            raise ValueError('temporary directory must be outside every source root')
    parent.mkdir(parents=True, exist_ok=True)
    return parent


# ------------------------------------------------------------------- creation

def _scans(db, collection_ids: list[int] | None, scan_ids: list[int] | None) -> list[dict]:
    ids = list(scan_ids or [])
    for collection_id in collection_ids or []:
        if not db.execute('SELECT 1 FROM collections WHERE id=?', (collection_id,)).fetchone():
            raise NotFound(f'collection {collection_id} does not exist')
        ids.extend(store.current_scan_ids(db, collection_id))
    ids = sorted(set(ids))
    if not ids:
        raise ValueError('no finished catalog scans selected (choose collections with completed or partial scans)')
    rows = []
    for scan_id in ids:
        row = db.execute('SELECT id, case_id, collection_id, status, root_path FROM catalog_scans WHERE id=?', (scan_id,)).fetchone()
        if row is None:
            raise NotFound(f'catalog scan {scan_id} does not exist')
        if row['status'] not in ('completed', 'partial'):
            raise ValueError(f'catalog scan {scan_id} is {row["status"]}; only completed or partial scans can be planned')
        rows.append(dict(row))
    if len({r['case_id'] for r in rows}) != 1:
        raise ValueError('selected scans belong to different cases')
    return rows


def _validate_options(options: dict) -> dict:
    allowed = {'billing_profile_id', 'billing_profiles_path', 'recording_patterns_path', 'ledger_profiles_path'}
    unknown = set(options) - allowed
    if unknown:
        raise ValueError(f'unknown plan options: {sorted(unknown)}')
    clean = {k: v for k, v in options.items() if v not in (None, '')}
    for key in ('billing_profiles_path', 'recording_patterns_path', 'ledger_profiles_path'):
        if key in clean:
            path = Path(clean[key]).expanduser().resolve(strict=True)
            if not path.is_file():
                raise ValueError(f'{key} must be a file')
            clean[key] = str(path)
    if 'billing_profile_id' in clean:
        from ..importers.billing import load_profiles
        path = clean.get('billing_profiles_path')
        profiles = load_profiles(Path(path) if path else None)
        if clean['billing_profile_id'] not in {p['id'] for p in profiles['profiles']}:
            raise ValueError(f'billing profile not found: {clean["billing_profile_id"]}')
    return clean


def _default_opt_ins(candidates: list[dict]) -> dict:
    names = set()
    for candidate in candidates:
        importer = IMPORTERS.get(candidate['importer'])
        if importer:
            names.update(importer.required_opt_ins)
            names.update(importer.optional_opt_ins)
    return {name: False for name in sorted(names)}


def create_plan(*, collection_ids: list[int] | None = None, scan_ids: list[int] | None = None,
                options: dict | None = None, limits: PlanLimits | None = None,
                detectors_path: str | Path | None = None, temp_dir: str | Path | None = None,
                actor: str = 'user') -> dict:
    limits = limits or PlanLimits()
    options = _validate_options(dict(options or {}))
    table = load_detectors(detectors_path)
    with closing(dbmod.connect()) as db:
        scans = _scans(db, collection_ids, scan_ids)
    spool = Path(tempfile.mkdtemp(prefix='ageds-importplan-probe-', dir=work_parent(temp_dir)))
    try:
        with EntryReader(spool) as reader:
            detected = detect([s['id'] for s in scans], reader=reader, table=table, limits=limits, options=options)
            from .cross import pre_import
            cross = pre_import(detected['items'], reader, limits)
            probe_bytes = reader.bytes_read
    finally:
        shutil.rmtree(spool, ignore_errors=True)
    coverage = dict(detected['coverage'], bytes_read_by_planner=probe_bytes)
    items = detected['items']
    with closing(dbmod.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            cursor = db.execute('''INSERT INTO import_plans(case_id,planner_version,detector_schema,detector_version,
                    detector_sha256,scan_ids_json,collection_ids_json,options_json,limits_json,coverage_json,
                    cross_export_json,actor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (scans[0]['case_id'], PLANNER_VERSION, table['schema'], str(table['version']), table['_sha256'],
                 _json([s['id'] for s in scans]), _json(sorted({s['collection_id'] for s in scans})), _json(options),
                 _json(asdict(limits)), _json(coverage), _json(cross), actor))
            plan_id = int(cursor.lastrowid)
            for ordinal, item in enumerate(items, 1):
                for candidate in item.candidates:
                    importer = IMPORTERS[candidate['importer']]
                    candidate['required_opt_ins'] = list(importer.required_opt_ins)
                    candidate['optional_opt_ins'] = list(importer.optional_opt_ins)
                    candidate['params'] = [p.name for p in importer.params]
                    candidate['label'] = importer.label
                chosen = item.detection.get('chosen')
                row = db.execute('''INSERT INTO import_plan_items(plan_id,ordinal,item_key,unit_kind,collection_id,scan_id,
                        entry_id,locator,name,effective_type,candidates_json,ambiguous,default_importer,
                        default_parameters_json,default_opt_ins_json,detection_json,member_count,estimated_bytes)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (plan_id, ordinal, item.key, item.unit_kind, item.collection_id, item.scan_id, item.entry_id,
                     item.locator, item.name, item.effective_type, _json(item.candidates),
                     1 if item.detection.get('ambiguous') else 0, chosen, _json(item.default_params),
                     _json(_default_opt_ins(item.candidates)), _json(item.detection), len(item.members),
                     item.estimated_bytes))
                item_id = int(row.lastrowid)
                db.executemany('INSERT INTO import_plan_item_members(item_id,entry_id,role,relative_path) VALUES(?,?,?,?)',
                               [(item_id, entry_id, role, relative) for entry_id, role, relative in item.members])
            db.execute('INSERT INTO import_plan_versions(plan_id,version,actor,note) VALUES(?,?,?,?)',
                       (plan_id, 1, actor, 'generated by the planner (all items proposed)'))
            db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)",
                       (actor, 'import_plan_created', 'import_plan', plan_id,
                        _json({'scan_ids': [s['id'] for s in scans], 'items': len(items),
                               'detector_version': table['version'], 'detector_sha256': table['_sha256']})))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return get_plan(plan_id, limit=1)


# -------------------------------------------------------------------- reading

def _plan_row(db, plan_id: int) -> dict:
    row = db.execute('SELECT * FROM import_plans WHERE id=?', (plan_id,)).fetchone()
    if row is None:
        raise NotFound('import plan does not exist')
    plan = dict(row)
    for key in ('scan_ids_json', 'collection_ids_json', 'options_json', 'limits_json', 'coverage_json', 'cross_export_json'):
        plan[key[:-5]] = json.loads(plan.pop(key))
    plan['latest_version'] = db.execute('SELECT MAX(version) FROM import_plan_versions WHERE plan_id=?', (plan_id,)).fetchone()[0]
    return plan


def list_plans(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), MAX_PAGE))
    with closing(dbmod.connect()) as db:
        rows = db.execute('''SELECT p.id, p.created_at, p.actor, p.detector_version, p.scan_ids_json, p.collection_ids_json,
                (SELECT COUNT(*) FROM import_plan_items i WHERE i.plan_id=p.id) AS items,
                (SELECT MAX(version) FROM import_plan_versions v WHERE v.plan_id=p.id) AS latest_version,
                (SELECT COUNT(*) FROM import_plan_runs r WHERE r.plan_id=p.id) AS runs
                FROM import_plans p ORDER BY p.id DESC LIMIT ?''', (limit,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['scan_ids'] = json.loads(item.pop('scan_ids_json'))
        item['collection_ids'] = json.loads(item.pop('collection_ids_json'))
        result.append(item)
    return result


def _items(db, plan_id: int) -> list[dict]:
    items = []
    for row in db.execute('SELECT * FROM import_plan_items WHERE plan_id=? ORDER BY ordinal', (plan_id,)):
        item = dict(row)
        for key in ('candidates_json', 'default_parameters_json', 'default_opt_ins_json', 'detection_json'):
            item[key[:-5]] = json.loads(item.pop(key))
        item['ambiguous'] = bool(item['ambiguous'])
        items.append(item)
    return items


def _states(db, plan_id: int, version: int) -> dict[int, dict]:
    states = {}
    for row in db.execute('''SELECT d.*, v.version FROM import_plan_decisions d JOIN import_plan_versions v ON v.id=d.version_id
                             WHERE v.plan_id=? AND v.version<=? ORDER BY v.version, d.id''', (plan_id, version)):
        states[row['item_id']] = {'status': row['status'], 'importer': row['importer'],
                                  'parameters': json.loads(row['parameters_json']), 'opt_ins': json.loads(row['opt_ins_json']),
                                  'reason': row['reason'], 'decided_in_version': row['version']}
    return states


def _attempts(db, plan_id: int) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for row in db.execute('''SELECT a.id, a.run_id, a.item_id, a.attempt, a.status, a.importer, a.config_sha256, a.started_at,
                             a.finished_at, a.result_json, a.error, a.job_id FROM import_plan_attempts a
                             JOIN import_plan_items i ON i.id=a.item_id WHERE i.plan_id=? ORDER BY a.id''', (plan_id,)):
        attempt = dict(row)
        attempt['result'] = json.loads(attempt.pop('result_json') or '{}')
        found.setdefault(row['item_id'], []).append(attempt)
    return found


def done_attempts(plan_id: int) -> list[dict]:
    with closing(dbmod.connect()) as db:
        attempts = _attempts(db, plan_id)
    return [a for values in attempts.values() for a in values if a['status'] == 'done']


def state_of(item: dict, states: dict) -> dict:
    return states.get(item['id']) or {'status': 'proposed', 'importer': item['default_importer'],
                                      'parameters': dict(item['default_parameters']),
                                      'opt_ins': dict(item['default_opt_ins']), 'reason': None, 'decided_in_version': 1}


def blockers(item: dict, state: dict) -> list[str]:
    importer_key = state['importer']
    if not importer_key:
        return ['importer_not_chosen' + (':ambiguous_detection' if item['ambiguous'] else '')]
    if importer_key not in {c['importer'] for c in item['candidates']}:
        return [f'importer_not_a_candidate:{importer_key}']
    importer = IMPORTERS.get(importer_key)
    if importer is None:
        return [f'importer_not_registered:{importer_key}']
    found = []
    if item['unit_kind'] not in importer.unit_kinds:
        found.append(f'unit_kind_not_supported:{item["unit_kind"]}')
    for name in importer.required_opt_ins:
        if state['opt_ins'].get(name) is not True:
            found.append(f'opt_in_disabled:{name}')
    for param in importer.params:
        value = state['parameters'].get(param.name)
        if param.required and value in (None, ''):
            found.append(f'parameter_missing:{param.name}')
    # whatsapp_export with several chat texts and no chat_member imports each chat (adapters._whatsapp).
    return found


def _validate_parameters(item: dict, importer_key: str, parameters: dict, opt_ins: dict, plan_options: dict) -> None:
    importer = IMPORTERS[importer_key]
    known = {p.name: p for p in importer.params}
    unknown = set(parameters) - set(known)
    if unknown:
        raise ValueError(f'item {item["id"]}: unknown parameters for {importer_key}: {sorted(unknown)}')
    allowed_opt_ins = set(importer.required_opt_ins) | set(importer.optional_opt_ins)
    for name, value in opt_ins.items():
        if name not in set(item['default_opt_ins']) | allowed_opt_ins:
            raise ValueError(f'item {item["id"]}: unknown opt-in {name!r}')
        if not isinstance(value, bool):
            raise ValueError(f'item {item["id"]}: opt-in {name!r} must be true or false')
    for name, value in parameters.items():
        param = known[name]
        if value is None:
            continue
        if param.kind == 'int':
            if not isinstance(value, int) or isinstance(value, bool) or not param.minimum <= value <= param.maximum:
                raise ValueError(f'item {item["id"]}: {name} must be an integer {param.minimum}-{param.maximum}')
        elif not isinstance(value, str) or len(value) > 1000:
            raise ValueError(f'item {item["id"]}: {name} must be text')
    if importer_key == 'billing' and parameters.get('profile_id'):
        from ..importers.billing import load_profiles
        path = plan_options.get('billing_profiles_path')
        ids = {p['id'] for p in load_profiles(Path(path) if path else None)['profiles']}
        if parameters['profile_id'] not in ids:
            raise ValueError(f'item {item["id"]}: billing profile not found: {parameters["profile_id"]}')
    if importer_key == 'whatsapp_export' and parameters.get('chat_member'):
        if parameters['chat_member'] not in (item['detection'].get('chat_members') or []):
            raise ValueError(f'item {item["id"]}: chat_member must be one of the detected chat texts')


def _display(item: dict, state: dict, attempts: list[dict]) -> tuple[str, dict | None]:
    last = attempts[-1] if attempts else None
    if state['status'] != 'approved':
        return state['status'], last
    current = config_sha256(state['importer'], state['parameters'], state['opt_ins'])
    if any(a['status'] == 'done' and a['config_sha256'] == current for a in attempts):
        return 'done', last
    if last and last['status'] in ('running', 'failed', 'refused'):
        return last['status'], last
    return 'approved', last


def _item_view(item: dict, state: dict, attempts: list[dict], *, full: bool) -> dict:
    status, last = _display(item, state, attempts)
    view = {k: item[k] for k in ('id', 'ordinal', 'item_key', 'unit_kind', 'collection_id', 'scan_id', 'entry_id', 'locator',
                                 'name', 'effective_type', 'candidates', 'ambiguous', 'default_importer', 'member_count',
                                 'estimated_bytes')}
    view['context'] = item['detection'].get('context', [])
    view['chat_members'] = item['detection'].get('chat_members')
    view['state'] = state
    view['status'] = status
    view['blockers'] = blockers(item, state) if state['status'] != 'skipped' else []
    view['attempts'] = len(attempts)
    if last:
        view['last_attempt'] = {k: last[k] for k in ('id', 'run_id', 'attempt', 'status', 'importer', 'started_at',
                                                     'finished_at', 'error', 'job_id')}
        view['last_attempt']['result'] = last['result'] if full else {
            k: last['result'].get(k) for k in ('footprint', 'counts', 'artifact_ids', 'refusal', 'members_skipped')}
    if full:
        view['detection'] = item['detection']
    return view


def _filtered(views: list[dict], filters: dict) -> list[dict]:
    result = views
    if filters.get('importer'):
        wanted = filters['importer']
        result = [v for v in result if v['state']['importer'] == wanted or
                  (not v['state']['importer'] and wanted in {c['importer'] for c in v['candidates']})]
    if filters.get('status'):
        result = [v for v in result if v['status'] == filters['status']]
    if filters.get('ambiguous'):
        result = [v for v in result if v['ambiguous']]
    if filters.get('blocked'):
        result = [v for v in result if v['blockers']]
    if filters.get('collection_id') is not None:
        result = [v for v in result if v['collection_id'] == filters['collection_id']]
    if filters.get('q'):
        needle = filters['q'].lower()
        result = [v for v in result if needle in v['locator'].lower() or needle in v['name'].lower()]
    return result


def get_plan(plan_id: int, *, version: int | None = None, filters: dict | None = None, after_ordinal: int | None = None,
             limit: int = 200, full: bool = False) -> dict:
    limit = max(1, min(int(limit), MAX_PAGE))
    with closing(dbmod.connect()) as db:
        plan = _plan_row(db, plan_id)
        version = plan['latest_version'] if version is None else int(version)
        if not 1 <= version <= plan['latest_version']:
            raise NotFound('plan version does not exist')
        items = _items(db, plan_id)
        states = _states(db, plan_id, version)
        attempts = _attempts(db, plan_id)
        versions = [dict(r) for r in db.execute('''SELECT v.version, v.actor, v.note, v.created_at,
                (SELECT COUNT(*) FROM import_plan_decisions d WHERE d.version_id=v.id) AS decisions
                FROM import_plan_versions v WHERE v.plan_id=? ORDER BY v.version DESC LIMIT 50''', (plan_id,))]
        runs = [dict(r, summary=json.loads(r['summary_json'])) for r in db.execute(
            'SELECT * FROM import_plan_runs WHERE plan_id=? ORDER BY id DESC LIMIT 20', (plan_id,))]
        labels = {r['id']: r['label'] for r in db.execute('SELECT id, label FROM collections')}
    for run in runs:
        run.pop('summary_json')
        run['options'] = json.loads(run.pop('options_json'))
    views = [_item_view(i, state_of(i, states), attempts.get(i['id'], []), full=full) for i in items]
    for view in views:
        view['collection_label'] = labels.get(view['collection_id'])
    summary = _summary(plan, version, views)
    selected = _filtered(views, filters or {})
    if after_ordinal is not None:
        selected = [v for v in selected if v['ordinal'] > after_ordinal]
    page = selected[:limit]
    return {'plan': plan, 'version': version, 'versions': versions, 'runs': runs, 'summary': summary,
            'items': page, 'matching': len(selected), 'has_more': len(selected) > limit,
            'next_after_ordinal': page[-1]['ordinal'] if len(selected) > limit else None, 'limit': limit}


def get_item(plan_id: int, item_id: int, *, version: int | None = None) -> dict:
    with closing(dbmod.connect()) as db:
        plan = _plan_row(db, plan_id)
        version = plan['latest_version'] if version is None else int(version)
        item = next((i for i in _items(db, plan_id) if i['id'] == item_id), None)
        if item is None:
            raise NotFound('plan item does not exist')
        states = _states(db, plan_id, version)
        attempts = _attempts(db, plan_id).get(item_id, [])
        members = [dict(r) for r in db.execute('''SELECT m.entry_id, m.role, m.relative_path, e.locator, e.status, e.sha256,
                e.size_bytes, e.sniffed_type FROM import_plan_item_members m JOIN catalog_entries e ON e.id=m.entry_id
                WHERE m.item_id=? ORDER BY m.id LIMIT 500''', (item_id,))]
        history = [dict(r) for r in db.execute('''SELECT v.version, v.actor, v.created_at, d.status, d.importer, d.parameters_json,
                d.opt_ins_json, d.reason FROM import_plan_decisions d JOIN import_plan_versions v ON v.id=d.version_id
                WHERE d.item_id=? ORDER BY v.version''', (item_id,))]
    view = _item_view(item, state_of(item, states), attempts, full=True)
    view['members'] = members
    view['decision_history'] = history
    view['attempt_history'] = attempts
    return view


def _summary(plan: dict, version: int, views: list[dict]) -> dict:
    by_status = {s: 0 for s in DISPLAY_STATUSES}
    by_importer: dict[str, dict] = {}
    blocked: dict[str, int] = {}
    approved_bytes = approved_members = 0
    for view in views:
        by_status[view['status']] = by_status.get(view['status'], 0) + 1
        key = view['state']['importer'] or 'undecided:' + '|'.join(sorted(c['importer'] for c in view['candidates']))
        slot = by_importer.setdefault(key, {'items': 0, 'estimated_bytes': 0, **{s: 0 for s in DISPLAY_STATUSES}})
        slot['items'] += 1
        slot['estimated_bytes'] += view['estimated_bytes']
        slot[view['status']] += 1
        for reason in view['blockers']:
            name = reason.split(':')[0] if not reason.startswith(('opt_in_disabled', 'parameter_missing')) else reason
            blocked[name] = blocked.get(name, 0) + 1
        if view['state']['status'] == 'approved' and view['status'] != 'done':
            approved_bytes += view['estimated_bytes']
            approved_members += view['member_count']
    return {
        'plan_id': plan['id'], 'version': version, 'items': len(views), 'by_status': by_status,
        'by_importer': dict(sorted(by_importer.items())), 'blocked': dict(sorted(blocked.items())),
        'ambiguous': sum(1 for v in views if v['ambiguous']),
        'opt_in_disabled': sum(1 for v in views if any(b.startswith('opt_in_disabled') for b in v['blockers'])),
        'to_run': {'items': sum(1 for v in views if v['state']['status'] == 'approved' and v['status'] != 'done'
                                and not v['blockers']),
                   'estimated_bytes': approved_bytes, 'members': approved_members},
        'not_planned': plan['coverage'].get('not_planned', {}).get('counts', {}),
        'coverage_complete': plan['coverage'].get('complete'),
        'coverage_incomplete_reasons': plan['coverage'].get('incomplete_reasons', {}),
        'cross_export': {k: plan['cross_export'].get(k) for k in ('shared_conversations_total',)}
        | {'identical_conversation_file_sets': len(plan['cross_export'].get('identical_conversation_files', []))},
        'dry_run': 'read-only summary of the stored plan; nothing is read from sources or written',
    }


def summary(plan_id: int, version: int | None = None) -> dict:
    return get_plan(plan_id, version=version, limit=1)['summary']


# -------------------------------------------------------------------- review

def decide(plan_id: int, *, base_version: int, decisions: list[dict] | None = None, bulk: dict | None = None,
           actor: str, note: str | None = None) -> dict:
    """Append a plan version with item decisions; refused when ``base_version`` is not the latest."""
    if not actor or not str(actor).strip():
        raise ValueError('actor is required')
    decisions = list(decisions or [])
    with closing(dbmod.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            plan = _plan_row(db, plan_id)
            latest = plan['latest_version']
            if base_version != latest:
                raise Conflict(f'plan {plan_id} is at version {latest}; reload before deciding (base {base_version})')
            items = {i['id']: i for i in _items(db, plan_id)}
            states = _states(db, plan_id, latest)
            attempts = _attempts(db, plan_id)
            changes: dict[int, dict] = {}
            not_applied = []
            if bulk:
                action = bulk.get('action')
                if action not in ('approve', 'skip', 'propose'):
                    raise ValueError('bulk action must be approve, skip or propose')
                target = {'approve': 'approved', 'skip': 'skipped', 'propose': 'proposed'}[action]
                views = [_item_view(i, state_of(i, states), attempts.get(i['id'], []), full=False) for i in items.values()]
                selection = _filtered(views, bulk.get('filter') or {})
                if bulk.get('item_ids') is not None:
                    wanted = set(bulk['item_ids'])
                    selection = [v for v in selection if v['id'] in wanted]
                for view in selection:
                    current = view['state']
                    if current['status'] == target:
                        continue
                    if action == 'approve':
                        if current['status'] != 'proposed' and not bulk.get('include_skipped'):
                            continue
                        if view['blockers']:
                            not_applied.append({'item_id': view['id'], 'ordinal': view['ordinal'], 'blockers': view['blockers']})
                            continue
                    changes[view['id']] = {**current, 'status': target, 'reason': bulk.get('reason')}
            for decision in decisions:
                item_id = decision.get('item_id')
                if item_id not in items:
                    raise NotFound(f'plan item {item_id} does not belong to plan {plan_id}')
                item = items[item_id]
                current = changes.get(item_id) or state_of(item, states)
                new = {'status': decision.get('status', current['status']),
                       'importer': decision.get('importer', current['importer']),
                       'parameters': {**current['parameters'], **(decision.get('parameters') or {})},
                       'opt_ins': {**current['opt_ins'], **(decision.get('opt_ins') or {})},
                       'reason': decision.get('reason')}
                new['parameters'] = {k: v for k, v in new['parameters'].items() if v not in (None, '')}
                if new['status'] not in DECISION_STATUSES:
                    raise ValueError(f'item {item_id}: status must be one of {DECISION_STATUSES}')
                if new['importer'] is not None:
                    if new['importer'] not in {c['importer'] for c in item['candidates']}:
                        raise ValueError(f'item {item_id}: importer {new["importer"]!r} is not a candidate for this item')
                    _validate_parameters(item, new['importer'], new['parameters'], new['opt_ins'], plan['options'])
                if new['status'] == 'approved':
                    found = blockers(item, new)
                    if found:
                        raise ValueError(f'item {item_id} cannot be approved: {", ".join(found)}')
                changes[item_id] = new
            effective = {}
            for item_id, new in changes.items():
                old = state_of(items[item_id], states)
                if any(new[k] != old[k] for k in ('status', 'importer', 'parameters', 'opt_ins')):
                    effective[item_id] = new
            if not effective:
                db.rollback()
                return {'plan_id': plan_id, 'version': latest, 'changed': 0, 'not_applied': not_applied}
            version = latest + 1
            version_id = db.execute('INSERT INTO import_plan_versions(plan_id,version,actor,note) VALUES(?,?,?,?)',
                                    (plan_id, version, actor, note)).lastrowid
            db.executemany('''INSERT INTO import_plan_decisions(version_id,item_id,status,importer,parameters_json,opt_ins_json,reason)
                              VALUES(?,?,?,?,?,?,?)''',
                           [(version_id, item_id, new['status'], new['importer'], _json(new['parameters']),
                             _json(new['opt_ins']), new.get('reason')) for item_id, new in sorted(effective.items())])
            db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)",
                       (actor, 'import_plan_decided', 'import_plan', plan_id,
                        _json({'version': version, 'changed': len(effective), 'bulk': bulk.get('action') if bulk else None,
                               'by_status': {s: sum(1 for n in effective.values() if n['status'] == s) for s in DECISION_STATUSES},
                               'not_applied': len(not_applied)})))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'plan_id': plan_id, 'version': version, 'changed': len(effective), 'not_applied': not_applied}

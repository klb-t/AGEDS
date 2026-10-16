"""Preregistration: a hypothesis definition is hashed and stored BEFORE any analysis.

A definition is the hypothesis text plus its operationalization (stimulus and
outcome event filters, windows, lag histogram, null model, permutation count,
seed, …). It is validated strictly (unknown keys are refused so nothing is
silently ignored), normalized (defaults filled in, set-like lists sorted) and
hashed as canonical JSON. Versions are append-only and chained: each version
records the previous version's record hash. Registration proves only the order
of records in this database, not what the author had seen before registering;
the disclosure section reports how many inputs already existed at that time.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .hypotheses import (CANONICALIZATION, HypothesisError, HypothesisNotFound, canonical_json, clean_actor,
                         clean_text, normalize_selector, sha256_canonical, utc_now)

DEFINITION_SCHEMA = 'ageds.hypothesis-definition/v1'
NULL_MODELS = ('circular_shift', 'permutation', 'stratified_weekday_hour', 'block_permutation')
DIRECTIONS = ('increase', 'decrease', 'two_sided')
TOP_KEYS = ('schema', 'title', 'text', 'stimulus', 'outcome', 'density', 'eligible', 'windows', 'lags',
            'direction', 'analysis', 'dossier', 'notes')
ANALYSIS_DEFAULTS = {
    'permutations': 999,
    'seed': 1,
    'null_model': 'circular_shift',
    'block_s': 7 * 86400,
    'stratify_zone': 'UTC',
    'assume_zone': None,
    'span': None,
    'exclude_self_pairs': True,
    'density_excludes_stimuli': True,
    'density_excludes_outcomes': True,
    'control_period_s': 86400,
    'flag_threshold': 0.05,
    'density_ratio_warning': 1.5,
}
MAX_PERMUTATIONS = 100_000
MAX_SEED = 2**63 - 1
MAX_ABS_SECONDS = 10 * 366 * 86400
MAX_WINDOWS = 20
MAX_LAG_BINS = 500
MAX_TITLE_CHARS = 300
MAX_QUERIES = 20


def _int(value, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise HypothesisError(f'{name} must be an integer from {low} to {high}')
    return value


def _number(value, name: str, low: float, high: float, *, inclusive_high: bool = False) -> float | int:
    if type(value) not in (int, float) or not math.isfinite(value) or not low < value or \
            (value > high if inclusive_high else value >= high):
        raise HypothesisError(f'{name} must be a finite number in ({low}, {high}{"]" if inclusive_high else ")"}')
    return value


def _bool(value, name: str) -> bool:
    if type(value) is not bool:
        raise HypothesisError(f'{name} must be true or false')
    return value


def _zone(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise HypothesisError(f'{name} must be an IANA time zone name')
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HypothesisError(f'{name}: unknown time zone {value!r}') from exc
    return value


def _utc(value, name: str) -> str:
    if not isinstance(value, str):
        raise HypothesisError(f'{name} must be ISO-8601 text with an explicit offset')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise HypothesisError(f'{name} is not ISO-8601: {value!r}') from exc
    if parsed.tzinfo is None:
        raise HypothesisError(f'{name} needs an explicit offset; a wall-clock time has no position on the UTC axis')
    return parsed.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _windows(value) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_WINDOWS:
        raise HypothesisError(f'windows must be a list of 1 to {MAX_WINDOWS} windows')
    result, labels = [], set()
    for index, window in enumerate(value):
        if not isinstance(window, dict) or set(window) - {'label', 'start_s', 'end_s'}:
            raise HypothesisError(f'windows[{index}] must be {{label, start_s, end_s}}')
        label = clean_text(window.get('label'), f'windows[{index}].label', max_chars=80)
        if label in labels:
            raise HypothesisError(f'windows[{index}].label repeats {label!r}')
        labels.add(label)
        start = _int(window.get('start_s'), f'windows[{index}].start_s', -MAX_ABS_SECONDS, MAX_ABS_SECONDS)
        end = _int(window.get('end_s'), f'windows[{index}].end_s', -MAX_ABS_SECONDS, MAX_ABS_SECONDS)
        if end <= start:
            raise HypothesisError(f'windows[{index}] needs start_s < end_s (half-open [start, end) relative to the stimulus)')
        result.append({'label': label, 'start_s': start, 'end_s': end})
    return result


def _lags(value, windows: list[dict]) -> dict:
    low = min(w['start_s'] for w in windows)
    high = max(w['end_s'] for w in windows)
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {'min_s', 'max_s', 'bin_s'}:
        raise HypothesisError('lags must be {min_s, max_s, bin_s}')
    lag_min = _int(value.get('min_s', low), 'lags.min_s', -MAX_ABS_SECONDS, MAX_ABS_SECONDS)
    lag_max = _int(value.get('max_s', high), 'lags.max_s', -MAX_ABS_SECONDS, MAX_ABS_SECONDS)
    if lag_max <= lag_min:
        raise HypothesisError('lags needs min_s < max_s')
    default_bin = max(1, -(-(lag_max - lag_min) // 24))
    bin_s = _int(value.get('bin_s', default_bin), 'lags.bin_s', 1, MAX_ABS_SECONDS)
    if -(-(lag_max - lag_min) // bin_s) > MAX_LAG_BINS:
        raise HypothesisError(f'lags would produce more than {MAX_LAG_BINS} bins; increase bin_s')
    return {'min_s': lag_min, 'max_s': lag_max, 'bin_s': bin_s}


def _analysis(value) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise HypothesisError('analysis must be an object')
    unknown = sorted(set(value) - set(ANALYSIS_DEFAULTS))
    if unknown:
        raise HypothesisError('analysis has unknown keys: ' + ', '.join(unknown))
    merged = {**ANALYSIS_DEFAULTS, **value}
    result = {
        'permutations': _int(merged['permutations'], 'analysis.permutations', 1, MAX_PERMUTATIONS),
        'seed': _int(merged['seed'], 'analysis.seed', 0, MAX_SEED),
        'block_s': _int(merged['block_s'], 'analysis.block_s', 60, MAX_ABS_SECONDS),
        'stratify_zone': _zone(merged['stratify_zone'], 'analysis.stratify_zone'),
        'assume_zone': None if merged['assume_zone'] is None else _zone(merged['assume_zone'], 'analysis.assume_zone'),
        'exclude_self_pairs': _bool(merged['exclude_self_pairs'], 'analysis.exclude_self_pairs'),
        'density_excludes_stimuli': _bool(merged['density_excludes_stimuli'], 'analysis.density_excludes_stimuli'),
        'density_excludes_outcomes': _bool(merged['density_excludes_outcomes'], 'analysis.density_excludes_outcomes'),
        'control_period_s': _int(merged['control_period_s'], 'analysis.control_period_s', 60, MAX_ABS_SECONDS),
        'flag_threshold': _number(merged['flag_threshold'], 'analysis.flag_threshold', 0, 1),
        'density_ratio_warning': _number(merged['density_ratio_warning'], 'analysis.density_ratio_warning', 1, 1e6),
    }
    if merged['null_model'] not in NULL_MODELS:
        raise HypothesisError('analysis.null_model must be one of ' + ', '.join(NULL_MODELS))
    result['null_model'] = merged['null_model']
    span = merged['span']
    if span is not None:
        if not isinstance(span, dict) or set(span) != {'start_utc', 'end_utc'}:
            raise HypothesisError('analysis.span must be null or {start_utc, end_utc}')
        start, end = _utc(span['start_utc'], 'analysis.span.start_utc'), _utc(span['end_utc'], 'analysis.span.end_utc')
        if end <= start:
            raise HypothesisError('analysis.span needs start_utc < end_utc')
        span = {'start_utc': start, 'end_utc': end}
    result['span'] = span
    return result


def _dossier(value) -> dict:
    if value is None:
        return {'queries': []}
    if not isinstance(value, dict) or set(value) - {'queries'}:
        raise HypothesisError('dossier must be {queries: [FTS5 text, ...]}')
    queries = value.get('queries', [])
    if not isinstance(queries, list) or len(queries) > MAX_QUERIES:
        raise HypothesisError(f'dossier.queries must be a list of up to {MAX_QUERIES} FTS5 queries')
    checked = []
    for index, query in enumerate(queries):
        checked.append(normalize_selector({'text_query': query}, f'dossier.queries[{index}]', require_criterion=True)['text_query'])
    return {'queries': checked}


def normalize_definition(raw) -> dict:
    """Return the normalized definition (the exact object that is hashed)."""
    if not isinstance(raw, dict):
        raise HypothesisError('definition must be a JSON object')
    unknown = sorted(set(raw) - set(TOP_KEYS))
    if unknown:
        raise HypothesisError('definition has unknown keys: ' + ', '.join(unknown))
    if raw.get('schema', DEFINITION_SCHEMA) != DEFINITION_SCHEMA:
        raise HypothesisError(f'definition schema must be {DEFINITION_SCHEMA}')
    windows = _windows(raw.get('windows'))
    direction = raw.get('direction', 'increase')
    if direction not in DIRECTIONS:
        raise HypothesisError('direction must be one of ' + ', '.join(DIRECTIONS))
    definition = {
        'schema': DEFINITION_SCHEMA,
        'title': clean_text(raw.get('title'), 'title', max_chars=MAX_TITLE_CHARS),
        'text': clean_text(raw.get('text'), 'text'),
        'stimulus': normalize_selector(raw.get('stimulus'), 'stimulus', require_criterion=True),
        'outcome': normalize_selector(raw.get('outcome'), 'outcome', require_criterion=True),
        'density': normalize_selector(raw.get('density'), 'density', require_criterion=False),
        'eligible': normalize_selector(raw.get('eligible'), 'eligible', require_criterion=False),
        'windows': windows,
        'lags': _lags(raw.get('lags'), windows),
        'direction': direction,
        'analysis': _analysis(raw.get('analysis')),
        'dossier': _dossier(raw.get('dossier')),
        'notes': clean_text(raw.get('notes'), 'notes', required=False),
    }
    return definition


def preview(raw) -> dict:
    """Validate and hash without storing; the page shows this before registration."""
    definition = normalize_definition(raw)
    canonical = canonical_json(definition).decode('utf-8')
    return {'definition': definition, 'canonical': canonical, 'canonicalization': CANONICALIZATION,
            'definition_sha256': sha256_canonical(definition), 'stored': False}


def _record_hash(hypothesis_id: int, version_number: int, definition_sha256: str, registered_at: str,
                 previous_record_sha256: str | None, actor: str, change_note: str | None) -> str:
    return sha256_canonical({'hypothesis_id': hypothesis_id, 'version_number': version_number,
                             'definition_sha256': definition_sha256, 'registered_at': registered_at,
                             'previous_record_sha256': previous_record_sha256, 'actor': actor,
                             'change_note': change_note})


def _insert_version(db: sqlite3.Connection, hypothesis_id: int, definition: dict, actor: str,
                    change_note: str | None, previous: sqlite3.Row | None) -> int:
    canonical = canonical_json(definition).decode('utf-8')
    digest = sha256_canonical(definition)
    registered_at = utc_now()
    number = 1 if previous is None else previous['version_number'] + 1
    previous_record = None if previous is None else previous['record_sha256']
    record = _record_hash(hypothesis_id, number, digest, registered_at, previous_record, actor, change_note)
    cur = db.execute('''INSERT INTO hypothesis_versions(hypothesis_id,version_number,previous_version_id,definition_json,
        definition_sha256,canonicalization,registered_at,actor,change_note,previous_record_sha256,record_sha256)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (hypothesis_id, number, None if previous is None else previous['id'], canonical,
                                         digest, CANONICALIZATION, registered_at, actor, change_note, previous_record, record))
    vid = int(cur.lastrowid)
    db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES (?,?,?,?,?)",
               (actor, 'hypothesis_version_registered', 'hypothesis_version', vid,
                json.dumps({'hypothesis_id': hypothesis_id, 'version_number': number, 'definition_sha256': digest},
                           ensure_ascii=False)))
    return vid


def register_hypothesis(db: sqlite3.Connection, raw_definition, *, actor=None, change_note=None) -> dict:
    """Create a hypothesis with version 1. The caller's connection is committed here."""
    definition = normalize_definition(raw_definition)
    actor = clean_actor(actor)
    change_note = clean_text(change_note, 'change_note', max_chars=2000, required=False)
    db.execute('BEGIN IMMEDIATE')
    try:
        hid = int(db.execute('INSERT INTO hypotheses(created_at,actor) VALUES(?,?)', (utc_now(), actor)).lastrowid)
        vid = _insert_version(db, hid, definition, actor, change_note, None)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {**get_version(db, vid), 'created': True}


def register_version(db: sqlite3.Connection, hypothesis_id: int, raw_definition, *, actor=None, change_note=None) -> dict:
    """Append a new version; an unchanged definition returns the latest version (``created: false``)."""
    definition = normalize_definition(raw_definition)
    actor = clean_actor(actor)
    change_note = clean_text(change_note, 'change_note', max_chars=2000, required=False)
    db.execute('BEGIN IMMEDIATE')
    try:
        if not db.execute('SELECT 1 FROM hypotheses WHERE id=?', (hypothesis_id,)).fetchone():
            raise HypothesisNotFound(f'hypothesis {hypothesis_id} does not exist')
        latest = db.execute('SELECT * FROM hypothesis_versions WHERE hypothesis_id=? ORDER BY version_number DESC LIMIT 1',
                            (hypothesis_id,)).fetchone()
        if latest['definition_sha256'] == sha256_canonical(definition):
            db.rollback()
            return {**get_version(db, int(latest['id'])), 'created': False}
        vid = _insert_version(db, hypothesis_id, definition, actor, change_note, latest)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {**get_version(db, vid), 'created': True}


def verify_version_row(row: sqlite3.Row) -> dict:
    """Recompute both hashes from the stored row; a mismatch means the row was altered."""
    try:
        definition = json.loads(row['definition_json'])
        canonical_ok = canonical_json(definition).decode('utf-8') == row['definition_json']
        definition_ok = sha256_canonical(definition) == row['definition_sha256']
    except (ValueError, TypeError):
        definition, canonical_ok, definition_ok = None, False, False
    record_ok = _record_hash(row['hypothesis_id'], row['version_number'], row['definition_sha256'], row['registered_at'],
                             row['previous_record_sha256'], row['actor'], row['change_note']) == row['record_sha256']
    status = 'hash_matches' if canonical_ok and definition_ok and record_ok else 'hash_mismatch'
    return {'status': status, 'definition_hash_ok': definition_ok, 'canonical_ok': canonical_ok,
            'record_hash_ok': record_ok, 'definition': definition}


def get_version(db: sqlite3.Connection, version_id: int) -> dict:
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM hypothesis_versions WHERE id=?', (version_id,)).fetchone()
    if row is None:
        raise HypothesisNotFound(f'hypothesis version {version_id} does not exist')
    check = verify_version_row(row)
    analysed = db.execute("SELECT MIN(created_at) AS first, COUNT(*) AS n FROM hypothesis_runs "
                          "WHERE hypothesis_version_id=? AND kind='event_study'", (version_id,)).fetchone()
    return {
        'hypothesis_id': row['hypothesis_id'], 'version_id': row['id'], 'version_number': row['version_number'],
        'previous_version_id': row['previous_version_id'], 'definition': check['definition'],
        'definition_sha256': row['definition_sha256'], 'canonicalization': row['canonicalization'],
        'registered_at': row['registered_at'], 'first_analysed_at': analysed['first'],
        'event_study_runs': analysed['n'], 'actor': row['actor'], 'change_note': row['change_note'],
        'previous_record_sha256': row['previous_record_sha256'], 'record_sha256': row['record_sha256'],
        'integrity': {k: check[k] for k in ('status', 'definition_hash_ok', 'canonical_ok', 'record_hash_ok')},
    }


def require_registered(db: sqlite3.Connection, version_id: int) -> dict:
    """The only entry point analyses use: refuses unknown or altered definitions."""
    version = get_version(db, version_id)
    if version['integrity']['status'] != 'hash_matches':
        raise HypothesisError(f'hypothesis version {version_id} no longer matches its registered hash; analysis refused')
    return version


def list_versions(db: sqlite3.Connection, hypothesis_id: int) -> list[dict]:
    ids = [r[0] for r in db.execute('SELECT id FROM hypothesis_versions WHERE hypothesis_id=? ORDER BY version_number',
                                    (hypothesis_id,))]
    if not ids:
        raise HypothesisNotFound(f'hypothesis {hypothesis_id} does not exist')
    return [get_version(db, vid) for vid in ids]


def list_hypotheses(db: sqlite3.Connection, *, limit: int = 200, offset: int = 0) -> dict:
    db.row_factory = sqlite3.Row
    rows = db.execute('''SELECT h.id, h.created_at, h.actor,
          (SELECT v.id FROM hypothesis_versions v WHERE v.hypothesis_id=h.id ORDER BY v.version_number DESC LIMIT 1) AS latest
          FROM hypotheses h ORDER BY h.id LIMIT ? OFFSET ?''', (limit + 1, offset)).fetchall()
    items = []
    for row in rows[:limit]:
        latest = get_version(db, row['latest'])
        first = db.execute('''SELECT MIN(r.created_at) FROM hypothesis_runs r JOIN hypothesis_versions v
                              ON v.id=r.hypothesis_version_id WHERE v.hypothesis_id=? AND r.kind='event_study' ''',
                           (row['id'],)).fetchone()[0]
        items.append({'hypothesis_id': row['id'], 'created_at': row['created_at'], 'title': (latest['definition'] or {}).get('title'),
                      'latest_version_id': latest['version_id'], 'latest_version_number': latest['version_number'],
                      'latest_definition_sha256': latest['definition_sha256'], 'registered_at': latest['registered_at'],
                      'first_analysed_at': first, 'integrity': latest['integrity']['status']})
    return {'hypotheses': items, 'limit': limit, 'offset': offset, 'has_more': len(rows) > limit}

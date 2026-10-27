"""Evidence dossier: pinned pointers to sources, human classification, candidate proposals.

An item points to exactly one stored record and pins its version: an event
(with a hash of its body), an artifact (content hash, optionally one acquisition
observation), a derived-text version, a citation anchor (quote hash and
selector) or an annotation (body hash at the time it was added). The excerpt is
copied from the database, never from model text.

Classification is a human decision recorded as an append-only row
(``supporting`` / ``contradicting`` / ``context`` / ``rejected``, or back to
``candidate``). Automatic proposals — FTS hits, deterministic rules and an
optional LLM provider interface — only ever create ``candidate`` items. The LLM
interface is inert by default (provider ``none``); any other provider needs an
explicit per-run opt-in that is recorded in the run, and its output is
validated against the database and kept as labelled model text.
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass

from .hypotheses import (DISCLAIMER, HypothesisError, HypothesisNotFound, canonical_json, clean_actor, clean_text,
                         code_version, disclaimer, selector_matches, sha256_canonical, sha256_text, utc_now)
from .preregistration import require_registered

TARGET_KINDS = ('event', 'artifact', 'derived_text', 'anchor', 'annotation')
# Parsed rows of an event that an event item can pin (like an acquisition of an artifact item).
EVENT_RECORDS = ('ledger_entry_id', 'health_sample_id')
CLASSIFICATIONS = ('candidate', 'supporting', 'contradicting', 'context', 'rejected')
HUMAN_CLASSES = CLASSIFICATIONS
EXCERPT_CHARS = 500
MAX_CANDIDATES = 500
MAX_RATIONALE_CHARS = 5000
FTS_KIND_MAP = {'event': 'event', 'derived_text': 'derived_text', 'artifact': 'artifact'}


# ---------------------------------------------------------------- locators

def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:EXCERPT_CHARS]


def _event_record(db: sqlite3.Connection, event_id: int, *, ledger_entry_id, health_sample_id) -> tuple[dict, str] | None:
    """A ledger row or health sample parsed from this event's source, pinned by its own row hash."""
    if ledger_entry_id is not None and health_sample_id is not None:
        raise HypothesisError('give at most one of ledger_entry_id and health_sample_id')
    if ledger_entry_id is not None:
        row = db.execute('SELECT * FROM ledger_entries WHERE id=? AND event_id=?', (ledger_entry_id, event_id)).fetchone()
        if row is None:
            raise HypothesisNotFound(f'ledger entry {ledger_entry_id} of event {event_id} does not exist')
        record = {'table': 'ledger_entries', 'id': row['id'], 'artifact_id': row['artifact_id'],
                  'statement_id': row['statement_id'], 'parser': row['parser'], 'parser_version': row['parser_version'],
                  'source_position': row['source_position'], 'row_sha256': row['row_sha256']}
        text = ' · '.join(str(v) for v in (row['record_kind'],
                                           f"{row['amount_exact']} {row['currency'] or ''}".strip() if row['amount_exact'] else None,
                                           row['counterparty_name'], row['description']) if v)
        return record, text
    if health_sample_id is not None:
        row = db.execute('SELECT * FROM health_samples WHERE id=? AND session_event_id=?', (health_sample_id, event_id)).fetchone()
        if row is None:
            raise HypothesisNotFound(f'health sample {health_sample_id} of event {event_id} does not exist')
        record = {'table': 'health_samples', 'id': row['id'], 'artifact_id': row['artifact_id'], 'parser': row['parser'],
                  'parser_version': row['parser_version'], 'source_position': row['source_position'],
                  'row_sha256': row['row_sha256']}
        text = ' · '.join(str(v) for v in (row['start_raw'], row['sample_type'],
                                           f"{row['value_raw']} {row['unit'] or ''}".strip() if row['value_raw'] else None,
                                           row['basis']) if v)
        return record, text
    return None


def locate(db: sqlite3.Connection, kind: str, target_id: int, *, observation_id: int | None = None,
           ledger_entry_id: int | None = None, health_sample_id: int | None = None) -> tuple[dict, str | None]:
    """Return ``(locator, excerpt)`` pinning the current version of one record, or raise NotFound."""
    db.row_factory = sqlite3.Row
    if kind not in TARGET_KINDS:
        raise HypothesisError('target kind must be one of ' + ', '.join(TARGET_KINDS))
    if type(target_id) is not int or target_id < 1:
        raise HypothesisError('target id must be a positive integer')
    if observation_id is not None and kind != 'artifact':
        raise HypothesisError('observation_id applies to artifact items only')
    if (ledger_entry_id is not None or health_sample_id is not None) and kind != 'event':
        raise HypothesisError('ledger_entry_id and health_sample_id apply to event items only')
    for value in (ledger_entry_id, health_sample_id):
        if value is not None and (type(value) is not int or value < 1):
            raise HypothesisError('record ids must be positive integers')
    if kind == 'event':
        row = db.execute('SELECT id,artifact_id,source_id,event_type,external_id,subject,body FROM events WHERE id=?',
                         (target_id,)).fetchone()
        if row is None:
            raise HypothesisNotFound(f'event {target_id} does not exist')
        locator = {'kind': 'event', 'event_id': row['id'], 'artifact_id': row['artifact_id'], 'source_id': row['source_id'],
                   'event_type': row['event_type'], 'external_id': row['external_id'],
                   'body_sha256': sha256_text(row['body'] or '')}
        text = ((row['subject'] + ' — ') if row['subject'] else '') + (row['body'] or '')
        found = _event_record(db, row['id'], ledger_entry_id=ledger_entry_id, health_sample_id=health_sample_id)
        if found is not None:  # only then is the key present, so existing event locators keep their hash
            locator['record'], text = found
        return locator, _excerpt(text)
    if kind == 'artifact':
        row = db.execute('SELECT id,sha256,original_name,size_bytes FROM artifacts WHERE id=?', (target_id,)).fetchone()
        if row is None:
            raise HypothesisNotFound(f'artifact {target_id} does not exist')
        observation = None
        if observation_id is not None:
            obs = db.execute('SELECT id,source_id,source_locator,observed_at,acquisition_kind,sha256 FROM source_observations '
                             'WHERE id=? AND artifact_id=?', (observation_id, target_id)).fetchone()
            if obs is None:
                raise HypothesisNotFound(f'observation {observation_id} of artifact {target_id} does not exist')
            observation = dict(obs)
        locator = {'kind': 'artifact', 'artifact_id': row['id'], 'sha256': row['sha256'], 'size_bytes': row['size_bytes'],
                   'observation_id': observation_id, 'observation': observation}
        return locator, _excerpt(row['original_name'])
    if kind == 'derived_text':
        row = db.execute('SELECT id,artifact_id,kind,run_id,text FROM derived_text WHERE id=?', (target_id,)).fetchone()
        if row is None:
            raise HypothesisNotFound(f'derived text {target_id} does not exist')
        locator = {'kind': 'derived_text', 'derived_text_id': row['id'], 'artifact_id': row['artifact_id'],
                   'text_kind': row['kind'], 'run_id': row['run_id'], 'text_sha256': sha256_text(row['text'])}
        return locator, _excerpt(row['text'])
    if kind == 'anchor':
        row = db.execute('SELECT * FROM evidence_anchors WHERE id=?', (target_id,)).fetchone()
        if row is None:
            raise HypothesisNotFound(f'citation anchor {target_id} does not exist')
        locator = {'kind': 'anchor', 'anchor_id': row['id'], 'artifact_id': row['artifact_id'],
                   'derived_text_id': row['derived_text_id'], 'start_ms': row['start_ms'], 'end_ms': row['end_ms'],
                   'quote_sha256': row['quote_sha256'], 'selector': json.loads(row['selector_json'])}
        return locator, _excerpt(row['quote_text'])
    row = db.execute('SELECT * FROM annotations WHERE id=?', (target_id,)).fetchone()
    if row is None:
        raise HypothesisNotFound(f'annotation {target_id} does not exist')
    locator = {'kind': 'annotation', 'annotation_id': row['id'], 'artifact_id': row['artifact_id'], 'event_id': row['event_id'],
               'derived_text_id': row['derived_text_id'], 'start_ms': row['start_ms'], 'end_ms': row['end_ms'],
               'body_sha256': sha256_text(row['body']), 'updated_at': row['updated_at']}
    return locator, _excerpt(row['body'])


def _verify(db: sqlite3.Connection, item: sqlite3.Row) -> str:
    stored = json.loads(item['locator_json'])
    try:
        record = stored.get('record') or {}
        current, _ = locate(db, item['target_kind'], item['target_id'], observation_id=stored.get('observation_id'),
                            ledger_entry_id=record.get('id') if record.get('table') == 'ledger_entries' else None,
                            health_sample_id=record.get('id') if record.get('table') == 'health_samples' else None)
    except HypothesisNotFound:
        return 'missing'
    return 'matches' if sha256_canonical(current) == item['locator_sha256'] else 'changed'


# ---------------------------------------------------------------- items and decisions

def _hypothesis_latest_version(db: sqlite3.Connection, hypothesis_id: int) -> int:
    row = db.execute('SELECT id FROM hypothesis_versions WHERE hypothesis_id=? ORDER BY version_number DESC LIMIT 1',
                     (hypothesis_id,)).fetchone()
    if row is None:
        raise HypothesisNotFound(f'hypothesis {hypothesis_id} does not exist')
    return int(row[0])


def _version_of(db: sqlite3.Connection, hypothesis_id: int, version_id: int | None) -> int:
    if version_id is None:
        return _hypothesis_latest_version(db, hypothesis_id)
    row = db.execute('SELECT hypothesis_id FROM hypothesis_versions WHERE id=?', (version_id,)).fetchone()
    if row is None or row[0] != hypothesis_id:
        raise HypothesisError(f'version {version_id} is not a version of hypothesis {hypothesis_id}')
    return version_id


def _insert_item(db, hypothesis_id, kind, target_id, locator, excerpt, origin, run_id, metadata, actor) -> tuple[int, bool]:
    digest = sha256_canonical(locator)
    existing = db.execute('SELECT id FROM hypothesis_dossier_items WHERE hypothesis_id=? AND locator_sha256=?',
                          (hypothesis_id, digest)).fetchone()
    if existing:
        return int(existing[0]), False
    cur = db.execute('''INSERT INTO hypothesis_dossier_items(hypothesis_id,target_kind,target_id,locator_json,locator_sha256,
        excerpt,origin,origin_run_id,metadata_json,created_at,actor) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                     (hypothesis_id, kind, target_id, canonical_json(locator).decode('utf-8'), digest, excerpt, origin,
                      run_id, canonical_json(metadata).decode('utf-8'), utc_now(), actor))
    return int(cur.lastrowid), True


def _decide(db, item_id, version_id, classification, decided_by, rationale, actor) -> int:
    cur = db.execute('''INSERT INTO hypothesis_dossier_decisions(item_id,hypothesis_version_id,classification,decided_by,
        rationale,created_at,actor) VALUES(?,?,?,?,?,?,?)''',
                     (item_id, version_id, classification, decided_by, rationale, utc_now(), actor))
    db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES (?,?,?,?,?)",
               (actor, 'dossier_decision', 'hypothesis_dossier_item', item_id,
                json.dumps({'classification': classification, 'decided_by': decided_by, 'version_id': version_id})))
    return int(cur.lastrowid)


def add_item(db: sqlite3.Connection, hypothesis_id: int, kind: str, target_id: int, *, observation_id: int | None = None,
             classification: str | None = None, rationale: str | None = None, version_id: int | None = None,
             note: str | None = None, actor=None, ledger_entry_id: int | None = None,
             health_sample_id: int | None = None) -> dict:
    """Human adds a pointer; with ``classification`` it is classified in the same transaction."""
    actor = clean_actor(actor)
    rationale = clean_text(rationale, 'rationale', max_chars=MAX_RATIONALE_CHARS, required=False)
    note = clean_text(note, 'note', max_chars=MAX_RATIONALE_CHARS, required=False)
    if classification is not None and classification not in HUMAN_CLASSES:
        raise HypothesisError('classification must be one of ' + ', '.join(HUMAN_CLASSES))
    db.execute('BEGIN IMMEDIATE')
    try:
        version_id = _version_of(db, hypothesis_id, version_id)
        locator, excerpt = locate(db, kind, target_id, observation_id=observation_id, ledger_entry_id=ledger_entry_id,
                                  health_sample_id=health_sample_id)
        item_id, created = _insert_item(db, hypothesis_id, kind, target_id, locator, excerpt, 'human', None,
                                        {'note': note} if note else {}, actor)
        if created or classification is not None:
            _decide(db, item_id, version_id, classification or 'candidate', 'human', rationale, actor)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {**get_item(db, item_id), 'created': created}


def classify(db: sqlite3.Connection, item_id: int, classification: str, *, rationale: str | None = None,
             version_id: int | None = None, actor=None) -> dict:
    """Append a human decision; earlier decisions remain as the audit trail."""
    actor = clean_actor(actor)
    rationale = clean_text(rationale, 'rationale', max_chars=MAX_RATIONALE_CHARS, required=False)
    if classification not in HUMAN_CLASSES:
        raise HypothesisError('classification must be one of ' + ', '.join(HUMAN_CLASSES))
    db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute('SELECT hypothesis_id FROM hypothesis_dossier_items WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise HypothesisNotFound(f'dossier item {item_id} does not exist')
        version_id = _version_of(db, int(row[0]), version_id)
        _decide(db, item_id, version_id, classification, 'human', rationale, actor)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_item(db, item_id)


def get_item(db: sqlite3.Connection, item_id: int) -> dict:
    db.row_factory = sqlite3.Row
    item = db.execute('SELECT * FROM hypothesis_dossier_items WHERE id=?', (item_id,)).fetchone()
    if item is None:
        raise HypothesisNotFound(f'dossier item {item_id} does not exist')
    decisions = [dict(r) for r in db.execute('''SELECT d.id, d.classification, d.decided_by, d.rationale, d.created_at,
        d.actor, d.hypothesis_version_id, v.version_number FROM hypothesis_dossier_decisions d
        JOIN hypothesis_versions v ON v.id = d.hypothesis_version_id WHERE d.item_id=? ORDER BY d.id''', (item_id,))]
    current = decisions[-1] if decisions else None
    return {'item_id': item['id'], 'hypothesis_id': item['hypothesis_id'], 'target_kind': item['target_kind'],
            'target_id': item['target_id'], 'locator': json.loads(item['locator_json']),
            'locator_sha256': item['locator_sha256'], 'excerpt': item['excerpt'], 'origin': item['origin'],
            'origin_run_id': item['origin_run_id'], 'metadata': json.loads(item['metadata_json']),
            'created_at': item['created_at'], 'actor': item['actor'],
            'classification': current['classification'] if current else 'candidate',
            'classified_by': current['decided_by'] if current else 'system',
            'human_reviewed': any(d['decided_by'] == 'human' and d['classification'] != 'candidate' for d in decisions),
            'decisions': decisions, 'source_check': _verify(db, item)}


def list_dossier(db: sqlite3.Connection, hypothesis_id: int) -> dict:
    _hypothesis_latest_version(db, hypothesis_id)
    ids = [r[0] for r in db.execute('SELECT id FROM hypothesis_dossier_items WHERE hypothesis_id=? ORDER BY id', (hypothesis_id,))]
    items = [get_item(db, i) for i in ids]
    counts = {c: 0 for c in CLASSIFICATIONS}
    for item in items:
        counts[item['classification']] += 1
    return {'disclaimer': disclaimer(), 'hypothesis_id': hypothesis_id, 'items': items, 'counts': counts,
            'note': 'Klasyfikacja jest decyzją człowieka; propozycje automatyczne pozostają kandydatami do akceptacji.'}


def disconfirmation_status(db: sqlite3.Connection, hypothesis_id: int) -> dict:
    """Was material that would count against the hypothesis preregistered, searched and reviewed?"""
    latest = db.execute('''SELECT id, definition_json FROM hypothesis_versions WHERE hypothesis_id=?
                           ORDER BY version_number DESC LIMIT 1''', (hypothesis_id,)).fetchone()
    if latest is None:
        raise HypothesisNotFound(f'hypothesis {hypothesis_id} does not exist')
    dossier_def = json.loads(latest['definition_json']).get('dossier', {})
    runs = db.execute('''SELECT r.hypothesis_version_id, r.parameters_json, r.result_json FROM hypothesis_runs r
                         JOIN hypothesis_versions v ON v.id=r.hypothesis_version_id
                         WHERE v.hypothesis_id=? AND r.kind='candidate_search' ORDER BY r.id''', (hypothesis_id,)).fetchall()
    contrary_locators, latest_runs = set(), 0
    for run in runs:
        if json.loads(run['parameters_json']).get('direction') not in ('disconfirming', 'both'):
            continue
        latest_runs += run['hypothesis_version_id'] == latest['id']
        for proposal in json.loads(run['result_json']).get('proposals', []):
            if 'disconfirming' in ((proposal.get('hint') or {}).get('search_directions') or []):
                contrary_locators.add(proposal['locator_sha256'])
    by_class = {c: 0 for c in CLASSIFICATIONS}
    for row in db.execute('SELECT id, locator_sha256 FROM hypothesis_dossier_items WHERE hypothesis_id=?', (hypothesis_id,)):
        if row['locator_sha256'] in contrary_locators:
            by_class[get_item(db, row['id'])['classification']] += 1
    return {'preregistered_queries': len(dossier_def.get('disconfirming_queries', [])),
            'preregistered_criteria': bool(dossier_def.get('disconfirmation_criteria')),
            'searches_for_latest_version': latest_runs,
            'contrary_hits_by_classification': by_class,
            'note': 'a hit of a disconfirming query is a candidate, not a contradiction; finding none is not proof '
                    'that none exists (see documentation gaps)'}


# ---------------------------------------------------------------- candidate proposals

@dataclass
class CandidateRequest:
    """What a provider may see: the hypothesis and DB-derived excerpts of the candidate pool."""
    hypothesis_text: str
    definition_sha256: str
    pool: list[dict]


class CandidateProvider:
    name = 'abstract'
    model = 'unknown'
    model_version = 'unknown'
    external = True

    def propose(self, request: CandidateRequest) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError


class NoneProvider(CandidateProvider):
    """Default: no model, no call, no proposals."""
    name = 'none'
    model = 'none'
    model_version = 'none'
    external = False

    def propose(self, request: CandidateRequest) -> list[dict]:
        return []


PROVIDERS: dict[str, CandidateProvider] = {'none': NoneProvider()}


def register_provider(provider: CandidateProvider) -> None:
    """Make a provider selectable. No real provider ships in this repository."""
    if not provider.name or provider.name == 'none':
        raise HypothesisError('provider needs a distinct name')
    PROVIDERS[provider.name] = provider


def _rule_hits(db: sqlite3.Connection, definition: dict, limit: int, category_config=None) -> list[dict]:
    from .eventstudy import load_inputs, uncertainty_s
    params = definition['analysis']
    loaded, fts, cats, _ = load_inputs(db, definition, params, category_config=category_config)
    threshold = params['max_time_uncertainty_s'] if params['max_time_uncertainty_s'] is not None else definition['lags']['bin_s']
    # same operationalization as the event study: coarse times follow the registered policy
    items = [i for i in loaded['items'] if params['imprecise_times'] == 'keep'
             or (uncertainty_s(i) is None or uncertainty_s(i) <= threshold)]
    stim = [i for i in items if selector_matches(i, definition['stimulus'], fts_ids=fts.get('stimulus'),
                                                 category_ids=cats.get('stimulus'))]
    out = [i for i in items if selector_matches(i, definition['outcome'], fts_ids=fts.get('outcome'),
                                                category_ids=cats.get('outcome'))]
    hits = [{'kind': 'event', 'id': i['event_id'], 'hint': {'role': 'stimulus'}, 't': i['t_start']} for i in stim]
    stim_ids = {i['event_id'] for i in stim}
    stim_sorted = sorted(stim, key=lambda i: (i['t_start'], i['event_id']))
    stim_times = [i['t_start'] for i in stim_sorted]
    low = min(w['start_s'] for w in definition['windows'])
    high = max(w['end_s'] for w in definition['windows'])
    for o in out:
        if o['event_id'] in stim_ids:
            continue
        best = None
        # only stimuli with lag = o - s in [low, high) can fall in any window
        for s in stim_sorted[bisect_right(stim_times, o['t_start'] - high):bisect_right(stim_times, o['t_start'] - low)]:
            lag = o['t_start'] - s['t_start']
            for w in definition['windows']:
                if w['start_s'] <= lag < w['end_s'] and (best is None or abs(lag) < abs(best['lag_s'])):
                    best = {'role': 'outcome_in_window', 'window': w['label'], 'lag_s': round(lag, 3),
                            'stimulus_event_id': s['event_id']}
        if best:
            hits.append({'kind': 'event', 'id': o['event_id'], 'hint': best, 't': o['t_start']})
    hits.sort(key=lambda h: (h['t'], h['id']))
    return [{k: v for k, v in h.items() if k != 't'} for h in hits[:limit]]


DIRECTIONS = ('definition', 'disconfirming', 'both')


def candidate_search(db: sqlite3.Connection, hypothesis_id: int, *, version_id: int | None = None,
                     queries: list[str] | None = None, use_rules: bool = True, provider: str = 'none',
                     opt_in: bool = False, limit: int = 50, actor=None, category_config=None,
                     direction: str | None = None) -> dict:
    """Propose candidates (FTS + rules + optional provider); every proposal stays ``candidate``.

    ``direction``: ``definition`` (the supporting queries), ``disconfirming`` (the preregistered
    queries for material that would count against the hypothesis) or ``both`` (default when the
    definition has disconfirming queries). Each FTS hint names its ``search_direction``; a hit is
    still only a candidate, and its direction is a search label, not a classification.
    """
    from ..search import SearchQueryError, search_page
    db.row_factory = sqlite3.Row
    actor = clean_actor(actor)
    if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES:
        raise HypothesisError(f'limit must be an integer from 1 to {MAX_CANDIDATES}')
    if type(use_rules) is not bool or type(opt_in) is not bool:
        raise HypothesisError('use_rules and opt_in must be true or false')
    if provider not in PROVIDERS:
        raise HypothesisError(f'unknown candidate provider {provider!r}; available: {", ".join(sorted(PROVIDERS))}')
    chosen = PROVIDERS[provider]
    if provider != 'none' and not opt_in:
        raise HypothesisError(f'provider {provider!r} needs an explicit per-run opt-in (opt_in=true); nothing was sent')
    version_id = _version_of(db, hypothesis_id, version_id)
    version = require_registered(db, version_id)
    definition = version['definition']
    contrary = list(definition['dossier'].get('disconfirming_queries', []))
    if direction is None:
        direction = 'both' if contrary and queries is None else 'definition'
    if direction not in DIRECTIONS:
        raise HypothesisError(f'direction must be one of {", ".join(DIRECTIONS)}')
    if direction != 'definition' and not contrary:
        raise HypothesisError('the registered definition has no dossier.disconfirming_queries; register a version with them')
    if queries is None:
        queries = list(definition['dossier']['queries']) or [definition[n]['text_query'] for n in ('stimulus', 'outcome')
                                                              if 'text_query' in definition[n]]
    if not isinstance(queries, list) or any(not isinstance(q, str) or not q.strip() for q in queries) or len(queries) > 20:
        raise HypothesisError('queries must be a list of up to 20 nonblank FTS5 queries')
    planned = ([(q, 'definition') for q in queries] if direction in ('definition', 'both') else []) + \
              ([(q, 'disconfirming') for q in contrary] if direction in ('disconfirming', 'both') else [])
    fts_report, proposals = [], []
    for query, search_direction in planned:
        try:
            page = search_page(query, min(limit, 200))
        except SearchQueryError as exc:
            raise HypothesisError(f'query {query!r}: {exc}') from exc
        fts_report.append({'query': query, 'search_direction': search_direction, 'results': len(page['results']),
                           'has_more': page['has_more']})
        for hit in page['results']:
            kind = FTS_KIND_MAP.get(hit['object_kind'])
            if kind:
                proposals.append({'origin': 'fts', 'kind': kind, 'id': int(hit['object_id']),
                                  'hint': {'query': query, 'search_direction': search_direction}})
    rule_hits = _rule_hits(db, definition, limit, category_config) if use_rules else []
    proposals.extend({'origin': 'rule', **h} for h in rule_hits)
    pool = []
    for proposal in proposals:
        try:
            locator, excerpt = locate(db, proposal['kind'], proposal['id'])
        except HypothesisNotFound:
            continue
        pool.append({'kind': proposal['kind'], 'id': proposal['id'], 'excerpt': excerpt})
    rejected, provider_count = [], 0
    if provider != 'none':
        returned = chosen.propose(CandidateRequest(definition['text'], version['definition_sha256'], pool[:limit]))
        if not isinstance(returned, list):
            raise HypothesisError('provider returned a non-list; nothing stored')
        for index, raw in enumerate(returned[:limit]):
            provider_count += 1
            if not isinstance(raw, dict) or raw.get('target_kind') not in TARGET_KINDS or type(raw.get('target_id')) is not int:
                rejected.append({'index': index, 'reason': 'malformed proposal (needs target_kind and integer target_id)'})
                continue
            try:
                locate(db, raw['target_kind'], raw['target_id'])
            except (HypothesisNotFound, HypothesisError) as exc:
                rejected.append({'index': index, 'reason': f'reference does not resolve: {exc}'})
                continue
            rationale = raw.get('rationale')
            hint = {'model_rationale': rationale[:MAX_RATIONALE_CHARS] if isinstance(rationale, str) else None,
                    'model_proposed_classification': raw.get('proposed_classification')
                    if raw.get('proposed_classification') in CLASSIFICATIONS else None,
                    'label': 'model output; a hypothesis about relevance, not evidence'}
            proposals.append({'origin': 'llm', 'kind': raw['target_kind'], 'id': raw['target_id'], 'hint': hint})
    directions: dict[tuple, set] = {}
    for proposal in proposals:
        found = proposal['hint'].get('search_direction') if isinstance(proposal.get('hint'), dict) else None
        if found:
            directions.setdefault((proposal['origin'], proposal['kind'], proposal['id']), set()).add(found)
    unique, seen = [], set()
    for proposal in proposals:
        key = (proposal['origin'], proposal['kind'], proposal['id'])
        if key in seen:
            continue
        seen.add(key)
        if key in directions:  # a hit of both directions says so
            proposal = {**proposal, 'hint': {**proposal['hint'], 'search_directions': sorted(directions[key])}}
        try:
            locator, excerpt = locate(db, proposal['kind'], proposal['id'])
        except HypothesisNotFound:
            rejected.append({'origin': proposal['origin'], 'kind': proposal['kind'], 'id': proposal['id'],
                             'reason': 'record vanished'})
            continue
        unique.append({**proposal, 'locator': locator, 'excerpt': excerpt, 'locator_sha256': sha256_canonical(locator)})
    parameters = {'queries': queries, 'direction': direction,
                  'disconfirming_queries': contrary if direction != 'definition' else [],
                  'use_rules': use_rules, 'provider': provider, 'provider_model': chosen.model,
                  'provider_model_version': chosen.model_version, 'opt_in': opt_in, 'limit': limit}
    input_record = [[p['origin'], p['kind'], p['id'], p['locator_sha256']] for p in unique]
    result = {'schema': 'ageds.dossier-candidates/v1', 'disclaimer': disclaimer(),
              'hypothesis': {'hypothesis_id': hypothesis_id, 'version_id': version_id,
                             'definition_sha256': version['definition_sha256']},
              'parameters': parameters, 'fts': fts_report, 'rule_hits': len(rule_hits),
              'provider': {'name': provider, 'opt_in': opt_in, 'external': chosen.external, 'returned': provider_count},
              'proposals': [{'origin': p['origin'], 'kind': p['kind'], 'id': p['id'], 'locator_sha256': p['locator_sha256'],
                             'hint': p['hint']} for p in unique],
              'rejected': rejected, 'status_of_all_proposals': 'candidate'}
    db.execute('BEGIN IMMEDIATE')
    try:
        cur = db.execute('''INSERT INTO hypothesis_runs(hypothesis_version_id,kind,definition_sha256,parameters_json,seed,
            code_version,input_sha256,input_count,provider,provider_opt_in,result_json,result_sha256,created_at,actor)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (version_id, 'candidate_search', version['definition_sha256'], canonical_json(parameters).decode('utf-8'),
                          None, code_version(), sha256_canonical(input_record), len(unique), provider, int(opt_in),
                          canonical_json(result).decode('utf-8'), sha256_canonical(result), utc_now(), actor))
        run_id = int(cur.lastrowid)
        added, present = [], []
        for proposal in unique:
            item_id, created = _insert_item(db, hypothesis_id, proposal['kind'], proposal['id'], proposal['locator'],
                                            proposal['excerpt'], proposal['origin'], run_id, {'hint': proposal['hint']}, actor)
            if created:
                _decide(db, item_id, version_id, 'candidate', 'system', None, 'system')
                added.append(item_id)
            elif item_id not in added and item_id not in present:
                present.append(item_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {'run_id': run_id, 'added_item_ids': added, 'already_present_item_ids': present, 'rejected': rejected,
            'fts': fts_report, 'rule_hits': len(rule_hits), 'provider': result['provider'],
            'result_sha256': sha256_canonical(result), 'disclaimer': disclaimer()}


__all__ = ['CLASSIFICATIONS', 'CandidateProvider', 'CandidateRequest', 'DISCLAIMER', 'NoneProvider', 'PROVIDERS',
           'TARGET_KINDS', 'add_item', 'candidate_search', 'classify', 'get_item', 'list_dossier', 'locate',
           'register_provider']

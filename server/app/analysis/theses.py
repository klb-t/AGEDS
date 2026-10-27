"""Thesis typology and epistemic disclosure for a hypothesis dossier.

Each thesis carries one status from the typology (quote → paraphrase →
inference chain → analogy → speculation → counterfactual → normative thesis)
and points to dossier items. Flags such as ``unsupported`` are COMPUTED on
every read from the stored links and decisions; they are never stored, so they
cannot drift from the evidence. Revisions are new rows linked to the previous
thesis; nothing is overwritten.

The disclosure has four quadrants (known knowns, known unknowns, unknown
knowns, unknown unknowns). Human text is append-only per quadrant; computed
facts (exclusions, gaps, unreviewed material, inputs that already existed at
registration) are derived from the stored runs and records at read time.
"""
from __future__ import annotations

import json
import sqlite3

from .hypotheses import HypothesisError, HypothesisNotFound, clean_actor, clean_text, disclaimer, utc_now

TYPOLOGY = (
    ('quote', 'cytat', 'quote'),
    ('paraphrase', 'parafraza', 'paraphrase'),
    ('inference_chain', 'łańcuch inferencji', 'inference chain'),
    ('analogy', 'analogia', 'analogy'),
    ('speculation', 'spekulacja', 'speculation'),
    ('counterfactual', 'kontrfaktyk', 'counterfactual'),
    ('normative', 'teza normatywna', 'normative thesis'),
)
STATUS_TYPES = tuple(t[0] for t in TYPOLOGY)
EXACT_TEXT_KINDS = ('anchor', 'event', 'derived_text', 'annotation')
QUADRANTS = (
    ('known_knowns', 'Wiemy, że wiemy', 'Known knowns'),
    ('known_unknowns', 'Wiemy, czego nie wiemy', 'Known unknowns'),
    ('unknown_knowns', 'Nie uwzględniamy tego, co wiemy', 'Unknown knowns'),
    ('unknown_unknowns', 'Nie wiemy, czego nie wiemy', 'Unknown unknowns'),
)
QUADRANT_IDS = tuple(q[0] for q in QUADRANTS)
FLAG_TEXT = {
    'unsupported': ('brak wskaźnika do dowodu', 'no pointer to evidence'),
    'evidence_not_accepted': ('wskazane elementy nie zostały zaakceptowane przez człowieka',
                              'referenced items have not been accepted by a human'),
    'quote_without_exact_source': ('cytat bez źródła o dokładnym tekście', 'quote without an exact-text source'),
    'source_changed': ('źródło zmieniło się od dodania do dossier', 'a source changed since it was added'),
    'superseded': ('zastąpiona nowszą wersją', 'superseded by a newer revision'),
    'no_disconfirmation_search': ('nie szukano materiału sprzecznego (brak wyszukiwania z zapytaniami przeciwnymi '
                                  'dla bieżącej wersji hipotezy)',
                                  'no search for contrary material (no disconfirming-query search for the current version)'),
    'contrary_candidates_unreviewed': ('trafienia zapytań przeciwnych czekają na ocenę człowieka',
                                       'hits of disconfirming queries await human review'),
}


def add_thesis(db: sqlite3.Connection, hypothesis_id: int, text: str, status_type: str, evidence_item_ids: list[int] | None,
               *, previous_thesis_id: int | None = None, actor=None) -> dict:
    actor = clean_actor(actor)
    text = clean_text(text, 'thesis text', max_chars=10_000)
    if status_type not in STATUS_TYPES:
        raise HypothesisError('status_type must be one of ' + ', '.join(STATUS_TYPES))
    evidence_item_ids = evidence_item_ids or []
    if not isinstance(evidence_item_ids, list) or any(type(i) is not int or i < 1 for i in evidence_item_ids) \
            or len(evidence_item_ids) > 1000:
        raise HypothesisError('evidence_item_ids must be a list of up to 1000 positive integers')
    db.execute('BEGIN IMMEDIATE')
    try:
        if not db.execute('SELECT 1 FROM hypotheses WHERE id=?', (hypothesis_id,)).fetchone():
            raise HypothesisNotFound(f'hypothesis {hypothesis_id} does not exist')
        if previous_thesis_id is not None:
            prev = db.execute('SELECT hypothesis_id FROM hypothesis_theses WHERE id=?', (previous_thesis_id,)).fetchone()
            if prev is None or prev[0] != hypothesis_id:
                raise HypothesisError(f'thesis {previous_thesis_id} is not a thesis of hypothesis {hypothesis_id}')
            if db.execute('SELECT 1 FROM hypothesis_theses WHERE previous_thesis_id=?', (previous_thesis_id,)).fetchone():
                raise HypothesisError(f'thesis {previous_thesis_id} already has a revision; revise the latest one')
        for item_id in evidence_item_ids:
            row = db.execute('SELECT hypothesis_id FROM hypothesis_dossier_items WHERE id=?', (item_id,)).fetchone()
            if row is None or row[0] != hypothesis_id:
                raise HypothesisError(f'dossier item {item_id} does not belong to hypothesis {hypothesis_id}')
        cur = db.execute('''INSERT INTO hypothesis_theses(hypothesis_id,previous_thesis_id,status_type,text,created_at,actor)
                            VALUES(?,?,?,?,?,?)''', (hypothesis_id, previous_thesis_id, status_type, text, utc_now(), actor))
        thesis_id = int(cur.lastrowid)
        db.executemany('INSERT INTO hypothesis_thesis_evidence(thesis_id,item_id) VALUES(?,?)',
                       [(thesis_id, i) for i in sorted(set(evidence_item_ids))])
        db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES (?,?,?,?,?)",
                   (actor, 'thesis_added', 'hypothesis_thesis', thesis_id,
                    json.dumps({'hypothesis_id': hypothesis_id, 'status_type': status_type,
                                'previous_thesis_id': previous_thesis_id})))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return next(t for t in list_theses(db, hypothesis_id)['theses'] if t['thesis_id'] == thesis_id)


def _flags(status_type: str, items: list[dict], superseded: bool) -> list[str]:
    flags = []
    if not items:
        flags.append('unsupported')
    elif not any(i['classification'] in ('supporting', 'contradicting', 'context') for i in items):
        flags.append('evidence_not_accepted')
    if status_type == 'quote' and items and not any(i['target_kind'] in EXACT_TEXT_KINDS for i in items):
        flags.append('quote_without_exact_source')
    if any(i['source_check'] != 'matches' for i in items):
        flags.append('source_changed')
    if superseded:
        flags.append('superseded')
    return flags


def list_theses(db: sqlite3.Connection, hypothesis_id: int) -> dict:
    from .dossier import get_item
    db.row_factory = sqlite3.Row
    if not db.execute('SELECT 1 FROM hypotheses WHERE id=?', (hypothesis_id,)).fetchone():
        raise HypothesisNotFound(f'hypothesis {hypothesis_id} does not exist')
    rows = db.execute('SELECT * FROM hypothesis_theses WHERE hypothesis_id=? ORDER BY id', (hypothesis_id,)).fetchall()
    superseded = {r['previous_thesis_id']: r['id'] for r in rows if r['previous_thesis_id'] is not None}
    from .dossier import disconfirmation_status
    contrary = disconfirmation_status(db, hypothesis_id) if rows else None
    labels = {t[0]: (index + 1, t[1], t[2]) for index, t in enumerate(TYPOLOGY)}
    cache: dict[int, dict] = {}
    theses = []
    for row in rows:
        ids = [r[0] for r in db.execute('SELECT item_id FROM hypothesis_thesis_evidence WHERE thesis_id=? ORDER BY item_id', (row['id'],))]
        items = []
        for item_id in ids:
            if item_id not in cache:
                cache[item_id] = get_item(db, item_id)
            items.append(cache[item_id])
        flags = _flags(row['status_type'], items, row['id'] in superseded)
        # About the hypothesis's search for contrary material, not about this thesis's own evidence.
        contrary_flags = [] if row['id'] in superseded else (
            (['no_disconfirmation_search'] if not contrary['searches_for_latest_version'] else [])
            + (['contrary_candidates_unreviewed'] if contrary['contrary_hits_by_classification']['candidate'] else []))
        rank, pl, en = labels[row['status_type']]
        theses.append({
            'thesis_id': row['id'], 'hypothesis_id': hypothesis_id, 'text': row['text'], 'status_type': row['status_type'],
            'status_rank': rank, 'status_label_pl': pl, 'status_label_en': en,
            'previous_thesis_id': row['previous_thesis_id'], 'superseded_by': superseded.get(row['id']),
            'evidence': [{'item_id': i['item_id'], 'target_kind': i['target_kind'], 'target_id': i['target_id'],
                          'classification': i['classification'], 'source_check': i['source_check']} for i in items],
            'flags': flags, 'flags_text': {f: FLAG_TEXT[f] for f in flags}, 'unsupported': 'unsupported' in flags,
            'disconfirmation_flags': contrary_flags,
            'disconfirmation_flags_text': {f: FLAG_TEXT[f] for f in contrary_flags},
            'flags_computed': True, 'created_at': row['created_at'], 'actor': row['actor'],
        })
    current = [t for t in theses if t['superseded_by'] is None]
    return {'disclaimer': disclaimer(), 'hypothesis_id': hypothesis_id,
            'typology': [{'id': t[0], 'rank': i + 1, 'pl': t[1], 'en': t[2]} for i, t in enumerate(TYPOLOGY)],
            'theses': theses, 'current_count': len(current),
            'unsupported_current': sum(1 for t in current if t['unsupported']),
            # Review 2 P3: pointers only to candidates or rejected items are not accepted evidence either.
            'without_accepted_evidence_current': sum(1 for t in current if {'unsupported', 'evidence_not_accepted'} & set(t['flags']))}


# ---------------------------------------------------------------- disclosure

def add_disclosure(db: sqlite3.Connection, hypothesis_id: int, quadrant: str, text: str, *, actor=None) -> dict:
    actor = clean_actor(actor)
    text = clean_text(text, 'disclosure text', max_chars=20_000)
    if quadrant not in QUADRANT_IDS:
        raise HypothesisError('quadrant must be one of ' + ', '.join(QUADRANT_IDS))
    db.execute('BEGIN IMMEDIATE')
    try:
        if not db.execute('SELECT 1 FROM hypotheses WHERE id=?', (hypothesis_id,)).fetchone():
            raise HypothesisNotFound(f'hypothesis {hypothesis_id} does not exist')
        prev = db.execute('SELECT id FROM hypothesis_disclosures WHERE hypothesis_id=? AND quadrant=? ORDER BY id DESC LIMIT 1',
                          (hypothesis_id, quadrant)).fetchone()
        db.execute('''INSERT INTO hypothesis_disclosures(hypothesis_id,quadrant,text,previous_id,created_at,actor)
                      VALUES(?,?,?,?,?,?)''', (hypothesis_id, quadrant, text, prev[0] if prev else None, utc_now(), actor))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return disclosure_view(db, hypothesis_id)


def _latest_event_study(db: sqlite3.Connection, hypothesis_id: int) -> dict | None:
    row = db.execute('''SELECT r.id FROM hypothesis_runs r JOIN hypothesis_versions v ON v.id=r.hypothesis_version_id
                        WHERE v.hypothesis_id=? AND r.kind='event_study' ORDER BY r.id DESC LIMIT 1''', (hypothesis_id,)).fetchone()
    if row is None:
        return None
    from .eventstudy import get_run
    return get_run(db, int(row[0]))


def _sqlite_seconds(utc_text: str) -> str:
    return utc_text[:19].replace('T', ' ')


def disclosure_view(db: sqlite3.Connection, hypothesis_id: int) -> dict:
    from .dossier import list_dossier
    db.row_factory = sqlite3.Row
    versions = db.execute('SELECT * FROM hypothesis_versions WHERE hypothesis_id=? ORDER BY version_number', (hypothesis_id,)).fetchall()
    if not versions:
        raise HypothesisNotFound(f'hypothesis {hypothesis_id} does not exist')
    first, latest = versions[0], versions[-1]
    run = _latest_event_study(db, hypothesis_id)
    result = run['result'] if run else None
    dossier = list_dossier(db, hypothesis_id)
    theses = list_theses(db, hypothesis_id)
    total_events = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    before_first = db.execute('SELECT COUNT(*) FROM events WHERE created_at < ?', (_sqlite_seconds(first['registered_at']),)).fetchone()[0]
    referenced = {e['item_id'] for t in theses['theses'] if t['superseded_by'] is None for e in t['evidence']}
    accepted_unused = [i['item_id'] for i in dossier['items']
                       if i['classification'] in ('supporting', 'contradicting') and i['item_id'] not in referenced]

    known_knowns = [
        {'fact': 'registered_versions', 'value': len(versions)},
        {'fact': 'latest_definition_sha256', 'value': latest['definition_sha256']},
        {'fact': 'latest_registered_at', 'value': latest['registered_at']},
        {'fact': 'dossier_counts', 'value': dossier['counts']},
    ]
    from .dossier import disconfirmation_status
    contrary = disconfirmation_status(db, hypothesis_id)
    known_unknowns = [
        {'fact': 'candidates_awaiting_human_review', 'value': dossier['counts']['candidate']},
        {'fact': 'disconfirmation_search', 'value': contrary,
         'flagged': (not contrary['preregistered_queries'] or not contrary['searches_for_latest_version']
                     or contrary['contrary_hits_by_classification']['candidate'] > 0)},
        {'fact': 'current_theses_without_evidence', 'value': theses['unsupported_current']},
        {'fact': 'current_theses_without_accepted_evidence', 'value': theses['without_accepted_evidence_current'],
         'note': 'no pointer, or pointers only to candidates/rejected items (flags unsupported, evidence_not_accepted)'},
        {'fact': 'items_whose_source_changed_or_vanished',
         'value': sum(1 for i in dossier['items'] if i['source_check'] != 'matches')},
    ]
    unknown_knowns = [
        {'fact': 'events_imported_before_first_registration', 'value': before_first, 'of_total_events': total_events,
         'note': 'Prerejestracja dowodzi kolejności zapisów w tej bazie, nie tego, czego autor nie widział wcześniej '
                 '(porównanie z dokładnością do sekundy).'},
        {'fact': 'accepted_items_not_used_by_any_current_thesis', 'value': len(accepted_unused), 'item_ids': accepted_unused[:200]},
        {'fact': 'contradicting_items', 'value': dossier['counts']['contradicting']},
    ]
    unknown_unknowns = [
        {'fact': 'undocumented_events', 'value': 'unknown',
         'note': 'Zdarzenia nieudokumentowane lub niezaimportowane nie mogą zostać policzone; brak zapisu nie oznacza braku zdarzenia.'},
    ]
    if result:
        inp = result['input']
        known_knowns.append({'fact': 'latest_event_study', 'run_id': run['run_id'], 'result_sha256': run['result_sha256'],
                             'status': result['status'], 'stimulus_count': inp['stimulus_count'],
                             'outcome_count': inp['outcome_count'], 'density_count': inp['density_count'],
                             'windows': [{'label': w['label'], 'p_naive': w['naive']['p_value'],
                                          'p_normalized': w['normalized'].get('p_value')} for w in result['windows']]})
        known_unknowns += [
            {'fact': 'untimed_events_excluded', 'value': inp['untimed_excluded']['count'],
             'by_reason': inp['untimed_excluded']['by_reason']},
            {'fact': 'events_outside_span', 'value': inp['outside_span']},
            {'fact': 'flagged_warnings', 'value': [w['id'] for w in result['warnings'] if w['flagged']]},
            {'fact': 'weak_time_basis', 'value': next(w for w in result['warnings'] if w['id'] == 'time_basis')['computed']['weak_count']},
        ]
        if result['null_model'].get('strata_insufficient'):
            known_unknowns.append({'fact': 'strata_without_enough_eligible_times', 'value': result['null_model']['strata_insufficient']})
        series = result.get('control_series') or {}
        unknown_unknowns.append({'fact': 'documentation_gaps', 'empty_periods': series.get('empty_periods'),
                                 'period_s': series.get('period_s'), 'longest_gaps': series.get('longest_gaps', [])[:5]})
    else:
        known_unknowns.append({'fact': 'event_study', 'value': 'not run yet'})
    computed = {'known_knowns': known_knowns, 'known_unknowns': known_unknowns, 'unknown_knowns': unknown_knowns,
                'unknown_unknowns': unknown_unknowns}
    quadrants = []
    for qid, pl, en in QUADRANTS:
        history = [dict(r) for r in db.execute('''SELECT id, text, previous_id, created_at, actor FROM hypothesis_disclosures
                                                   WHERE hypothesis_id=? AND quadrant=? ORDER BY id''', (hypothesis_id, qid))]
        quadrants.append({'quadrant': qid, 'label_pl': pl, 'label_en': en,
                          'human_text': history[-1]['text'] if history else None,
                          'human_history': history, 'computed_facts': computed[qid]})
    return {'disclaimer': disclaimer(), 'hypothesis_id': hypothesis_id, 'quadrants': quadrants,
            'latest_event_study_run_id': run['run_id'] if run else None}

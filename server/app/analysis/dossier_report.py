"""Report payload of a hypothesis dossier (track E2 → reports, track F).

``report_payload(db, hypothesis_id)`` assembles, read-only, what a dossier
report prints: the preregistered versions with their hash chain and integrity
check, the latest definition (operationalization), dossier items with their
current human classification and source check, theses with the typology and
the flags computed on read (``unsupported`` …), event-study runs with their
stored results (windows, warnings, exclusions), the disclosure quadrants and
the fixed hashed disclaimer label of this layer (``hypotheses.DISCLAIMER``,
reused, never copied). Every number comes from stored results or SQL counts.
The rows read are returned under ``inputs`` for the report's method record and
anchor pointers under ``anchors`` for the citation bundle.
"""
from __future__ import annotations

import sqlite3

from . import dossier, eventstudy, preregistration, theses
from .hypotheses import disclaimer

SCHEMA = 'ageds.hypothesis-dossier-report-payload/v1'
MAX_RUNS = 50


def _window_rows(run: dict) -> list[dict]:
    out = []
    for window in run['result'].get('windows') or []:
        naive, normalized = window.get('naive') or {}, window.get('normalized') or {}
        out.append({'run_id': run['run_id'], 'window': window.get('label'), 'start_s': window.get('start_s'),
                    'end_s': window.get('end_s'), 'observed_pairs': naive.get('observed'),
                    'null_mean_pairs': naive.get('null_mean'), 'null_q025': naive.get('null_q025'),
                    'null_q975': naive.get('null_q975'), 'p_value': naive.get('p_value'),
                    'k_extreme': naive.get('k_extreme'), 'n_permutations': naive.get('n_permutations'),
                    'stimuli_with_outcome': naive.get('stimuli_with_outcome'),
                    'unique_outcomes_in_windows': naive.get('unique_outcomes_in_windows'),
                    'normalized_observed': normalized.get('observed'), 'normalized_null_mean': normalized.get('null_mean'),
                    'normalized_p_value': normalized.get('p_value'), 'normalized_undefined': normalized.get('n_undefined'),
                    'p_formula': naive.get('p_formula')})
    return out


def _warning_rows(run: dict) -> list[dict]:
    return [{'run_id': run['run_id'], 'warning': w.get('id'), 'flagged': w.get('flagged'), 'severity': w.get('severity'),
             'text_pl': w.get('text_pl'), 'text_en': w.get('text_en'), 'computed': w.get('computed')}
            for w in run['result'].get('warnings') or []]


def report_payload(db: sqlite3.Connection, hypothesis_id: int) -> dict:
    db.row_factory = sqlite3.Row
    hypothesis_id = int(hypothesis_id)
    versions = preregistration.list_versions(db, hypothesis_id)  # raises HypothesisNotFound
    latest = versions[-1]
    definition = latest['definition'] or {}
    listed = dossier.list_dossier(db, hypothesis_id)
    thesis_view = theses.list_theses(db, hypothesis_id)
    disclosure = theses.disclosure_view(db, hypothesis_id)
    run_summaries = eventstudy.list_runs(db, hypothesis_id)
    runs = [eventstudy.get_run(db, r['id']) for r in run_summaries[-MAX_RUNS:]]
    event_studies = [r for r in runs if r['kind'] == 'event_study']
    inputs: dict[str, list] = {'hypotheses': [dict(db.execute('SELECT * FROM hypotheses WHERE id=?', (hypothesis_id,)).fetchone())]}
    inputs['hypothesis_versions'] = [dict(r) for r in db.execute(
        'SELECT * FROM hypothesis_versions WHERE hypothesis_id=? ORDER BY version_number', (hypothesis_id,))]
    inputs['hypothesis_runs'] = [dict(db.execute('SELECT * FROM hypothesis_runs WHERE id=?', (r['run_id'],)).fetchone())
                                 for r in runs]
    inputs['hypothesis_dossier_items'] = [dict(r) for r in db.execute(
        'SELECT * FROM hypothesis_dossier_items WHERE hypothesis_id=? ORDER BY id', (hypothesis_id,))]
    inputs['hypothesis_dossier_decisions'] = [dict(r) for r in db.execute(
        '''SELECT d.* FROM hypothesis_dossier_decisions d JOIN hypothesis_dossier_items i ON i.id=d.item_id
           WHERE i.hypothesis_id=? ORDER BY d.id''', (hypothesis_id,))]
    inputs['hypothesis_theses'] = [dict(r) for r in db.execute(
        'SELECT * FROM hypothesis_theses WHERE hypothesis_id=? ORDER BY id', (hypothesis_id,))]
    inputs['hypothesis_disclosures'] = [dict(r) for r in db.execute(
        'SELECT * FROM hypothesis_disclosures WHERE hypothesis_id=? ORDER BY id', (hypothesis_id,))]
    anchors = []
    for item in listed['items']:
        locator = item['locator']
        if item['target_kind'] == 'anchor' and locator.get('artifact_id') is not None:
            anchors.append({'item_id': item['item_id'], 'artifact_id': locator['artifact_id'], 'anchor_id': item['target_id'],
                            'classification': item['classification']})
    exclusions = []
    for run in event_studies:
        untimed = (run['result'].get('input') or {}).get('untimed_excluded') or {}
        for reason, count in sorted((untimed.get('by_reason') or {}).items()):
            exclusions.append({'run_id': run['run_id'], 'exclusion': f'untimed:{reason}', 'count': count})
        uncertainty = (run['result'].get('input') or {}).get('time_uncertainty') or {}
        if uncertainty:
            exclusions.append({'run_id': run['run_id'], 'exclusion': f'time_uncertainty:{uncertainty.get("policy")}',
                               'count': uncertainty.get('imprecise')})
        exclusions.append({'run_id': run['run_id'], 'exclusion': 'outside_span',
                           'count': (run['result'].get('input') or {}).get('outside_span')})
    return {
        'schema': SCHEMA, 'disclaimer': disclaimer(), 'hypothesis_id': hypothesis_id,
        'hypothesis': {'hypothesis_id': hypothesis_id, 'title': definition.get('title'), 'text': definition.get('text'),
                       'latest_version_id': latest['version_id'], 'latest_version_number': latest['version_number'],
                       'latest_definition_sha256': latest['definition_sha256'],
                       'latest_record_sha256': latest['record_sha256'], 'registered_at': latest['registered_at'],
                       'first_analysed_at': min((v['first_analysed_at'] for v in versions if v['first_analysed_at']),
                                                default=None),
                       'canonicalization': latest['canonicalization']},
        'versions': [{'version_id': v['version_id'], 'version_number': v['version_number'],
                      'definition_sha256': v['definition_sha256'], 'record_sha256': v['record_sha256'],
                      'previous_record_sha256': v['previous_record_sha256'], 'registered_at': v['registered_at'],
                      'first_analysed_at': v['first_analysed_at'], 'event_study_runs': v['event_study_runs'],
                      'integrity': v['integrity']['status'], 'actor': v['actor'], 'change_note': v['change_note']}
                     for v in versions],
        'operationalization': [{'key': key, 'value': definition.get(key)} for key in
                               ('stimulus', 'outcome', 'density', 'eligible', 'windows', 'lags', 'direction', 'analysis',
                                'category_config_sha256') if key in definition],
        'dossier_counts': listed['counts'],
        'dossier': [{'item_id': i['item_id'], 'target_kind': i['target_kind'], 'target_id': i['target_id'],
                     'classification': i['classification'], 'classified_by': i['classified_by'],
                     'human_reviewed': i['human_reviewed'], 'decisions': len(i['decisions']),
                     'source_check': i['source_check'], 'origin': i['origin'], 'origin_run_id': i['origin_run_id'],
                     'locator_sha256': i['locator_sha256'], 'excerpt': i['excerpt'], 'created_at': i['created_at']}
                    for i in listed['items']],
        'typology': thesis_view['typology'],
        'theses': [{'thesis_id': t['thesis_id'], 'status_type': t['status_type'], 'status_rank': t['status_rank'],
                    'status_label_pl': t['status_label_pl'], 'status_label_en': t['status_label_en'], 'text': t['text'],
                    'evidence_item_ids': [e['item_id'] for e in t['evidence']], 'flags': t['flags'],
                    'unsupported': t['unsupported'], 'flags_computed': t['flags_computed'],
                    'previous_thesis_id': t['previous_thesis_id'], 'superseded_by': t['superseded_by'],
                    'created_at': t['created_at'], 'actor': t['actor']} for t in thesis_view['theses']],
        'theses_summary': {'current': thesis_view['current_count'], 'unsupported_current': thesis_view['unsupported_current']},
        'runs': [{'run_id': r['run_id'], 'kind': r['kind'], 'hypothesis_version_id': r['hypothesis_version_id'],
                  'definition_sha256': r['definition_sha256'], 'created_at': r['created_at'], 'seed': r['seed'],
                  'null_model': ((r['result'].get('null_model') or {}).get('name') if r['kind'] == 'event_study' else None),
                  'permutations': (r['result'].get('parameters') or {}).get('permutations') if r['kind'] == 'event_study' else None,
                  'status': r['result'].get('status'), 'source_rule': (r['result'].get('input') or {}).get('rule'),
                  'source_rule_sha256': ((r['result'].get('input') or {}).get('source') or {}).get('rule_sha256'),
                  'input_sha256': r['input_sha256'], 'input_count': r['input_count'], 'result_sha256': r['result_sha256'],
                  'integrity': r['integrity'], 'identical_result_runs': r['identical_result_runs'],
                  'code_version': r['code_version'], 'provider': r['provider'], 'provider_opt_in': r['provider_opt_in'],
                  'deviations': r['result'].get('deviations_from_registration')} for r in runs],
        'runs_listed_limit': MAX_RUNS, 'runs_total': len(run_summaries),
        'windows': [row for run in event_studies for row in _window_rows(run)],
        'warnings': [row for run in event_studies for row in _warning_rows(run)],
        'exclusions': exclusions,
        'disclosure': [{'quadrant': q['quadrant'], 'label_pl': q['label_pl'], 'label_en': q['label_en'],
                        'human_text': q['human_text'], 'human_revisions': len(q['human_history']),
                        'computed_facts': q['computed_facts']} for q in disclosure['quadrants']],
        'anchors': anchors,
        'inputs': inputs,
    }

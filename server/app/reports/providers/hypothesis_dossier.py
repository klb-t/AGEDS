"""Hypothesis dossier (area E2): operationalization, evidence for/against,
theses with typology, event study and limitations, under the mandatory hashed
disclaimer label of the analysis layer (``hypotheses.DISCLAIMER`` reused through
``reports.disclaimers.interpretive`` after its SHA-256 check, never copied).

Upstream (area E2, ``server.app.analysis.dossier_report.report_payload(db,
hypothesis_id)``): preregistered versions with the hash chain and integrity
check, the latest definition, dossier items with the current human
classification and source check, theses with flags computed on read
(``unsupported`` …), event-study runs with per-window results, warnings and
exclusions, and the disclosure quadrants. Anchors pinned in the dossier feed the
citation bundle. Until a hypothesis id is given the section is ``unavailable``.
"""
from __future__ import annotations

from .. import disclaimers
from ..blocks import kv, notice, prose
from . import Section
from ._upstream_view import call_upstream, disclaimer_notices, typed_table

VERSION = 'hypothesis_dossier.v2'
UPSTREAM = (('server.app.analysis.dossier_report', 'report_payload'),)
VERSION_COLUMNS = [('version_number', 'number'), ('version_id', 'id'), ('definition_sha256', 'hash'),
                   ('record_sha256', 'hash'), ('previous_record_sha256', 'hash'), ('registered_at', 'time'),
                   ('first_analysed_at', 'time'), ('event_study_runs', 'number'), ('integrity', 'text'),
                   ('actor', 'text'), ('change_note', 'text')]
DOSSIER_COLUMNS = [('item_id', 'id'), ('target_kind', 'text'), ('target_id', 'id'), ('classification', 'text'),
                   ('classified_by', 'text'), ('human_reviewed', 'bool'), ('decisions', 'number'),
                   ('source_check', 'text'), ('origin', 'text'), ('origin_run_id', 'id'), ('locator_sha256', 'hash'),
                   ('excerpt', 'quote'), ('created_at', 'time')]
THESIS_COLUMNS = [('thesis_id', 'id'), ('status_rank', 'number'), ('status_type', 'text'), ('status_label', 'text'),
                  ('text', 'quote'), ('evidence_item_ids', 'json'), ('unsupported', 'bool'), ('flags', 'json'),
                  ('flags_computed', 'bool'), ('superseded_by', 'id'), ('created_at', 'time'), ('actor', 'text')]
RUN_COLUMNS = [('run_id', 'id'), ('kind', 'text'), ('hypothesis_version_id', 'id'), ('definition_sha256', 'hash'),
               ('created_at', 'time'), ('seed', 'number'), ('null_model', 'text'), ('permutations', 'number'),
               ('status', 'text'), ('source_rule', 'text'), ('source_rule_sha256', 'hash'), ('input_sha256', 'hash'),
               ('input_count', 'number'), ('result_sha256', 'hash'), ('integrity', 'text'),
               ('identical_result_runs', 'json'), ('code_version', 'text'), ('provider', 'text'),
               ('provider_opt_in', 'bool'), ('deviations', 'json')]
WINDOW_COLUMNS = [('run_id', 'id'), ('window', 'text'), ('start_s', 'number'), ('end_s', 'number'),
                  ('observed_pairs', 'number'), ('null_mean_pairs', 'number'), ('null_q025', 'number'),
                  ('null_q975', 'number'), ('p_value', 'number'), ('k_extreme', 'number'), ('n_permutations', 'number'),
                  ('p_formula', 'text'), ('stimuli_with_outcome', 'number'), ('unique_outcomes_in_windows', 'number'),
                  ('normalized_observed', 'number'), ('normalized_null_mean', 'number'),
                  ('normalized_p_value', 'number'), ('normalized_undefined', 'number')]
WARNING_COLUMNS = [('run_id', 'id'), ('warning', 'text'), ('flagged', 'bool'), ('severity', 'text'),
                   ('warning_text', 'text'), ('computed', 'json')]
HUMAN_TEXT = {'pl': 'Teksty tez i ujawnień wpisał człowiek; flagi i liczby obliczył kod.',
              'en': 'Thesis and disclosure texts were typed by a person; flags and numbers were computed by code.'}


def provide(ctx) -> dict:
    section = Section(ctx, 'hypothesis_dossier', __name__, VERSION, 'section_hypothesis_dossier')
    disclaimer_notices(section, 'hypothesis_dossier', [
        disclaimers.interpretive(ctx.lang),
        disclaimers.fixed(disclaimers.CORRELATION_ID, disclaimers.CORRELATION, ctx.lang)])
    value, _ = call_upstream(ctx, section, section_name='hypothesis_dossier', upstream=UPSTREAM, id_param='hypothesis_id')
    if value is None:
        return section.result()
    _render(section, ctx, value)
    return section.result()


def _render(section: Section, ctx, value: dict) -> None:
    lang, max_rows = ctx.lang, ctx.params['max_rows']
    expected = disclaimers.interpretive(lang)
    stored = value.get('disclaimer') or {}
    if stored.get(f'sha256_{lang}', stored.get('sha256_pl')) != expected['sha256']:
        section.status('partial', 'the dossier disclaimer label differs from the analysis layer label')
    section.block(prose('hypothesis_dossier.human_note', f'{VERSION}.human_note', HUMAN_TEXT.get(lang, HUMAN_TEXT['pl']), {}))
    section.block(kv('hypothesis_dossier.hypothesis', sorted(value['hypothesis'].items()), lang, title_key='hypothesis'))
    section.block(typed_table('hypothesis_dossier.versions', value['versions'], lang, max_rows=max_rows,
                              columns=VERSION_COLUMNS, title_key='registered_versions'))
    section.block(typed_table('hypothesis_dossier.operationalization', value['operationalization'], lang,
                              max_rows=max_rows, columns=[('key', 'text'), ('value', 'json')],
                              title_key='operationalization'))
    section.block(kv('hypothesis_dossier.dossier_counts', sorted(value['dossier_counts'].items()), lang,
                     title_key='dossier_counts'))
    section.block(typed_table('hypothesis_dossier.items', value['dossier'], lang, max_rows=max_rows,
                              columns=DOSSIER_COLUMNS, title_key='dossier_items'))
    section.block(typed_table('hypothesis_dossier.typology', value['typology'], lang, max_rows=max_rows,
                              columns=[('rank', 'number'), ('id', 'text'), ('pl', 'text'), ('en', 'text')],
                              title_key='thesis_typology'))
    theses = [{**t, 'status_label': t['status_label_en'] if lang == 'en' else t['status_label_pl']} for t in value['theses']]
    section.block(typed_table('hypothesis_dossier.theses', theses, lang, max_rows=max_rows, columns=THESIS_COLUMNS,
                              title_key='theses'))
    section.block(kv('hypothesis_dossier.theses_summary', sorted(value['theses_summary'].items()), lang,
                     title_key='theses_summary'))
    section.block(typed_table('hypothesis_dossier.runs', value['runs'], lang, max_rows=max_rows, columns=RUN_COLUMNS,
                              title_key='analysis_runs', total_rows=value['runs_total']))
    section.block(typed_table('hypothesis_dossier.windows', value['windows'], lang, max_rows=max_rows,
                              columns=WINDOW_COLUMNS, title_key='event_study_windows'))
    warnings = [{**w, 'warning_text': w['text_en'] if lang == 'en' else w['text_pl']} for w in value['warnings']]
    section.block(typed_table('hypothesis_dossier.warnings', warnings, lang, max_rows=max_rows, columns=WARNING_COLUMNS,
                              title_key='event_study_warnings'))
    section.block(typed_table('hypothesis_dossier.exclusions', value['exclusions'], lang, max_rows=max_rows,
                              columns=[('run_id', 'id'), ('exclusion', 'text'), ('count', 'json')], title_key='exclusions'))
    disclosure = [{**q, 'quadrant_label': q['label_en'] if lang == 'en' else q['label_pl']} for q in value['disclosure']]
    section.block(typed_table('hypothesis_dossier.disclosure', disclosure, lang, max_rows=max_rows,
                              columns=[('quadrant', 'text'), ('quadrant_label', 'text'), ('human_text', 'quote'),
                                       ('human_revisions', 'number'), ('computed_facts', 'json')],
                              title_key='disclosure'))
    if value['runs_total'] > len(value['runs']):
        section.exclude('analysis_runs_not_listed', value['runs_total'] - len(value['runs']))
    for row in value['exclusions']:
        section.exclude(f'run:{row["run_id"]}:{row["exclusion"]}', row['count'])
    unsupported = value['theses_summary']['unsupported_current']
    section.exclude('current_theses_without_evidence', unsupported)
    broken = [v['version_number'] for v in value['versions'] if v['integrity'] != 'hash_matches'] + \
        [r['run_id'] for r in value['runs'] if r['integrity'] != 'hash_matches']
    if broken:
        section.status('partial', 'a registered version or stored run no longer matches its recorded hash')
        section.block(notice('hypothesis_dossier.integrity', 'warning', section.data['reason']))
    if any(len(rows) > max_rows for rows in (value['dossier'], value['theses'], value['windows'], value['warnings'])):
        section.status('partial', 'row limit reached')
    event_studies = [r for r in value['runs'] if r['kind'] == 'event_study']
    if event_studies:
        section.data['rule'] = event_studies[-1]['source_rule']
    for run in value['runs']:
        if run['provider'] not in (None, 'none'):
            section.model_run({'table': 'hypothesis_runs', 'run_id': run['run_id'], 'status': run['status'] or 'unknown',
                               'tool': 'unknown', 'tool_version': 'unknown', 'provider': run['provider'],
                               'model': 'unknown', 'model_version': 'unknown'})
    for table_name, rows in value['inputs'].items():
        section.input_rows(table_name, rows)
    anchors = []
    for pointer in value['anchors']:
        section.citation(pointer['artifact_id'], pointer['anchor_id'])
        anchors.append(pointer['anchor_id'])
    # Later sections (citations with scope) list exactly the anchors pinned in this dossier.
    ctx.scope.setdefault('anchor_ids', [])
    ctx.scope['anchor_ids'] = sorted(set(ctx.scope['anchor_ids']) | set(anchors))
    ctx.scope.setdefault('artifact_ids', set()).update(p['artifact_id'] for p in value['anchors'])

"""Time budget (area E1): a stored, append-only budget run rendered as computed.

Upstream (area E1, ``server.app.analysis.budget_report.report_payload(db,
budget_run_id)``): the stored ``analysis_runs`` row of kind ``time_budget``
flattened into rows — run identity and result-hash check, window, time source
(timeline rule, SHA-256, projection build), category definitions (rule
provenance, never rule values), totals and candles with lower / probable /
upper bounds, wick and body over the sensitivity grid, the ``nieobserwowane``
band, sensitivity ranges and flips, exclusions and parameters. Nothing is
recomputed here. Until a run id is given the section is ``unavailable``.
"""
from __future__ import annotations

from .. import disclaimers
from ..blocks import kv, notice
from ..common import sha256_text
from . import Section
from ._upstream_view import call_upstream, disclaimer_notices, typed_table

VERSION = 'time_budget.v2'
UPSTREAM = (('server.app.analysis.budget_report', 'report_payload'),)
BOUNDS = [('lower', 'number'), ('probable', 'number'), ('upper', 'number'), ('wick_low', 'number'),
          ('wick_high', 'number'), ('body_low', 'number'), ('body_high', 'number')]
TOTAL_COLUMNS = [('category', 'text'), *BOUNDS]
CANDLE_COLUMNS = [('granularity', 'text'), ('bucket', 'text'), ('start_utc', 'time'), ('end_utc', 'time'),
                  ('length_s', 'number'), ('partial', 'bool'), ('category', 'text'), *BOUNDS,
                  ('event_count', 'number'), ('event_ids_truncated', 'bool'), ('unstable', 'json')]


def provide(ctx) -> dict:
    section = Section(ctx, 'time_budget', __name__, VERSION, 'section_time_budget')
    disclaimer_notices(section, 'time_budget', [
        disclaimers.interpretive(ctx.lang), disclaimers.fixed(disclaimers.UNOBSERVED_ID, disclaimers.UNOBSERVED, ctx.lang)])
    value, _ = call_upstream(ctx, section, section_name='time_budget', upstream=UPSTREAM, id_param='budget_run_id')
    if value is None:
        return section.result()
    _render(section, ctx, value)
    return section.result()


def _render(section: Section, ctx, value: dict) -> None:
    lang, max_rows = ctx.lang, ctx.params['max_rows']
    run, window, source = value['run'], value['window'], value['source']
    if isinstance(value.get('disclaimer'), str):  # E1's own stored label, hashed as stored in the result
        section.block(notice('time_budget.result_disclaimer', 'info', value['disclaimer'],
                             sha256=sha256_text(value['disclaimer']), source='server.app.analysis.budget'))
    section.data['rule'] = source.get('source_rule')
    section.block(kv('time_budget.run', sorted(run.items()), lang, title_key='budget_run'))
    section.block(kv('time_budget.window', sorted(window.items()), lang, title_key='budget_window'))
    section.block(kv('time_budget.source', sorted(source.items()), lang, title_key='time_source'))
    section.block(typed_table('time_budget.category_totals', value['category_totals'], lang, max_rows=max_rows,
                              columns=TOTAL_COLUMNS, title_key='category_totals'))
    section.block(kv('time_budget.totals', sorted(value['totals'].items()), lang, title_key='budget_totals'))
    section.block(typed_table('time_budget.candles', value['candles'], lang, max_rows=max_rows, columns=CANDLE_COLUMNS,
                              title_key='candles'))
    section.block(kv('time_budget.sensitivity', sorted(value['sensitivity'].items()), lang, title_key='sensitivity'))
    section.block(typed_table('time_budget.sensitivity_ranges', value['sensitivity_ranges'], lang, max_rows=max_rows,
                              title_key='sensitivity_ranges'))
    section.block(typed_table('time_budget.sensitivity_points', value['sensitivity_points'], lang, max_rows=max_rows,
                              columns=[('index', 'number'), ('differs', 'json'), ('unobserved_probable', 'number')],
                              title_key='sensitivity_points'))
    section.block(typed_table('time_budget.flips', value['flips'], lang, max_rows=max_rows,
                              columns=[('kind', 'text'), ('point_index', 'number'), ('differs', 'json'), ('detail', 'json')],
                              title_key='flips'))
    section.block(typed_table('time_budget.categories', value['categories'], lang, max_rows=max_rows,
                              columns=[('category', 'text'), ('label', 'text'), ('color', 'text'), ('reserved', 'bool')],
                              title_key='category_definitions'))
    section.block(kv('time_budget.category_config', sorted(value['category_config'].items()), lang,
                     title_key='category_config'))
    section.block(typed_table('time_budget.category_rules', value['category_rules'], lang, max_rows=max_rows,
                              title_key='category_rules'))
    section.block(kv('time_budget.labels', sorted(value['labels'].items()), lang, title_key='labelling'))
    section.block(typed_table('time_budget.exclusions', value['exclusions'], lang, max_rows=max_rows,
                              columns=[('exclusion', 'text'), ('count', 'json')], title_key='exclusions'))
    section.block(typed_table('time_budget.method', value['method'], lang, max_rows=max_rows,
                              columns=[('key', 'text'), ('value', 'json')], title_key='budget_method'))
    section.block(typed_table('time_budget.parameters', value['parameters'], lang, max_rows=max_rows,
                              columns=[('parameter', 'text'), ('value', 'json')], title_key='parameters'))
    for row in value['exclusions']:
        section.exclude(f'budget:{row["exclusion"]}', row['count'])
    for name, rows in (('candles', value['candles']), ('flips', value['flips'])):
        if len(rows) > max_rows:
            section.exclude(f'{name}_not_listed_over_max_rows', len(rows) - max_rows)
            section.status('partial', 'row limit reached')
    for key in ('code_version', 'config_version'):
        if run.get(key) in (None, 'unknown'):
            section.unknown(f'analysis_run.{run["run_id"]}.{key}')
    if run['integrity'] != 'hash_matches':
        section.status('partial', f'stored result of run {run["run_id"]} does not match its recorded SHA-256')
    section.observe_time(window.get('from_utc'), 'budget_window')
    section.observe_time(window.get('to_utc'), 'budget_window')
    for table_name, rows in value['inputs'].items():
        section.input_rows(table_name, rows)
    model_run = next((row['value'] for row in value['parameters'] if row['parameter'] == 'model_label_run_id'), None)
    if model_run is not None:
        stored = ctx.db.execute('SELECT * FROM analysis_model_runs WHERE id=?', (model_run,)).fetchone()
        section.model_run({'table': 'analysis_model_runs', 'run_id': model_run, 'status': stored['status'] if stored else 'unknown',
                           'tool': 'unknown', 'tool_version': 'unknown',
                           'provider': stored['provider'] if stored else 'unknown',
                           'model': stored['model'] if stored else 'unknown',
                           'model_version': stored['model_version'] if stored else 'unknown'})
        if stored is not None:
            section.input_row('analysis_model_runs', stored)

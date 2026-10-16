"""Timeline section: area C's projection (display rule, projection build,
placement, transcript segments, correlation hypotheses with their review
status) next to the chronology with quotes.

The chronology rows come from the shared scoped events (``_events``), which
read the analysis seam; the seam delegates to the same timeline projection,
so both parts of the section use one rule, one projection build and one
read-only snapshot. Upstream contract (area C, ``server.app.timeline.report``):
``report_section(db, *, from_utc, to_utc, event_ids, artifact_ids, assume_zone,
assumption_rule, include_segments, topic, limit, max_scan) -> dict`` (computed
values only, never refreshes the cache). Without the timeline package the
section is the interim chronology and says so.
"""
from __future__ import annotations

from .. import disclaimers
from ..blocks import kv, notice, table
from ..common import sha256_text
from ..labels import label
from . import Section, load_upstream
from . import chronology

VERSION = 'timeline.v2'
UPSTREAM = (('server.app.timeline.report', 'report_section'),)
PLACEMENT_COLUMNS = [('basis', 'text'), ('precision', 'text'), ('items', 'number'), ('with_interval', 'number'),
                     ('max_uncertainty_s', 'number')]
FLAG_COLUMNS = [('flag', 'text'), ('value', 'text'), ('items', 'number')]
SEGMENT_COLUMNS = [('time_utc', 'time'), ('interval_from', 'time'), ('interval_to', 'time'), ('basis', 'text'),
                   ('artifact_id', 'id'), ('derived_text_id', 'id'), ('segment_index', 'number'),
                   ('segment_start_s', 'number'), ('segment_end_s', 'number'), ('transcript_is_latest', 'bool'),
                   ('run_id', 'id'), ('language', 'text'), ('language_basis', 'text'), ('quote', 'quote'),
                   ('quote_truncated', 'bool'), ('quote_sha256', 'hash')]
CORRELATION_COLUMNS = [('hypothesis_id', 'id'), ('status', 'text'), ('status_at', 'time'), ('status_actor', 'text'),
                       ('status_reason', 'text'), ('decisions', 'number'), ('relation', 'text'), ('left', 'text'),
                       ('right', 'text'), ('rule_id', 'text'), ('rule_version', 'number'), ('rule_sha256', 'hash'),
                       ('run_id', 'id'), ('ambiguity_group', 'text'), ('candidate_count', 'number'),
                       ('criteria', 'json'), ('criteria_sha256', 'hash'), ('probability', 'number')]


def provide(ctx) -> dict:
    section = Section(ctx, 'timeline', __name__, f'{VERSION}+{chronology.VERSION}', 'section_timeline')
    chronology.fill(section, ctx)
    function, where = load_upstream(UPSTREAM)
    if function is None:
        section.data['blocks'].insert(0, notice('timeline.fallback', 'info',
                                                f'{label(ctx.lang, "upstream")}: {where}. '
                                                f'{label(ctx.lang, "rule")}: {section.data["rule"]}'))
        section.exclude('timeline_projection_unavailable', True)
        return section.result()
    params = ctx.params
    try:
        value = function(ctx.db, from_utc=params['from'], to_utc=params['to'],
                         event_ids=sorted(ctx.scope.get('event_ids') or ()),
                         artifact_ids=sorted(ctx.scope.get('artifact_ids') or ()),
                         assume_zone=params['assume_zone'], assumption_rule=params['assumption_rule'],
                         include_segments=params['counterparty'] is None, topic=params['topic'],
                         limit=params['max_rows'], max_scan=params['max_events'])
    except Exception as exc:  # another area's code: report, do not hide
        section.status('error', f'{where} raised {type(exc).__name__}: {exc}')
        section.block(notice('timeline.error', 'error', section.data['reason']))
        return section.result()
    _render(section, ctx, value, where)
    return section.result()


def _render(section: Section, ctx, value: dict, where: str) -> None:
    lang, params = ctx.lang, ctx.params
    rule, projection = value['rule'], value['projection']
    section.data['rule'] = rule['id']
    blocks = [notice('timeline.upstream', 'info', f'{label(lang, "upstream")}: {where}')]
    blocks.append(kv('timeline.projection', [
        ('rule', rule['id']), ('rule_sha256', rule['sha256']), ('rule_origin', rule['origin']),
        ('projection_id', projection['id']), ('projection_params', projection['params']),
        ('projection_key_params', projection['key_params']), ('params_sha256', projection['params_sha256']), ('build_id', projection['build_id']),
        ('build_mode', projection['build_mode']), ('build_finished_at', projection['build_finished_at']),
        ('snapshot_status', projection['status'])], lang, title_key='timeline_projection'))
    blocks.append(table('timeline.placement', PLACEMENT_COLUMNS, value['placement'], lang, title_key='placement'))
    blocks.append(table('timeline.flags', FLAG_COLUMNS, value['flags'], lang, title_key='placement_flags'))
    summary = value['segment_summary']
    rows = []
    for row in value['segments']:
        text = row.get('text')
        limit = params['quote_chars']
        rows.append({**row, 'quote': text[:limit] if isinstance(text, str) else None,
                     'quote_truncated': bool(row.get('text_truncated')) or (isinstance(text, str) and len(text) > limit),
                     'quote_sha256': sha256_text(text[:limit]) if isinstance(text, str) else None})
        section.observe_time(row['time_utc'], 'event_utc')
    _cite_segments(section, ctx.db, value['segments'])
    if params['counterparty'] is not None:
        section.exclude('segments_excluded_by_counterparty_filter', True)
    for key in ('segments_filtered_by_topic', 'segments_scan_limit_hit'):
        if summary.get(key):
            section.exclude(key, summary[key])
    if summary.get('segments_truncated'):
        section.exclude('segments_not_listed_over_max_rows', True)
        if section.data['status'] == 'ok':
            section.status('partial', 'row or event scan limit reached')
    blocks.append(table('timeline.segments', SEGMENT_COLUMNS, rows, lang, title_key='transcript_segments',
                        total_rows=summary['segments_in_window'] if summary['segments_in_window'] is not None else len(rows),
                        truncated=bool(summary.get('segments_truncated'))))
    fixed = disclaimers.fixed(disclaimers.CORRELATION_HYPOTHESIS_ID, disclaimers.CORRELATION_HYPOTHESIS, lang)
    blocks.append(notice('timeline.correlation_label', 'info', fixed['text'], sha256=fixed['sha256'],
                         source=fixed['source']))
    correlation_summary = value['correlation_summary']
    blocks.append(kv('timeline.correlation_summary', sorted(correlation_summary.items()), lang,
                     title_key='correlation_hypotheses'))
    blocks.append(table('timeline.correlations', CORRELATION_COLUMNS, value['correlations'],
                        lang, total_rows=correlation_summary['hypotheses_total'],
                        truncated=bool(correlation_summary['truncated'])))
    if correlation_summary['truncated']:
        section.exclude('correlations_not_listed_over_max_rows', True)
    for table_name, entries in value['inputs'].items():
        section.input_rows(table_name, entries)
    # Projection and correlation blocks first, then the chronology computed earlier in this section.
    section.data['blocks'] = blocks + section.data['blocks']


def _cite_segments(section: Section, db, segments: list[dict]) -> None:
    """Artifacts and transcript versions behind the listed segments: inputs and cited hashes."""
    for artifact_id in sorted({row['artifact_id'] for row in segments if row.get('artifact_id') is not None}):
        artifact = db.execute('SELECT * FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
        if artifact is None:
            section.unknown(f'artifact.{artifact_id}')
            continue
        section.input_row('artifacts', artifact)
        section.cite('artifacts', {'id': artifact['id'], 'sha256': artifact['sha256'], 'size_bytes': artifact['size_bytes']})
    for version_id in sorted({row['derived_text_id'] for row in segments if row.get('derived_text_id') is not None}):
        version = db.execute('SELECT * FROM derived_text WHERE id=?', (version_id,)).fetchone()
        if version is None:
            section.unknown(f'derived_text.{version_id}')
            continue
        section.input_row('derived_text', version)
        section.cite('derived_text', {'id': version['id'], 'artifact_id': version['artifact_id'],
                                      'text_sha256': sha256_text(version['text']) if version['text'] is not None else None,
                                      'run_id': version['run_id']})
        if version['run_id'] is None:
            section.unknown(f'derived_text.{version_id}.processing_run')

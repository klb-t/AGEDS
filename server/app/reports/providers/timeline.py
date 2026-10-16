"""Timeline section: the timeline area's projection when present, else the
interim chronology over the analysis source seam (its rule id is shown).

Upstream contract expected from area C (wired by the integrator, see
docs/REPORTS.md): ``report_section(db, *, from_utc, to_utc, counterparty,
topic, event_types, limit) -> dict`` with at least ``rule`` (id/version) and
``items`` (plain dicts). Its dict is rendered by the generic adapter as given.
"""
from __future__ import annotations

from ..blocks import generic_blocks, notice
from ..labels import label
from . import Section, load_upstream
from . import chronology

VERSION = 'timeline.v1'
UPSTREAM = (('server.app.timeline.report', 'report_section'),)


def provide(ctx) -> dict:
    function, where = load_upstream(UPSTREAM)
    if function is None:
        result = chronology.provide(ctx, section_name='timeline', title_key='section_timeline')
        result['provider'] = __name__
        result['provider_version'] = f'{VERSION}+{chronology.VERSION}'
        result['blocks'].insert(0, notice('timeline.fallback', 'info',
                                          f'{label(ctx.lang, "upstream")}: {where}. '
                                          f'{label(ctx.lang, "rule")}: {result["rule"]}'))
        result['exclusions']['timeline_projection_unavailable'] = True
        return result
    section = Section(ctx, 'timeline', __name__, VERSION, 'section_timeline')
    params = ctx.params
    try:
        value = function(ctx.db, from_utc=params['from'], to_utc=params['to'], counterparty=params['counterparty'],
                         topic=params['topic'], event_types=params['event_types'], limit=params['max_rows'])
    except Exception as exc:  # another area's code: report, do not hide
        section.status('error', f'{where} raised {type(exc).__name__}: {exc}')
        section.block(notice('timeline.error', 'error', section.data['reason']))
        return section.result()
    rule = value.get('rule') if isinstance(value, dict) else None
    section.data['rule'] = rule if isinstance(rule, str) else (rule or {}).get('id') if isinstance(rule, dict) else None
    section.block(notice('timeline.upstream', 'info', f'{label(ctx.lang, "upstream")}: {where}'))
    for block in generic_blocks(value, 'timeline.upstream', ctx.lang, max_rows=params['max_rows']):
        section.block(block)
    return section.result()

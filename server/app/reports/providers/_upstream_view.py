"""Shared body of sections fed by an analysis area's stored result (not a section)."""
from __future__ import annotations

from ..blocks import generic_blocks, notice
from ..labels import label
from . import Section, load_upstream


def upstream_section(ctx, *, section_name: str, version: str, title_key: str, upstream, id_param: str,
                     labels: list[dict]) -> dict:
    section = Section(ctx, section_name, f'{__package__}.{section_name}', version, title_key)
    for fixed in labels:
        section.block(notice(f'{section_name}.{fixed["id"]}', 'disclaimer', fixed['text'], sha256=fixed['sha256'],
                             source=fixed['source']))
    identifier = ctx.params[id_param]
    function, where = load_upstream(upstream)
    if function is None:
        return section.unavailable(where)
    if identifier is None:
        return section.unavailable(f'parameter {id_param} not given')
    try:
        value = function(ctx.db, identifier)
    except Exception as exc:  # another area's code: report, do not hide
        section.status('error', f'{where} raised {type(exc).__name__}: {exc}')
        section.block(notice(f'{section_name}.error', 'error', section.data['reason']))
        return section.result()
    section.block(notice(f'{section_name}.upstream', 'info', f'{label(ctx.lang, "upstream")}: {where}'))
    if isinstance(value, dict):
        for key in ('model_runs', 'runs'):
            for run in value.get(key) or []:
                if isinstance(run, dict):
                    section.model_run({'table': f'{section_name}_runs', 'run_id': run.get('id', run.get('run_id')),
                                       **{field: run.get(field, 'unknown') for field in
                                          ('tool', 'tool_version', 'provider', 'model', 'model_version')}})
    for block in generic_blocks(value, f'{section_name}.upstream', ctx.lang, max_rows=ctx.params['max_rows']):
        section.block(block)
    return section.result()

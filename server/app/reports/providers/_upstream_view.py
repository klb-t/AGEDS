"""Shared body of sections fed by an analysis area's stored result (not a section)."""
from __future__ import annotations

from ..blocks import generic_blocks, notice, table
from ..labels import label
from . import Section, load_upstream


def disclaimer_notices(section: Section, section_name: str, labels: list[dict]) -> None:
    for fixed in labels:
        section.block(notice(f'{section_name}.{fixed["id"]}', 'disclaimer', fixed['text'], sha256=fixed['sha256'],
                             source=fixed['source']))


def call_upstream(ctx, section: Section, *, section_name: str, upstream, id_param: str):
    """``(value, where)`` from the first importable upstream function, or ``(None, None)`` after
    marking the section unavailable (module, parameter or record missing) or error (it raised)."""
    identifier = ctx.params[id_param]
    function, where = load_upstream(upstream)
    if function is None:
        section.unavailable(where)
        return None, None
    if identifier is None:
        section.unavailable(f'parameter {id_param} not given')
        return None, None
    try:
        value = function(ctx.db, identifier)
    except LookupError as exc:  # the requested stored record does not exist: no data, not a crash
        section.unavailable(f'{where}: {exc}')
        return None, None
    except Exception as exc:  # another area's code: report, do not hide
        section.status('error', f'{where} raised {type(exc).__name__}: {exc}')
        section.block(notice(f'{section_name}.error', 'error', section.data['reason']))
        return None, None
    section.block(notice(f'{section_name}.upstream', 'info', f'{label(ctx.lang, "upstream")}: {where}'))
    return value, where


def _format(values) -> str:
    present = [v for v in values if v is not None]
    if present and all(isinstance(v, bool) for v in present):
        return 'bool'
    if present and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return 'number'
    if present and all(isinstance(v, str) for v in present):
        return 'text'
    return 'json' if present else 'text'


def typed_table(block_id: str, rows: list[dict], lang: str, *, max_rows: int, title_key: str | None = None,
                columns: list[tuple[str, str]] | None = None, total_rows: int | None = None) -> dict:
    """A table of computed rows; column formats inferred from the values when not given (values unchanged)."""
    if columns is None:
        keys: list[str] = []
        for row in rows[:max_rows]:
            for key in row:
                if key not in keys:
                    keys.append(key)
        columns = [(key, _format([row.get(key) for row in rows[:max_rows]])) for key in keys]
    total = len(rows) if total_rows is None else total_rows
    return table(block_id, columns, rows[:max_rows], lang, title_key=title_key, total_rows=total,
                 truncated=total > min(len(rows), max_rows))


def upstream_section(ctx, *, section_name: str, version: str, title_key: str, upstream, id_param: str,
                     labels: list[dict]) -> dict:
    """Generic adapter: an upstream dict rendered as given (scalars, tables, bounded nesting)."""
    section = Section(ctx, section_name, f'{__package__}.{section_name}', version, title_key)
    disclaimer_notices(section, section_name, labels)
    value, _ = call_upstream(ctx, section, section_name=section_name, upstream=upstream, id_param=id_param)
    if value is None:
        return section.result()
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

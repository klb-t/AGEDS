"""Render blocks: the only shapes a report template knows how to display.

A section's ``blocks`` hold computed values and code-defined labels. The HTML
template renders every value through one function that tags it with its JSON
Pointer into ``report.json`` (``data-k``), so each number in the HTML can be
traced to the computed document. Static template text contains no digits.

Prose is never free text with numbers in it: a ``prose`` block is a code
template with ``{slot}`` placeholders resolved from computed values. A prose
template whose author is ``model`` is refused if it contains any numeral, and
must use only declared slots (there is no model-written prose in this
revision; the check guards future adapters). ``human`` prose (for example the
open questions a user typed) is shown as human input and may contain digits.
"""
from __future__ import annotations

import re
from typing import Any

from .common import ReportInputError, has_numeral
from .labels import label

BLOCK_KINDS = ('kv', 'table', 'prose', 'notice')
COLUMN_FORMATS = ('text', 'number', 'id', 'hash', 'time', 'bool', 'quote', 'json', 'path')
PROSE_AUTHORS = ('code', 'model', 'human')
NOTICE_LEVELS = ('info', 'warning', 'unavailable', 'disclaimer', 'error')
_SLOT = re.compile(r'\{([a-z_][a-z0-9_]*)\}')
_BRACES = re.compile(r'[{}]')


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def kv(block_id: str, rows: list[tuple[str, Any]], lang: str, *, title_key: str | None = None) -> dict:
    return {'kind': 'kv', 'id': block_id, 'title': label(lang, title_key) if title_key else None,
            'rows': [{'key': key, 'label': label(lang, key), 'value': value} for key, value in rows]}


def table(block_id: str, columns: list[tuple[str, str]], rows: list[dict], lang: str, *,
          title_key: str | None = None, total_rows: int | None = None, truncated: bool = False,
          href_columns: dict[str, str] | None = None) -> dict:
    """``columns`` = [(key, format)]; ``href_columns`` maps a column to the row key holding its link."""
    keys = [key for key, _ in columns]
    for key, fmt in columns:
        if fmt not in COLUMN_FORMATS:
            raise ValueError(f'unknown column format {fmt}')
    href_columns = href_columns or {}
    out_rows = []
    for row in rows:
        item = {key: row.get(key) for key in keys}
        for column, href_key in href_columns.items():
            item[href_key] = row.get(href_key)
        out_rows.append(item)
    return {'kind': 'table', 'id': block_id, 'title': label(lang, title_key) if title_key else None,
            'columns': [{'key': key, 'label': label(lang, key), 'format': fmt,
                         'href_key': href_columns.get(key)} for key, fmt in columns],
            'rows': out_rows, 'total_rows': len(rows) if total_rows is None else total_rows,
            'truncated': bool(truncated)}


def prose(block_id: str, template_id: str, text: str, slots: dict[str, Any], *, author: str = 'code') -> dict:
    """A placeholder-resolved sentence; numbers only ever come from ``slots``."""
    if author not in PROSE_AUTHORS:
        raise ReportInputError(f'unknown prose author {author}')
    if not isinstance(text, str):
        raise ReportInputError('prose text must be a string')
    names = _SLOT.findall(text)
    if author != 'human':
        unknown = sorted(set(names) - set(slots))
        if unknown:
            raise ReportInputError(f'prose {template_id} uses undeclared slots: {unknown}')
        if _BRACES.search(_SLOT.sub('', text)):
            raise ReportInputError(f'prose {template_id} has unbalanced placeholder braces')
        for name, value in slots.items():
            if not _scalar(value):
                raise ReportInputError(f'prose slot {name} must be a scalar')
    if author == 'model' and has_numeral(_SLOT.sub('', text)):
        raise ReportInputError(f'model-provided prose {template_id} contains a numeral; numbers must come from slots')
    parts = []
    position = 0
    for match in _SLOT.finditer(text) if author != 'human' else ():
        if match.start() > position:
            parts.append({'text': text[position:match.start()]})
        parts.append({'slot': match.group(1), 'value': slots[match.group(1)]})
        position = match.end()
    if position < len(text):
        parts.append({'text': text[position:]})
    return {'kind': 'prose', 'id': block_id, 'template_id': template_id, 'author': author,
            'text': text, 'slots': dict(slots), 'parts': parts}


def notice(block_id: str, level: str, text: str, *, sha256: str | None = None, source: str = 'code') -> dict:
    if level not in NOTICE_LEVELS:
        raise ValueError(f'unknown notice level {level}')
    return {'kind': 'notice', 'id': block_id, 'level': level, 'text': text, 'sha256': sha256, 'source': source}


def generic_blocks(value: Any, block_prefix: str, lang: str, *, max_rows: int, depth: int = 0) -> list[dict]:
    """Adapt a plain upstream dict (another area's computed result) into blocks.

    Scalars become key/value rows, lists of flat objects become tables, nested
    objects recurse (bounded). Keys are shown as given; values are not changed.
    """
    blocks: list[dict] = []
    if depth > 4:
        return [notice(f'{block_prefix}_depth', 'warning', label(lang, 'nested_value_not_shown'))]
    if not isinstance(value, dict):
        value = {'value': value}
    scalars = [(key, item) for key, item in value.items() if _scalar(item)]
    if scalars:
        blocks.append({'kind': 'kv', 'id': block_prefix, 'title': None,
                       'rows': [{'key': key, 'label': key, 'value': item} for key, item in scalars]})
    for key, item in value.items():
        child = f'{block_prefix}.{key}'
        if isinstance(item, dict):
            sub = generic_blocks(item, child, lang, max_rows=max_rows, depth=depth + 1)
            if sub:
                sub[0] = {**sub[0], 'title': key}
            blocks.extend(sub)
        elif isinstance(item, list):
            if item and all(isinstance(entry, dict) for entry in item):
                columns: list[str] = []
                for entry in item[:max_rows]:
                    for column in entry:
                        if column not in columns:
                            columns.append(column)
                rows = []
                for entry in item[:max_rows]:
                    rows.append({column: (entry.get(column) if _scalar(entry.get(column)) else _compact(entry.get(column)))
                                 for column in columns})
                blocks.append({'kind': 'table', 'id': child, 'title': key,
                               'columns': [{'key': column, 'label': column, 'format': 'text', 'href_key': None}
                                           for column in columns],
                               'rows': rows, 'total_rows': len(item), 'truncated': len(item) > max_rows})
            else:
                rows = [{'value': entry if _scalar(entry) else _compact(entry)} for entry in item[:max_rows]]
                blocks.append({'kind': 'table', 'id': child, 'title': key,
                               'columns': [{'key': 'value', 'label': key, 'format': 'text', 'href_key': None}],
                               'rows': rows, 'total_rows': len(item), 'truncated': len(item) > max_rows})
    return blocks


def _compact(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str)

"""Common timeline: a versioned projection over events and transcript segments.

The projection is a view, not new truth. Public entry points for other areas
(for example time budgets) are ``project_window`` and ``iter_projected_items``;
both refresh the materialized cache first and return items with the selected
time, its basis, precision and uncertainty interval. See docs/TIMELINE.md.
"""
from __future__ import annotations

from ..db import session
from .projection import ensure_projection
from .query import Filters, iter_window, make_filters, window
from .ruleset import Rule, display_params, load_rule
from .timeutil import parse_bound

__all__ = ['project_window', 'iter_projected_items', 'make_filters', 'display_params', 'load_rule']


def _resolve(rule: Rule | str | None) -> Rule:
    if isinstance(rule, Rule):
        return rule
    return load_rule(rule, kind='display_rule')


def iter_projected_items(start: str | None = None, end: str | None = None, *, filters: Filters | None = None,
                         rule: Rule | str | None = None, params: dict | None = None, match: str = 'display',
                         candidates: bool = False, batch: int = 1000, max_rows: int | None = None):
    """Yield projected items in ascending time order for ``[start, end)`` (UTC ISO bounds).

    Each item carries ``time.sort_us``, ``time.interval`` (``from_us``/``to_us``),
    ``time.end_us`` (duration end when known), ``time.basis`` and ``time.precision``.
    Undated items are not yielded; count them with ``window(...)['undated_count']``.
    """
    resolved = _resolve(rule)
    with session() as db:
        projection = ensure_projection(db, resolved, params or {})
        yield from iter_window(db, projection, resolved, start_us=parse_bound(start), end_us=parse_bound(end, end=True),
                               filters=filters, match=match, candidates=candidates, batch=batch, max_rows=max_rows,
                               group=None)


def project_window(start: str | None = None, end: str | None = None, *, filters: Filters | None = None,
                   rule: Rule | str | None = None, params: dict | None = None, match: str = 'display',
                   max_rows: int = 100_000) -> dict:
    """Materialize a window as one dict: items, rule/projection identity and completeness."""
    resolved = _resolve(rule)
    with session() as db:
        projection = ensure_projection(db, resolved, params or {})
        items = list(iter_window(db, projection, resolved, start_us=parse_bound(start),
                                 end_us=parse_bound(end, end=True), filters=filters, match=match,
                                 candidates=False, max_rows=max_rows + 1, group=None))
        first = window(db, projection, resolved, start_us=parse_bound(start), end_us=parse_bound(end, end=True),
                       filters=filters, match=match, limit=1, candidates=False, group=None)
    return {'rule': projection['rule'], 'params': projection['params'],
            'projection': {'id': projection['id'], 'build_id': projection['build']['id']},
            'items': items[:max_rows], 'complete': len(items) <= max_rows,
            'undated_count': first['undated_count'], 'notes': first['notes']}

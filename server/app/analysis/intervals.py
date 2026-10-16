"""Interval arithmetic and sessionization on UTC epoch seconds (stable API).

All functions are pure and deterministic. An interval is ``(start, end)`` with
``start < end``; empty or inverted intervals are dropped by ``normalize``.
Touching intervals (``a.end == b.start``) merge, so a union never counts the
same second twice.

Sessionization (rule ``sessionize.v1``): items sorted by ``(start, key)``; an
item joins the current session when ``start - session_end < gap_s`` (strictly
less, as specified: "joined when the gap is below the threshold"), where
``session_end`` is the maximum end seen so far in that session. Overlapping
items (negative gap) always join. A point item has ``end == start``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

Interval = tuple[float, float]
SESSION_RULE = 'sessionize.v1'


def normalize(intervals: Iterable[Interval]) -> list[Interval]:
    """Sorted, merged, non-empty intervals (the union of the input)."""
    cleaned = sorted((float(a), float(b)) for a, b in intervals if b > a)
    merged: list[list[float]] = []
    for a, b in cleaned:
        if merged and a <= merged[-1][1]:
            if b > merged[-1][1]:
                merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def union(*interval_lists: Iterable[Interval]) -> list[Interval]:
    return normalize(i for intervals in interval_lists for i in intervals)


def total(intervals: Iterable[Interval]) -> float:
    """Measure of the union (seconds)."""
    return sum(b - a for a, b in normalize(intervals))


def clip(intervals: Iterable[Interval], lo: float, hi: float) -> list[Interval]:
    return normalize((max(a, lo), min(b, hi)) for a, b in intervals)


def intersect(left: Iterable[Interval], right: Iterable[Interval]) -> list[Interval]:
    a_list, b_list = normalize(left), normalize(right)
    out, i, j = [], 0, 0
    while i < len(a_list) and j < len(b_list):
        lo = max(a_list[i][0], b_list[j][0])
        hi = min(a_list[i][1], b_list[j][1])
        if lo < hi:
            out.append((lo, hi))
        if a_list[i][1] < b_list[j][1]:
            i += 1
        else:
            j += 1
    return out


def complement(intervals: Iterable[Interval], lo: float, hi: float) -> list[Interval]:
    """Gaps of the union inside ``[lo, hi)``."""
    gaps, cursor = [], lo
    for a, b in clip(intervals, lo, hi):
        if a > cursor:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < hi:
        gaps.append((cursor, hi))
    return gaps


def subtract(left: Iterable[Interval], right: Iterable[Interval]) -> list[Interval]:
    base = normalize(left)
    if not base:
        return []
    return intersect(base, complement(right, base[0][0], base[-1][1]))


def _default_start(item) -> float:
    return item['t_start']


def _default_end(item) -> float:
    end = item.get('t_end')
    return item['t_start'] if end is None else max(end, item['t_start'])


def _default_key(item):
    return item['event_id']


def sessionize(items: Iterable, gap_s: float, *, start: Callable = _default_start, end: Callable = _default_end,
               key: Callable = _default_key) -> list[dict]:
    """Group items into sessions; returns ``{'start','end','keys','count'}`` dicts in time order."""
    if gap_s < 0:
        raise ValueError('gap_s must be >= 0')
    ordered = sorted(items, key=lambda it: (start(it), key(it)))
    sessions: list[dict] = []
    for it in ordered:
        s, e = float(start(it)), float(end(it))
        if sessions and s - sessions[-1]['end'] < gap_s:
            current = sessions[-1]
            current['end'] = max(current['end'], e)
            current['keys'].append(key(it))
            current['count'] += 1
        else:
            sessions.append({'start': s, 'end': e, 'keys': [key(it)], 'count': 1})
    return sessions


def weighted_segments(intervals: Iterable[tuple[float, float, str, float]],
                      breakpoints: Iterable[float] = ()) -> list[tuple[float, float, dict[str, float]]]:
    """Elementary segments with the per-key maximum weight of covering intervals.

    ``intervals`` are ``(start, end, key, weight)``. Inside one key the weight is a
    union by maximum (two parallel intervals of one key never add up). Returned
    segments are split at every interval endpoint and at ``breakpoints`` and only
    cover times where at least one key is active.
    """
    boundaries: dict[float, list[tuple[int, str, float]]] = {}
    for a, b, k, w in intervals:
        if b <= a or w <= 0:
            continue
        boundaries.setdefault(float(a), []).append((1, k, float(w)))
        boundaries.setdefault(float(b), []).append((-1, k, float(w)))
    for p in breakpoints:
        boundaries.setdefault(float(p), [])
    active: dict[str, dict[float, int]] = {}
    out = []
    points = sorted(boundaries)
    for index, p in enumerate(points):
        for delta, k, w in boundaries[p]:
            counts = active.setdefault(k, {})
            counts[w] = counts.get(w, 0) + delta
            if counts[w] == 0:
                del counts[w]
            if not counts:
                del active[k]
        if index + 1 < len(points) and active:
            out.append((p, points[index + 1], {k: max(ws) for k, ws in active.items()}))
    return out

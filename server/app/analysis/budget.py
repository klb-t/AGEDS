"""Time budget with explicit bounds (rule ``time_budget.v3``); an interpretive artifact.

Between two observed timestamps the activity is unknown. For every category the
budget reports three variants built from intervals on the UTC axis:

``lower`` (L)    only explicitly confirmed time: source start–end ranges (call
                 logs, calendar entries, timed blocks) and declared durations
                 (call ``duration``, WAV header of a recording).
``probable`` (P) L plus configurable heuristics: typing time before an outgoing
                 text (``chars / typing_chars_per_min``, capped), reading time
                 after an incoming chatbot answer (``chars /
                 reading_chars_per_min``, capped) and session spans (events of
                 one category joined while the gap is below ``session_gap_s``).
``upper`` (U)    P plus gap attribution: every gap between observations is
                 attributed to the category on each side, ``min(gap,
                 max_attribution_s)`` seconds adjacent to that side.

Inside one category, time is the union of its intervals weighted by the label
weight (union by maximum): parallel sessions never add up. Across categories
``overlap_rule`` decides: ``split_equal`` (shares scaled to sum 1 when they
exceed it), ``priority`` (``priority`` list, then configuration order) or
``count_both`` (every category keeps its own union; the overlap and the double
counted time are reported). For ``split_equal``/``priority`` the variants are
layered: confirmed time takes precedence over estimates and estimates over gap
attribution, so lower <= probable <= upper per category. Observed time whose
label weights sum below 1 is assigned to ``bez_kategorii``; that remainder is
always taken from the best-supported layer (also under ``count_both``), so it
keeps the same ordering.

``nieobserwowane`` is the time no observation covers in a variant: its lower
value is ``length - observed(U)`` and its upper value ``length - observed(L)``.
It is never idleness.

Candles (per day/week/month in ``zone``, per category): ``lower``/``probable``/
``upper`` at the run's parameters; wick = [min over the grid of lower, max over
the grid of upper]; body = [min, max] over the grid of probable. The grid is the
sensitivity grid (``grid_mode`` ``one_at_a_time`` around the run's parameters or
``full`` Cartesian product, bounded by ``max_grid_points``).
"""
from __future__ import annotations

import bisect
import itertools
import json
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

from . import calendar as cal
from . import categories as cat
from . import intervals as iv
from .provenance import canonical_json, code_sha256, code_version, event_set_sha256, sha256_json
from .source import RULE_ID as SOURCE_RULE, load_items

RULE = 'time_budget.v3'
# Method-record history. The computation is unchanged between versions; only what the
# stored method record says changed. Runs stored under an earlier version keep their
# result and result hash (analysis_runs is append-only) and are read as historical.
RULE_HISTORY = {
    'time_budget.v1': ('method.source_rule always said analysis_source.v1 (the interim seam) even when the '
                       'items came through the timeline seam; the true rule and hash were only in '
                       'input.source_rule / input.source_rule_sha256'),
    'time_budget.v2': ('method.source_rule, method.source_rule_sha256 and method.seam name the rule that '
                       'actually placed the items (e.g. display_time@1 with its SHA-256); found by the '
                       'end-to-end acceptance AGEDS-TASK-20261016-Z'),
    'time_budget.v3': ('count_both: the bez_kategorii remainder is taken from the best-supported layer (confirmed > '
                       'estimated > gap attribution) like split_equal/priority, so lower <= probable <= upper holds for '
                       'it too; categories keep their full per-variant union (review 2 finding 8, '
                       'AGEDS-TASK-20261016-R2)'),
}
RESULT_SCHEMA = 'ageds.time_budget_result.v1'
OVERLAP_RULES = ('split_equal', 'priority', 'count_both')
GRID_KEYS = ('session_gap_s', 'typing_chars_per_min', 'reading_chars_per_min', 'max_attribution_s', 'overlap_rule')
GRID_MODES = ('one_at_a_time', 'full')
VARIANTS = ('lower', 'probable', 'upper')
DEFAULTS = json.loads((Path(__file__).parent / 'data' / 'budget_defaults.json').read_text('utf-8'))['params']
DISCLAIMER = ('Wynik interpretacyjny: szacunek z jawnym modelem i przedziałami, nie obserwacja. '
              '„nieobserwowane” oznacza brak obserwacji, a nie bezczynność. '
              'Interpretive result: an estimate with an explicit model and bounds, not an observation; '
              '"nieobserwowane" means no observation, never idleness.')

_RANGES = {'session_gap_s': (0, 7 * 86400), 'typing_chars_per_min': (1, 100_000),
           'reading_chars_per_min': (1, 1_000_000), 'max_typing_s': (0, 86400), 'max_reading_s': (0, 86400),
           'max_attribution_s': (0, 7 * 86400), 'flip_tolerance_s': (0, 86400), 'duration_lookback_s': (0, 7 * 86400),
           'max_time_uncertainty_s': (0, 366 * 86400)}
_INTS = {'max_grid_points': (1, 4096), 'max_candle_events': (0, 1_000_000), 'max_flip_records': (0, 100_000),
         'max_items': (1, 10_000_000)}
_STR_LISTS = ('excluded_event_types', 'chat_content_kinds', 'typing_directions', 'reading_directions', 'priority')
_OPT_STR_LISTS = ('typing_event_types', 'reading_event_types')
_BOOLS = ('include_hidden', 'wav_header_durations')
UNCERTAINTY_MODES = ('widen_upper', 'ignore')
FOLD_POLICIES = ('exclude', 'carry')


def _number(name, value):
    low, high = _RANGES[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f'{name} must be a number in [{low}, {high}]')
    return value


def _check(name, value):
    if name in _RANGES:
        return _number(name, value)
    if name in _INTS:
        low, high = _INTS[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{name} must be an integer in [{low}, {high}]')
        return value
    if name == 'overlap_rule':
        if value not in OVERLAP_RULES:
            raise ValueError(f'overlap_rule must be one of {OVERLAP_RULES}')
        return value
    if name == 'uncertainty_mode':
        if value not in UNCERTAINTY_MODES:
            raise ValueError(f'uncertainty_mode must be one of {UNCERTAINTY_MODES}')
        return value
    if name == 'ambiguous_fold':
        if value not in FOLD_POLICIES:
            raise ValueError(f'ambiguous_fold must be one of {FOLD_POLICIES}')
        return value
    if name == 'timeline_rule':
        if value is not None and (not isinstance(value, str) or not value or len(value) > 200):
            raise ValueError('timeline_rule must be a rule reference such as display_time@1, or null')
        return value
    if name == 'grid_mode':
        if value not in GRID_MODES:
            raise ValueError(f'grid_mode must be one of {GRID_MODES}')
        return value
    if name in _STR_LISTS or name in _OPT_STR_LISTS:
        if value is None and name in _OPT_STR_LISTS:
            return None
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or len(value) > 1000:
            raise ValueError(f'{name} must be a list of strings')
        return value
    if name in _BOOLS:
        if not isinstance(value, bool):
            raise ValueError(f'{name} must be true or false')
        return value
    if name == 'granularities':
        if not isinstance(value, list) or not value or any(g not in cal.GRANULARITIES for g in value) \
                or len(set(value)) != len(value):
            raise ValueError(f'granularities must be a non-empty subset of {cal.GRANULARITIES}')
        return value
    if name == 'model_label_run_id':
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError('model_label_run_id must be a positive integer or null')
        return value
    if name == 'grid':
        if not isinstance(value, dict) or any(k not in GRID_KEYS for k in value):
            raise ValueError(f'grid keys must be among {GRID_KEYS}')
        out = {}
        for k, values in value.items():
            if not isinstance(values, list) or len(values) > 64:
                raise ValueError(f'grid.{k} must be a list of at most 64 values')
            out[k] = [_check(k, v) for v in values]
        return out
    raise ValueError(f'unknown parameter: {name}')


def effective_params(overrides: dict | None = None) -> dict:
    """Defaults merged with overrides (``grid`` merges per key) and validated."""
    overrides = overrides or {}
    if not isinstance(overrides, dict):
        raise ValueError('params must be a JSON object')
    unknown = sorted(set(overrides) - set(DEFAULTS))
    if unknown:
        raise ValueError(f'unknown parameters: {unknown}')
    params = json.loads(json.dumps(DEFAULTS))
    for key, value in overrides.items():
        if key == 'grid':
            params['grid'] = {**params['grid'], **(value if isinstance(value, dict) else {'_invalid': value})}
        else:
            params[key] = value
    return {key: _check(key, params[key]) for key in sorted(params)}


def _distinct_values(values: list) -> list:
    """Axis values without repeats (canonical JSON identity), first occurrence first."""
    out, seen = [], set()
    for value in values:
        key = canonical_json(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def grid_points(params: dict) -> tuple[list[dict], int, bool]:
    """Grid points; index 0 is always the run's own parameters.

    Returns ``(points, points_total, truncated)``. The grid is enumerated lazily
    and stops after ``max_grid_points`` distinct points; ``points_total`` is
    computed from the distinct axis values (review 2 finding 3), so a ``full``
    grid of 64 values on every axis never materializes its Cartesian product.
    """
    base = {k: params[k] for k in GRID_KEYS}
    grid = params['grid']
    cap = params['max_grid_points']
    if params['grid_mode'] == 'one_at_a_time':
        axes = {k: [v for v in _distinct_values(grid.get(k, [])) if v != base[k]] for k in GRID_KEYS}
        total = 1 + sum(len(values) for values in axes.values())
        candidates = ({**base, k: v} for k in GRID_KEYS for v in axes[k])
    else:
        axes_list = [_distinct_values(grid.get(k) or [base[k]]) for k in GRID_KEYS]
        base_key = canonical_json(base)
        base_in_product = all(canonical_json(base[k]) in {canonical_json(v) for v in values}
                              for k, values in zip(GRID_KEYS, axes_list))
        total = (0 if base_in_product else 1)
        size = 1
        for values in axes_list:
            size *= len(values)
        total += size
        candidates = (point for point in (dict(zip(GRID_KEYS, combo)) for combo in itertools.product(*axes_list))
                      if canonical_json(point) != base_key)
    points = [base]
    for point in candidates:
        if len(points) >= cap:
            break
        points.append(point)
    return points, total, total > cap


# ------------------------------------------------------------------ preparation

def _filter_reason(item: dict, params: dict) -> str | None:
    if item['event_type'] in params['excluded_event_types']:
        return f'excluded_event_type:{item["event_type"]}'
    if (item.get('hidden') or (item.get('chat') or {}).get('hidden')) and not params['include_hidden']:
        return 'hidden_by_projection'
    if (item.get('time_basis') or {}).get('dst') == 'ambiguous_fold' and params['ambiguous_fold'] == 'exclude':
        return 'ambiguous_fold'
    width = item.get('t_hi', item['t_start']) - item.get('t_lo', item['t_start'])
    if width > params['max_time_uncertainty_s']:
        return 'time_uncertainty_over_limit'
    chat = item.get('chat')
    if chat:
        if chat.get('content_kind') is not None and chat['content_kind'] not in params['chat_content_kinds']:
            return f'chat_content_kind:{chat["content_kind"]}'
    return None


def _role_direction(item: dict) -> str | None:
    if item.get('direction'):
        return item['direction']
    role = (item.get('chat') or {}).get('role')
    return {'user': 'outgoing', 'human': 'outgoing', 'assistant': 'incoming'}.get(role)


def _prepare(items: list[dict], labels: dict[int, dict], params: dict) -> list[dict]:
    acts = []
    for it in items:
        direction = _role_direction(it)
        explicit, basis = None, None
        if it['t_end'] is not None and it['t_end'] > it['t_start']:
            explicit, basis = (it['t_start'], it['t_end']), 'source_end'
        elif it.get('declared_duration_s'):
            explicit, basis = (it['t_start'], it['t_start'] + it['declared_duration_s']), it.get('duration_basis')
        chars = it.get('text_chars') or 0
        typing = chars > 0 and direction in params['typing_directions'] and \
            (params['typing_event_types'] is None or it['event_type'] in params['typing_event_types'])
        reading = chars > 0 and direction in params['reading_directions'] and \
            (params['reading_event_types'] is None or it['event_type'] in params['reading_event_types'])
        label = labels[it['event_id']]
        acts.append({'eid': it['event_id'], 't': it['t_start'], 'explicit': explicit, 'explicit_basis': basis,
                     'lo': it.get('t_lo', it['t_start']), 'hi': it.get('t_hi', it['t_start']),
                     'chars': chars, 'typing': typing, 'reading': reading,
                     'cats': label['categories'], 'path': label['path']})
    return acts


def _point_intervals(acts: list[dict], point: dict, params: dict):
    """Interval lists for one grid point: (start, end, category, weight, event_ids, role)."""
    confirmed, estimated, extents = [], [], {}
    for a in acts:
        spans = []
        if a['explicit']:
            spans.append((a['explicit'][0], a['explicit'][1], 'explicit'))
        if a['typing']:
            d = min(a['chars'] * 60.0 / point['typing_chars_per_min'], params['max_typing_s'])
            if d > 0:
                spans.append((a['t'] - d, a['t'], 'typing'))
        if a['reading']:
            d = min(a['chars'] * 60.0 / point['reading_chars_per_min'], params['max_reading_s'])
            if d > 0:
                spans.append((a['t'], a['t'] + d, 'reading'))
        for s, e, role in spans:
            target = confirmed if role == 'explicit' else estimated
            for c, w in a['cats'].items():
                target.append((s, e, c, w, (a['eid'],), role))
        extents[a['eid']] = (min([a['t']] + [s for s, _, _ in spans]), max([a['t']] + [e for _, e, _ in spans]))
    members = defaultdict(list)
    for a in acts:
        for c, w in a['cats'].items():
            members[c].append((a['eid'], w))
    for c in sorted(members):
        weight_of = dict(members[c])
        sessions = iv.sessionize(members[c], point['session_gap_s'], start=lambda m: extents[m[0]][0],
                                 end=lambda m: extents[m[0]][1], key=lambda m: m[0])
        for s in sessions:
            if s['count'] > 1 and s['end'] > s['start']:
                estimated.append((s['start'], s['end'], c, max(weight_of[k] for k in s['keys']), tuple(s['keys']),
                                  'session'))
    return confirmed, estimated, extents


def _gap_intervals(confirmed, estimated, acts, extents, horizon: tuple[float, float], max_attribution: float):
    """Attribute every gap between observation blocks to its neighbours (capped)."""
    observations = [(s, e, c, w, eids) for s, e, c, w, eids, _ in confirmed + estimated]
    observations += [(a['t'], a['t'], c, w, (a['eid'],)) for a in acts for c, w in a['cats'].items()]
    observations.sort(key=lambda o: (o[0], o[1], o[2], o[4]))
    blocks = []
    for s, e, c, w, eids in observations:
        if blocks and s <= blocks[-1]['end']:
            b = blocks[-1]
            if s == b['start']:
                b['first'].append((c, w, eids))
            if e > b['end']:
                b['end'], b['last'] = e, [(c, w, eids)]
            elif e == b['end']:
                b['last'].append((c, w, eids))
        else:
            blocks.append({'start': s, 'end': e, 'first': [(c, w, eids)], 'last': [(c, w, eids)]})
    out = []

    def attribute(neighbours, a, b, side):
        merged = {}
        for c, w, eids in neighbours:
            prev = merged.get(c)
            merged[c] = (max(prev[0], w) if prev else w, (prev[1] if prev else ()) + eids)
        for c in sorted(merged):
            w, eids = merged[c]
            out.append((a, b, c, w, tuple(sorted(set(eids))), side))

    lo, hi = horizon
    for index, b in enumerate(blocks):
        before = blocks[index - 1]['end'] if index else lo
        if b['start'] > before:
            ext = min(b['start'] - before, max_attribution)
            if ext > 0:
                attribute(b['first'], b['start'] - ext, b['start'], 'gap_before')
        after = blocks[index + 1]['start'] if index + 1 < len(blocks) else hi
        if after > b['end']:
            ext = min(after - b['end'], max_attribution)
            if ext > 0:
                attribute(b['last'], b['end'], b['end'] + ext, 'gap_after')
    return out


def _uncertainty_intervals(acts: list[dict]) -> list[tuple]:
    """Upper variant only: the item could lie anywhere in its projected interval [t_lo, t_hi]."""
    out = []
    for a in acts:
        if a['hi'] > a['lo']:
            duration = a['explicit'][1] - a['explicit'][0] if a['explicit'] else 0.0
            for c, w in a['cats'].items():
                out.append((a['lo'], a['hi'] + duration, c, w, (a['eid'],), 'time_uncertainty'))
    return out


def _resolve(shares: dict[str, float], rule: str, order: list[str]) -> dict[str, float]:
    if rule == 'count_both':
        return dict(shares)
    if rule == 'split_equal':
        total = sum(shares.values())
        return {c: s / total for c, s in shares.items()} if total > 1 else dict(shares)
    out, remaining = {}, 1.0
    for c in order:
        if c in shares and remaining > 0:
            take = min(shares[c], remaining)
            out[c] = take
            remaining -= take
    return out


def _allocate(levels: list[list[tuple]], day_list: list[dict], lo: float, hi: float, rule: str, order: list[str]):
    """Per-day seconds for each variant: alloc[v][day][category], observed[v][day], overlap[v][day]."""
    tagged = []
    for level, intervals in enumerate(levels):
        for s, e, c, w, _, _ in intervals:
            a, b = max(s, lo), min(e, hi)
            if b > a:
                tagged.append((a, b, (level, c), w))
    starts = [d['start'] for d in day_list]
    breakpoints = starts + [hi]
    n = len(day_list)
    alloc = [[defaultdict(float) for _ in range(n)] for _ in VARIANTS]
    observed = [[0.0] * n for _ in VARIANTS]
    overlap = [[0.0] * n for _ in VARIANTS]
    cache: dict = {}

    def resolved_of(basis: dict) -> tuple:
        """(resolved items, unlabelled remainder, competing-claims flag), memoized per share set."""
        key = tuple(sorted(basis.items()))
        hit = cache.get(key)
        if hit is None:
            resolved = _resolve(basis, rule, order)
            total = sum(basis.values())
            rest = 1.0 - min(1.0, total) if rule == 'count_both' else 1.0 - sum(min(f, 1.0) for f in resolved.values())
            hit = cache[key] = (tuple(resolved.items()), rest if rest > 1e-12 else 0.0, total > 1 + 1e-9)
        return hit

    for a, b, shares in iv.weighted_segments(tagged, breakpoints):
        if a < lo or b > hi:
            continue
        day = bisect.bisect_right(starts, a) - 1
        dt = b - a
        s0, s1, s2 = {}, {}, {}
        for (level, c), w in shares.items():
            if w > s2.get(c, 0.0):
                s2[c] = w
            if level <= 1 and w > s1.get(c, 0.0):
                s1[c] = w
            if level == 0 and w > s0.get(c, 0.0):
                s0[c] = w
        per_variant = (s0, s1, s2)
        layer = s0 or s1 or s2  # layered: confirmed > estimated > gap attribution
        for v, s in enumerate(per_variant):
            if not s:
                continue
            observed[v][day] += dt
            items, rest, competing = resolved_of(s)
            if competing:
                overlap[v][day] += dt
            if rule != 'count_both':
                items, rest, _ = resolved_of(layer)
            else:
                # Each category keeps its own union per variant, but the unlabelled remainder is derived
                # from the best-supported layer like the other rules, so lower <= probable <= upper
                # holds for bez_kategorii too (review 2 finding 8).
                rest = resolved_of(layer)[1]
            target = alloc[v][day]
            for c, f in items:
                target[c] += f * dt
            if rest:
                target[cat.UNLABELLED] += rest * dt
    return alloc, observed, overlap


def _sign(x: float, tol: float) -> int:
    return 0 if abs(x) <= tol else (1 if x > 0 else -1)


# ------------------------------------------------------------------ main entry

def compute_budget(items: list[dict], labels: dict[int, dict], config: cat.CategoryConfig, *, lo: float, hi: float,
                   zone_name: str, params: dict) -> dict:
    """Pure computation over prepared items and labels (no database access)."""
    zone = cal.zone_info(zone_name)
    day_list = cal.days(lo, hi, zone)
    points, grid_total, grid_truncated = grid_points(params)
    acts = _prepare(items, labels, params)
    label_cats = sorted({c for a in acts for c in a['cats']})
    palette = cat.category_palette(config, label_cats)
    category_ids = [c['id'] for c in palette if c['id'] != cat.UNOBSERVED]
    order = [c for c in params['priority'] if c in category_ids] + \
        [c for c in category_ids if c not in params['priority']]
    activity_ids = [c for c in category_ids if c not in cat.RESERVED]
    margin = horizon_margin(params, points)
    horizon = (lo - margin, hi + margin)
    tol = params['flip_tolerance_s']
    gran_buckets = {g: cal.buckets(day_list, g) for g in params['granularities']}

    def bucketize(alloc, observed, overlap):
        """Per granularity → per bucket: {'cat': {c: [L,P,U]}, 'observed': [..], 'overlap': [..]}."""
        out = {}
        for g, bl in gran_buckets.items():
            rows = []
            for b in bl:
                cats = {c: [sum(alloc[v][d].get(c, 0.0) for d in b['days']) for v in range(3)] for c in category_ids}
                rows.append({'cat': cats, 'observed': [sum(observed[v][d] for d in b['days']) for v in range(3)],
                             'overlap': [sum(overlap[v][d] for d in b['days']) for v in range(3)]})
            out[g] = rows
        return out

    base = None
    stats = {}  # (g, bucket index, category) -> [minL, maxU, minP, maxP]
    ustats = {}  # (g, bucket index) -> [minUnobsU, maxUnobsL, minUnobsP, maxUnobsP]
    flips = {'ordering': [], 'top_category': [], 'trend': []}
    flip_counts = Counter()
    unstable_candles = defaultdict(set)
    sens_points = []
    contributions = None
    for index, point in enumerate(points):
        confirmed, estimated, extents = _point_intervals(acts, point, params)
        gaps = _gap_intervals(confirmed, estimated, acts, extents, horizon, point['max_attribution_s'])
        if params['uncertainty_mode'] == 'widen_upper':
            gaps += _uncertainty_intervals(acts)
        alloc, observed, overlap = _allocate([confirmed, estimated, gaps], day_list, lo, hi, point['overlap_rule'], order)
        per = bucketize(alloc, observed, overlap)
        totals = {c: [sum(alloc[v][d].get(c, 0.0) for d in range(len(day_list))) for v in range(3)] for c in category_ids}
        obs_tot = [sum(observed[v]) for v in range(3)]
        window_len = hi - lo
        sens_points.append({'index': index, 'point': point,
                            'differs': {k: point[k] for k in GRID_KEYS if index and point[k] != points[0][k]},
                            'totals': {c: {'lower': _r(t[0]), 'probable': _r(t[1]), 'upper': _r(t[2])}
                                       for c, t in totals.items()},
                            'unobserved': {'lower': _r(window_len - obs_tot[2]), 'probable': _r(window_len - obs_tot[1]),
                                           'upper': _r(window_len - obs_tot[0])}})
        for g, rows in per.items():
            for bi, row in enumerate(rows):
                length = gran_buckets[g][bi]['length_s']
                for c, (lv, pv, uv) in row['cat'].items():
                    s = stats.get((g, bi, c))
                    stats[(g, bi, c)] = [lv, uv, pv, pv] if s is None else \
                        [min(s[0], lv), max(s[1], uv), min(s[2], pv), max(s[3], pv)]
                un = [length - row['observed'][2], length - row['observed'][0], length - row['observed'][1]]
                s = ustats.get((g, bi))
                ustats[(g, bi)] = [un[0], un[1], un[2], un[2]] if s is None else \
                    [min(s[0], un[0]), max(s[1], un[1]), min(s[2], un[2]), max(s[3], un[2])]
        if index == 0:
            base = {'per': per, 'totals': totals, 'observed': obs_tot,
                    'overlap': [sum(overlap[v]) for v in range(3)]}
            contributions = _contributions(acts, confirmed + estimated + gaps, day_list, lo, hi)
            continue
        _detect_flips(base, per, totals, activity_ids, gran_buckets, index, sens_points[-1]['differs'], tol,
                      params['max_flip_records'], flips, flip_counts, unstable_candles)

    label_paths = {a['eid']: a['path'] for a in acts}
    candles = {}
    for g, bl in gran_buckets.items():
        rows = []
        for bi, b in enumerate(bl):
            row = base['per'][g][bi]
            entry = {'bucket': b['key'], 'start_utc': cal.iso_utc(b['start']), 'end_utc': cal.iso_utc(b['end']),
                     'length_s': _r(b['length_s']), 'nominal_length_s': _r(b['nominal_length_s']),
                     'partial': b['partial'], 'categories': {}}
            for c in category_ids:
                lv, pv, uv = row['cat'][c]
                s = stats[(g, bi, c)]
                contrib = {}
                for d in b['days']:
                    for eid, roles in contributions.get((d, c), {}).items():
                        contrib.setdefault(eid, set()).update(roles)
                if not (s[1] or contrib):
                    continue
                ids = sorted(contrib)
                cap = params['max_candle_events']
                entry['categories'][c] = {
                    'lower': _r(lv), 'probable': _r(pv), 'upper': _r(uv),
                    'wick_low': _r(s[0]), 'wick_high': _r(s[1]), 'body_low': _r(s[2]), 'body_high': _r(s[3]),
                    'event_count': len(ids), 'event_ids': ids[:cap], 'event_ids_truncated': len(ids) > cap,
                    'roles': dict(sorted(Counter(r for e in ids for r in contrib[e]).items())),
                    'label_paths': dict(sorted(Counter(label_paths[e] for e in ids).items())),
                    'unstable': sorted(unstable_candles.get((g, bi, c), ())),
                }
            u = ustats[(g, bi)]
            length = b['length_s']
            entry['unobserved'] = {'lower': _r(length - row['observed'][2]), 'probable': _r(length - row['observed'][1]),
                                   'upper': _r(length - row['observed'][0]), 'wick_low': _r(u[0]),
                                   'wick_high': _r(u[1]), 'body_low': _r(u[2]), 'body_high': _r(u[3])}
            entry['observed'] = {v: _r(row['observed'][i]) for i, v in enumerate(VARIANTS)}
            entry['overlap_s'] = {v: _r(row['overlap'][i]) for i, v in enumerate(VARIANTS)}
            entry['unstable'] = sorted({r for (gg, bb, _), rs in unstable_candles.items() if gg == g and bb == bi for r in rs})
            rows.append(entry)
        candles[g] = rows

    window_len = hi - lo
    ranges = {}
    for c in category_ids:
        probs = [p['totals'][c]['probable'] for p in sens_points]
        basev = probs[0]
        ranges[c] = {'base': basev, 'min': min(probs), 'max': max(probs),
                     'relative_spread': _r((max(probs) - min(probs)) / basev) if basev > 0 else None}
    totals = {c: {'lower': _r(base['totals'][c][0]), 'probable': _r(base['totals'][c][1]),
                  'upper': _r(base['totals'][c][2]),
                  'wick_low': min(p['totals'][c]['lower'] for p in sens_points),
                  'wick_high': max(p['totals'][c]['upper'] for p in sens_points),
                  'body_low': ranges[c]['min'], 'body_high': ranges[c]['max']} for c in category_ids}
    alloc_sum_p = sum(base['totals'][c][1] for c in category_ids)
    return {
        'palette': palette,
        'grid': {'mode': params['grid_mode'], 'points_evaluated': len(points), 'points_total': grid_total,
                 'truncated': grid_truncated, 'keys': list(GRID_KEYS)},
        'margin_s': margin,
        'totals': {'window_length_s': _r(window_len), 'categories': totals,
                   'unobserved': {'lower': _r(window_len - base['observed'][2]),
                                  'probable': _r(window_len - base['observed'][1]),
                                  'upper': _r(window_len - base['observed'][0]),
                                  'wick_low': min(p['unobserved']['lower'] for p in sens_points),
                                  'wick_high': max(p['unobserved']['upper'] for p in sens_points)},
                   'observed': {v: _r(base['observed'][i]) for i, v in enumerate(VARIANTS)},
                   'overlap_s': {v: _r(base['overlap'][i]) for i, v in enumerate(VARIANTS)},
                   'double_counted_probable_s': _r(max(0.0, alloc_sum_p - base['observed'][1]))},
        'candles': candles,
        'sensitivity': {'points': sens_points, 'ranges': ranges, 'tolerance_s': tol,
                        'flip_counts': dict(sorted(flip_counts.items())), 'flips': flips,
                        'flip_records_truncated': any(flip_counts[k] > len(flips[k]) for k in flips),
                        'unstable': bool(flip_counts)},
        'activity_count': len(acts),
    }


MAX_UTC_OFFSET_S = 14 * 3600


def _reaches(item: dict, early: float) -> bool:
    """The item (with its end, declared duration or uncertainty interval) reaches ``early`` or later."""
    end = max(item['t_start'], item.get('t_end') or item['t_start'], item.get('t_hi') or item['t_start'],
              item['t_start'] + (item.get('declared_duration_s') or 0))
    return end >= early


def horizon_margin(params: dict, points: list[dict]) -> float:
    return max([params['max_typing_s'], params['max_reading_s']] +
               [max(p['session_gap_s'], p['max_attribution_s']) for p in points]) + 1.0


def _contributions(acts, intervals, day_list, lo, hi):
    starts = [d['start'] for d in day_list]
    out: dict = defaultdict(lambda: defaultdict(set))
    for s, e, c, _, eids, role in intervals:
        a, b = max(s, lo), min(e, hi)
        if b <= a:
            continue
        first = bisect.bisect_right(starts, a) - 1
        last = bisect.bisect_left(starts, b) - 1
        for d in range(first, last + 1):
            for eid in eids:
                out[(d, c)][eid].add(role)
    for a in acts:
        if lo <= a['t'] < hi:
            d = bisect.bisect_right(starts, a['t']) - 1
            for c in a['cats']:
                out[(d, c)][a['eid']].add('point')
    return out


def _detect_flips(base, per, totals, activity_ids, gran_buckets, index, differs, tol, cap, flips, counts, unstable):
    def record(kind, entry):
        counts[kind] += 1
        if len(flips[kind]) < cap:
            flips[kind].append({**entry, 'point_index': index, 'differs': differs})

    for a, b in itertools.combinations(activity_ids, 2):
        d0 = base['totals'][a][1] - base['totals'][b][1]
        d1 = totals[a][1] - totals[b][1]
        if _sign(d0, tol) * _sign(d1, tol) < 0:
            record('ordering', {'categories': [a, b], 'base_difference_s': _r(d0), 'point_difference_s': _r(d1)})
    for g, rows in per.items():
        base_rows = base['per'][g]
        for bi, row in enumerate(rows):
            bvals = {c: base_rows[bi]['cat'][c][1] for c in activity_ids}
            pvals = {c: row['cat'][c][1] for c in activity_ids}
            if bvals:
                top = max(sorted(bvals), key=lambda c: bvals[c])
                clear = all(bvals[top] - v > tol for c, v in bvals.items() if c != top)
                if clear and bvals[top] > tol:
                    beaten = sorted(c for c in activity_ids if c != top and pvals[c] - pvals[top] > tol)
                    if beaten:
                        record('top_category', {'granularity': g, 'bucket': gran_buckets[g][bi]['key'],
                                                'base_top': top, 'point_top': max(beaten, key=lambda c: pvals[c])})
                        for c in [top, *beaten]:
                            unstable[(g, bi, c)].add('top_category_flip')
            if bi == 0:
                continue
            for c in activity_ids:
                d0 = base_rows[bi]['cat'][c][1] - base_rows[bi - 1]['cat'][c][1]
                d1 = row['cat'][c][1] - rows[bi - 1]['cat'][c][1]
                if _sign(d0, tol) * _sign(d1, tol) < 0:
                    record('trend', {'granularity': g, 'from_bucket': gran_buckets[g][bi - 1]['key'],
                                     'to_bucket': gran_buckets[g][bi]['key'], 'category': c,
                                     'base_change_s': _r(d0), 'point_change_s': _r(d1)})
                    unstable[(g, bi - 1, c)].add('trend_flip')
                    unstable[(g, bi, c)].add('trend_flip')


def _r(x: float) -> float:
    return round(float(x), 3) + 0.0


def method_record(params: dict, source: dict | None = None) -> dict:
    """Method description of a run; ``source`` is the run's ``input`` (seam, rule id and rule SHA-256).

    Without ``source`` (a description of the method only, no run) the seam is ``unknown`` rather than
    the interim rule, which earlier versions named unconditionally.
    """
    source = source or {}
    return {
        'rule': RULE, 'rule_history': RULE_HISTORY,
        'seam': source.get('seam') or 'unknown',
        'source_rule': source.get('source_rule') or 'unknown',
        'source_rule_sha256': source.get('source_rule_sha256') or 'unknown',
        'interim_fallback_rule': SOURCE_RULE,
        'session_rule': iv.SESSION_RULE, 'labeller': cat.LABELLER_ID,
        'artifact_class': 'interpretive',
        'variants': {
            'lower': 'confirmed ranges only: source end time or declared duration (call log, WAV header)',
            'probable': 'lower + typing (outgoing text) + reading (incoming chatbot answer) + session spans per category',
            'upper': 'probable + each gap attributed to each neighbouring category, min(gap, max_attribution_s)',
        },
        'union': 'within a category: union of intervals weighted by label weight (maximum); no double counting',
        'overlap_rule': params['overlap_rule'],
        'overlap_time': 'time where the shares of simultaneously active categories sum above 1 (competing claims)',
        'overlap_rules': {
            'split_equal': 'simultaneous shares scaled to sum 1 when they exceed it; layered confirmed > estimated > gap',
            'priority': 'shares assigned in priority order up to 1; layered confirmed > estimated > gap',
            'count_both': 'each category keeps its full union; overlap and double counting reported',
        },
        'unlabelled_remainder': f'observed time whose label weights sum below 1 goes to {cat.UNLABELLED}',
        'unobserved': f'{cat.UNOBSERVED} = bucket length - observed time of a variant (lower uses U, upper uses L); not idleness',
        'candle': {'lower/probable/upper': 'at the run parameters (grid point 0)',
                   'wick': '[min over grid of lower, max over grid of upper]',
                   'body': '[min, max] over grid of probable'},
        'instability': ('ordering of window totals between two categories, the top category of a bucket, or the '
                        'direction of change between consecutive buckets changes sign (beyond flip_tolerance_s) at '
                        'some grid point'),
        'calendar': 'buckets are local days/ISO weeks/months in the stated zone; DST days last 23 h or 25 h',
        'exclusions': 'untimed items (unknown zone, no parseable time) and filtered items are counted, not placed',
        'time_source': ('timeline display rule (seam timeline): t_start is the display time; items whose uncertainty '
                        'interval [t_lo, t_hi] is wider than max_time_uncertainty_s are excluded; with uncertainty_mode '
                        'widen_upper the upper variant also covers [t_lo, t_hi + confirmed duration]'),
        'hidden': 'items hidden by the timeline projection (chat parts, hidden chat messages) are excluded unless include_hidden',
        'ambiguous_fold': ('a wall clock in a repeated DST hour has two instants; ambiguous_fold=exclude (default) '
                           'excludes it with that reason; carry keeps the display (earlier) instant for lower/probable, '
                           'flags it and lets the upper variant cover both instants (t_lo..t_hi)'),
    }


def prepare_inputs(db, config: cat.CategoryConfig, *, window_from: str, window_to: str, zone: str,
                   params: dict | None = None, assume_zone: str | None = None, assumption_rule: str | None = None,
                   persist: bool = True, need_fields: bool = False) -> dict:
    """Validate, load items (window plus margin), filter, label and (optionally) store the labelling run.

    Call it before writing in the same connection: the timeline projection refreshes in its own transaction.
    ``run_budget``/``run_features`` pass ``persist=False`` and store the labelling run only after the
    computation, in one short write transaction (``persist_labels``; review 2 finding 2).
    """
    from . import model_runs
    p = effective_params(params)
    z = cal.zone_info(zone)
    if assume_zone is not None:
        cal.zone_info(assume_zone)
    lo, hi = cal.parse_bound(window_from, z), cal.parse_bound(window_to, z)
    if not hi > lo:
        raise ValueError('window end must be after its start')
    cal.days(lo, hi, z)  # validates the window length before any loading
    points, _, _ = grid_points(p)
    margin = horizon_margin(p, points)
    # Review 2 P1: a declared duration (call log, WAV header) is not in the projection's end time, so an
    # item that starts before the margin can still reach into it. Load ``duration_lookback_s`` earlier
    # and keep from that extra range only the items whose end or declared duration reaches the margin.
    early = lo - margin
    lookback = p['duration_lookback_s']
    loaded = load_items(db, start_utc=cal.iso_utc(early - lookback), end_utc=cal.iso_utc(hi + margin),
                        assume_zone=assume_zone, assumption_rule=assumption_rule, include_hidden=True,
                        limit=p['max_items'], fields=need_fields or cat.needs_fields(config),
                        media_durations=p['wav_header_durations'], timeline_rule=p['timeline_rule'])
    if lookback:
        loaded['items'] = [it for it in loaded['items'] if _reaches(it, early)]
        loaded['untimed'] = [u for u in loaded['untimed']
                             if u.get('local_as_utc') is None or u['local_as_utc'] + MAX_UTC_OFFSET_S >= early]
    kept, filtered = [], Counter()
    for it in loaded['items']:
        reason = _filter_reason(it, p)
        if reason:
            filtered[reason] += 1
        else:
            kept.append(it)
    ids = [it['event_id'] for it in kept]
    annotations = cat.load_effective_annotations(db, ids)
    model_labels = None
    if p['model_label_run_id'] is not None:
        model_labels = model_runs.load_outputs(db, p['model_label_run_id'], purpose='labels')
    labels = cat.label_items(kept, config, annotations=annotations, model_labels=model_labels)
    for it in kept:  # bodies were needed only for keyword rules; free them before the grid computation
        if it.get('fields'):
            it['fields'].pop('body', None)
    input_sha = event_set_sha256(ids)
    label_run = None
    untimed = loaded['untimed']
    in_window = sum(1 for it in kept if it['t_start'] < hi and max(it['t_start'], it['t_end'] or it['t_start']) >= lo)
    window = {'from': window_from, 'to': window_to, 'zone': zone, 'from_utc': cal.iso_utc(lo), 'to_utc': cal.iso_utc(hi),
              'margin_s': margin, 'assume_zone': assume_zone, 'assumption_rule': assumption_rule}
    widths = [it['t_hi'] - it['t_lo'] for it in kept if it.get('t_hi') is not None and it['t_hi'] > it['t_lo']]
    day_starts = [d['start'] for d in cal.days(lo, hi, z)]
    crossing = sum(1 for it in kept if it.get('t_hi') is not None and
                   bisect.bisect_right(day_starts, it['t_lo']) != bisect.bisect_right(day_starts, it['t_hi']))
    prepared = {
        'params': p, 'lo': lo, 'hi': hi, 'zone': zone, 'window': window, 'items': kept, 'labels': labels,
        'label_run': label_run, 'input_sha256': input_sha,
        'labels_record': {'label_run_id': label_run['label_run_id'] if label_run else None,
                          'labels_sha256': cat.labels_sha256(labels),
                          'by_path': dict(sorted(Counter(l['path'] for l in labels.values()).items())),
                          'issues': dict(sorted(Counter(i for l in labels.values() for i in l['issues']).items())),
                          'model_label_run_id': p['model_label_run_id']},
        'input': {'seam': loaded.get('seam'), 'source_rule': loaded['rule'], 'source_rule_sha256': loaded.get('rule_sha256'),
                  'projection': loaded.get('projection'), 'projection_params': loaded.get('params'),
                  'event_ids_sha256': input_sha, 'items_used': len(kept), 'duration_lookback_s': lookback,
                  'items_in_window': in_window, 'items_in_margin_only': len(kept) - in_window,
                  'items_sha256': sha256_json([[it['event_id'], it['t_start'], it['t_end'], it.get('t_lo'), it.get('t_hi'),
                                                it.get('basis'), it.get('declared_duration_s'), it.get('text_chars'),
                                                it.get('direction')] for it in kept]),
                  'time_uncertainty': {'items_with_interval': len(widths), 'max_width_s': _r(max(widths)) if widths else 0.0,
                                       'items_crossing_day_boundary': crossing, 'mode': p['uncertainty_mode'],
                                       'max_time_uncertainty_s': p['max_time_uncertainty_s']}},
        'exclusions': {
            'untimed': {'count': len(untimed), 'by_reason': dict(sorted(Counter(u['reason'] for u in untimed).items())),
                        'event_ids': [u['event_id'] for u in untimed[:1000]],
                        'event_ids_truncated': len(untimed) > 1000,
                        'scope': ('unknown-zone wall clocks that could fall in the loaded window under any real UTC '
                                  'offset, and every event without a usable time (undated)')},
            'filtered': {'count': sum(filtered.values()), 'by_reason': dict(sorted(filtered.items()))},
            'assumed_zone_items': sum(1 for it in kept if it['time_basis'].get('assumed_zone')),
            'hidden_excluded_by_seam': len(loaded.get('hidden_excluded', [])),
            'ambiguous_fold_items_carried': sum(1 for it in kept if (it.get('time_basis') or {}).get('dst') == 'ambiguous_fold'),
            'source_truncated': loaded['truncated'],
        },
    }
    if persist:
        persist_labels(db, prepared, config)
    return prepared


@contextmanager
def write_transaction(db):
    """One short write transaction for storing a finished computation (a savepoint inside the caller's)."""
    if db.in_transaction:
        db.execute('SAVEPOINT analysis_persist')
        try:
            yield db
        except BaseException:
            db.execute('ROLLBACK TO SAVEPOINT analysis_persist')
            db.execute('RELEASE SAVEPOINT analysis_persist')
            raise
        db.execute('RELEASE SAVEPOINT analysis_persist')
        return
    db.execute('BEGIN IMMEDIATE')
    try:
        yield db
    except BaseException:
        db.rollback()
        raise
    db.commit()


def persist_labels(db, prepared: dict, config: cat.CategoryConfig) -> dict:
    """Store (or reuse) the labelling run of ``prepared`` and record its id in ``labels_record``."""
    label_run = cat.record_label_run(db, prepared['labels'], config, input_sha256=prepared['input_sha256'],
                                     code_version=code_version(),
                                     parameters={'model_label_run_id': prepared['params']['model_label_run_id']})
    prepared['label_run'] = label_run
    prepared['labels_record'] = {**prepared['labels_record'], 'label_run_id': label_run['label_run_id']}
    return label_run


def run_budget(db, config: cat.CategoryConfig, *, window_from: str, window_to: str, zone: str,
               params: dict | None = None, assume_zone: str | None = None, assumption_rule: str | None = None,
               persist: bool = True) -> dict:
    from . import runs
    # Read and compute without a write transaction; the label run and the analysis run are stored
    # together afterwards in one short transaction (review 2 finding 2: no lock during the grid).
    prepared = prepare_inputs(db, config, window_from=window_from, window_to=window_to, zone=zone, params=params,
                              assume_zone=assume_zone, assumption_rule=assumption_rule, persist=False)
    p = prepared['params']
    computed = compute_budget(prepared['items'], prepared['labels'], config, lo=prepared['lo'], hi=prepared['hi'],
                              zone_name=zone, params=p)
    result = {
        'schema': RESULT_SCHEMA, 'artifact_class': 'interpretive', 'disclaimer': DISCLAIMER,
        'method': method_record(p, prepared['input']), 'parameters': p,
        'code': {'version': code_version(), 'sha256': code_sha256()},
        'window': prepared['window'], 'config': config.describe(), 'categories': computed['palette'],
        'labels': prepared['labels_record'], 'input': prepared['input'], 'exclusions': prepared['exclusions'],
        **{k: computed[k] for k in ('grid', 'totals', 'candles', 'sensitivity')},
    }
    stored = None
    if persist:
        with write_transaction(db):
            persist_labels(db, prepared, config)
            result['labels'] = prepared['labels_record']
            stored = runs.store_run(db, kind='time_budget', parameters={'params': p, 'window': result['window']},
                                    config_sha256=config.sha256, config_version=config.version, source_rule=RULE,
                                    input_sha256=prepared['input_sha256'],
                                    label_run_id=prepared['labels_record']['label_run_id'], result=result)
    return {'run': stored, 'result': result}

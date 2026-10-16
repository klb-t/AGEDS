"""Event study: outcome events around stimulus events vs. a seeded permutation null.

Method (all integer arithmetic on UTC milliseconds; no parametric p-values):

* Stimulus, outcome, density and eligible sets are event filters from the
  registered definition, evaluated over ``analysis.source.load_items``. Events
  without a placeable time (unknown zone, no candidate) are excluded and counted.
* Window statistic: number of (stimulus, outcome) pairs with lag in the
  half-open window ``[start_s, end_s)``; lag = outcome time − stimulus time.
* Detection-bias control: documentation density ``D`` (default: every timed
  event that is neither a stimulus nor an outcome). The normalized statistic is
  the excess of outcomes over the global documentation share,
  ``O_w − (|O|/|D|)·D_w``, compared integer-exactly as ``O_w·|D| − |O|·D_w``.
* Null: the stimulus times are moved ``n`` times by the registered null model
  (circular shift within the observed span, permutation among eligible event
  times, permutation within weekday×hour strata, or block permutation) using
  the versioned SplitMix64 generator seeded from the definition.
* Empirical p-value ``(k + 1) / (n + 1)``, where ``k`` counts null statistics at
  least as extreme as the observed one in the registered direction.

Warnings (causation, detection bias, seasonality, clustering, dependence,
unknown-zone exclusions, time basis, preregistration deviations) are always
present in the result with their computed values; ``flagged`` marks the ones
the data trigger. The result is auxiliary material, never a causal finding.
"""
from __future__ import annotations

import json
import math
import sqlite3
from array import array
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .hypotheses import (HypothesisError, HypothesisNotFound, canonical_json, category_event_ids, clean_actor,
                         code_version, disclaimer, fts_event_ids, selector_matches, sha256_canonical, utc_now)
from .preregistration import _analysis, require_registered

RESULT_SCHEMA = 'ageds.event-study-result/v1'
RNG_ID = 'splitmix64.v1'
QUANTILE_RULE = 'nearest_rank_ceil'
OVERRIDABLE = ('permutations', 'seed', 'null_model', 'block_s', 'stratify_zone')
SEASONALITY_TV_THRESHOLD = 0.2
DEPENDENCE_CV_THRESHOLD = 1.5
WEAK_TIME_KINDS = ('file_mtime', 'export_time', 'observed', 'filename_derived', 'other')
MASK64 = (1 << 64) - 1


@dataclass(frozen=True)
class EventStudyLimits:
    max_events: int = 1_000_000
    max_fts_ids: int = 1_000_000
    max_work: int = 60_000_000
    max_null_cells: int = 10_000_000
    max_control_bins: int = 5_000
    max_listed_ids: int = 1_000

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class SplitMix64:
    """Small, versioned PRNG so results do not depend on Python's ``random`` internals."""

    def __init__(self, seed: int):
        self.state = seed & MASK64

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        """Uniform integer in ``[0, n)`` by rejection (no modulo bias)."""
        if n <= 0:
            raise ValueError('n must be positive')
        limit = (1 << 64) - ((1 << 64) % n)
        while True:
            x = self.next_u64()
            if x < limit:
                return x % n

    def sample_indices(self, n: int, k: int) -> list[int]:
        """``k`` distinct indices from ``range(n)``: sparse Fisher–Yates, O(k) memory."""
        if not 0 <= k <= n:
            raise ValueError('cannot sample more items than available')
        swaps: dict[int, int] = {}
        out = []
        for i in range(k):
            j = i + self.below(n - i)
            vi, vj = swaps.get(i, i), swaps.get(j, j)
            swaps[j] = vi
            out.append(vj)
        return out

    def permutation(self, n: int) -> list[int]:
        return self.sample_indices(n, n)


# ---------------------------------------------------------------- small exact helpers

def _round(value, digits: int = 9):
    if value is None:
        return None
    return round(float(value), digits)


def p_value(k: int, n: int) -> float:
    """Empirical permutation p-value; never 0 and never above 1."""
    if not 0 <= k <= n or n < 1:
        raise ValueError('need 0 <= k <= n and n >= 1')
    return (k + 1) / (n + 1)


def _quantile(sorted_values, q_permille: int):
    n = len(sorted_values)
    index = max(0, -(-(q_permille * n) // 1000) - 1)
    return sorted_values[min(index, n - 1)]


def _extreme_count(null_values, observed: int, direction: str) -> int:
    if direction == 'increase':
        return sum(1 for v in null_values if v >= observed)
    if direction == 'decrease':
        return sum(1 for v in null_values if v <= observed)
    n, total = len(null_values), sum(null_values)
    distance = abs(n * observed - total)
    return sum(1 for v in null_values if abs(n * v - total) >= distance)


def _summary(null_values, observed: int, direction: str, scale: int = 1) -> dict:
    """Null distribution summary; ``scale`` divides integer statistics for display only."""
    ordered = sorted(null_values)
    n = len(ordered)
    k = _extreme_count(null_values, observed, direction)
    mean = sum(ordered) / n / scale
    return {'observed': _round(observed / scale), 'null_mean': _round(mean),
            'null_q025': _round(_quantile(ordered, 25) / scale), 'null_q500': _round(_quantile(ordered, 500) / scale),
            'null_q975': _round(_quantile(ordered, 975) / scale), 'k_extreme': k, 'n_permutations': n,
            'p_value': _round(p_value(k, n), 12), 'p_formula': '(k+1)/(n+1)'}


def _count(sorted_times, lo: int, hi: int) -> int:
    return bisect_left(sorted_times, hi) - bisect_left(sorted_times, lo)


def _utc_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _parse_utc_ms(text: str) -> int:
    return round(datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp() * 1000)


# ---------------------------------------------------------------- null models

class _NullModel:
    def __init__(self, name: str, stimuli: list[int], t0: int, t1: int, eligible: list[int], block_ms: int,
                 zone: str, rng: SplitMix64):
        self.name, self.stimuli, self.t0, self.t1, self.rng = name, stimuli, t0, t1, rng
        self.span = t1 - t0
        self.info: dict = {'name': name}
        if name == 'permutation':
            if len(eligible) < len(stimuli):
                raise HypothesisError(f'permutation null needs at least as many eligible times ({len(eligible)}) as stimuli ({len(stimuli)})')
            self.eligible = eligible
            self.info['eligible_times'] = len(eligible)
        elif name == 'stratified_weekday_hour':
            tz = ZoneInfo(zone)
            buckets: dict[tuple[int, int], list[int]] = {}
            for t in eligible:
                buckets.setdefault(_weekday_hour(t, tz), []).append(t)
            groups: dict[tuple[int, int], list[int]] = {}
            for index, s in enumerate(stimuli):
                groups.setdefault(_weekday_hour(s, tz), []).append(index)
            self.plan, fixed, insufficient = [], [], []
            for key in sorted(groups):
                members, pool = groups[key], buckets.get(key, [])
                if len(pool) < len(members):
                    fixed.extend(members)
                    insufficient.append({'weekday': key[0], 'hour': key[1], 'stimuli': len(members), 'eligible': len(pool)})
                else:
                    self.plan.append((members, pool))
            self.fixed = [stimuli[i] for i in fixed]
            self.info.update({'zone': zone, 'strata_used': len(self.plan), 'strata_insufficient': insufficient,
                              'stimuli_kept_in_place': len(fixed)})
        elif name == 'block_permutation':
            self.block = block_ms
            self.blocks = -(-self.span // block_ms)
            if self.blocks < 2:
                raise HypothesisError('block_permutation needs at least two blocks inside the observed span; decrease block_s')
            self.info.update({'block_s': block_ms // 1000, 'blocks': self.blocks})
        elif name != 'circular_shift':
            raise HypothesisError(f'unknown null model {name}')

    def draw(self) -> list[int]:
        rng, t0, span = self.rng, self.t0, self.span
        if self.name == 'circular_shift':
            u = rng.below(span)
            return [t0 + (s - t0 + u) % span for s in self.stimuli]
        if self.name == 'permutation':
            return [self.eligible[i] for i in rng.sample_indices(len(self.eligible), len(self.stimuli))]
        if self.name == 'stratified_weekday_hour':
            out = list(self.fixed)
            for members, pool in self.plan:
                out.extend(pool[i] for i in rng.sample_indices(len(pool), len(members)))
            return out
        order = rng.permutation(self.blocks)
        block = self.block
        return [t0 + (order[(s - t0) // block] * block + (s - t0) % block) % span for s in self.stimuli]


def _weekday_hour(ms: int, tz: ZoneInfo) -> tuple[int, int]:
    local = datetime.fromtimestamp(ms / 1000, tz)
    return local.weekday(), local.hour


# ---------------------------------------------------------------- statistics per stimulus set

def _window_counts(stimuli, times, windows_ms) -> list[int]:
    out = []
    for lo, hi in windows_ms:
        total = 0
        for s in stimuli:
            total += bisect_left(times, s + hi) - bisect_left(times, s + lo)
        out.append(total)
    return out


def _histogram(stimuli, times, edges_ms) -> list[int]:
    counts = [0] * (len(edges_ms) - 1)
    for s in stimuli:
        previous = bisect_left(times, s + edges_ms[0])
        for j in range(1, len(edges_ms)):
            current = bisect_left(times, s + edges_ms[j])
            counts[j - 1] += current - previous
            previous = current
    return counts


def _distribution(times, tz: ZoneInfo) -> dict:
    weekday, hour = [0] * 7, [0] * 24
    for t in times:
        w, h = _weekday_hour(t, tz)
        weekday[w] += 1
        hour[h] += 1
    return {'weekday': weekday, 'hour': hour}


def _tv(a: list[int], b: list[int]) -> float | None:
    sa, sb = sum(a), sum(b)
    if not sa or not sb:
        return None
    return 0.5 * sum(abs(x / sa - y / sb) for x, y in zip(a, b))


def _alignment(a: list[int], b: list[int], base: list[int]) -> float | None:
    sa, sb, sc = sum(a), sum(b), sum(base)
    if not sa or not sb or not sc:
        return None
    return sum((x / sa - z / sc) * (y / sb - z / sc) for x, y, z in zip(a, b, base))


def _cv(times: list[int]) -> float | None:
    gaps = [b - a for a, b in zip(times, times[1:])]
    if len(gaps) < 2:
        return None
    mean = sum(gaps) / len(gaps)
    if mean == 0:
        return None
    var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
    return math.sqrt(var) / mean


def _overlap(stimuli_sorted: list[int], width_ms: int) -> dict:
    pairs, j = 0, 0
    neighbor = [False] * len(stimuli_sorted)
    for i, s in enumerate(stimuli_sorted):
        while stimuli_sorted[j] <= s - width_ms:
            j += 1
        pairs += i - j
        if i - j > 0:
            neighbor[i] = True
            neighbor[i - 1] = True
    return {'overlapping_pairs': pairs, 'stimuli_with_overlap': sum(neighbor)}


def _union_count(stimuli_sorted: list[int], times: list[int], lo: int, hi: int) -> int:
    total, current = 0, None
    for s in stimuli_sorted:
        a, b = s + lo, s + hi
        if current is None or a > current[1]:
            if current is not None:
                total += _count(times, *current)
            current = [a, b]
        else:
            current[1] = max(current[1], b)
    if current is not None:
        total += _count(times, *current)
    return total


def _control_series(all_times, stim, outc, dens, t0: int, t1: int, period_ms: int, max_bins: int) -> dict:
    factor = 1
    start = (t0 // period_ms) * period_ms
    bins = -(-(t1 - start) // period_ms)
    if bins > max_bins:
        factor = -(-bins // max_bins)
        period_ms *= factor
        start = (t0 // period_ms) * period_ms
        bins = -(-(t1 - start) // period_ms)

    def series(times):
        counts = [0] * bins
        for t in times:
            counts[(t - start) // period_ms] += 1
        return counts
    all_counts = series(all_times)
    gaps, run_start = [], None
    for index, value in enumerate(all_counts + [1]):
        if value == 0 and run_start is None:
            run_start = index
        elif value != 0 and run_start is not None:
            gaps.append((index - run_start, run_start))
            run_start = None
    gaps.sort(key=lambda g: (-g[0], g[1]))
    return {
        'period_s': period_ms // 1000, 'coarsened_by_factor': factor, 'start_utc': _utc_text(start), 'bins': bins,
        'counts_all': all_counts, 'counts_stimulus': series(stim), 'counts_outcome': series(outc),
        'counts_density': series(dens),
        'empty_periods': sum(1 for v in all_counts if v == 0),
        'longest_gaps': [{'periods': length, 'start_utc': _utc_text(start + first * period_ms),
                          'end_utc': _utc_text(start + (first + length) * period_ms)} for length, first in gaps[:10]],
    }


# ---------------------------------------------------------------- warnings

def _warning(wid: str, flagged: bool, severity: str, pl: str, en: str, computed: dict) -> dict:
    return {'id': wid, 'flagged': bool(flagged), 'severity': severity if flagged else 'info',
            'text_pl': pl, 'text_en': en, 'computed': computed}


ALWAYS_PRESENT_WARNINGS = ('causation', 'detection_bias', 'seasonality', 'clustering', 'dependence',
                           'unknown_zone_excluded', 'time_basis', 'preregistration_deviation')


def _warnings(ctx: dict) -> list[dict]:
    windows = ctx.get('windows') or []
    disagreement = [w['label'] for w in windows
                    if w['naive']['p_value'] <= ctx['flag_threshold'] < (w['normalized'] or {}).get('p_value', 0)]
    dense = [w['label'] for w in windows
             if w['density']['ratio'] is not None and w['density']['ratio'] >= ctx['density_ratio_warning']]
    season = ctx['seasonality']
    clustering = ctx['clustering']
    overlapped = [c for c in clustering['windows'] if c['overlapping_pairs'] > 0]
    preserves = ctx['null_model'] in ('circular_shift', 'block_permutation')
    untimed = ctx['untimed']
    return [
        _warning('causation', True, 'info',
                 'Współwystępowanie w czasie nie jest przyczynowością. Wynik opisuje rozkład opóźnień między zapisanymi '
                 'zdarzeniami, nie mechanizm ani kierunek wpływu.',
                 'Co-occurrence in time is not causation. The result describes lags between recorded events, not a '
                 'mechanism or direction of influence.', {}),
        _warning('detection_bias', bool(disagreement or dense), 'high' if disagreement else 'warning',
                 'Błąd detekcji: w okresach konfliktu zwykle dokumentuje się więcej, więc nadwyżka zdarzeń wyniku może '
                 'być artefaktem gęstości zapisu. Porównaj wynik surowy z wynikiem znormalizowanym gęstością '
                 'dokumentacji i serią kontrolną wszystkich zdarzeń.' +
                 (' Wynik surowy przekracza próg, a znormalizowany nie (okna: ' + ', '.join(disagreement) + ').' if disagreement else ''),
                 'Detection bias: more is usually documented during conflict, so an excess of outcome events can be an '
                 'artifact of documentation density. Compare the raw result with the density-normalized result and the '
                 'all-events control series.' +
                 (' The raw result passes the threshold but the normalized one does not (windows: ' + ', '.join(disagreement) + ').' if disagreement else ''),
                 {'naive_vs_normalized_disagreement_windows': disagreement, 'dense_documentation_windows': dense,
                  'flag_threshold': ctx['flag_threshold'], 'density_ratio_warning': ctx['density_ratio_warning'],
                  'density_events': ctx['density_count']}),
        _warning('seasonality', season['shared_structure'] and ctx['null_model'] != 'stratified_weekday_hour', 'warning',
                 'Sezonowość: bodźce i wyniki mogą mieć wspólny rytm tygodniowy/dobowy niezależnie od siebie. '
                 'Sprawdź rozkłady dzień tygodnia × godzina i model zerowy stratified_weekday_hour.' +
                 (' Model zerowy permutuje w blokach dzień×godzina.' if ctx['null_model'] == 'stratified_weekday_hour' else ''),
                 'Seasonality: stimuli and outcomes may share a weekly/daily rhythm independently of each other. Check '
                 'the weekday × hour distributions and the stratified_weekday_hour null model.' +
                 (' The null model permutes within weekday×hour blocks.' if ctx['null_model'] == 'stratified_weekday_hour' else ''),
                 {'zone': season['zone'], 'tv_weekday': season['tv_weekday'], 'tv_hour': season['tv_hour'],
                  'shared_structure': season['shared_structure'], 'threshold': SEASONALITY_TV_THRESHOLD}),
        _warning('clustering', bool(overlapped), 'warning' if preserves else 'high',
                 'Kumulacja: okna bodźców nakładają się, więc te same zdarzenia wyniku liczą się wielokrotnie, a pary '
                 'nie są niezależne. ' + ('Model zerowy zachowuje skupienia bodźców.' if preserves else
                                          'Wybrany model zerowy niszczy skupienia bodźców — użyj circular_shift lub block_permutation.'),
                 'Accumulation: stimulus windows overlap, so the same outcome events are counted repeatedly and pairs '
                 'are not independent. ' + ('The null model preserves stimulus clustering.' if preserves else
                                            'The selected null model destroys stimulus clustering — use circular_shift or block_permutation.'),
                 {'windows': clustering['windows'], 'null_preserves_clustering': preserves}),
        _warning('dependence', (ctx['outcome_cv'] or 0) > DEPENDENCE_CV_THRESHOLD, 'warning',
                 'Zależność między zdarzeniami: zdarzenia wyniku występują seriami (współczynnik zmienności odstępów '
                 'powyżej progu); test permutacyjny przesuwa tylko bodźce, a zależność wyników zwiększa rozrzut.',
                 'Dependence between events: outcome events arrive in bursts (inter-event gap coefficient of variation '
                 'above the threshold); the permutation test moves stimuli only and outcome dependence widens the spread.',
                 {'outcome_gap_cv': _round(ctx['outcome_cv']), 'stimulus_gap_cv': _round(ctx['stimulus_cv']),
                  'threshold': DEPENDENCE_CV_THRESHOLD}),
        _warning('unknown_zone_excluded', untimed['count'] > 0, 'warning',
                 f'Zdarzenia bez umiejscowienia na osi UTC (np. nieznana strefa) są wyłączone: {untimed["count"]}.',
                 f'Events without a position on the UTC axis (e.g. unknown zone) are excluded: {untimed["count"]}.',
                 {'count': untimed['count'], 'by_reason': untimed['by_reason'], 'assume_zone': ctx['assume_zone']}),
        _warning('time_basis', ctx['weak_time_basis'] > 0, 'warning',
                 'Część czasów bodźców/wyników pochodzi ze słabych źródeł (np. czas modyfikacji pliku, eksportu).',
                 'Some stimulus/outcome times come from weak bases (e.g. file modification or export time).',
                 {'weak_kinds': list(WEAK_TIME_KINDS), 'weak_count': ctx['weak_time_basis'], 'by_kind': ctx['time_basis']}),
        _warning('preregistration_deviation', bool(ctx['deviations']), 'high',
                 'Parametry tego przebiegu różnią się od zarejestrowanych: ' +
                 ', '.join(d['key'] for d in ctx['deviations']) + '.' if ctx['deviations'] else
                 'Przebieg użył dokładnie zarejestrowanych parametrów.',
                 'This run used parameters different from the registered ones: ' +
                 ', '.join(d['key'] for d in ctx['deviations']) + '.' if ctx['deviations'] else
                 'The run used exactly the registered parameters.',
                 {'deviations': ctx['deviations']}),
    ]


# ---------------------------------------------------------------- core computation

def compute_event_study(definition: dict, params: dict, loaded: dict, *, fts: dict[str, set],
                        categories: dict[str, set], limits: EventStudyLimits = EventStudyLimits(),
                        deviations: list | None = None) -> tuple[dict, str, int]:
    """Pure computation; returns ``(result_without_run_fields, input_sha256, input_count)``."""
    deviations = deviations or []
    items = [dict(i, t_ms=round(i['t_start'] * 1000)) for i in loaded['items']]
    untimed = loaded['untimed']
    by_reason: dict[str, int] = {}
    for u in untimed:
        by_reason[u['reason']] = by_reason.get(u['reason'], 0) + 1
    if params['span']:
        t0, t1 = _parse_utc_ms(params['span']['start_utc']), _parse_utc_ms(params['span']['end_utc'])
    elif items:
        t0, t1 = min(i['t_ms'] for i in items), max(i['t_ms'] for i in items) + 1
    else:
        t0 = t1 = 0
    inside = [i for i in items if t0 <= i['t_ms'] < t1]
    outside = len(items) - len(inside)

    def select(name):
        return [i for i in inside if selector_matches(i, definition[name], fts_ids=fts.get(name), category_ids=categories.get(name))]
    stim_items = select('stimulus')
    stim_ids = {i['event_id'] for i in stim_items}
    out_items = select('outcome')
    self_pairs = sorted(i['event_id'] for i in out_items if i['event_id'] in stim_ids)
    if params['exclude_self_pairs']:
        out_items = [i for i in out_items if i['event_id'] not in stim_ids]
    out_ids = {i['event_id'] for i in out_items}
    dens_items = [i for i in select('density')
                  if not (params['density_excludes_stimuli'] and i['event_id'] in stim_ids)
                  and not (params['density_excludes_outcomes'] and i['event_id'] in out_ids)]
    elig_times = sorted({i['t_ms'] for i in select('eligible')})
    S = sorted(i['t_ms'] for i in stim_items)
    O = sorted(i['t_ms'] for i in out_items)
    D = sorted(i['t_ms'] for i in dens_items)
    all_times = sorted(i['t_ms'] for i in inside)

    input_record = {
        'rule': loaded['rule'], 'assume_zone': loaded.get('assume_zone'), 'span_ms': [t0, t1],
        'stimulus': sorted([i['event_id'], i['t_ms']] for i in stim_items),
        'outcome': sorted([i['event_id'], i['t_ms']] for i in out_items),
        'density': sorted([i['event_id'], i['t_ms']] for i in dens_items),
        'eligible_times': elig_times, 'self_pairs_excluded': self_pairs if params['exclude_self_pairs'] else [],
        'untimed': sorted([u['event_id'], u['reason']] for u in untimed), 'outside_span': outside,
    }
    input_sha256 = sha256_canonical(input_record)
    input_count = len(stim_items) + len(out_items) + len(dens_items)

    windows_ms = [(w['start_s'] * 1000, w['end_s'] * 1000) for w in definition['windows']]
    lags = definition['lags']
    nb = -(-(lags['max_s'] - lags['min_s']) // lags['bin_s'])
    edges_s = [lags['min_s'] + j * lags['bin_s'] for j in range(nb)] + [lags['max_s']]
    edges_ms = [e * 1000 for e in edges_s]
    tz = ZoneInfo(params['stratify_zone'])

    time_basis: dict[str, int] = {}
    for i in stim_items + out_items:
        kind = i['time_basis']['kind']
        time_basis[kind] = time_basis.get(kind, 0) + 1
    season_all, season_s, season_o = _distribution(all_times, tz), _distribution(S, tz), _distribution(O, tz)
    tv_weekday = {'stimulus': _round(_tv(season_s['weekday'], season_all['weekday'])),
                  'outcome': _round(_tv(season_o['weekday'], season_all['weekday']))}
    tv_hour = {'stimulus': _round(_tv(season_s['hour'], season_all['hour'])),
               'outcome': _round(_tv(season_o['hour'], season_all['hour']))}

    def shared(tv, key):
        align = _alignment(season_s[key], season_o[key], season_all[key])
        return (tv['stimulus'] or 0) >= SEASONALITY_TV_THRESHOLD and (tv['outcome'] or 0) >= SEASONALITY_TV_THRESHOLD \
            and (align or 0) > 0
    seasonality = {'zone': params['stratify_zone'], 'weekday': {'stimulus': season_s['weekday'], 'outcome': season_o['weekday'],
                                                               'all': season_all['weekday']},
                   'hour': {'stimulus': season_s['hour'], 'outcome': season_o['hour'], 'all': season_all['hour']},
                   'tv_weekday': tv_weekday, 'tv_hour': tv_hour,
                   'shared_structure': shared(tv_weekday, 'weekday') or shared(tv_hour, 'hour')}
    clustering = {'windows': [{'label': w['label'], 'width_s': w['end_s'] - w['start_s'],
                               **_overlap(S, (w['end_s'] - w['start_s']) * 1000)} for w in definition['windows']]}

    base = {
        'schema': RESULT_SCHEMA, 'disclaimer': disclaimer(), 'rng': RNG_ID, 'seed': params['seed'],
        'parameters': params, 'deviations_from_registration': deviations,
        'definition': {'windows': definition['windows'], 'lags': lags, 'direction': definition['direction'],
                       'stimulus': definition['stimulus'], 'outcome': definition['outcome'],
                       'density': definition['density'], 'eligible': definition['eligible']},
        'input': {
            'input_sha256': input_sha256, 'rule': loaded['rule'], 'assume_zone': loaded.get('assume_zone'),
            'timed_events': len(items), 'outside_span': outside,
            'span': {'start_utc': _utc_text(t0), 'end_utc': _utc_text(t1)} if t1 > t0 else None,
            'stimulus_count': len(S), 'outcome_count': len(O), 'density_count': len(D), 'eligible_times': len(elig_times),
            'self_pairs': len(self_pairs), 'self_pairs_excluded': params['exclude_self_pairs'],
            'untimed_excluded': {'count': len(untimed), 'by_reason': dict(sorted(by_reason.items())),
                                 'event_ids': sorted(u['event_id'] for u in untimed)[:limits.max_listed_ids],
                                 'listed_limit': limits.max_listed_ids},
            'time_basis': dict(sorted(time_basis.items())),
        },
        'seasonality': seasonality, 'clustering': clustering,
        'dependence': {'outcome_gap_cv': _round(_cv(O)), 'stimulus_gap_cv': _round(_cv(S))},
        'limits': limits.as_dict(), 'quantile_rule': QUANTILE_RULE,
    }
    ctx = {'flag_threshold': params['flag_threshold'], 'density_ratio_warning': params['density_ratio_warning'],
           'seasonality': seasonality, 'clustering': clustering, 'null_model': params['null_model'],
           'untimed': base['input']['untimed_excluded'], 'assume_zone': loaded.get('assume_zone'),
           'outcome_cv': _cv(O), 'stimulus_cv': _cv(S), 'density_count': len(D), 'deviations': deviations,
           'weak_time_basis': sum(v for k, v in time_basis.items() if k in WEAK_TIME_KINDS), 'time_basis': time_basis}

    reasons = []
    if t1 - t0 < 2:
        reasons.append('observed span shorter than 2 ms')
    if not S:
        reasons.append('no stimulus events inside the span')
    if not O:
        reasons.append('no outcome events inside the span')
    if reasons:
        base.update(status='insufficient_data', reasons=reasons, windows=[], lag_histogram=None, null_model={'name': params['null_model']},
                    control_series=_control_series(all_times, S, O, D, t0, max(t1, t0 + 1), params['control_period_s'] * 1000,
                                                   limits.max_control_bins))
        base['warnings'] = _warnings(ctx)
        return base, input_sha256, input_count

    n = params['permutations']
    work = n * len(S) * (4 * len(windows_ms) + len(edges_ms))
    if work > limits.max_work:
        raise HypothesisError(f'event study work estimate {work} exceeds max_work {limits.max_work}; '
                              'reduce permutations or stimuli, or raise the limit explicitly')
    cells = n * (2 * len(windows_ms) + nb)
    if cells > limits.max_null_cells:
        raise HypothesisError(f'null distribution needs {cells} cells, above max_null_cells {limits.max_null_cells}')

    rng = SplitMix64(params['seed'])
    null = _NullModel(params['null_model'], S, t0, t1, elig_times, params['block_s'] * 1000, params['stratify_zone'], rng)
    observed_o = _window_counts(S, O, windows_ms)
    observed_d = _window_counts(S, D, windows_ms)
    observed_hist = _histogram(S, O, edges_ms)
    null_o = [array('q') for _ in windows_ms]
    null_d = [array('q') for _ in windows_ms]
    null_h = [array('q') for _ in range(nb)]
    for _ in range(n):
        shifted = null.draw()
        for index, value in enumerate(_window_counts(shifted, O, windows_ms)):
            null_o[index].append(value)
        for index, value in enumerate(_window_counts(shifted, D, windows_ms)):
            null_d[index].append(value)
        for index, value in enumerate(_histogram(shifted, O, edges_ms)):
            null_h[index].append(value)

    direction = definition['direction']
    n_o, n_d = len(O), len(D)
    windows = []
    for index, w in enumerate(definition['windows']):
        lo, hi = windows_ms[index]
        obs = observed_o[index]
        naive = _summary(null_o[index], obs, direction)
        naive_mean = sum(null_o[index]) / n
        naive.update({
            'observed_per_stimulus': _round(obs / len(S)), 'null_mean_per_stimulus': _round(naive_mean / len(S)),
            'difference_per_stimulus': _round((obs - naive_mean) / len(S)),
            'ratio_to_null_mean': _round(obs / naive_mean) if naive_mean else None,
            'stimuli_with_outcome': sum(1 for s in S if _count(O, s + lo, s + hi) > 0),
            'unique_outcomes_in_windows': _union_count(S, O, lo, hi),
        })
        normalized = None
        if n_d:
            excess = [n_d * vo - n_o * vd for vo, vd in zip(null_o[index], null_d[index])]
            obs_excess = n_d * obs - n_o * observed_d[index]
            normalized = _summary(excess, obs_excess, direction, scale=n_d)
            normalized.update({'statistic': 'O_w - (|O|/|D|)*D_w', 'outcome_share_of_documentation': _round(n_o / n_d),
                               'difference_per_stimulus': _round((obs_excess - sum(excess) / n) / n_d / len(S))})
        d_mean = sum(null_d[index]) / n
        windows.append({
            'label': w['label'], 'start_s': w['start_s'], 'end_s': w['end_s'], 'naive': naive,
            'normalized': normalized if normalized else {'status': 'unavailable', 'reason': 'no density events'},
            'density': {'observed': observed_d[index], 'null_mean': _round(d_mean),
                        'ratio': _round(observed_d[index] / d_mean) if d_mean else None},
        })
    hist_null = [sorted(col) for col in null_h]
    base.update(
        status='ok', windows=windows, null_model=null.info,
        lag_histogram={'edges_s': edges_s, 'observed': observed_hist,
                       'null_mean': [_round(sum(c) / n) for c in hist_null],
                       'null_q025': [_quantile(c, 25) for c in hist_null],
                       'null_q500': [_quantile(c, 500) for c in hist_null],
                       'null_q975': [_quantile(c, 975) for c in hist_null]},
        control_series=_control_series(all_times, S, O, D, t0, t1, params['control_period_s'] * 1000, limits.max_control_bins),
    )
    ctx['windows'] = [{'label': w['label'], 'naive': w['naive'],
                       'normalized': w['normalized'] if 'p_value' in w['normalized'] else None,
                       'density': w['density']} for w in windows]
    base['warnings'] = _warnings(ctx)
    return base, input_sha256, input_count


# ---------------------------------------------------------------- persistence

def effective_parameters(definition: dict, overrides: dict | None) -> tuple[dict, list]:
    overrides = dict(overrides or {})
    unknown = sorted(set(overrides) - set(OVERRIDABLE))
    if unknown:
        raise HypothesisError('only these parameters can be overridden per run (recorded as deviations): ' +
                              ', '.join(OVERRIDABLE) + f'; got {", ".join(unknown)}')
    registered = definition['analysis']
    params = _analysis({**registered, **overrides})
    deviations = [{'key': key, 'registered': registered[key], 'used': params[key]}
                  for key in OVERRIDABLE if params[key] != registered[key]]
    return params, deviations


def run_event_study(db: sqlite3.Connection, version_id: int, *, overrides: dict | None = None, actor=None,
                    limits: EventStudyLimits = EventStudyLimits()) -> dict:
    """Run on a REGISTERED version only and append the result; returns the stored run."""
    from .source import load_items
    db.row_factory = sqlite3.Row
    actor = clean_actor(actor)
    version = require_registered(db, version_id)
    definition = version['definition']
    params, deviations = effective_parameters(definition, overrides)
    loaded = load_items(db, assume_zone=params['assume_zone'], limit=limits.max_events)
    if loaded['truncated']:
        raise HypothesisError(f'more than max_events={limits.max_events} events; the analysis refuses partial input')
    fts, categories = {}, {}
    for name in ('stimulus', 'outcome', 'density', 'eligible'):
        selector = definition[name]
        if 'text_query' in selector:
            ids, truncated = fts_event_ids(db, selector['text_query'], max_ids=limits.max_fts_ids)
            if truncated:
                raise HypothesisError(f'{name}.text_query matches more than max_fts_ids={limits.max_fts_ids} events')
            fts[name] = ids
        if 'categories' in selector:
            categories[name] = category_event_ids(db, loaded['items'], selector['categories'])
    result, input_sha256, input_count = compute_event_study(definition, params, loaded, fts=fts, categories=categories,
                                                            limits=limits, deviations=deviations)
    result['hypothesis'] = {'hypothesis_id': version['hypothesis_id'], 'version_id': version['version_id'],
                            'version_number': version['version_number'], 'definition_sha256': version['definition_sha256'],
                            'registered_at': version['registered_at'], 'title': definition['title']}
    result_json = canonical_json(result).decode('utf-8')
    result_sha256 = sha256_canonical(result)
    db.execute('BEGIN IMMEDIATE')
    try:
        cur = db.execute('''INSERT INTO hypothesis_runs(hypothesis_version_id,kind,definition_sha256,parameters_json,seed,
            code_version,input_sha256,input_count,provider,provider_opt_in,result_json,result_sha256,created_at,actor)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (version_id, 'event_study', version['definition_sha256'], canonical_json(params).decode('utf-8'),
                          params['seed'], code_version(), input_sha256, input_count, 'none', 0, result_json, result_sha256,
                          utc_now(), actor))
        run_id = int(cur.lastrowid)
        db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES (?,?,?,?,?)",
                   (actor, 'event_study_run', 'hypothesis_run', run_id,
                    json.dumps({'version_id': version_id, 'result_sha256': result_sha256}, ensure_ascii=False)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_run(db, run_id)


def get_run(db: sqlite3.Connection, run_id: int) -> dict:
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM hypothesis_runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        raise HypothesisNotFound(f'run {run_id} does not exist')
    result = json.loads(row['result_json'])
    integrity = 'hash_matches' if sha256_canonical(result) == row['result_sha256'] else 'hash_mismatch'
    same = [r[0] for r in db.execute('SELECT id FROM hypothesis_runs WHERE result_sha256=? AND id<>? ORDER BY id',
                                     (row['result_sha256'], run_id))]
    return {'run_id': row['id'], 'kind': row['kind'], 'hypothesis_version_id': row['hypothesis_version_id'],
            'definition_sha256': row['definition_sha256'], 'parameters': json.loads(row['parameters_json']),
            'seed': row['seed'], 'code_version': row['code_version'], 'input_sha256': row['input_sha256'],
            'input_count': row['input_count'], 'provider': row['provider'], 'provider_opt_in': bool(row['provider_opt_in']),
            'result_sha256': row['result_sha256'], 'created_at': row['created_at'], 'actor': row['actor'],
            'integrity': integrity, 'identical_result_runs': same, 'result': result}


def list_runs(db: sqlite3.Connection, hypothesis_id: int, *, kind: str | None = None) -> list[dict]:
    db.row_factory = sqlite3.Row
    sql = '''SELECT r.id, r.kind, r.hypothesis_version_id, v.version_number, r.seed, r.created_at, r.result_sha256,
             r.input_sha256, r.code_version, r.provider, r.provider_opt_in, r.actor
             FROM hypothesis_runs r JOIN hypothesis_versions v ON v.id = r.hypothesis_version_id WHERE v.hypothesis_id=?'''
    args: list = [hypothesis_id]
    if kind:
        sql += ' AND r.kind=?'
        args.append(kind)
    return [dict(r) for r in db.execute(sql + ' ORDER BY r.id', args)]

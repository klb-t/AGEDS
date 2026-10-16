"""Event study: outcome events around stimulus events vs. a seeded permutation null.

Method (all integer arithmetic on UTC milliseconds; no parametric p-values):

* Stimulus, outcome, density and eligible sets are event filters from the
  registered definition, evaluated over ``analysis.source.load_items``. Events
  without a placeable time (unknown zone, no candidate) are excluded and counted.
* Window statistic: number of (stimulus, outcome) pairs with lag in the
  half-open window ``[start_s, end_s)``; lag = outcome time − stimulus time.
* Detection-bias control: documentation density ``D`` (default: every timed
  event that is neither a stimulus nor an outcome). The normalized statistic is
  the outcome share of documentation inside the windows,
  ``ΣO_w / Σ(O_w + D_w)`` (``ΣO_w / ΣD_w`` when ``D`` includes outcomes),
  compared as exact fractions; null draws with an empty denominator are
  undefined and reported, not counted.
* Null: the stimulus times are moved ``n`` times by the registered null model
  (circular shift within the observed span, permutation among eligible events,
  permutation within weekday×hour strata, or permutation of the full blocks of
  the span) using the versioned SplitMix64 generator seeded from the definition.
  Method ``ageds.event-study-method/v2`` (review 2 findings 1 and 12): a
  permuted stimulus takes the time of an eligible *event* and is treated as that
  event, exactly as observed stimuli are treated — outcome events are not
  eligible while self pairs are excluded, and a borrowed density event is not
  counted as its own documentation when stimuli are excluded from density; the
  last, partial block of a block permutation stays in place.
* Empirical p-value ``(k + 1) / (n + 1)``, where ``k`` counts null statistics at
  least as extreme as the observed one in the registered direction (two-sided:
  distance from the null mean; for the normalized share the mean is rounded to a
  double with ``math.fsum`` and the distances are compared exactly).
* Preregistration check: every run records fingerprints of its selected sets; a
  later run of the same version compares the events that already existed at the
  version's first such run and reports changed sets, new events and tag/label
  edits made after registration (review 2 finding 7).

Warnings (causation, detection bias, seasonality, clustering, dependence,
unknown-zone exclusions, time basis, preregistration deviations) are always
present in the result with their computed values; ``flagged`` marks the ones
the data trigger. The result is auxiliary material, never a causal finding.
"""
from __future__ import annotations

import json
import math
import sqlite3
from fractions import Fraction
from array import array
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .hypotheses import (HypothesisError, HypothesisNotFound, canonical_json, category_event_ids, clean_actor,
                         code_version, disclaimer, fts_event_ids, resolve_category_config, selector_matches,
                         sha256_canonical, utc_now)
from .preregistration import _analysis, require_registered

RESULT_SCHEMA = 'ageds.event-study-result/v2'
METHOD_ID = 'ageds.event-study-method/v2'
METHOD_HISTORY = {
    'ageds.event-study-method/v1': (
        'permutation and stratified nulls drew stimulus times from every eligible event, including outcome and density '
        'events, so null placements produced lag-0 pairs with the borrowed event that observed stimuli never have '
        '(p-values miscalibrated, e.g. decrease p=0.001 on independent data); block_permutation wrapped a full block '
        'onto the start of the span when the span was not a multiple of block_s; two-sided normalized tests summed '
        'exact fractions (cost not bounded by max_work); deviation check covered only parameter overrides'),
    'ageds.event-study-method/v2': (
        'eligible pool = eligible events minus outcome events while self pairs are excluded; a permuted stimulus that '
        'borrows a density event does not count that event (density_excludes_stimuli); full blocks are permuted and the '
        'partial last block stays in place; extremes compared exactly without accumulating fractions, display means by '
        'math.fsum; selected-set fingerprints and tag/label edits after registration are checked against the version\'s '
        'first run (review 2, AGEDS-TASK-20261016-R2)'),
}
RNG_ID = 'splitmix64.v1'
QUANTILE_RULE = 'nearest_rank_ceil'
OVERRIDABLE = ('permutations', 'seed', 'null_model', 'block_s', 'stratify_zone')
SEASONALITY_TV_THRESHOLD = 0.2
DEPENDENCE_CV_THRESHOLD = 1.5
WEAK_TIME_KINDS = ('file_mtime', 'export_time', 'observed', 'filename_derived', 'other')
PRECISION_WIDTH_S = {'year': 366 * 86400, 'month': 31 * 86400, 'day': 86400, 'hour': 3600, 'minute': 60, 'second': 1,
                     'millisecond': 0.001, 'microsecond': 0.000001}
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


def _mean(values) -> float:
    """Display mean (correctly rounded sum of the values as doubles); never accumulates exact fractions."""
    return math.fsum(float(v) for v in values) / len(values)


def _extreme_count(null_values, observed, direction: str) -> int:
    """Null values at least as extreme as ``observed``; every comparison is exact.

    Integer statistics use the exact mean (``|n·v − Σv| ≥ |n·obs − Σv|``). Fractions are
    compared one by one (cross-multiplication inside ``Fraction``), never summed: the
    two-sided centre is the null mean rounded to a double (``math.fsum``), an exact dyadic
    rational, so the cost stays linear (review 2 finding 9).
    """
    if direction == 'increase':
        return sum(1 for v in null_values if v >= observed)
    if direction == 'decrease':
        return sum(1 for v in null_values if v <= observed)
    if all(type(v) is int for v in null_values) and type(observed) is int:
        n, total = len(null_values), sum(null_values)
        distance = abs(n * observed - total)
        return sum(1 for v in null_values if abs(n * v - total) >= distance)
    center = Fraction(_mean(null_values))
    distance = abs(observed - center)
    return sum(1 for v in null_values if abs(v - center) >= distance)


def _summary(null_values, observed, direction: str, scale: int = 1) -> dict:
    """Null distribution summary over exact values (ints or Fractions); ``scale`` is display only."""
    ordered = sorted(null_values)
    n = len(ordered)
    k = _extreme_count(null_values, observed, direction)
    mean = _mean(ordered) / scale
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
    """Moves the stimulus times; ``draw`` returns ``(times, borrowed_density)``.

    ``eligible`` is a list of ``(time_ms, event_id)`` (one entry per eligible event, review
    2 finding 1); ``borrowed_density`` counts the placements whose borrowed event is in
    ``density_ids`` — each such placement would otherwise count that very event as
    documentation at lag 0, which an observed stimulus never does.
    """

    def __init__(self, name: str, stimuli: list[int], t0: int, t1: int, eligible: list[tuple[int, int]], block_ms: int,
                 zone: str, rng: SplitMix64, density_ids: frozenset = frozenset()):
        self.name, self.stimuli, self.t0, self.t1, self.rng = name, stimuli, t0, t1, rng
        self.span = t1 - t0
        self.density_ids = density_ids
        self.info: dict = {'name': name}
        if name == 'permutation':
            if len(eligible) < len(stimuli):
                raise HypothesisError(f'permutation null needs at least as many eligible events ({len(eligible)}) as '
                                      f'stimuli ({len(stimuli)})')
            self.eligible = eligible
            self.info.update({'eligible_events': len(eligible), 'eligible_times': len({t for t, _ in eligible})})
        elif name == 'stratified_weekday_hour':
            tz = ZoneInfo(zone)
            buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}
            for entry in eligible:
                buckets.setdefault(_weekday_hour(entry[0], tz), []).append(entry)
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
            # Only full blocks are permuted; the partial block at the end of the span (when the span is
            # not a multiple of block_s) stays in place with its stimuli (review 2 finding 12).
            self.block = block_ms
            self.blocks = self.span // block_ms
            if self.blocks < 2:
                raise HypothesisError('block_permutation needs at least two blocks inside the observed span (only full '
                                      'blocks are permuted); decrease block_s')
            self.full_end = t0 + self.blocks * block_ms
            self.info.update({'block_s': block_ms // 1000, 'blocks': self.blocks,
                              'partial_block_s': (t1 - self.full_end) / 1000,
                              'stimuli_kept_in_partial_block': sum(1 for s in stimuli if s >= self.full_end)})
        elif name != 'circular_shift':
            raise HypothesisError(f'unknown null model {name}')

    def _borrowed(self, entries) -> int:
        return sum(1 for _, eid in entries if eid in self.density_ids) if self.density_ids else 0

    def draw(self) -> tuple[list[int], int]:
        rng, t0, span = self.rng, self.t0, self.span
        if self.name == 'circular_shift':
            u = rng.below(span)
            return [t0 + (s - t0 + u) % span for s in self.stimuli], 0
        if self.name == 'permutation':
            chosen = [self.eligible[i] for i in rng.sample_indices(len(self.eligible), len(self.stimuli))]
            return [t for t, _ in chosen], self._borrowed(chosen)
        if self.name == 'stratified_weekday_hour':
            out, borrowed = list(self.fixed), 0
            for members, pool in self.plan:
                chosen = [pool[i] for i in rng.sample_indices(len(pool), len(members))]
                out.extend(t for t, _ in chosen)
                borrowed += self._borrowed(chosen)
            return out, borrowed
        order = rng.permutation(self.blocks)
        block, end = self.block, self.full_end
        return [t0 + order[(s - t0) // block] * block + (s - t0) % block if s < end else s for s in self.stimuli], 0


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
                           'unknown_zone_excluded', 'time_uncertainty', 'time_basis', 'preregistration_deviation')


def uncertainty_s(item: dict) -> float | None:
    """Width of the item's time uncertainty: the timeline interval if present, else its precision."""
    lo, hi = item.get('t_lo'), item.get('t_hi')
    if lo is not None and hi is not None:
        return max(0.0, hi - lo)
    return PRECISION_WIDTH_S.get((item.get('time_basis') or {}).get('precision'))


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
        _warning('time_uncertainty', bool(ctx['uncertainty']['imprecise'] or ctx['uncertainty']['unknown_width']), 'warning',
                 f'Niepewność czasu: {ctx["uncertainty"]["imprecise"]} zdarzeń bodźca/wyniku/dokumentacji ma przedział czasu '
                 f'szerszy niż {ctx["uncertainty"]["threshold_s"]} s (rozdzielczość opóźnień) — '
                 + ('wyłączone z analizy' if ctx['uncertainty']['policy'] == 'exclude' else 'zachowane mimo to')
                 + f'; o nieznanej szerokości: {ctx["uncertainty"]["unknown_width"]} (zachowane).',
                 f'Time uncertainty: {ctx["uncertainty"]["imprecise"]} stimulus/outcome/documentation events have a time '
                 f'interval wider than {ctx["uncertainty"]["threshold_s"]} s (lag resolution) — '
                 + ('excluded from the analysis' if ctx['uncertainty']['policy'] == 'exclude' else 'kept anyway')
                 + f'; unknown width: {ctx["uncertainty"]["unknown_width"]} (kept).',
                 ctx['uncertainty']),
        _warning('time_basis', ctx['weak_time_basis'] > 0, 'warning',
                 'Część czasów bodźców/wyników pochodzi ze słabych źródeł (np. czas modyfikacji pliku, eksportu).',
                 'Some stimulus/outcome times come from weak bases (e.g. file modification or export time).',
                 {'weak_kinds': list(WEAK_TIME_KINDS), 'weak_count': ctx['weak_time_basis'], 'by_kind': ctx['time_basis']}),
        _deviation_warning(ctx['deviations'], ctx['drift']),
    ]


def _deviation_warning(deviations: list, drift: dict) -> dict:
    """Parameter overrides, selection changes since the first run and edits after registration (review 2 #7)."""
    changed = [c['set'] for c in drift['changed_sets']]
    edits = drift['edits_after_registration']
    edited = edits.get('tag_edits', 0) + edits.get('category_corrections', 0)
    new = drift['new_events_since_baseline']
    pl, en = [], []
    if deviations:
        keys = ', '.join(d['key'] for d in deviations)
        pl.append(f'Parametry tego przebiegu różnią się od zarejestrowanych: {keys}.')
        en.append(f'This run used parameters different from the registered ones: {keys}.')
    elif changed or edited:
        pl.append('Przebieg użył zarejestrowanych parametrów, ale dane wejściowe filtrów się zmieniły.')
        en.append('The run used the registered parameters, but the inputs of the filters changed.')
    else:
        pl.append('Przebieg użył dokładnie zarejestrowanych parametrów.')
        en.append('The run used exactly the registered parameters.')
    if changed:
        pl.append('Zbiory zdarzeń wybrane przez zarejestrowane filtry zmieniły się od pierwszego przebiegu tej wersji '
                  f'(run {drift["baseline_run_id"]}) wśród zdarzeń, które już wtedy istniały: {", ".join(changed)} '
                  '(np. zmiana tagów, kategorii, czasu).')
        en.append('The event sets selected by the registered filters changed since the first run of this version '
                  f'(run {drift["baseline_run_id"]}) among events that already existed then: {", ".join(changed)} '
                  '(e.g. tag, category or time edits).')
    if edited:
        pl.append(f'Po rejestracji zmieniono tagi użyte w filtrach ({edits.get("tag_edits", 0)}) lub korekty kategorii '
                  f'({edits.get("category_corrections", 0)}).')
        en.append(f'After registration, tags used by the filters were edited ({edits.get("tag_edits", 0)}) or category '
                  f'corrections were added ({edits.get("category_corrections", 0)}).')
    if new:
        pl.append('Nowe zdarzenia od pierwszego przebiegu: ' + ', '.join(f'{k} {v}' for k, v in sorted(new.items())) + '.')
        en.append('New events since the first run: ' + ', '.join(f'{k} {v}' for k, v in sorted(new.items())) + '.')
    return _warning('preregistration_deviation', bool(deviations or changed or edited), 'high', ' '.join(pl),
                    ' '.join(en), {'deviations': deviations, **drift})


# ---------------------------------------------------------------- preregistration: selection drift

def selection_record(loaded: dict, sets: dict[str, list[dict]]) -> dict:
    """Fingerprints of the selected sets plus the largest event id this run could see."""
    seen = [i['event_id'] for key in ('items', 'untimed', 'hidden_excluded') for i in loaded.get(key) or ()]
    return {'max_event_id': max(seen, default=0),
            'sets': {name: {'count': len(items), 'event_ids_sha256': sha256_canonical(sorted(i['event_id'] for i in items))}
                     for name, items in sets.items()},
            '_ids': {name: sorted(i['event_id'] for i in items) for name, items in sets.items()}}


def selection_drift(selection: dict, baseline: dict | None, edits: dict | None) -> dict:
    """Compare with the baseline run among events that existed then (``id <= its max_event_id``).

    Identical inputs give an identical record (no run ids unless something changed), so a
    re-run on unchanged data keeps the same result hash.
    """
    ids = selection.pop('_ids')
    changed, new = [], {}
    if baseline is not None:
        limit = baseline['selection']['max_event_id']
        for name, before in sorted(baseline['selection']['sets'].items()):
            current = ids.get(name, [])
            existing = [eid for eid in current if eid <= limit]
            if sha256_canonical(existing) != before['event_ids_sha256']:
                changed.append({'set': name, 'baseline_count': before['count'], 'count_among_existing_events': len(existing)})
            if len(existing) != len(current):
                new[name] = len(current) - len(existing)
    record = {'changed_sets': changed, 'new_events_since_baseline': new,
              'edits_after_registration': edits or {'tag_edits': 0, 'category_corrections': 0}}
    if changed or new:
        record['baseline_run_id'] = baseline['run_id']
    return record


TAG_ACTIONS = ('tag_bulk_add', 'tag_bulk_remove', 'tag_rename', 'tag_merge')


def edits_after_registration(db: sqlite3.Connection, definition: dict, registered_at: str, event_ids: set[int]) -> dict:
    """Tag edits touching a tag named in the filters and category corrections of loaded events after registration.

    Audit timestamps have second resolution; an edit in the registration second counts.
    """
    names = {t for n in ('stimulus', 'outcome', 'density', 'eligible') for key in ('tags_any', 'tags_all')
             for t in definition[n].get(key, ())}
    tag_edits = 0
    if names:
        since = registered_at[:19].replace('T', ' ')
        for row in db.execute('SELECT action, details_json FROM audit_log WHERE action IN (?,?,?,?) AND ts >= ? ORDER BY id',
                              (*TAG_ACTIONS, since)):
            try:
                details = json.loads(row[1] or '{}')
            except ValueError:
                details = {}
            touched = {details.get('tag'), details.get('old_name'), details.get('new_name'),
                       (details.get('target') or {}).get('name')}
            touched |= {d.get('name') for d in details.get('sources_deleted') or () if isinstance(d, dict)}
            if row[0] in ('tag_bulk_add', 'tag_bulk_remove'):
                changed = ((details.get('targets') or {}).get('event') or {}).get('changed') or []
                if not changed:
                    continue
            if touched & names:
                tag_edits += 1
    corrections = 0
    if any('categories' in definition[n] for n in ('stimulus', 'outcome', 'density', 'eligible')):
        rows = db.execute('SELECT event_id FROM analysis_label_annotations WHERE substr(created_at, 1, 23) >= ?',
                          (registered_at[:23],))
        corrections = sum(1 for r in rows if r[0] in event_ids)
    return {'tag_edits': tag_edits, 'category_corrections': corrections}


def baseline_run(db: sqlite3.Connection, version_id: int) -> dict | None:
    """The version's first event-study run that recorded selection fingerprints (method v2 and later)."""
    row = db.execute("""SELECT id, json_extract(result_json, '$.selection') FROM hypothesis_runs
                        WHERE hypothesis_version_id=? AND kind='event_study'
                        AND json_extract(result_json, '$.selection') IS NOT NULL ORDER BY id LIMIT 1""",
                     (version_id,)).fetchone()
    return None if row is None else {'run_id': int(row[0]), 'selection': json.loads(row[1])}


# ---------------------------------------------------------------- core computation

def compute_event_study(definition: dict, params: dict, loaded: dict, *, fts: dict[str, set],
                        categories: dict[str, set], limits: EventStudyLimits = EventStudyLimits(),
                        deviations: list | None = None, category_info: dict | None = None,
                        baseline: dict | None = None, edits: dict | None = None) -> tuple[dict, str, int]:
    """Pure computation; returns ``(result_without_run_fields, input_sha256, input_count)``.

    ``baseline`` is ``{'run_id', 'selection'}`` of the version's first run that recorded
    selection fingerprints (None for the first run); ``edits`` counts tag/label edits made
    after registration (``edits_after_registration``). Both feed the preregistration check.
    """
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
    threshold = params['max_time_uncertainty_s'] if params['max_time_uncertainty_s'] is not None else definition['lags']['bin_s']
    widths = {i['event_id']: uncertainty_s(i) for i in inside}
    imprecise_ids = {eid for eid, w in widths.items() if w is not None and w > threshold}
    unknown_ids = {eid for eid, w in widths.items() if w is None}
    selected = {name: select(name) for name in ('stimulus', 'outcome', 'density', 'eligible')}
    involved = {i['event_id'] for name in ('stimulus', 'outcome', 'density') for i in selected[name]}
    uncertainty = {
        'policy': params['imprecise_times'], 'threshold_s': threshold,
        'imprecise': len(imprecise_ids & involved), 'unknown_width': len(unknown_ids & involved),
        'by_set': {name: {'selected': len(items_), 'imprecise': sum(1 for i in items_ if i['event_id'] in imprecise_ids),
                          'unknown_width': sum(1 for i in items_ if i['event_id'] in unknown_ids)}
                   for name, items_ in selected.items()},
        'event_ids_listed': sorted(imprecise_ids & involved)[:limits.max_listed_ids],
    }
    if params['imprecise_times'] == 'exclude':
        selected = {name: [i for i in items_ if i['event_id'] not in imprecise_ids] for name, items_ in selected.items()}
    stim_items = selected['stimulus']
    stim_ids = {i['event_id'] for i in stim_items}
    out_items = selected['outcome']
    self_pairs = sorted(i['event_id'] for i in out_items if i['event_id'] in stim_ids)
    if params['exclude_self_pairs']:
        out_items = [i for i in out_items if i['event_id'] not in stim_ids]
    out_ids = {i['event_id'] for i in out_items}
    dens_items = [i for i in selected['density']
                  if not (params['density_excludes_stimuli'] and i['event_id'] in stim_ids)
                  and not (params['density_excludes_outcomes'] and i['event_id'] in out_ids)]
    # Review 2 finding 1: a permuted stimulus borrows an eligible event's time and stands for that
    # event. Outcome events are not eligible while self pairs are excluded (an observed stimulus is
    # never its own outcome); a borrowed density event is discounted per draw (see _NullModel).
    pool_items = [i for i in selected['eligible'] if not (params['exclude_self_pairs'] and i['event_id'] in out_ids)]
    eligible = sorted((i['t_ms'], i['event_id']) for i in pool_items)
    excluded_outcomes = len(selected['eligible']) - len(pool_items)
    S = sorted(i['t_ms'] for i in stim_items)
    O = sorted(i['t_ms'] for i in out_items)
    D = sorted(i['t_ms'] for i in dens_items)
    all_times = sorted(i['t_ms'] for i in inside)

    input_record = {
        'rule': loaded['rule'], 'assume_zone': loaded.get('assume_zone'), 'span_ms': [t0, t1],
        'stimulus': sorted([i['event_id'], i['t_ms']] for i in stim_items),
        'outcome': sorted([i['event_id'], i['t_ms']] for i in out_items),
        'density': sorted([i['event_id'], i['t_ms']] for i in dens_items),
        'eligible_events': [[eid, t] for t, eid in eligible], 'eligible_excluded_outcomes': excluded_outcomes,
        'self_pairs_excluded': self_pairs if params['exclude_self_pairs'] else [],
        'untimed': sorted([u['event_id'], u['reason']] for u in untimed), 'outside_span': outside,
        'imprecise': sorted(imprecise_ids), 'uncertainty_policy': [params['imprecise_times'], threshold],
        'source_rule': loaded.get('rule'), 'source_rule_sha256': loaded.get('rule_sha256', 'unknown'),
        'categories': category_info,
    }
    input_sha256 = sha256_canonical(input_record)
    input_count = len(stim_items) + len(out_items) + len(dens_items)
    selection = selection_record(loaded, {'stimulus': stim_items, 'outcome': out_items, 'density': dens_items,
                                          'eligible': pool_items})
    drift = selection_drift(selection, baseline, edits)

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
        'schema': RESULT_SCHEMA, 'method': {'id': METHOD_ID, 'history': METHOD_HISTORY},
        'disclaimer': disclaimer(), 'rng': RNG_ID, 'seed': params['seed'], 'selection': selection,
        'parameters': params, 'deviations_from_registration': deviations,
        'definition': {'windows': definition['windows'], 'lags': lags, 'direction': definition['direction'],
                       'stimulus': definition['stimulus'], 'outcome': definition['outcome'],
                       'density': definition['density'], 'eligible': definition['eligible']},
        'input': {
            'input_sha256': input_sha256, 'rule': loaded['rule'], 'assume_zone': loaded.get('assume_zone'),
            'timed_events': len(items), 'outside_span': outside,
            'span': {'start_utc': _utc_text(t0), 'end_utc': _utc_text(t1)} if t1 > t0 else None,
            'stimulus_count': len(S), 'outcome_count': len(O), 'density_count': len(D),
            'eligible_events': len(eligible), 'eligible_times': len({t for t, _ in eligible}),
            'eligible_excluded_outcomes': excluded_outcomes,
            'self_pairs': len(self_pairs), 'self_pairs_excluded': params['exclude_self_pairs'],
            'untimed_excluded': {'count': len(untimed), 'by_reason': dict(sorted(by_reason.items())),
                                 'event_ids': sorted(u['event_id'] for u in untimed)[:limits.max_listed_ids],
                                 'listed_limit': limits.max_listed_ids},
            'time_basis': dict(sorted(time_basis.items())),
            'time_uncertainty': uncertainty,
            'source': {'rule': loaded.get('rule'), 'rule_sha256': loaded.get('rule_sha256', 'unknown'),
                       'params': loaded.get('params'), 'seam': 'analysis.source.load_items'},
            'categories': category_info,
        },
        'seasonality': seasonality, 'clustering': clustering,
        'dependence': {'outcome_gap_cv': _round(_cv(O)), 'stimulus_gap_cv': _round(_cv(S))},
        'limits': limits.as_dict(), 'quantile_rule': QUANTILE_RULE,
    }
    ctx = {'flag_threshold': params['flag_threshold'], 'density_ratio_warning': params['density_ratio_warning'],
           'seasonality': seasonality, 'clustering': clustering, 'null_model': params['null_model'],
           'untimed': base['input']['untimed_excluded'], 'assume_zone': loaded.get('assume_zone'),
           'outcome_cv': _cv(O), 'stimulus_cv': _cv(S), 'density_count': len(D), 'deviations': deviations,
           'drift': drift,
           'weak_time_basis': sum(v for k, v in time_basis.items() if k in WEAK_TIME_KINDS), 'time_basis': time_basis,
           'uncertainty': uncertainty}

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
    # The null loop plus the summaries: sorting and comparing n values per window, also for the
    # normalized share (exact fractions, compared one by one; review 2 finding 9).
    work = n * len(S) * (4 * len(windows_ms) + len(edges_ms)) + 2 * n * len(windows_ms) * (2 + n.bit_length())
    if work > limits.max_work:
        raise HypothesisError(f'event study work estimate {work} exceeds max_work {limits.max_work}; '
                              'reduce permutations or stimuli, or raise the limit explicitly')
    cells = n * (2 * len(windows_ms) + nb)
    if cells > limits.max_null_cells:
        raise HypothesisError(f'null distribution needs {cells} cells, above max_null_cells {limits.max_null_cells}')

    if params['null_model'] in ('permutation', 'stratified_weekday_hour') and all(eid in stim_ids for _, eid in eligible):
        raise HypothesisError(
            f'{params["null_model"]} null: every eligible event is a stimulus (outcome events are not eligible while '
            'exclude_self_pairs is on), so every permutation would reproduce the observed data; use circular_shift or '
            'block_permutation, or let other documented events be eligible')
    rng = SplitMix64(params['seed'])
    density_ids = frozenset(i['event_id'] for i in dens_items) if params['density_excludes_stimuli'] else frozenset()
    null = _NullModel(params['null_model'], S, t0, t1, eligible, params['block_s'] * 1000, params['stratify_zone'], rng,
                      density_ids=density_ids)
    null.info.update({'eligible_selected': len(selected['eligible']), 'eligible_excluded_outcomes': excluded_outcomes,
                      'borrowed_density_events_discounted': bool(density_ids) and params['null_model'] in
                      ('permutation', 'stratified_weekday_hour')})
    lag0 = [lo <= 0 < hi for lo, hi in windows_ms]
    observed_o = _window_counts(S, O, windows_ms)
    observed_d = _window_counts(S, D, windows_ms)
    observed_hist = _histogram(S, O, edges_ms)
    null_o = [array('q') for _ in windows_ms]
    null_d = [array('q') for _ in windows_ms]
    null_h = [array('q') for _ in range(nb)]
    for _ in range(n):
        shifted, borrowed = null.draw()
        for index, value in enumerate(_window_counts(shifted, O, windows_ms)):
            null_o[index].append(value)
        for index, value in enumerate(_window_counts(shifted, D, windows_ms)):
            null_d[index].append(value - borrowed if lag0[index] else value)
        for index, value in enumerate(_histogram(shifted, O, edges_ms)):
            null_h[index].append(value)

    direction = definition['direction']
    n_o, n_d = len(O), len(D)
    windows = []
    for index, w in enumerate(definition['windows']):
        lo, hi = windows_ms[index]
        obs = observed_o[index]
        naive = _summary(null_o[index], obs, direction)
        naive_mean = _mean(null_o[index])
        naive.update({
            'observed_per_stimulus': _round(obs / len(S)), 'null_mean_per_stimulus': _round(naive_mean / len(S)),
            'difference_per_stimulus': _round((obs - naive_mean) / len(S)),
            'ratio_to_null_mean': _round(obs / naive_mean) if naive_mean else None,
            'stimuli_with_outcome': sum(1 for s in S if _count(O, s + lo, s + hi) > 0),
            'unique_outcomes_in_windows': _union_count(S, O, lo, hi),
        })
        normalized = None
        if n_d:
            plus_outcomes = params['density_excludes_outcomes']

            def share(o, d):
                total = o + d if plus_outcomes else d
                return Fraction(o, total) if total > 0 else None
            observed_share = share(obs, observed_d[index])
            null_shares = [v for v in (share(o, d) for o, d in zip(null_o[index], null_d[index])) if v is not None]
            if observed_share is not None and null_shares:
                normalized = _summary(null_shares, observed_share, direction)
                null_share_mean = _mean(null_shares)
                normalized.update({
                    'statistic': 'sum(O_w) / sum(O_w + D_w)' if plus_outcomes else 'sum(O_w) / sum(D_w)',
                    'n_undefined': n - len(null_shares),
                    'global_share': _round(Fraction(n_o, n_o + n_d) if plus_outcomes else Fraction(n_o, n_d)),
                    'share_difference': _round(float(observed_share) - null_share_mean),
                    'share_ratio': _round(float(observed_share) / null_share_mean) if null_share_mean else None,
                })
        d_mean = _mean(null_d[index])
        windows.append({
            'label': w['label'], 'start_s': w['start_s'], 'end_s': w['end_s'], 'naive': naive,
            'normalized': normalized if normalized else {'status': 'unavailable',
                                                         'reason': 'no density events or empty denominators'},
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


def load_inputs(db: sqlite3.Connection, definition: dict, params: dict, *, limits: EventStudyLimits = EventStudyLimits(),
                category_config=None) -> tuple[dict, dict, dict, dict | None]:
    """Items through the analysis seam plus FTS and category selections (shared with the dossier rules)."""
    import inspect
    from .source import load_items
    config = resolve_category_config(definition.get('category_config_sha256'), category_config) \
        if any('categories' in definition[n] for n in ('stimulus', 'outcome', 'density', 'eligible')) else None
    accepted = inspect.signature(load_items).parameters
    kwargs = {'assume_zone': params['assume_zone'], 'limit': limits.max_events}
    if 'assumption_rule' in accepted:
        kwargs['assumption_rule'] = params['assumption_rule']
    if config is not None and 'fields' in accepted:
        from . import categories as category_module
        kwargs['fields'] = category_module.needs_fields(config)
    loaded = load_items(db, **kwargs)
    if loaded['truncated']:
        raise HypothesisError(f'more than max_events={limits.max_events} events; the analysis refuses partial input')
    fts, categories, info = {}, {}, None
    for name in ('stimulus', 'outcome', 'density', 'eligible'):
        selector = definition[name]
        if 'text_query' in selector:
            ids, truncated = fts_event_ids(db, selector['text_query'], max_ids=limits.max_fts_ids)
            if truncated:
                raise HypothesisError(f'{name}.text_query matches more than max_fts_ids={limits.max_fts_ids} events')
            fts[name] = ids
        if 'categories' in selector:
            categories[name], details = category_event_ids(db, loaded['items'], selector['categories'], config)
            info = info or {'config': config.describe(), 'sets': {}}
            info['sets'][name] = details
    return loaded, fts, categories, info


def run_event_study(db: sqlite3.Connection, version_id: int, *, overrides: dict | None = None, actor=None,
                    limits: EventStudyLimits = EventStudyLimits(), category_config=None) -> dict:
    """Run on a REGISTERED version only and append the result; returns the stored run."""
    db.row_factory = sqlite3.Row
    actor = clean_actor(actor)
    version = require_registered(db, version_id)
    definition = version['definition']
    params, deviations = effective_parameters(definition, overrides)
    loaded, fts, categories, category_info = load_inputs(db, definition, params, limits=limits, category_config=category_config)
    loaded_ids = {i['event_id'] for i in loaded['items']}
    result, input_sha256, input_count = compute_event_study(
        definition, params, loaded, fts=fts, categories=categories, limits=limits, deviations=deviations,
        category_info=category_info, baseline=baseline_run(db, version_id),
        edits=edits_after_registration(db, definition, version['registered_at'], loaded_ids))
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
                    json.dumps({'version_id': version_id, 'result_sha256': result_sha256,
                                'projection': loaded.get('projection')}, ensure_ascii=False)))
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

"""Review 2 (AGEDS-TASK-20261016-R2): event study method v2.

#1 null placements never borrow an outcome event (and a borrowed density event is not its own
documentation) + an H0 calibration over many independent synthetic datasets for every null model;
#12 block permutation with a partial last block; #9 two-sided normalized test without accumulating
fractions; #7 preregistration check of selected sets and tag edits. Synthetic data only.
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timezone
from fractions import Fraction

import pytest

from server.app import db as dbmod
from server.app import evidence
from server.app.analysis import eventstudy, preregistration
from server.app.analysis.hypotheses import HypothesisError
from server.tests.test_review_fixes_support import env  # noqa: F401 (fixture)

T0 = 1_700_000_000


def _items(spec, seed: int, days: int = 60) -> list[dict]:
    rnd = random.Random(seed)
    items, eid = [], 0
    for kind, count in spec:
        for _ in range(count):
            eid += 1
            t = float(T0 + rnd.randrange(0, days * 86400))
            items.append({'event_id': eid, 'event_type': kind, 't_start': t, 't_lo': t, 't_hi': t,
                          'time_basis': {'kind': 'sent', 'precision': 'second'}, 'tags': []})
    return items


def _run(items, *, direction='increase', null_model='circular_shift', windows=None, permutations=99, analysis=None,
         outcome=None):
    definition = preregistration.normalize_definition({
        'title': 't', 'text': 't', 'stimulus': {'event_types': ['letter']},
        'outcome': outcome or {'event_types': ['health']},
        'windows': windows or [{'label': 'w', 'start_s': 0, 'end_s': 6 * 3600}], 'direction': direction,
        'analysis': {'permutations': permutations, 'seed': 3, 'null_model': null_model, **(analysis or {})}})
    params, _ = eventstudy.effective_parameters(definition, None)
    loaded = {'items': items, 'untimed': [], 'rule': 'synthetic', 'assume_zone': None}
    return eventstudy.compute_event_study(definition, params, loaded, fts={}, categories={})[0]


# --------------------------------------------------------------------- finding 1: H0 calibration

CALIBRATION_DATASETS = 40


@pytest.mark.parametrize('null_model', ['circular_shift', 'permutation', 'stratified_weekday_hour', 'block_permutation'])
def test_review2_1_null_models_are_calibrated_under_independence(null_model):
    """Stimuli, outcomes and other documented events drawn independently and uniformly: p-values must be
    roughly uniform. Pre-fix the permutation/stratified nulls gave decrease p ~ 0.01-0.25 and increase
    p ~ 0.8-1.0 on such data (placements on outcome times create lag-0 pairs the data cannot have)."""
    pvalues = {(d, s): [] for d in ('increase', 'decrease') for s in ('naive', 'normalized')}
    for seed in range(CALIBRATION_DATASETS):
        items = _items((('letter', 25), ('health', 150), ('note', 600)), seed)
        for direction in ('increase', 'decrease'):
            window = _run(items, direction=direction, null_model=null_model)['windows'][0]
            pvalues[(direction, 'naive')].append(window['naive']['p_value'])
            pvalues[(direction, 'normalized')].append(window['normalized']['p_value'])
    summary = {f'{d}/{s}': (sum(p <= 0.05 for p in v) / len(v), sum(v) / len(v)) for (d, s), v in pvalues.items()}
    for key, (rejected, mean) in summary.items():
        assert rejected <= 0.15, (key, summary)          # nominal 0.05; deterministic seeds, generous band
        assert 0.35 <= mean <= 0.65, (key, summary)      # neither anti-conservative nor stuck at p = 1


def test_review2_1_default_pool_excludes_outcome_events():
    items = _items((('letter', 50), ('health', 500), ('note', 300)), 1, days=365)
    result = _run(items, null_model='permutation', windows=[{'label': 'after 1h', 'start_s': 0, 'end_s': 3600}],
                  permutations=199)
    info, naive = result['null_model'], result['windows'][0]['naive']
    assert info['eligible_selected'] == 850 and info['eligible_excluded_outcomes'] == 500 and info['eligible_events'] == 350
    assert result['input']['eligible_excluded_outcomes'] == 500
    assert naive['null_mean'] < 10  # pre-fix ~48 (stimuli placed exactly on outcome times) vs observed ~3
    # v3 (2026-10-17, location area L1) only adds the passive-signal density default; v2 stays in the history.
    assert result['method']['id'] == eventstudy.METHOD_ID and result['schema'] == 'ageds.event-study-result/v2'
    assert 'ageds.event-study-method/v2' in result['method']['history']


def test_review2_1_pool_of_only_the_stimuli_is_refused_not_reported_as_p_1():
    items = _items((('letter', 50), ('health', 500)), 2, days=365)  # nothing else was documented
    with pytest.raises(HypothesisError, match='every eligible event is a stimulus'):
        _run(items, null_model='permutation')
    assert _run(items, null_model='circular_shift')['status'] == 'ok'


def test_review2_1_borrowed_density_event_is_not_its_own_documentation():
    # Density events 10 min apart from everything; a window of [0, 1 s) can only count a coincident event.
    items = [{'event_id': i + 1, 'event_type': 'letter' if i < 5 else 'note', 't_start': float(T0 + 600 * i),
              't_lo': float(T0 + 600 * i), 't_hi': float(T0 + 600 * i), 'time_basis': {'kind': 'sent', 'precision': 'second'},
              'tags': []} for i in range(60)]
    items.append({'event_id': 99, 'event_type': 'health', 't_start': float(T0 + 600 * 70), 't_lo': float(T0 + 600 * 70),
                  't_hi': float(T0 + 600 * 70), 'time_basis': {'kind': 'sent', 'precision': 'second'}, 'tags': []})
    for model in ('permutation', 'stratified_weekday_hour'):
        result = _run(items, null_model=model, windows=[{'label': 'lag0', 'start_s': 0, 'end_s': 1}])
        density = result['windows'][0]['density']
        assert density['observed'] == 0 and density['null_mean'] == 0, (model, density)  # pre-fix null mean ~ 1 per stimulus
        assert result['null_model']['borrowed_density_events_discounted'] is True


# --------------------------------------------------------------------- finding 12

def test_review2_12_block_permutation_keeps_blocks_whole_with_a_partial_last_block():
    block = 7 * 86400 * 1000
    full = 5
    t0, t1 = 0, full * block + block // 2  # span is not a multiple of block_s
    offset = block - 1000  # late in every block: pre-fix a block mapped onto the partial slot wrapped to the start
    stimuli = [t0 + k * block + offset for k in range(full)] + [t0 + full * block + 5]
    null = eventstudy._NullModel('block_permutation', stimuli, t0, t1, [], block, 'UTC', eventstudy.SplitMix64(7))
    assert null.info['blocks'] == full and null.info['stimuli_kept_in_partial_block'] == 1
    for _ in range(50):
        times, borrowed = null.draw()
        assert borrowed == 0
        assert sorted((t - t0) // block for t in times[:full]) == list(range(full))  # a permutation of the full blocks
        assert all((t - t0) % block == offset for t in times[:full])                 # positions inside blocks kept
        assert times[full] == stimuli[full]                                          # the partial block stays


def test_review2_12_block_permutation_needs_two_full_blocks():
    block = 7 * 86400 * 1000
    with pytest.raises(HypothesisError, match='at least two blocks'):
        eventstudy._NullModel('block_permutation', [1], 0, block + block // 2, [], block, 'UTC', eventstudy.SplitMix64(1))


# --------------------------------------------------------------------- finding 9

def _shares(n: int, seed: int = 1) -> list[Fraction]:
    r = random.Random(seed)
    out = []
    for _ in range(n):
        d = r.randrange(200_000, 400_000)
        o = r.randrange(0, d // 10)
        out.append(Fraction(o, o + d))
    return out


def test_review2_9_two_sided_normalized_summary_is_fast():
    values = _shares(8000)
    started = time.monotonic()
    summary = eventstudy._summary(values, values[0], 'two_sided')
    assert time.monotonic() - started < 2.0  # pre-fix ~6 s here, hours at 100k permutations x 20 windows
    assert summary['n_permutations'] == 8000 and 1 <= summary['k_extreme'] <= 8000


@pytest.mark.parametrize('seed', range(30))
def test_review2_9_two_sided_extremes_match_the_exact_mean_criterion(seed):
    values = _shares(random.Random(seed).randint(5, 60), seed)
    observed = values[random.Random(seed + 1).randrange(len(values))]
    exact_mean = sum(values) / len(values)
    rounded = Fraction(math.fsum(float(v) for v in values) / len(values))
    distance = abs(observed - exact_mean)
    if any(abs(abs(v - exact_mean) - distance) <= 2 * abs(rounded - exact_mean) for v in values if v != observed):
        pytest.skip('a value lies within the rounding of the mean from the boundary')
    expected = sum(1 for v in values if abs(v - exact_mean) >= distance)
    assert eventstudy._extreme_count(values, observed, 'two_sided') == expected
    assert eventstudy._extreme_count(list(range(10)), 2, 'two_sided') == 6  # integers keep the exact criterion


def test_review2_9_work_estimate_includes_the_summaries():
    items = _items((('letter', 2), ('health', 20), ('note', 20)), 3)
    n, loop = 9999, 9999 * 2 * (4 * 1 + 25)  # permutations x stimuli x (4 x windows + histogram edges)
    limits = eventstudy.EventStudyLimits(max_work=loop + 1)  # the null loop alone fits; pre-fix this ran
    with pytest.raises(HypothesisError, match='work estimate'):
        eventstudy.compute_event_study(*_prepared(items, n), fts={}, categories={}, limits=limits)


def _prepared(items, permutations):
    definition = preregistration.normalize_definition({
        'title': 't', 'text': 't', 'stimulus': {'event_types': ['letter']}, 'outcome': {'event_types': ['health']},
        'windows': [{'label': 'w', 'start_s': 0, 'end_s': 3600}], 'direction': 'two_sided',
        'analysis': {'permutations': permutations}})
    params, _ = eventstudy.effective_parameters(definition, None)
    return definition, params, {'items': items, 'untimed': [], 'rule': 'synthetic', 'assume_zone': None}


# --------------------------------------------------------------------- finding 7

def _letters_and_notes(seed: int = 5) -> tuple[list[int], list[int], dict[int, float]]:
    rnd = random.Random(seed)
    source = evidence.ensure_source('synthetic', 'prereg')
    letters, notes, at = [], [], {}
    for i in range(160):
        t = T0 + rnd.randrange(0, 90 * 86400)
        kind = 'letter' if i < 20 else 'note'
        stamp = datetime.fromtimestamp(t, timezone.utc).isoformat()
        eid = evidence.add_event(source_id=source, artifact_id=None, event_type=kind, ts_start=stamp, body=f'{kind} {i}',
                                 external_id=f'p{i}')
        (letters if kind == 'letter' else notes).append(eid)
        at[eid] = t
    return letters, notes, at


def _register(definition_extra=None) -> int:
    with dbmod.session() as db:
        registered = preregistration.register_hypothesis(db, {
            'title': 'Notes tagged health follow letters', 'text': 'synthetic', 'stimulus': {'event_types': ['letter']},
            'outcome': {'tags_any': ['zdrowie']}, 'windows': [{'label': 'po', 'start_s': 0, 'end_s': 2 * 86400}],
            'analysis': {'permutations': 99, 'seed': 1}, **(definition_extra or {})})
    return registered['version_id']


def _event_study(version_id: int) -> dict:
    with dbmod.session() as db:
        return eventstudy.run_event_study(db, version_id, actor='test')


def _deviation(run: dict) -> dict:
    return next(w for w in run['result']['warnings'] if w['id'] == 'preregistration_deviation')


def test_review2_7_post_hoc_retagging_is_a_flagged_deviation(env):  # noqa: F811
    from server.app.workbench import tags
    letters, notes, at = _letters_and_notes()
    tags.apply_tag('zdrowie', [f'event:{n}' for n in notes[::7]])  # tagged before registration
    time.sleep(1.1)  # audit timestamps have second resolution
    version = _register()
    first, again = _event_study(version), _event_study(version)
    assert _deviation(first)['flagged'] is False
    assert again['result_sha256'] == first['result_sha256'] and again['identical_result_runs'] == [first['run_id']]
    # After seeing the result: tag the notes that follow letters, run the same registered version again.
    following = [n for n in notes if any(0 <= at[n] - at[s] < 2 * 86400 for s in letters)]
    tags.apply_tag('zdrowie', [f'event:{n}' for n in following])
    after = _event_study(version)
    warning = _deviation(after)
    assert warning['flagged'] is True and warning['severity'] == 'high'  # pre-fix: False, "exactly the registered parameters"
    assert 'outcome' in [c['set'] for c in warning['computed']['changed_sets']]
    assert warning['computed']['baseline_run_id'] == first['run_id']
    assert warning['computed']['edits_after_registration']['tag_edits'] == 1
    assert 'outcome' in warning['text_en'] and 'After registration' in warning['text_en']
    assert 'exactly the registered parameters' not in warning['text_en']
    assert after['result']['windows'][0]['naive']['p_value'] < first['result']['windows'][0]['naive']['p_value']


def test_review2_7_new_events_alone_are_reported_but_not_flagged(env):  # noqa: F811
    letters, notes, at = _letters_and_notes(6)
    version = _register({'outcome': {'event_types': ['note']}})
    first = _event_study(version)
    source = evidence.ensure_source('synthetic', 'prereg')
    evidence.add_event(source_id=source, artifact_id=None, event_type='note', body='late import', external_id='late',
                       ts_start=datetime.fromtimestamp(T0 + 3600, timezone.utc).isoformat())
    second = _event_study(version)
    warning = _deviation(second)
    assert warning['flagged'] is False and warning['computed']['changed_sets'] == []
    assert warning['computed']['new_events_since_baseline'] == {'outcome': 1}
    assert warning['computed']['baseline_run_id'] == first['run_id'] and 'New events' in warning['text_en']


def test_review2_7_tag_edits_between_registration_and_the_first_run_are_flagged(env):  # noqa: F811
    from server.app.workbench import tags
    letters, notes, at = _letters_and_notes(7)
    version = _register()
    tags.apply_tag('zdrowie', [f'event:{n}' for n in notes[:30]])
    tags.apply_tag('unrelated', [f'event:{n}' for n in notes[:30]])
    warning = _deviation(_event_study(version))
    assert warning['flagged'] is True and warning['computed']['changed_sets'] == []
    assert warning['computed']['edits_after_registration'] == {'tag_edits': 1, 'category_corrections': 0}

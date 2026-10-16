"""Review 2 (AGEDS-TASK-20261016-R2): time budget, feature series and model-run fixes.

Findings covered here: #2 (no write lock during the computation), #3 (lazy, deduplicated
sensitivity grid), #8 (count_both bounds ordering) and #13 (the synthetic ``fake`` model
provider is a test double behind ``EW_ANALYSIS_TEST_PROVIDERS``). Everything runs on
temporary synthetic databases; nothing depends on the reviewer's scratch reproducers.
"""
from __future__ import annotations

import itertools
import json
import random
import sqlite3
import threading

import pytest

from server.app.analysis import budget, calendar as cal, categories as cat, features, model_runs
from server.app.analysis.provenance import canonical_json
from server.tests.corpus import activity
from server.tests.test_analysis_budget import corpus  # noqa: F401 (fixture)
from server.tests.test_analysis_budget_api import _body, client  # noqa: F401 (fixture)


# --------------------------------------------------------------------- finding 2

@pytest.mark.parametrize('kind', ['time_budget', 'feature_series'])
def test_review2_2_no_write_lock_is_held_while_the_grid_is_computed(corpus, monkeypatch, kind):  # noqa: F811
    dbmod, manifest, ids, config = corpus
    entered, release = threading.Event(), threading.Event()
    real = budget.compute_budget

    def slow_compute(*args, **kwargs):  # stands in for a long sensitivity grid
        entered.set()
        assert release.wait(30)
        return real(*args, **kwargs)
    monkeypatch.setattr(budget, 'compute_budget', slow_compute)
    out = {}

    def run():
        fn = budget.run_budget if kind == 'time_budget' else features.run_features
        try:
            with dbmod.session() as db:
                out['run'] = fn(db, config, window_from=manifest['window']['from'], window_to=manifest['window']['to'],
                                zone=activity.ZONE)['run']
        except Exception as exc:  # reported below
            out['error'] = exc
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(60)
        other = sqlite3.connect(str(dbmod.settings.db_path), timeout=0.2)
        try:  # pre-fix: the label run INSERT held the write lock -> "database is locked"
            other.execute('BEGIN IMMEDIATE')
            other.execute("INSERT INTO audit_log(action) VALUES ('concurrent_writer_during_budget')")
            other.commit()
        finally:
            other.close()
    finally:
        release.set()
        worker.join(60)
    assert 'error' not in out, out.get('error')
    with dbmod.session() as db:
        row = db.execute('SELECT kind, label_run_id, result_json FROM analysis_runs WHERE id=?',
                         (out['run']['run_id'],)).fetchone()
        assert row['kind'] == kind and row['label_run_id'] is not None
        assert json.loads(row['result_json'])['labels']['label_run_id'] == row['label_run_id']
        assert db.execute('SELECT COUNT(*) FROM analysis_label_runs').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action='concurrent_writer_during_budget'").fetchone()[0] == 1


# --------------------------------------------------------------------- finding 3

def _reference_grid(params: dict) -> tuple[list[dict], int, bool]:
    """The pre-fix semantics (materialized product, whole-point dedup), as an independent oracle."""
    base = {k: params[k] for k in budget.GRID_KEYS}
    grid = params['grid']
    if params['grid_mode'] == 'one_at_a_time':
        candidates = [base] + [{**base, k: v} for k in budget.GRID_KEYS for v in grid.get(k, []) if v != base[k]]
    else:
        axes = [grid.get(k) or [base[k]] for k in budget.GRID_KEYS]
        candidates = [base] + [dict(zip(budget.GRID_KEYS, combo)) for combo in itertools.product(*axes)]
    points, seen = [], set()
    for p in candidates:
        key = canonical_json(p)
        if key not in seen:
            seen.add(key)
            points.append(p)
    return points[:params['max_grid_points']], len(points), len(points) > params['max_grid_points']


@pytest.mark.parametrize('mode', ['one_at_a_time', 'full'])
@pytest.mark.parametrize('seed', range(12))
def test_review2_3_lazy_grid_matches_the_materialized_grid(mode, seed):
    rnd = random.Random(seed)
    defaults = budget.effective_params({})
    pools = {'session_gap_s': [0, 60, 300, defaults['session_gap_s'], 900, 1800, 300],
             'typing_chars_per_min': [60, 120, defaults['typing_chars_per_min'], 300, 120],
             'reading_chars_per_min': [600, 900, defaults['reading_chars_per_min'], 1500],
             'max_attribution_s': [0, 120, defaults['max_attribution_s'], 3600, 0],
             'overlap_rule': ['split_equal', 'priority', 'count_both', 'priority']}
    grid = {k: [rnd.choice(v) for _ in range(rnd.randint(0, 6))] for k, v in pools.items() if rnd.random() < 0.8}
    params = budget.effective_params({'grid_mode': mode, 'grid': grid, 'max_grid_points': rnd.randint(1, 40)})
    assert budget.grid_points(params) == _reference_grid(params)


def test_review2_3_full_grid_never_materializes_the_product(monkeypatch):
    calls = [0]
    real = budget.canonical_json

    def counting(value):
        calls[0] += 1
        return real(value)
    monkeypatch.setattr(budget, 'canonical_json', counting)
    n = 16  # pre-fix: 16**5 = 1,048,576 point dicts built and hashed before the cap applied
    grid = {'session_gap_s': list(range(n)), 'typing_chars_per_min': list(range(1, n + 1)),
            'reading_chars_per_min': list(range(1, n + 1)), 'max_attribution_s': list(range(n)),
            'overlap_rule': ['split_equal'] * n}
    params = budget.effective_params({'grid_mode': 'full', 'grid': grid, 'max_grid_points': 3})
    points, total, truncated = budget.grid_points(params)
    assert len(points) == 3 and truncated and points[0] == {k: params[k] for k in budget.GRID_KEYS}
    assert total == n ** 4 + 1  # duplicate axis values count once; the run's own point is not in the product
    assert calls[0] < 200, calls[0]


def test_review2_3_http_full_grid_of_64_values_per_axis_is_bounded(client):  # noqa: F811
    grid = {'session_gap_s': list(range(1000, 1064)), 'typing_chars_per_min': list(range(1, 65)),
            'reading_chars_per_min': list(range(1, 65)), 'max_attribution_s': list(range(1000, 1064)),
            'overlap_rule': ['count_both', 'priority'] * 32}
    response = client.post('/api/analysis/budget/runs', json=_body(client, params={'grid_mode': 'full', 'grid': grid,
                                                                                  'max_grid_points': 2}))
    assert response.status_code == 200, response.text
    stored = client.get(f"/api/analysis/budget/runs/{response.json()['run']['run_id']}").json()['result']['grid']
    assert stored['points_evaluated'] == 2 and stored['truncated'] is True
    assert stored['points_total'] == 64 ** 4 * 2 + 1
    too_long = dict(grid, session_gap_s=list(range(65)))
    refused = client.post('/api/analysis/budget/runs', json=_body(client, params={'grid_mode': 'full', 'grid': too_long}))
    assert refused.status_code == 422 and 'at most 64 values' in refused.json()['detail']


# --------------------------------------------------------------------- finding 8

CONFIG = cat.parse_config({'schema': cat.CONFIG_SCHEMA, 'version': 't', 'categories': [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]})
ZONE = 'Europe/Warsaw'
EPS = 1e-2


def _random_budget(seed: int, rule: str):
    r = random.Random(seed)
    lo = cal.parse_bound('2024-03-29', cal.zone_info(ZONE))  # spans the spring DST change
    hi = cal.parse_bound('2024-04-03', cal.zone_info(ZONE))
    items, labels = [], {}
    for eid in range(1, r.randint(2, 60)):
        t = lo - 7200 + r.random() * (hi - lo + 14400)
        kind = r.random()
        end = t + r.random() * 7200 if kind < 0.3 else None
        declared = r.random() * 3000 if 0.3 <= kind < 0.45 else None
        width = r.choice([0, 0, 0, r.random() * 3600])
        items.append({'event_id': eid, 'event_type': r.choice(['sms', 'chat_message', 'call']), 't_start': t,
                      't_end': end, 't_lo': t - width / 2, 't_hi': t + width / 2,
                      'direction': r.choice(['outgoing', 'incoming', None]), 'text_chars': r.randint(0, 3000),
                      'chat': None, 'declared_duration_s': declared,
                      'duration_basis': 'source_declared' if declared else None, 'time_basis': {}, 'hidden': False})
        chosen = r.sample(['a', 'b', 'c'], r.randint(1, 3))
        labels[eid] = {'categories': {c: r.choice([0.3, 0.5, 1.0]) for c in chosen}, 'path': 'rule', 'issues': []}
    params = budget.effective_params({'overlap_rule': rule, 'max_grid_points': 40})
    return budget.compute_budget(items, labels, CONFIG, lo=lo, hi=hi, zone_name=ZONE, params=params), hi - lo


def _ordered(v: dict) -> bool:
    return v['lower'] <= v['probable'] + EPS and v['probable'] <= v['upper'] + EPS


@pytest.mark.parametrize('rule', budget.OVERLAP_RULES)
def test_review2_8_bounds_are_ordered_for_every_overlap_rule(rule):
    violations = []
    for seed in range(60):
        result, length = _random_budget(seed, rule)
        totals = result['totals']
        for c, v in totals['categories'].items():
            if not _ordered(v) or not (v['wick_low'] <= v['lower'] + EPS and v['upper'] <= v['wick_high'] + EPS):
                violations.append((seed, 'total', c, v))
        u = totals['unobserved']
        if not _ordered(u) or u['lower'] < -EPS or u['upper'] > length + EPS:
            violations.append((seed, 'unobserved', u))
        for g, rows in result['candles'].items():
            for row in rows:
                for c, v in row['categories'].items():
                    if not _ordered(v) or not (v['wick_low'] <= v['lower'] + EPS and v['upper'] <= v['wick_high'] + EPS):
                        violations.append((seed, g, row['bucket'], c, v['lower'], v['probable'], v['upper']))
                if rule != 'count_both':
                    shares = sum(v['probable'] for v in row['categories'].values())
                    if shares > row['observed']['probable'] + EPS:
                        violations.append((seed, g, row['bucket'], 'sum>observed', shares))
    assert violations == [], violations[:5]


def test_review2_8_count_both_unlabelled_remainder_follows_the_confirmed_layer():
    """A confirmed 0.3-labelled call overlapped by an estimated 1.0 session of another category."""
    lo = 1_700_000_000.0
    hi = lo + 86400
    items = [
        {'event_id': 1, 'event_type': 'call', 't_start': lo + 3600, 't_end': lo + 7200, 't_lo': lo + 3600,
         't_hi': lo + 3600, 'direction': None, 'text_chars': 0, 'chat': None, 'declared_duration_s': None,
         'duration_basis': None, 'time_basis': {}, 'hidden': False},
        {'event_id': 2, 'event_type': 'sms', 't_start': lo + 3000, 't_end': None, 't_lo': lo + 3000, 't_hi': lo + 3000,
         'direction': 'outgoing', 'text_chars': 10, 'chat': None, 'declared_duration_s': None, 'duration_basis': None,
         'time_basis': {}, 'hidden': False},
        {'event_id': 3, 'event_type': 'sms', 't_start': lo + 7000, 't_end': None, 't_lo': lo + 7000, 't_hi': lo + 7000,
         'direction': 'outgoing', 'text_chars': 10, 'chat': None, 'declared_duration_s': None, 'duration_basis': None,
         'time_basis': {}, 'hidden': False},
    ]
    labels = {1: {'categories': {'a': 0.3}, 'path': 'rule', 'issues': []},
              2: {'categories': {'b': 1.0}, 'path': 'rule', 'issues': []},
              3: {'categories': {'b': 1.0}, 'path': 'rule', 'issues': []}}
    params = budget.effective_params({'overlap_rule': 'count_both', 'session_gap_s': 7200, 'max_grid_points': 1})
    result = budget.compute_budget(items, labels, CONFIG, lo=lo, hi=hi, zone_name='UTC', params=params)
    rest = result['totals']['categories'][cat.UNLABELLED]
    assert rest['lower'] == pytest.approx(0.7 * 3600)  # pre-fix: lower 2520 s, probable/upper 0 s
    assert rest['lower'] <= rest['probable'] <= rest['upper']


# --------------------------------------------------------------------- finding 13

def test_review2_13_fake_provider_needs_the_test_environment(client, monkeypatch):  # noqa: F811
    monkeypatch.delenv(model_runs.TEST_PROVIDER_ENV, raising=False)
    labelled = client.post('/api/analysis/model-label-runs', json=_body(client, provider='fake', opt_in=True))
    assert labelled.status_code == 422 and model_runs.TEST_PROVIDER_ENV in labelled.json()['detail']
    tone = {'model_features': {'tone': {'provider': 'fake', 'opt_in': True}}}
    featured = client.post('/api/analysis/features/runs', json=_body(client, params=tone))
    assert featured.status_code == 422 and model_runs.TEST_PROVIDER_ENV in featured.json()['detail']
    from server.app import db
    with db.session() as conn:
        assert conn.execute('SELECT COUNT(*) FROM analysis_model_runs').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM analysis_runs').fetchone()[0] == 0
    monkeypatch.setenv(model_runs.TEST_PROVIDER_ENV, '1')
    allowed = client.post('/api/analysis/model-label-runs', json=_body(client, provider='fake', opt_in=True))
    assert allowed.status_code == 200 and allowed.json()['provider'] == 'fake'

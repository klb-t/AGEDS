"""Event study on hard synthetic series: planted effect, density-burst null, weekday confound,
clustered stimuli, unknown zones and coarse times, determinism and the p-value formula."""
import json

import pytest

from server.app.analysis import eventstudy as es, preregistration as pr
from server.app.analysis.hypotheses import DISCLAIMER, HypothesisError, sha256_canonical, sha256_text
from server.tests.corpus import hypotheses as corpus

DAY = 86400


@pytest.fixture
def db(tmp_path, monkeypatch):
    from server.app import db as dbmod, evidence
    from server.app.config import Settings
    s = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', s)
    monkeypatch.setattr(evidence, 'settings', s)
    dbmod.init_db()
    connection = dbmod.connect()
    yield connection
    connection.close()


def run_scenario(db, name, *, analysis=None, overrides=None, **definition_changes):
    scenario = corpus.SCENARIOS[name]()
    ids = corpus.populate(scenario)
    definition = corpus.definition_for(scenario, **(analysis or {}))
    definition.update(definition_changes)
    version = pr.register_hypothesis(db, definition)
    run = es.run_event_study(db, version['version_id'], overrides=overrides)
    return scenario, ids, version, run


def window(result, label):
    return next(w for w in result['windows'] if w['label'] == label)


def warning(result, wid):
    return next(w for w in result['warnings'] if w['id'] == wid)


def assert_common_contract(run):
    result = run['result']
    assert result['disclaimer'] == DISCLAIMER
    assert result['disclaimer']['sha256_pl'] == sha256_text(
        'Wynik analizatora jest materiałem pomocniczym dla człowieka/biegłego, nie wnioskiem przyczynowym.')
    assert [w['id'] for w in result['warnings']] == list(es.ALWAYS_PRESENT_WARNINGS)
    assert warning(result, 'causation')['flagged']
    assert run['integrity'] == 'hash_matches' and run['result_sha256'] == sha256_canonical(result)
    for w in result['windows']:
        for part in (w['naive'], w['normalized']):
            if 'p_value' in part:
                n, k = part['n_permutations'], part['k_extreme']
                assert part['p_value'] == round((k + 1) / (n + 1), 12)
                assert 1 / (n + 1) - 1e-12 <= part['p_value'] <= 1
                assert part['p_formula'] == '(k+1)/(n+1)'


def brute_pairs(scenario, lo, hi, stim_type='letter', out_type='health'):
    S = [e['t'] for e in scenario['events'] if e['event_type'] == stim_type and e['t'] is not None]
    O = [e['t'] for e in scenario['events'] if e['event_type'] == out_type and e['t'] is not None and not e.get('precision')]
    return sum(1 for s in S for o in O if lo <= o - s < hi)


def test_p_value_formula_bounds_and_rng_determinism():
    assert es.p_value(0, 999) == 1 / 1000
    assert es.p_value(999, 999) == 1.0
    assert es.p_value(4, 9) == 0.5
    for bad in ((-1, 10), (11, 10), (0, 0)):
        with pytest.raises(ValueError):
            es.p_value(*bad)
    assert es.SplitMix64(0).next_u64() == 0xE220A8397B1DCDAF  # published SplitMix64 reference value
    a, b = es.SplitMix64(42), es.SplitMix64(42)
    assert [a.next_u64() for _ in range(5)] == [b.next_u64() for _ in range(5)]
    rng = es.SplitMix64(7)
    sample = rng.sample_indices(1000, 50)
    assert len(set(sample)) == 50 and all(0 <= i < 1000 for i in sample)
    assert sorted(rng.permutation(31)) == list(range(31))
    counts = [0] * 6
    for _ in range(6000):
        counts[rng.below(6)] += 1
    assert min(counts) > 850 and max(counts) < 1150
    with pytest.raises(ValueError):
        rng.sample_indices(3, 4)


def test_planted_lagged_effect_is_detected_and_counts_match_brute_force(db):
    scenario, ids, _, run = run_scenario(db, 'planted_effect')
    assert_common_contract(run)
    result = run['result']
    assert result['status'] == 'ok'
    assert result['input']['stimulus_count'] == 24 and result['input']['outcome_count'] == 88
    after, before = window(result, 'after_3d'), window(result, 'before_3d')
    assert after['naive']['observed'] == brute_pairs(scenario, 0, 3 * DAY)
    assert before['naive']['observed'] == brute_pairs(scenario, -3 * DAY, 0)
    assert after['naive']['p_value'] <= 0.02 and after['normalized']['p_value'] <= 0.02
    assert before['naive']['p_value'] > 0.2
    assert after['naive']['ratio_to_null_mean'] > 1.5
    assert after['naive']['stimuli_with_outcome'] == 24
    hist = result['lag_histogram']
    assert sum(hist['observed']) == brute_pairs(scenario, hist['edges_s'][0], hist['edges_s'][-1])
    assert len(hist['edges_s']) == len(hist['observed']) + 1 == len(hist['null_q975']) + 1
    planted_bins = [j for j, e in enumerate(hist['edges_s'][:-1]) if DAY // 2 <= e < 2 * DAY]
    assert any(hist['observed'][j] > hist['null_q975'][j] for j in planted_bins)
    assert not warning(result, 'detection_bias')['flagged']
    assert not warning(result, 'clustering')['flagged']
    assert result['input']['source']['rule'] and result['input']['source']['rule_sha256']


def test_density_burst_null_naive_looks_significant_normalized_does_not(db):
    _, _, _, run = run_scenario(db, 'density_burst_null')
    assert_common_contract(run)
    result = run['result']
    for label in ('before_3d', 'after_3d'):
        w = window(result, label)
        assert w['naive']['p_value'] <= 0.05, label
        assert w['normalized']['p_value'] >= 0.1, label
        assert w['density']['ratio'] > 1.5, label
    bias = warning(result, 'detection_bias')
    assert bias['flagged'] and bias['severity'] == 'high'
    assert set(bias['computed']['naive_vs_normalized_disagreement_windows']) == {'before_3d', 'after_3d'}
    assert 'artefakt' in bias['text_pl'] or 'artifact' in bias['text_en']
    series = result['control_series']
    assert len(series['counts_all']) == series['bins'] and sum(series['counts_all']) == result['input']['timed_events']
    assert series['period_s'] == DAY
    from datetime import datetime
    offset = (round(datetime.fromisoformat(series['start_utc'].replace('Z', '+00:00')).timestamp()) - corpus.START) // DAY
    burst_days = {b + d for b, length in corpus.BURSTS for d in range(length)}
    busiest = sorted(range(series['bins']), key=lambda j: -series['counts_all'][j])[:10]
    assert all(j + offset in burst_days for j in busiest)  # the control series shows the documentation bursts


def test_weekday_confound_disappears_under_stratified_permutation(db):
    _, _, version, plain = run_scenario(db, 'weekday_confound', analysis={'null_model': 'permutation'})
    assert_common_contract(plain)
    result = plain['result']
    assert result['seasonality']['weekday']['stimulus'][0] == 14  # every stimulus on a Monday (UTC)
    assert result['seasonality']['shared_structure'] is True
    assert window(result, 'after_1d')['naive']['p_value'] <= 0.01
    assert warning(result, 'seasonality')['flagged']
    stratified = es.run_event_study(db, version['version_id'], overrides={'null_model': 'stratified_weekday_hour'})
    assert_common_contract(stratified)
    sresult = stratified['result']
    assert window(sresult, 'after_1d')['naive']['p_value'] >= 0.1
    assert not warning(sresult, 'seasonality')['flagged']
    assert sresult['null_model']['strata_insufficient'] == [] and sresult['null_model']['strata_used'] >= 1
    deviation = warning(sresult, 'preregistration_deviation')
    assert deviation['flagged'] and deviation['computed']['deviations'] == [
        {'key': 'null_model', 'registered': 'permutation', 'used': 'stratified_weekday_hour'}]
    assert not warning(result, 'preregistration_deviation')['flagged']


def test_clustered_stimuli_overlap_is_counted_and_null_choice_matters(db):
    scenario, _, version, circular = run_scenario(db, 'clustered_stimuli')
    assert_common_contract(circular)
    result = circular['result']
    overlap = result['clustering']['windows'][0]
    assert overlap['overlapping_pairs'] == scenario['expect']['overlapping_pairs'] == 75
    assert overlap['stimuli_with_overlap'] == 30
    w = window(result, 'after_2d')
    assert w['naive']['unique_outcomes_in_windows'] < w['naive']['observed']  # shared outcomes counted repeatedly
    clustering = warning(result, 'clustering')
    assert clustering['flagged'] and clustering['severity'] == 'warning'
    assert clustering['computed']['null_preserves_clustering'] is True
    permuted = es.run_event_study(db, version['version_id'], overrides={'null_model': 'permutation'})
    pw = window(permuted['result'], 'after_2d')
    assert warning(permuted['result'], 'clustering')['severity'] == 'high'
    band = lambda part: part['null_q975'] - part['null_q025']
    assert band(w['naive']) > 1.5 * band(pw['naive'])  # independent placement understates the spread
    block = es.run_event_study(db, version['version_id'], overrides={'null_model': 'block_permutation', 'block_s': 10 * DAY})
    assert block['result']['null_model']['blocks'] >= 2
    assert warning(block['result'], 'clustering')['computed']['null_preserves_clustering'] is True
    with pytest.raises(HypothesisError, match='at least two blocks'):
        es.run_event_study(db, version['version_id'], overrides={'null_model': 'block_permutation', 'block_s': 400 * DAY})


def test_unknown_zone_and_coarse_times_are_excluded_and_counted(db):
    scenario, ids, version, run = run_scenario(db, 'unknown_zone')
    assert_common_contract(run)
    result = run['result']
    excluded = result['input']['untimed_excluded']
    assert excluded['by_reason'] == scenario['expect']['untimed_without_zone']
    assert set(excluded['event_ids']) == set(ids['local_stimulus'] + ids['local_outcome'] + ids['dst_gap_outcome']
                                             + ids['timeless_outcome'])
    assert result['input']['stimulus_count'] == 5 and result['input']['outcome_count'] == 5
    unknown = warning(result, 'unknown_zone_excluded')
    assert unknown['flagged'] and unknown['computed']['count'] == 7
    uncertainty = result['input']['time_uncertainty']
    assert uncertainty['by_set']['outcome']['imprecise'] == 2 and uncertainty['policy'] == 'exclude'
    assert uncertainty['threshold_s'] == result['definition']['lags']['bin_s']
    assert set(uncertainty['event_ids_listed']) == set(ids['day_precision_outcome'])
    assert warning(result, 'time_uncertainty')['flagged']
    keep_version = pr.register_version(db, version['hypothesis_id'], corpus.definition_for(scenario, imprecise_times='keep'))
    kept = es.run_event_study(db, keep_version['version_id'])['result']
    assert kept['input']['outcome_count'] == 7
    assert kept['input']['input_sha256'] != result['input']['input_sha256']
    zoned_version = pr.register_version(db, version['hypothesis_id'], corpus.definition_for(
        scenario, assume_zone='Europe/Warsaw', assumption_rule='operator: device clock set to Europe/Warsaw'))
    zoned = es.run_event_study(db, zoned_version['version_id'])['result']
    assert zoned['input']['untimed_excluded']['by_reason'] == scenario['expect']['untimed_with_warsaw']
    assert zoned['input']['stimulus_count'] == 8
    assert warning(zoned, 'unknown_zone_excluded')['computed']['assume_zone'] == 'Europe/Warsaw'


def test_rerun_is_identical_and_parameters_change_the_hash(db):
    _, _, version, first = run_scenario(db, 'planted_effect')
    second = es.run_event_study(db, version['version_id'])
    assert second['result_sha256'] == first['result_sha256']
    assert second['input_sha256'] == first['input_sha256']
    assert second['identical_result_runs'] == [first['run_id']] and second['run_id'] != first['run_id']
    other = es.run_event_study(db, version['version_id'], overrides={'seed': 99})
    assert other['result_sha256'] != first['result_sha256'] and other['seed'] == 99
    assert other['input_sha256'] == first['input_sha256']
    assert warning(other['result'], 'preregistration_deviation')['flagged']
    stored = json.loads(db.execute('SELECT result_json FROM hypothesis_runs WHERE id=?', (first['run_id'],)).fetchone()[0])
    assert sha256_canonical(stored) == first['result_sha256']
    assert first['code_version'] == 'unknown' or len(first['code_version']) >= 40
    with pytest.raises(HypothesisError, match='only these parameters'):
        es.run_event_study(db, version['version_id'], overrides={'windows': []})


def test_limits_insufficient_data_self_pairs_and_text_query(db):
    scenario, ids, version, _ = run_scenario(db, 'planted_effect')
    before = db.execute('SELECT COUNT(*) FROM hypothesis_runs').fetchone()[0]
    with pytest.raises(HypothesisError, match='max_work'):
        es.run_event_study(db, version['version_id'], limits=es.EventStudyLimits(max_work=1000))
    with pytest.raises(HypothesisError, match='max_events'):
        es.run_event_study(db, version['version_id'], limits=es.EventStudyLimits(max_events=10))
    assert db.execute('SELECT COUNT(*) FROM hypothesis_runs').fetchone()[0] == before
    empty = pr.register_hypothesis(db, corpus.definition_for(scenario) | {'stimulus': {'event_types': ['absent']}})
    run = es.run_event_study(db, empty['version_id'])
    assert run['result']['status'] == 'insufficient_data'
    assert 'no stimulus events inside the span' in run['result']['reasons']
    assert [w['id'] for w in run['result']['warnings']] == list(es.ALWAYS_PRESENT_WARNINGS)
    overlapping = pr.register_hypothesis(db, corpus.definition_for(scenario) | {'outcome': {'event_types': ['health', 'letter']}})
    result = es.run_event_study(db, overlapping['version_id'])['result']
    assert result['input']['self_pairs'] == 24 and result['input']['outcome_count'] == 88
    by_text = pr.register_hypothesis(db, corpus.definition_for(scenario) | {'outcome': {'text_query': 'wizyta'}})
    text_result = es.run_event_study(db, by_text['version_id'])['result']
    assert text_result['input']['outcome_count'] == 88
    assert window(text_result, 'after_3d')['naive']['observed'] == brute_pairs(scenario, 0, 3 * DAY)


def test_category_selector_uses_registered_private_configuration(db, tmp_path):
    from server.app.analysis import categories
    scenario = corpus.SCENARIOS['planted_effect']()
    corpus.populate(scenario)
    config_data = {'schema': categories.CONFIG_SCHEMA, 'version': 'test-1',
                   'categories': [{'id': 'zdrowie'}, {'id': 'urzedowe'}], 'default_category': None,
                   'rules': [{'id': 'zdrowie-typ', 'kind': 'event_type', 'values': ['health'], 'weights': {'zdrowie': 1}},
                             {'id': 'urzedowe-typ', 'kind': 'event_type', 'values': ['letter'], 'weights': {'urzedowe': 1}}]}
    config = categories.parse_config(config_data)
    definition = corpus.definition_for(scenario) | {'stimulus': {'categories': ['urzedowe']},
                                                    'outcome': {'categories': ['zdrowie']},
                                                    'category_config_sha256': config.sha256}
    version = pr.register_hypothesis(db, definition)
    with pytest.raises(HypothesisError, match='configuration'):
        es.run_event_study(db, version['version_id'])  # no configuration supplied
    other = dict(config_data, version='test-2')
    with pytest.raises(HypothesisError, match='differs from the registered'):
        es.run_event_study(db, version['version_id'], category_config=other)
    path = tmp_path / 'private-categories.json'
    path.write_text(json.dumps(config_data), encoding='utf-8')
    run = es.run_event_study(db, version['version_id'], category_config=path)
    result = run['result']
    assert result['input']['stimulus_count'] == 24 and result['input']['outcome_count'] == 88
    assert result['input']['categories']['config']['sha256'] == config.sha256
    assert result['input']['categories']['sets']['outcome']['selected_by_path'] == {'rule': 88}
    by_type = pr.register_hypothesis(db, corpus.definition_for(scenario))
    plain = es.run_event_study(db, by_type['version_id'])['result']
    assert window(plain, 'after_3d')['naive'] == window(result, 'after_3d')['naive']


def test_corpus_part_builds_a_manifest_of_planted_cases(tmp_path):
    from server.tests.corpus import PARTS, build_corpus
    assert 'hypotheses' in PARTS
    manifest = build_corpus(tmp_path, ('hypotheses',))['hypotheses']
    assert set(manifest['scenarios']) == set(corpus.SCENARIOS)
    stored = json.loads((tmp_path / 'hypotheses' / 'scenarios.json').read_text(encoding='utf-8'))
    assert stored['clustered_stimuli']['expect']['overlapping_pairs'] == 75
    assert corpus.SCENARIOS['planted_effect']() == corpus.SCENARIOS['planted_effect']()  # generated deterministically
    bodies = {e['body'] for s in stored.values() for e in s['events']}
    assert bodies == {'pismo z urzędu', 'wpis zdrowotny wizyta', 'notatka robocza'}  # no personal data

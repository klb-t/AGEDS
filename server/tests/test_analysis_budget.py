"""Time budget and feature series on the hard synthetic activity corpus."""
import sqlite3

import pytest

from server.app.analysis import budget, categories, features, intervals, model_runs, runs
from server.tests.corpus import activity


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    from server.app import db as dbmod, evidence
    from server.app.config import Settings
    s = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', s)
    monkeypatch.setattr(evidence, 'settings', s)
    dbmod.init_db()
    manifest = activity.build(tmp_path / 'activity')
    ids = activity.populate(manifest)
    config = categories.load_config(manifest['config_path'])
    return dbmod, manifest, ids, config


def _run(dbmod, manifest, config, window='window', **kw):
    with dbmod.session() as db:
        return budget.run_budget(db, config, window_from=manifest[window]['from'], window_to=manifest[window]['to'],
                                 zone=activity.ZONE, **kw)


def _day(result, key, granularity='day'):
    return next(c for c in result['candles'][granularity] if c['bucket'] == key)


def test_parallel_chatbot_sessions_are_counted_once(corpus):
    dbmod, manifest, ids, config = corpus
    result = _run(dbmod, manifest, config)['result']
    day = _day(result, manifest['expect']['parallel_day'])
    work = day['categories']['praca_projekty']
    # Union of both conversations: first typing (09:59:54) to the last reading tail (11:01:30).
    start, end = activity.epoch('2025-03-27T10:00') - 6, activity.epoch('2025-03-27T11:01') + 30
    assert work['probable'] == pytest.approx(end - start)
    separate = 2 * ((activity.epoch('2025-03-27T10:41') + 30) - (activity.epoch('2025-03-27T10:00') - 6))
    assert work['probable'] < separate
    # same category: the overlap rule cannot change the union
    both = _run(dbmod, manifest, config, params={'overlap_rule': 'count_both'})['result']
    assert _day(both, manifest['expect']['parallel_day'])['categories']['praca_projekty']['probable'] == work['probable']
    assert day['overlap_s']['probable'] == 0  # one category active, nothing to resolve


def test_long_gap_is_unobserved_never_idle(corpus):
    dbmod, manifest, ids, config = corpus
    result = _run(dbmod, manifest, config)['result']
    for key in manifest['expect']['empty_days']:
        day = _day(result, key)
        assert day['unobserved']['lower'] == day['unobserved']['probable'] == day['unobserved']['upper'] == 86400
        assert day['categories'] == {} and day['observed'] == {'lower': 0, 'probable': 0, 'upper': 0}
    ids_in_palette = {c['id'] for c in result['categories']}
    assert categories.UNOBSERVED in ids_in_palette
    assert not ids_in_palette & {'idle', 'bezczynnosc', 'nic'}
    assert 'a nie bezczynność' in result['disclaimer'] and 'not idleness' in result['method']['unobserved']
    # the lone point event gets upper-bound time only, both neighbouring gaps capped at max_attribution_s
    lone = _day(result, manifest['expect']['lone_day'])
    assert lone['categories']['inne']['lower'] == lone['categories']['inne']['probable'] == 0
    assert lone['categories']['inne']['upper'] == 2 * 1800


def test_dst_days_have_23_and_25_hours(corpus):
    dbmod, manifest, ids, config = corpus
    result = _run(dbmod, manifest, config)['result']
    short = _day(result, manifest['expect']['dst_short_day'])
    assert short['length_s'] == 23 * 3600 and not short['partial']
    assert short['unobserved']['upper'] + short['observed']['lower'] == 23 * 3600
    week = result['candles']['week'][0]
    assert week['bucket'] == '2025-W13' and week['length_s'] == 4 * 86400 - 3600 and week['partial']
    fallback = _run(dbmod, manifest, config, window='fallback_window')['result']
    long_day = _day(fallback, manifest['expect']['dst_long_day'])
    assert long_day['length_s'] == 25 * 3600
    assert long_day['categories']['inne']['lower'] == 600  # the 01:30 call, counted once across the repeated hour


def test_call_durations_overlap_rules_and_weight_split(corpus):
    dbmod, manifest, ids, config = corpus
    split = _run(dbmod, manifest, config)['result']
    day = _day(split, manifest['expect']['call_day'])
    assert day['categories']['sprawy_urzedowe']['lower'] == manifest['expect']['official_call_s']
    assert day['categories']['sprawy_urzedowe']['roles']['explicit'] == 1
    # calendar entry split 0.5/0.5 by one rule; the 2 s WAV recording + 600 s call are confirmed 'inne'
    assert day['categories']['zdrowie']['lower'] == manifest['expect']['calendar_split']['zdrowie']
    assert day['categories']['praca_projekty']['lower'] == manifest['expect']['calendar_split']['praca_projekty']
    assert day['categories']['inne']['lower'] == 600 + manifest['expect']['recording_s']
    # the chat during the call: confirmed call time wins (layered), chat keeps 09:20-09:31:30 only
    assert day['categories']['praca_projekty']['probable'] == 1800 + (activity.epoch('2025-03-28T09:31') + 30
                                                                      - activity.epoch('2025-03-28T09:20'))
    both = _day(_run(dbmod, manifest, config, params={'overlap_rule': 'count_both'})['result'],
                manifest['expect']['call_day'])
    chat_span = (activity.epoch('2025-03-28T09:31') + 30) - (activity.epoch('2025-03-28T09:10') - 6)
    assert both['categories']['praca_projekty']['probable'] == 1800 + chat_span
    assert both['overlap_s']['probable'] == activity.epoch('2025-03-28T09:20') - (activity.epoch('2025-03-28T09:10') - 6)
    priority = _day(_run(dbmod, manifest, config, params={'overlap_rule': 'priority',
                                                           'priority': ['praca_projekty']})['result'],
                    manifest['expect']['call_day'])
    assert priority['categories']['sprawy_urzedowe']['lower'] == 1200  # nothing confirmed overlaps the call
    # bounds are ordered for every candle and grid wicks enclose them
    for result in (split,):
        for g, candles in result['candles'].items():
            for c in candles:
                for v in c['categories'].values():
                    assert v['wick_low'] <= v['lower'] <= v['probable'] <= v['upper'] <= v['wick_high']
                    assert v['body_low'] <= v['probable'] <= v['body_high']
                u = c['unobserved']
                assert u['wick_low'] <= u['lower'] <= u['probable'] <= u['upper'] <= u['wick_high']
                # accounting identity (non-double-counting rule): categories + unobserved = length
                alloc = sum(v['probable'] for v in c['categories'].values())
                assert alloc + u['probable'] == pytest.approx(c['length_s'], abs=1e-3)


def test_unknown_zone_events_are_excluded_and_reported(corpus):
    dbmod, manifest, ids, config = corpus
    result = _run(dbmod, manifest, config)['result']
    untimed = result['exclusions']['untimed']
    assert untimed['by_reason'] == {'unknown_zone': 2}
    assert set(untimed['event_ids']) == {ids['wa-nonexistent'], ids['wa-unknown']}
    assert result['exclusions']['filtered']['by_reason'] == manifest['expect']['filtered']
    assert untimed['by_reason'] == manifest['expect']['untimed_reasons']
    with pytest.raises(ValueError, match='assumption_rule'):
        _run(dbmod, manifest, config, assume_zone=activity.ZONE)
    assumed = _run(dbmod, manifest, config, assume_zone=activity.ZONE, assumption_rule='synthetic-operator-zone')['result']
    assert assumed['exclusions']['untimed']['by_reason'] == manifest['expect']['untimed_reasons_assumed_zone']
    assert assumed['exclusions']['untimed']['event_ids'] == [ids['wa-nonexistent']]
    assert assumed['exclusions']['assumed_zone_items'] == 1
    assert assumed['window']['assume_zone'] == activity.ZONE
    assert assumed['window']['assumption_rule'] == 'synthetic-operator-zone'
    assert assumed['input']['projection_params'] == {'assume_zone': activity.ZONE,
                                                     'assumption_rule': 'synthetic-operator-zone'}


def test_runs_record_timeline_rule_and_uncertainty(corpus):
    from server.app.timeline.ruleset import load_rule
    dbmod, manifest, ids, config = corpus
    result = _run(dbmod, manifest, config)['result']
    rule = load_rule(None, kind='display_rule')
    assert result['input']['seam'] == 'timeline'
    assert result['input']['source_rule'] == f'{rule.rule_id}@{rule.version}'
    assert result['input']['source_rule_sha256'] == rule.sha256
    assert result['input']['time_uncertainty']['mode'] == 'widen_upper'
    # a minute-precision wall clock under an operator zone has a 60 s interval: upper widens, lower/probable do not
    assumed = _run(dbmod, manifest, config, assume_zone=activity.ZONE, assumption_rule='synthetic-operator-zone')['result']
    tu = assumed['input']['time_uncertainty']
    assert tu['items_with_interval'] >= 1 and tu['max_width_s'] == 60
    day = _day(assumed, manifest['expect']['dst_short_day'])
    plain = _day(result, manifest['expect']['dst_short_day'])
    assert day['categories']['inne']['lower'] == plain['categories']['inne']['lower']
    strict = _run(dbmod, manifest, config, assume_zone=activity.ZONE, assumption_rule='synthetic-operator-zone',
                  params={'max_time_uncertainty_s': 30})['result']
    assert strict['exclusions']['filtered']['by_reason']['time_uncertainty_over_limit'] == 1
    hidden = _run(dbmod, manifest, config, params={'include_hidden': True})['result']
    assert 'hidden_by_projection' not in hidden['exclusions']['filtered']['by_reason']


def test_manual_override_precedence_with_visible_path(corpus):
    dbmod, manifest, ids, config = corpus
    before = _day(_run(dbmod, manifest, config)['result'], manifest['expect']['call_day'])
    assert ids['sms-override'] in before['categories']['zdrowie']['event_ids']
    with dbmod.session() as db:
        annotation = categories.add_annotation(db, ids['sms-override'], {'odpoczynek': 1}, note='synthetic')
    out = _run(dbmod, manifest, config)
    after = _day(out['result'], manifest['expect']['call_day'])
    assert ids['sms-override'] in after['categories']['odpoczynek']['event_ids']
    assert after['categories']['odpoczynek']['label_paths'] == {'annotation': 1}
    assert ids['sms-override'] not in after['categories']['zdrowie']['event_ids']
    with dbmod.session() as db:
        row = db.execute('SELECT path, annotation_id FROM analysis_event_labels WHERE label_run_id=? AND event_id=?',
                         (out['result']['labels']['label_run_id'], ids['sms-override'])).fetchone()
    assert tuple(row) == ('annotation', annotation)
    assert out['result']['labels']['by_path']['annotation'] == 1


def test_sensitivity_flags_top_category_and_ordering_flips(corpus):
    dbmod, manifest, ids, config = corpus
    result = _run(dbmod, manifest, config, window='flip_window')['result']
    sens = result['sensitivity']
    assert sens['unstable'] is True
    flips = sens['flips']['ordering']
    assert {tuple(f['categories']) for f in flips} == {('praca_projekty', 'zdrowie')}
    assert {next(iter(f['differs'])) for f in flips} == {'typing_chars_per_min', 'reading_chars_per_min'}
    day = _day(result, manifest['expect']['flip_day'])
    assert day['categories']['praca_projekty']['probable'] == 1200
    assert day['categories']['zdrowie']['probable'] == pytest.approx(999.9)
    assert 'top_category_flip' in day['categories']['zdrowie']['unstable']
    assert day['categories']['praca_projekty']['body_low'] == 800 and day['categories']['praca_projekty']['body_high'] == 2000
    stable = _run(dbmod, manifest, config, window='flip_window',
                  params={'grid': {'typing_chars_per_min': [180, 220], 'reading_chars_per_min': [900, 1100]}})['result']
    assert stable['sensitivity']['flip_counts'].get('ordering', 0) == 0
    assert not stable['candles']['day'][0]['unstable']


def test_determinism_and_new_params_create_new_run(corpus):
    dbmod, manifest, ids, config = corpus
    first = _run(dbmod, manifest, config)
    again = _run(dbmod, manifest, config)
    assert again['run']['reused'] is True and again['run']['run_id'] == first['run']['run_id']
    assert again['run']['result_sha256'] == first['run']['result_sha256']
    changed = _run(dbmod, manifest, config, params={'session_gap_s': 300})
    assert changed['run']['run_id'] != first['run']['run_id'] and changed['run']['reused'] is False
    with dbmod.session() as db:
        old = runs.get_run(db, first['run']['run_id'])
        assert old['result_sha256'] == first['run']['result_sha256'] and old['result'] == first['result']
        assert old['parameters']['params']['session_gap_s'] == 900
        assert old['config_sha256'] == config.sha256 and old['config_version'] == 'corpus-activity-1'
        assert old['code_version'] == 'unknown' or len(old['code_version']) == 40
        assert len(runs.list_runs(db, kind='time_budget')) == 2
        with pytest.raises(sqlite3.DatabaseError, match='append-only'):
            db.execute('UPDATE analysis_runs SET result_json="{}"')
        with pytest.raises(sqlite3.DatabaseError, match='append-only'):
            db.execute('DELETE FROM analysis_runs')
    # a config edit is a new hash and a new run; the pure computation is reproducible without storage
    edited = categories.parse_config({**config.data, 'version': 'corpus-activity-2'})
    third = _run(dbmod, manifest, edited)
    assert third['run']['run_id'] not in (first['run']['run_id'], changed['run']['run_id'])
    dry = _run(dbmod, manifest, config, persist=False)
    assert dry['run'] is None and dry['result']['candles'] == first['result']['candles']


def test_parameter_validation_and_grid_bounds(corpus):
    dbmod, manifest, ids, config = corpus
    with pytest.raises(ValueError, match='unknown parameters'):
        budget.effective_params({'typo': 1})
    with pytest.raises(ValueError, match='overlap_rule'):
        budget.effective_params({'overlap_rule': 'sum'})
    with pytest.raises(ValueError, match='grid keys'):
        budget.effective_params({'grid': {'priority': []}})
    with pytest.raises(ValueError):
        _run(dbmod, manifest, config, params={'session_gap_s': -1})
    with pytest.raises(ValueError, match='after its start'):
        with dbmod.session() as db:
            budget.run_budget(db, config, window_from='2025-04-02', window_to='2025-04-01', zone=activity.ZONE)
    with pytest.raises(ValueError, match='unknown zone'):
        with dbmod.session() as db:
            budget.run_budget(db, config, window_from='2025-04-01', window_to='2025-04-02', zone='Mars/Base')
    full = _run(dbmod, manifest, config, window='flip_window', params={'grid_mode': 'full', 'max_grid_points': 20})
    assert full['result']['grid'] == {'mode': 'full', 'points_evaluated': 20, 'points_total': 243, 'truncated': True,
                                      'keys': list(budget.GRID_KEYS)}


def test_model_labels_are_opt_in_hypotheses_below_rules(corpus):
    dbmod, manifest, ids, config = corpus
    with dbmod.session() as db:
        items = budget.load_items(db)['items']
        none = model_runs.run_model(db, items, purpose='labels')
        assert none['status'] == 'not_run' and none['outputs'] == 0 and none['opt_in'] is False
        with pytest.raises(ValueError, match='opt_in'):
            model_runs.run_model(db, items, purpose='labels', provider='fake', config=config)
        with pytest.raises(ValueError, match='unavailable'):
            model_runs.run_model(db, items, purpose='labels', provider='openai', opt_in=True, config=config)
        fake = model_runs.run_model(db, items, purpose='labels', provider='fake', opt_in=True, config=config)
        stored = model_runs.get_model_run(db, fake['model_run_id'])
        assert stored['provider'] == 'fake' and stored['opt_in'] is True and stored['metadata']['output_is_hypothesis']
    result = _run(dbmod, manifest, config, params={'model_label_run_id': fake['model_run_id']})['result']
    paths = result['labels']['by_path']
    assert 'model' in paths and 'default' not in paths      # model only replaces the default path
    assert paths['rule'] == _run(dbmod, manifest, config)['result']['labels']['by_path']['rule']
    assert result['labels']['model_label_run_id'] == fake['model_run_id']


def test_feature_series_deterministic_and_model_series_separate(corpus):
    dbmod, manifest, ids, config = corpus
    with dbmod.session() as db:
        out = features.run_features(db, config, window_from=manifest['window']['from'],
                                    window_to=manifest['window']['to'], zone=activity.ZONE,
                                    params={'budget_params': {'granularities': ['day']}})
    series = {s['id']: s for s in out['result']['series']}
    day = {v['bucket']: v for v in series['message_count']['values']['day']}
    assert day['2025-03-27']['value'] == 20 and day['2025-03-31']['value'] == 0
    official = {v['bucket']: v['value'] for v in series['official_counterparties_distinct']['values']['day']}
    assert official['2025-03-28'] == 2
    night = {v['bucket']: v for v in series['night_activity_share']['values']['day']}
    assert night['2025-03-28']['value'] == pytest.approx(1 / night['2025-03-28']['n'], abs=1e-3)
    assert night['2025-04-01']['value'] is None  # no events: unknown share, not zero
    responses = {v['bucket']: v for v in series['assistant_response_chars']['values']['day']}
    assert responses['2025-03-27']['value'] == 500 and responses['2025-03-27']['n'] == 10
    tone = series['tone']
    assert tone['kind'] == 'model_estimated' and tone['status'] == 'not_run'
    assert all(v['value'] is None for v in tone['values']['day'])
    assert all(s['kind'] == 'deterministic' for k, s in series.items() if k != 'tone')
    with dbmod.session() as db:
        fake = features.run_features(db, config, window_from=manifest['window']['from'],
                                     window_to=manifest['window']['to'], zone=activity.ZONE,
                                     params={'budget_params': {'granularities': ['day']},
                                             'model_features': {'tone': {'provider': 'fake', 'opt_in': True}}})
    fake_tone = {s['id']: s for s in fake['result']['series']}['tone']
    assert fake_tone['status'] == 'done' and fake_tone['model_run']['provider'] == 'fake'
    assert fake['run']['run_id'] != out['run']['run_id']
    assert {s['id']: s for s in fake['result']['series']}['message_count'] == series['message_count']


def test_interval_union_matches_independent_computation(corpus):
    dbmod, manifest, ids, config = corpus
    result = _run(dbmod, manifest, config, params={'overlap_rule': 'count_both'})['result']
    rows = [r for r in manifest['rows'] if r['kind'] == 'chat' and r['at'].date().isoformat() == '2025-03-27']
    spans = []
    for conv in {r['conv'] for r in rows}:
        times = sorted(r['at'].timestamp() for r in rows if r['conv'] == conv)
        spans.append((times[0] - 6, times[-1] + 30))
    assert _day(result, '2025-03-27')['categories']['praca_projekty']['probable'] == pytest.approx(intervals.total(spans))

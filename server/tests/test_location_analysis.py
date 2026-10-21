"""Shared-contract edits of the location area: analysis seam signal keys, budget place rules and inferred
ranges (time_budget.v4), passive-signal density default of event studies (method v3) — task L1."""
from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.analysis import budget, categories, eventstudy, preregistration
from server.app.analysis.source import load_items
from server.app.importers import call_log, ics, location
from server.app.location import places, presence
from server.tests.corpus import location as corpus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_settings = replace(dbmod.settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store')
    test_settings.store_dir.mkdir(parents=True)
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    manifest = corpus.build(tmp_path / 'corpus')
    files = {k: Path(v) for k, v in manifest['files'].items()}
    location.import_location_history_v2(files['Takeout/Location History/Records.json'], enable_location=True,
                                        stream='phone')
    location.import_location_history_v2(
        files['Takeout/Location History/Semantic Location History/2024/2024_JANUARY.json'], enable_location=True,
        stream='phone')
    ics.import_ics(files['calendar/meetings.ics'])
    call_log.import_calls_xml(files['phone/calls.xml'])
    presence.run_presence(places.load_places(manifest['places_path']), publish=True, actor='tester')
    return {'m': manifest, 'f': files}


CONFIG = {'schema': 'ageds.activity_categories.v1', 'version': 'l1-test',
          'categories': [{'id': 'dom'}, {'id': 'praca'}, {'id': 'dojazd'}, {'id': 'spotkania'}],
          'rules': [{'id': 'p-home', 'kind': 'place', 'values': ['home'], 'weights': {'dom': 1}},
                    {'id': 'p-work', 'kind': 'place', 'values': ['work'], 'weights': {'praca': 1}},
                    {'id': 'travel', 'kind': 'event_type', 'values': ['location_segment'], 'weights': {'dojazd': 1}},
                    {'id': 'cal', 'kind': 'event_type', 'values': ['calendar_event'], 'weights': {'spotkania': 1}}]}


def test_analysis_items_carry_signal_and_place_ids(env):
    with dbmod.session() as db:
        loaded = load_items(db, start_utc='2024-01-15T00:00:00Z', end_utc='2024-01-16T00:00:00Z')
    by_type = {}
    for item in loaded['items']:
        by_type.setdefault(item['event_type'], []).append(item)
    assert loaded['seam'] == 'timeline'
    assert {i['signal']['basis'] for i in by_type['place_presence']} == {'derived'}
    assert {p for i in by_type['place_presence'] for p in i['place_ids']} == {'home', 'work', 'shop'}
    assert {i['signal']['basis'] for i in by_type['location_visit']} == {'provider_inference', 'provider_user_edit'}
    assert {i['signal']['class'] for i in by_type['location_samples']} == {'passive'}
    assert by_type['calendar_event'][0]['signal'] is None and by_type['calendar_event'][0]['place_ids'] == []


def test_place_rule_kind_labels_presence_with_provenance():
    config = categories.parse_config(CONFIG)
    item = {'event_id': 1, 'event_type': 'place_presence', 'place_ids': ['home'], 'tags': []}
    label = categories.label_items([item], config)[1]
    assert label['path'] == 'rule' and label['categories'] == {'dom': 1.0}
    assert label['provenance']['rules'] == [{'rule_id': 'p-home', 'rule_version': 1, 'kind': 'place', 'weights': {'dom': 1.0}}]
    other = categories.label_items([{'event_id': 2, 'event_type': 'sms', 'tags': []}], config)[2]
    assert other['path'] == 'default'
    with pytest.raises(ValueError):
        categories.parse_config({**CONFIG, 'rules': [{'id': 'x', 'kind': 'geocode', 'values': ['a'], 'weights': {'dom': 1}}]})


def test_budget_v4_inferred_and_derived_ranges_are_probable_not_confirmed(env):
    config = categories.parse_config(CONFIG)
    with dbmod.session() as db:
        run = budget.run_budget(db, config, window_from='2024-01-15T00:00:00Z', window_to='2024-01-16T00:00:00Z',
                                zone='UTC')
    result = run['result']
    assert result['method']['rule'] == 'time_budget.v5' and 'time_budget.v4' in result['method']['rule_history']
    totals = result['totals']['categories']
    for category in ('dom', 'praca', 'dojazd'):  # derived presence / provider travel segment: never confirmed
        assert totals[category]['lower'] == 0.0 and totals[category]['probable'] > 0, category
    # the calendar entry stays a confirmed range (3600 s; the confirmed 60 s call inside it takes half of its minute)
    assert totals['spotkania']['lower'] == 3570.0
    assert 'location_samples' in result['parameters']['excluded_event_types']
    roles = {}
    for candle in result['candles']['day']:
        for category, value in candle['categories'].items():
            for role, n in (value.get('roles') or {}).items():
                roles.setdefault(category, set()).add(role)
    assert 'derived' in roles.get('dom', set()) and 'provider_inference' in roles.get('dojazd', set())
    assert 'explicit' in roles.get('spotkania', set())


def test_budget_without_signal_metadata_is_unchanged_by_v4():
    """Items without metadata.signal follow exactly the v3 computation (no inferred spans)."""
    params = budget.effective_params({})
    items = [{'event_id': 1, 'event_type': 'call', 't_start': 100.0, 't_end': 400.0, 't_lo': 100.0, 't_hi': 100.0,
              'direction': 'incoming', 'text_chars': None, 'chat': None, 'signal': None},
             {'event_id': 2, 'event_type': 'location_segment', 't_start': 500.0, 't_end': 800.0, 't_lo': 500.0,
              't_hi': 500.0, 'direction': None, 'text_chars': None, 'chat': None,
              'signal': {'class': 'passive', 'basis': 'provider_inference'}}]
    labels = {1: {'categories': {'a': 1.0}, 'path': 'rule'}, 2: {'categories': {'a': 1.0}, 'path': 'rule'}}
    acts = budget._prepare(items, labels, params)
    assert acts[0]['explicit'] == (100.0, 400.0) and acts[0]['inferred'] is None
    assert acts[1]['explicit'] is None and acts[1]['inferred'] == (500.0, 800.0, 'provider_inference')


# ------------------------------------------------------------------ E2: passive density default

T0 = 1_700_000_000


def _items(seed: int):
    rnd = random.Random(seed)
    items, eid = [], 0
    for kind, count, signal in (('letter', 20, None), ('health', 80, None), ('note', 200, None),
                                ('location_samples', 400, {'class': 'passive', 'basis': 'derived'})):
        for _ in range(count):
            eid += 1
            t = float(T0 + rnd.randrange(0, 60 * 86400))
            items.append({'event_id': eid, 'event_type': kind, 't_start': t, 't_lo': t, 't_hi': t, 'signal': signal,
                          'time_basis': {'kind': 'sent', 'precision': 'second'}, 'tags': []})
    return items


RAW = {'title': 't', 'text': 't', 'stimulus': {'event_types': ['letter']}, 'outcome': {'event_types': ['health']},
       'windows': [{'label': 'w', 'start_s': 0, 'end_s': 6 * 3600}], 'analysis': {'permutations': 49, 'seed': 3}}


def _compute(definition):
    params, _ = eventstudy.effective_parameters(definition, None)
    loaded = {'items': _items(1), 'untimed': [], 'rule': 'synthetic', 'assume_zone': None}
    return params, eventstudy.compute_event_study(definition, params, loaded, fts={}, categories={})[0]


def test_new_definitions_exclude_passive_signals_from_density_by_default():
    definition = preregistration.normalize_definition(RAW)
    assert definition['analysis']['density_excludes_signal_classes'] == ['passive']
    params, result = _compute(definition)
    assert result['method']['id'] == 'ageds.event-study-method/v3'
    assert result['input']['density_count'] == 200  # notes only; 400 passive samples excluded
    assert result['input']['density_signal_exclusion'] == {'classes': ['passive'], 'excluded_by_class': {'passive': 400},
                                                           'basis': 'definition'}
    opted_out = preregistration.normalize_definition({**RAW, 'analysis': {**RAW['analysis'],
                                                                          'density_excludes_signal_classes': []}})
    _, kept = _compute(opted_out)
    assert kept['input']['density_count'] == 600
    with pytest.raises(Exception):
        preregistration.normalize_definition({**RAW, 'analysis': {'density_excludes_signal_classes': ['loud']}})


def test_definitions_registered_before_the_default_keep_their_stored_behaviour(env, monkeypatch):
    legacy = preregistration.normalize_definition(RAW)
    del legacy['analysis']['density_excludes_signal_classes']  # what a pre-2026-10-17 registration stored
    params, result = _compute(legacy)
    assert params['density_excludes_signal_classes'] == [] and params['density_signal_basis'] == 'legacy_definition_without_key'
    assert result['input']['density_count'] == 600 and result['input']['density_signal_exclusion']['excluded_by_class'] == {}
    original = preregistration.normalize_definition

    def pre_upgrade(raw):
        out = original(raw)
        out['analysis'].pop('density_excludes_signal_classes', None)
        return out
    monkeypatch.setattr(preregistration, 'normalize_definition', pre_upgrade)
    raw = {**RAW, 'stimulus': {'event_types': ['calendar_event']}, 'outcome': {'event_types': ['call']},
           'windows': [{'label': 'w', 'start_s': -86400, 'end_s': 86400}], 'analysis': {'permutations': 9, 'seed': 1,
                                                                                         'null_model': 'circular_shift'}}
    with dbmod.session() as db:
        old = preregistration.register_hypothesis(db, raw, actor='before-upgrade')
    monkeypatch.setattr(preregistration, 'normalize_definition', original)
    assert 'density_excludes_signal_classes' not in old['definition']['analysis']
    assert old['integrity']['status'] == 'hash_matches'
    with dbmod.session() as db:
        stored = eventstudy.run_event_study(db, old['version_id'], actor='t')
        fresh = preregistration.register_hypothesis(db, raw, actor='after-upgrade')
        new = eventstudy.run_event_study(db, fresh['version_id'], actor='t')
    old_input, new_input = stored['result']['input'], new['result']['input']
    assert stored['parameters']['density_signal_basis'] == 'legacy_definition_without_key'
    assert old_input['density_signal_exclusion']['classes'] == [] and new_input['density_signal_exclusion']['classes'] == ['passive']
    passive = new_input['density_signal_exclusion']['excluded_by_class'].get('passive', 0)
    assert passive > 0 and old_input['density_count'] == new_input['density_count'] + passive
    assert fresh['definition_sha256'] != old['definition_sha256']

"""Sleep/workout sessions and travel segments in the time budget: provider-inferred, never confirmed by default."""
from __future__ import annotations

from pathlib import Path

from server.app import db as dbmod
from server.app.analysis import budget, categories
from server.app.importers import health_apple, tabular_profiles
from server.tests.ledger_support import env  # noqa: F401  (fixture)

CONFIG = {
    'schema': 'ageds.activity_categories.v1', 'version': 'ledger-test-1',
    'categories': [{'id': 'sen', 'label': 'Sen'}, {'id': 'ruch', 'label': 'Ruch'}, {'id': 'transport', 'label': 'Transport'}],
    'default_category': None, 'combine': 'first',
    'rules': [
        {'id': 'travel', 'version': 1, 'kind': 'event_type', 'values': ['travel_segment'], 'weights': {'transport': 1}},
        {'id': 'sleep-workout', 'version': 1, 'kind': 'signal_basis', 'values': ['provider_inference'],
         'event_types': ['health_session'], 'weights': {'sen': 1}},
        {'id': 'user-entered', 'version': 1, 'kind': 'signal_basis', 'values': ['provider_user_edit'],
         'weights': {'ruch': 1}},
    ],
}


def _run(params=None):
    config = categories.parse_config(CONFIG)
    with dbmod.session() as db:
        result = budget.run_budget(db, config, window_from='2024-03-30', window_to='2024-04-02', zone='Europe/Amsterdam',
                                   params={'overlap_rule': 'count_both', **(params or {})}, persist=False)['result']
    result['by_category'] = result['totals']['categories']
    return result


def test_provider_inferred_durations_are_estimates_unless_the_parameter_says_so(env):
    health_apple.import_apple_health(Path(env['m']['health']['apple']['export']), enable_health=True)
    tabular_profiles.import_tabular(Path(env['m']['rides']['path']))
    default = _run()
    assert default['parameters']['inferred_duration_layer'] == 'probable'
    assert default['parameters']['inferred_duration_bases'] == ['provider_inference', 'derived']
    assert 'inferred_durations' in default['method']
    sen, transport, ruch = (default['by_category'][c] for c in ('sen', 'transport', 'ruch'))
    assert sen['lower'] == 0 and sen['probable'] > 0  # sleep sessions and the run: estimates
    assert transport['lower'] == 0 and transport['probable'] == 2570 + 1500  # two ride trips with explicit offsets
    assert ruch['lower'] == ruch['probable'] == 1200  # the user-entered yoga workout is declared, not inferred
    counted = _run({'inferred_duration_layer': 'lower'})
    assert counted['by_category']['sen']['lower'] == counted['by_category']['sen']['probable'] > 0
    assert counted['by_category']['transport']['lower'] > 0
    narrowed = _run({'inferred_duration_bases': ['provider_inference', 'derived', 'provider_user_edit']})
    assert narrowed['by_category']['ruch']['lower'] == 0 and narrowed['by_category']['ruch']['probable'] > 0
    only_derived = _run({'inferred_duration_bases': ['derived']})
    assert only_derived['by_category']['sen']['lower'] == only_derived['by_category']['sen']['probable'] > 0


def test_signal_basis_rule_kind_validates(env):
    config = categories.parse_config(CONFIG)
    assert [r.kind for r in config.rules] == ['event_type', 'signal_basis', 'signal_basis']

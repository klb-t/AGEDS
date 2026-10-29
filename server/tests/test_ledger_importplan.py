"""Ledger detectors and adapters through the import plan: proposals, opt-in blockers, execution, idempotence."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from server.app import db as dbmod, evidence
from server.app.catalog import store
from server.app.catalog.scan import run_catalog_scan
from server.app.importplan import execute, plan
from server.app.importplan.adapters import IMPORTERS
from server.app.importplan.detect import load_detectors
from server.tests.corpus import ledger as corpus
from server.tests.test_catalog_scan import tree_state

EXPECTED = {
    'statements/camt053_two_statements.xml': 'camt053',
    'statements/camt053_v8.xml': 'camt053',
    'statements/statement.sta': 'mt940',
    'tables/bank_pl_cp1250.csv': 'ledger_tabular',
    'tables/bank_nl.csv': 'ledger_tabular',
    'tables/bank_dc_ambiguous.csv': 'ledger_tabular',
    'tables/transit_same_row.csv': 'ledger_tabular',
    'tables/transit_taps.csv': 'ledger_tabular',
    'tables/rides.csv': 'ledger_tabular',
    'health/apple/export.xml': 'health_apple',
    'health/apple/export.zip!/apple_health_export/export.xml': 'health_apple',
    'health/Takeout/Fit/All Sessions/2024-03-30T23_00_00+01_00_SLEEP.json': 'health_google_fit',
    'health/Takeout/Fit/Daily activity metrics/Daily activity metrics.csv': 'health_google_fit',
    'health/Samsung Health/com.samsung.shealth.sleep.20240401120000.csv': 'health_samsung',
    'health/Takeout/Fitbit/Global Export Data/sleep-2024-03-30.json': 'health_fitbit',
    'health/Takeout/Fitbit/Global Export Data/exercise-0.json': 'health_fitbit',
}
PROFILE = {'tables/bank_pl_cp1250.csv': 'example_bank_pl_style_v1', 'tables/rides.csv': 'example_ride_trips_v1',
           'tables/transit_taps.csv': 'example_transit_taps_v1'}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    settings = replace(dbmod.settings, data_dir=data, db_path=data / 'evidence.db', store_dir=data / 'store',
                       scan_roots=(tmp_path / 'corpus',))
    for module in (dbmod, evidence):
        monkeypatch.setattr(module, 'settings', settings)
    dbmod.init_db()
    manifest = corpus.build(tmp_path / 'corpus')
    collection = store.create_collection('Ledger corpus (synthetic)', device_or_account='ledger')['id']
    run_catalog_scan(tmp_path / 'corpus', collection, temp_dir=tmp_path / 'spool')
    return {'tmp': tmp_path, 'manifest': manifest, 'collection': collection, 'before': tree_state(tmp_path / 'corpus')}


def test_registry_and_detector_table_are_loaded():
    keys = {'ledger_tabular', 'camt053', 'mt940', 'health_apple', 'health_google_fit', 'health_samsung', 'health_fitbit'}
    assert keys <= set(IMPORTERS)
    assert all(IMPORTERS[k].required_opt_ins == ('enable_health',) for k in keys if k.startswith('health_'))
    table = load_detectors()
    assert 'ledger.v1.json' in [e['name'] for e in table['_extensions']]


def test_plan_proposes_ledger_importers_and_blocks_health_without_opt_in(world):
    created = plan.create_plan(collection_ids=[world['collection']], actor='tester', temp_dir=world['tmp'] / 'work')
    items = plan.get_plan(created['plan']['id'], limit=500)['items']
    found = {i['locator']: i for i in items}
    for locator, importer in EXPECTED.items():
        assert locator in found, locator
        assert found[locator]['state']['importer'] == importer, (locator, found[locator]['candidates'])
    for locator, importer in EXPECTED.items():
        blockers = found[locator]['blockers']
        if importer.startswith('health_'):
            assert 'opt_in_disabled:enable_health' in blockers
        if importer == 'ledger_tabular':
            assert 'parameter_missing:profile_id' in blockers
    assert 'health/Samsung Health/plain.csv' not in found


def test_approved_ledger_items_execute_through_the_real_importers(world):
    plan_id = plan.create_plan(collection_ids=[world['collection']], actor='tester',
                               temp_dir=world['tmp'] / 'work')['plan']['id']
    items = {i['locator']: i for i in plan.get_plan(plan_id, limit=500)['items']}
    chosen = ['statements/camt053_two_statements.xml', 'statements/statement.sta', *PROFILE,
              'health/apple/export.xml', 'health/Takeout/Fitbit/Global Export Data/sleep-2024-03-30.json']
    decisions = []
    for locator in chosen:
        item = items[locator]
        decision = {'item_id': item['id'], 'status': 'approved'}
        if locator in PROFILE:
            decision['parameters'] = {'profile_id': PROFILE[locator]}
        if item['state']['importer'].startswith('health_'):
            decision['opt_ins'] = {'enable_health': True}
        decisions.append(decision)
    version = plan.get_plan(plan_id, limit=1)['version']
    plan.decide(plan_id, base_version=version, decisions=decisions, actor='tester', note='ledger')
    result = execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work', item_ids=[d['item_id'] for d in decisions])
    assert tree_state(world['tmp'] / 'corpus') == world['before']
    with dbmod.session() as db:
        statuses = [r[0] for r in db.execute('SELECT status FROM import_plan_attempts ORDER BY id')]
        types = {r[0]: r[1] for r in db.execute('SELECT event_type, COUNT(*) FROM events GROUP BY event_type')}
        located = db.execute("SELECT COUNT(*) FROM source_observations WHERE source_locator LIKE 'catalog:collection/%'"
                             ).fetchone()[0]
        audit = [json.loads(r[0]) for r in db.execute("SELECT details_json FROM audit_log WHERE action='import_run'")]
    assert statuses and set(statuses) == {'done'}, (statuses, result)
    assert types['financial_transaction'] > 0 and types['health_session'] > 0 and types['travel_segment'] > 0
    assert located >= len(chosen)
    assert any(a['importer'] == 'apple_health_export' and a['options']['enable_health'] is True for a in audit)
    with dbmod.session() as db:
        before = {t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                  for t in ('events', 'ledger_entries', 'health_samples', 'ledger_statements')}
    execute.execute_plan(plan_id, actor='tester', temp_dir=world['tmp'] / 'work', rerun_done=True,
                         item_ids=[d['item_id'] for d in decisions])
    with dbmod.session() as db:
        after = {t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in before}
    assert after == before  # re-running the same items reuses every row


def test_unknown_tabular_profile_is_refused_when_the_plan_is_decided(world):
    plan_id = plan.create_plan(collection_ids=[world['collection']], actor='tester',
                               temp_dir=world['tmp'] / 'work')['plan']['id']
    item = {i['locator']: i for i in plan.get_plan(plan_id, limit=500)['items']}[next(iter(PROFILE))]
    with pytest.raises(ValueError, match='tabular profile not found'):
        plan.decide(plan_id, base_version=1, actor='tester',
                    decisions=[{'item_id': item['id'], 'parameters': {'profile_id': 'no-such-profile'}}])
    plan.decide(plan_id, base_version=1, actor='tester',
                decisions=[{'item_id': item['id'], 'parameters': {'profile_id': PROFILE[next(iter(PROFILE))]}}])

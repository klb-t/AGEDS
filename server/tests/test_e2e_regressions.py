"""Regression tests for defects exposed by the end-to-end acceptance (AGEDS-TASK-20261016-Z).

1. A file-name import run (no artifact) wrote an audit row ``object_kind='artifact'``
   with a NULL id; ``metadata-verify`` then failed with ``broken_object_reference``.
2. ``import-plan-decide --approve A --approve B`` kept only the last list.
3. The time-budget method record named the interim rule ``analysis_source.v1`` while
   the items came through the timeline seam (``display_time@1``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from server.app import db as dbmod
from server.app import evidence


@pytest.fixture()
def database(tmp_path, monkeypatch):
    settings = replace(dbmod.settings, data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    for module in (dbmod, evidence):
        monkeypatch.setattr(module, 'settings', settings)
    dbmod.init_db()
    return tmp_path


def _verify_package():
    from server.app.packages import export_metadata_package, validate_metadata_package
    package = json.loads(json.dumps(export_metadata_package()))
    return validate_metadata_package(package)


def test_file_name_import_run_audit_names_no_target_and_package_verifies(database):
    from server.app.importers import recording_names
    folder = database / 'names'
    folder.mkdir()
    (folder / 'out-+48000000102-2024-01-06-18-30-00.amr').write_bytes(b'#!AMR\n')
    recording_names.observe_paths([folder])
    with dbmod.session() as db:
        rows = [dict(r) for r in db.execute("SELECT object_kind, object_id FROM audit_log WHERE action='import_run'")]
    assert rows == [{'object_kind': None, 'object_id': None}]
    result = _verify_package()
    assert result['valid'] is True, result['errors']
    assert not [w for w in result['warnings'] if w['code'] == 'audit_target_id_missing']


def test_historical_audit_rows_with_kind_but_no_id_verify_as_warnings(database):
    # Rows written before the fix are kept (audit_log is never rewritten); a real dangling id stays an error.
    with dbmod.session() as db:
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('import_run','artifact',NULL,'{}')")
    result = _verify_package()
    assert result['valid'] is True, result['errors']
    assert [w['code'] for w in result['warnings'] if w['code'] == 'audit_target_id_missing'] == ['audit_target_id_missing']
    with dbmod.session() as db:
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('import_run','artifact',987654,'{}')")
    broken = _verify_package()
    assert broken['valid'] is False
    assert [e['code'] for e in broken['errors']] == ['broken_object_reference']


def test_repeated_decision_flags_accumulate():
    from server.app.cli import _parser
    from server.app.commands.importplan import _decisions
    args = _parser().parse_args(['import-plan-decide', '1', '--base-version', '1', '--actor', 'tester',
                                 '--approve', '3', '--importer', '3=chatgpt_export', '--approve', '5', '7',
                                 '--skip', '9', '--skip', '10'])
    assert args.approve == [3, 5, 7] and args.skip == [9, 10] and args.propose == []
    decisions = {d['item_id']: d for d in _decisions(args)}
    assert {i: d['status'] for i, d in decisions.items()} == {3: 'approved', 5: 'approved', 7: 'approved',
                                                              9: 'skipped', 10: 'skipped'}
    assert decisions[3]['importer'] == 'chatgpt_export'
    # the default list is not shared between parses
    assert _parser().parse_args(['import-plan-decide', '1', '--base-version', '1', '--actor', 't']).approve == []


def test_budget_method_record_names_the_rule_that_placed_the_items(database):
    from server.app.analysis import budget, budget_report, categories, runs
    from server.app.analysis.provenance import canonical_json
    from server.app.timeline.ruleset import load_rule
    from server.tests.corpus import activity
    manifest = activity.build(database / 'activity')
    activity.populate(manifest)
    config = categories.load_config(manifest['config_path'])
    with dbmod.session() as db:
        new = budget.run_budget(db, config, window_from=manifest['window']['from'], window_to=manifest['window']['to'],
                                zone=activity.ZONE)
    rule = load_rule(None, kind='display_rule')
    method, source = new['result']['method'], new['result']['input']
    # v4 (2026-10-17, location area L1) only changes provider-inferred/derived items; v1-v3 stay in the history.
    assert method['rule'] == budget.RULE == 'time_budget.v5'
    assert {'time_budget.v1', 'time_budget.v3'} <= set(method['rule_history'])
    assert method['seam'] == source['seam'] == 'timeline'
    assert method['source_rule'] == source['source_rule'] == f'{rule.rule_id}@{rule.version}'
    assert method['source_rule_sha256'] == source['source_rule_sha256'] == rule.sha256
    assert method['interim_fallback_rule'] == 'analysis_source.v1'
    assert budget.method_record(budget.effective_params(None))['source_rule'] == 'unknown'

    # A run stored by the previous version stays retrievable with its own hash and renders.
    historical = json.loads(json.dumps(new['result']))
    historical['method']['rule'] = 'time_budget.v1'
    historical['method']['source_rule'] = 'analysis_source.v1'
    for key in ('rule_history', 'seam', 'source_rule_sha256', 'interim_fallback_rule'):
        historical['method'].pop(key)
    with dbmod.session() as db:
        stored = runs.store_run(db, kind='time_budget', parameters={'params': historical['parameters'],
                                                                    'window': historical['window']},
                                config_sha256=config.sha256, config_version=config.version, source_rule='time_budget.v1',
                                input_sha256=new['result']['labels']['labels_sha256'], label_run_id=None, result=historical)
        old = runs.get_run(db, stored['run_id'])
        view = budget_report.report_payload(db, stored['run_id'])
    assert old['result'] == historical
    assert hashlib.sha256(canonical_json(old['result']).encode('utf-8')).hexdigest() == old['result_sha256']
    assert view['run']['model_rule'] == 'time_budget.v1' and view['run']['integrity'] == 'hash_matches'
    assert view['source']['source_rule'] == 'display_time@1'
    assert {row['key']: row['value'] for row in view['method']}['source_rule'] == 'analysis_source.v1'

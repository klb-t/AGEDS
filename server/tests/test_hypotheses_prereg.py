"""Preregistration: canonical hashing, append-only versions, refusal of unregistered/altered definitions."""
import json
import sqlite3

import pytest

from server.app.analysis import eventstudy, preregistration as pr
from server.app.analysis.hypotheses import DISCLAIMER, HypothesisError, HypothesisNotFound, sha256_canonical
from server.tests.corpus import hypotheses as corpus


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


def base_definition(**changes):
    definition = {
        'title': 'Letters and health entries',
        'text': 'After a letter, health entries become more frequent.',
        'stimulus': {'event_types': ['letter']},
        'outcome': {'event_types': ['health']},
        'windows': [{'label': 'after', 'start_s': 0, 'end_s': 86400}],
    }
    definition.update(changes)
    return definition


def test_normalization_fills_defaults_and_set_order_does_not_change_hash():
    a = pr.preview(base_definition(stimulus={'event_types': ['b', 'a', 'a']}))
    b = pr.preview(base_definition(stimulus={'event_types': ['a', 'b']}))
    assert a['definition_sha256'] == b['definition_sha256']
    assert a['definition']['stimulus'] == {'event_types': ['a', 'b']}
    assert a['definition']['analysis']['permutations'] == 999 and a['definition']['analysis']['seed'] == 1
    assert a['definition']['lags'] == {'min_s': 0, 'max_s': 86400, 'bin_s': 3600}
    assert sha256_canonical(json.loads(a['canonical'])) == a['definition_sha256']
    assert a['stored'] is False
    # a different window order is a different operationalization
    two = [{'label': 'x', 'start_s': 0, 'end_s': 10}, {'label': 'y', 'start_s': 10, 'end_s': 20}]
    assert pr.preview(base_definition(windows=two))['definition_sha256'] != \
        pr.preview(base_definition(windows=two[::-1]))['definition_sha256']


@pytest.mark.parametrize('bad, message', [
    ({'unexpected': 1}, 'unknown keys'),
    ({'analysis': {'permutations': 0}}, 'permutations'),
    ({'analysis': {'permutations': True}}, 'permutations'),
    ({'analysis': {'seed': -1}}, 'seed'),
    ({'analysis': {'null_model': 'bootstrap'}}, 'null_model'),
    ({'analysis': {'assume_zone': 'Europe/Warsaw'}}, 'assumption_rule'),
    ({'analysis': {'assumption_rule': 'x'}}, 'without analysis.assume_zone'),
    ({'analysis': {'assume_zone': 'Mars/Base', 'assumption_rule': 'x'}}, 'unknown time zone'),
    ({'analysis': {'span': {'start_utc': '2025-01-01 00:00', 'end_utc': '2025-02-01T00:00:00Z'}}}, 'explicit offset'),
    ({'analysis': {'flag_threshold': float('nan')}}, 'flag_threshold'),
    ({'analysis': {'imprecise_times': 'ignore'}}, 'imprecise_times'),
    ({'stimulus': {}}, 'at least one criterion'),
    ({'stimulus': {'event_types': []}}, 'nonempty list'),
    ({'outcome': {'text_query': 'a AND ('}}, 'text_query'),
    ({'windows': [{'label': 'w', 'start_s': 5, 'end_s': 5}]}, 'start_s < end_s'),
    ({'windows': [{'label': 'w', 'start_s': 0, 'end_s': 5}, {'label': 'w', 'start_s': 5, 'end_s': 9}]}, 'repeats'),
    ({'lags': {'min_s': 0, 'max_s': 10_000, 'bin_s': 1}}, 'more than'),
    ({'direction': 'up'}, 'direction'),
    ({'title': ''}, 'title'),
    ({'outcome': {'categories': ['zdrowie']}}, 'category_config_sha256'),
    ({'category_config_sha256': 'ABC'}, 'SHA-256'),
    ({'schema': 'other/v9'}, 'schema'),
])
def test_invalid_definitions_are_refused_explicitly(bad, message):
    with pytest.raises(HypothesisError, match=message):
        pr.normalize_definition(base_definition(**bad))


def test_register_versions_chain_and_unchanged_definition(db):
    first = pr.register_hypothesis(db, base_definition(), actor='analyst', change_note='initial')
    assert first['created'] and first['version_number'] == 1 and first['previous_version_id'] is None
    assert first['integrity']['status'] == 'hash_matches'
    assert first['first_analysed_at'] is None
    same = pr.register_version(db, first['hypothesis_id'], base_definition())
    assert same['created'] is False and same['version_id'] == first['version_id']
    second = pr.register_version(db, first['hypothesis_id'], base_definition(windows=[
        {'label': 'after', 'start_s': 0, 'end_s': 2 * 86400}]), change_note='wider window')
    assert second['created'] and second['version_number'] == 2
    assert second['previous_version_id'] == first['version_id']
    assert second['previous_record_sha256'] == first['record_sha256']
    assert second['definition_sha256'] != first['definition_sha256']
    assert second['registered_at'] > first['registered_at']
    versions = pr.list_versions(db, first['hypothesis_id'])
    assert [v['version_number'] for v in versions] == [1, 2]
    assert versions[0]['definition']['windows'][0]['end_s'] == 86400  # the old version stays
    with pytest.raises(HypothesisNotFound):
        pr.register_version(db, 999, base_definition())


def test_immutability_triggers_refuse_update_and_delete(db):
    v = pr.register_hypothesis(db, base_definition())
    run = eventstudy.run_event_study(db, v['version_id'])
    statements = [
        ('UPDATE hypotheses SET actor=? WHERE id=?', ('x', v['hypothesis_id'])),
        ('DELETE FROM hypotheses WHERE id=?', (v['hypothesis_id'],)),
        ('UPDATE hypothesis_versions SET definition_json=? WHERE id=?', ('{}', v['version_id'])),
        ('DELETE FROM hypothesis_versions WHERE id=?', (v['version_id'],)),
        ('UPDATE hypothesis_runs SET result_json=? WHERE id=?', ('{}', run['run_id'])),
        ('DELETE FROM hypothesis_runs WHERE id=?', (run['run_id'],)),
    ]
    for sql, args in statements:
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute(sql, args)
        db.rollback()


def test_version_chain_trigger_refuses_forks_and_foreign_links(db):
    a = pr.register_hypothesis(db, base_definition())
    b = pr.register_hypothesis(db, base_definition(title='Other'))
    row = db.execute('SELECT * FROM hypothesis_versions WHERE id=?', (a['version_id'],)).fetchone()
    insert = '''INSERT INTO hypothesis_versions(hypothesis_id,version_number,previous_version_id,definition_json,
        definition_sha256,canonicalization,registered_at,actor,change_note,previous_record_sha256,record_sha256)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)'''
    cases = [
        (b['hypothesis_id'], 2, a['version_id'], row['record_sha256']),  # previous version of another hypothesis
        (a['hypothesis_id'], 3, a['version_id'], row['record_sha256']),  # skips a number
        (a['hypothesis_id'], 2, a['version_id'], '0' * 64),             # wrong previous record hash
    ]
    for hid, number, prev, prev_hash in cases:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, (hid, number, prev, row['definition_json'], row['definition_sha256'], row['canonicalization'],
                                row['registered_at'], 'x', None, prev_hash, '1' * 64))
        db.rollback()
    with pytest.raises(sqlite3.IntegrityError):  # version 1 cannot have a predecessor
        db.execute(insert, (a['hypothesis_id'], 1, a['version_id'], row['definition_json'], row['definition_sha256'],
                            row['canonicalization'], row['registered_at'], 'x', None, row['record_sha256'], '1' * 64))
    db.rollback()


def test_analysis_refuses_unregistered_or_altered_definitions(db):
    with pytest.raises(HypothesisNotFound):
        eventstudy.run_event_study(db, 12345)
    v = pr.register_hypothesis(db, base_definition())
    # a run row cannot name a hash that was not registered for the version, nor predate registration
    with pytest.raises(sqlite3.IntegrityError, match='registered definition hash'):
        db.execute('''INSERT INTO hypothesis_runs(hypothesis_version_id,kind,definition_sha256,parameters_json,input_sha256,
            input_count,result_json,result_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                   (v['version_id'], 'event_study', 'f' * 64, '{}', 'x', 0, '{}', 'a' * 64, '2999-01-01T00:00:00.000000Z'))
    db.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='registered before the run'):
        db.execute('''INSERT INTO hypothesis_runs(hypothesis_version_id,kind,definition_sha256,parameters_json,input_sha256,
            input_count,result_json,result_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                   (v['version_id'], 'event_study', v['definition_sha256'], '{}', 'x', 0, '{}', 'a' * 64,
                    '2000-01-01T00:00:00.000000Z'))
    db.rollback()
    # tampering below the triggers (dropped trigger) is detected by re-hashing before any analysis
    db.execute('DROP TRIGGER hypothesis_versions_no_update')
    altered = json.loads(db.execute('SELECT definition_json FROM hypothesis_versions WHERE id=?', (v['version_id'],)).fetchone()[0])
    altered['windows'][0]['end_s'] = 7 * 86400
    db.execute('UPDATE hypothesis_versions SET definition_json=? WHERE id=?',
               (json.dumps(altered, sort_keys=True, separators=(',', ':')), v['version_id']))
    db.commit()
    assert pr.get_version(db, v['version_id'])['integrity']['status'] == 'hash_mismatch'
    with pytest.raises(HypothesisError, match='no longer matches its registered hash'):
        eventstudy.run_event_study(db, v['version_id'])
    assert db.execute('SELECT COUNT(*) FROM hypothesis_runs').fetchone()[0] == 0


def test_registered_at_versus_first_analysed_at(db):
    corpus.populate(corpus.SCENARIOS['unknown_zone']())
    v = pr.register_hypothesis(db, base_definition(analysis={'permutations': 19}))
    assert pr.get_version(db, v['version_id'])['first_analysed_at'] is None
    first = eventstudy.run_event_study(db, v['version_id'])
    eventstudy.run_event_study(db, v['version_id'])
    version = pr.get_version(db, v['version_id'])
    assert version['first_analysed_at'] == first['created_at'] >= version['registered_at']
    assert version['event_study_runs'] == 2
    listing = pr.list_hypotheses(db)['hypotheses'][0]
    assert listing['registered_at'] == version['registered_at'] and listing['first_analysed_at'] == first['created_at']
    audit = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE action IN ('hypothesis_version_registered','event_study_run')")]
    assert audit == ['hypothesis_version_registered', 'event_study_run', 'event_study_run']
    assert first['result']['disclaimer'] == DISCLAIMER


def test_documented_disclaimer_hashes_match_the_code():
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'HYPOTHESES.md').read_text(encoding='utf-8')
    assert DISCLAIMER['pl'] in doc and DISCLAIMER['sha256_pl'] in doc and DISCLAIMER['sha256_en'] in doc

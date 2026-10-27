import json
from pathlib import Path

import pytest

from server.app.analysis import categories as cat

EXAMPLE = Path(__file__).resolve().parents[1] / 'app' / 'analysis' / 'data' / 'categories.example.json'


def _config(**over):
    data = {
        'schema': cat.CONFIG_SCHEMA, 'version': 't1',
        'categories': [{'id': 'praca_projekty'}, {'id': 'sprawy_urzedowe'}, {'id': 'zdrowie'}, {'id': 'inne'}],
        'default_category': 'inne', 'official_categories': ['sprawy_urzedowe'],
        'rules': [
            {'id': 'cp', 'version': 2, 'kind': 'counterparty', 'values': ['+48 000 000 101', 'Office@Example.org'],
             'weights': {'sprawy_urzedowe': 1}},
            {'id': 'dom', 'kind': 'email_domain', 'values': ['urzad.example.org'], 'weights': {'sprawy_urzedowe': 1}},
            {'id': 'kw', 'kind': 'keyword', 'values': ['recepta'], 'weights': {'zdrowie': 1}},
            {'id': 'kw-word', 'kind': 'keyword', 'values': ['plan'], 'whole_word': True, 'fields': ['subject'],
             'weights': {'praca_projekty': 0.6, 'zdrowie': 0.4}},
            {'id': 'bot', 'kind': 'chatbot', 'values': ['claude'], 'weights': {'praca_projekty': 0.7, 'inne': 0.3}},
            {'id': 'calls', 'kind': 'event_type', 'values': ['call'], 'event_types': ['call'], 'weights': {'inne': 1}},
        ]}
    data.update(over)
    return cat.parse_config(data)


def _item(eid, **kw):
    fields = {k: kw.pop(k) for k in ('sender', 'recipients', 'subject', 'body', 'contact_label', 'phone_or_address') if k in kw}
    base = {'event_id': eid, 'event_type': 'sms', 'counterparty': None, 'chatbot': None, 'tags': [], 'thread_id': None}
    base.update(kw)
    base['fields'] = fields
    return base


def test_example_config_is_generic_and_valid():
    config = cat.load_config(EXAMPLE)
    assert set(config.category_ids) >= {'praca_projekty', 'sprawy_urzedowe', 'zdrowie', 'odpoczynek', 'inne'}
    assert cat.UNOBSERVED not in config.category_ids and config.file_sha256 and config.origin == 'file'
    text = EXAMPLE.read_text()
    assert '@' not in text.replace('@example', '')


def test_hash_is_canonical_and_reserved_categories_refused():
    a = _config()
    b = cat.parse_config(json.loads(json.dumps(a.data, indent=4)))
    assert a.sha256 == b.sha256 and a.version == 't1'
    assert _config(version='t2').sha256 != a.sha256
    with pytest.raises(ValueError, match='reserved'):
        _config(categories=[{'id': 'nieobserwowane'}], default_category=None, official_categories=[], rules=[])
    with pytest.raises(ValueError, match='reserved'):
        _config(rules=[{'id': 'x', 'kind': 'tag', 'values': ['t'], 'weights': {'nieobserwowane': 1}}])
    with pytest.raises(ValueError, match=r'\(0, 1\]'):
        _config(rules=[{'id': 'x', 'kind': 'tag', 'values': ['t'], 'weights': {'inne': 1.5}}])
    with pytest.raises(ValueError, match='duplicate rule'):
        _config(rules=[{'id': 'x', 'kind': 'tag', 'values': ['t'], 'weights': {'inne': 1}}] * 2)
    with pytest.raises(ValueError, match='schema'):
        cat.parse_config({'categories': []})


def test_rules_match_with_provenance_and_weights():
    config = _config()
    items = [
        _item(1, counterparty='+48000000101'),                                   # digits match
        _item(2, sender='Someone <desk@mail.urzad.example.org>', event_type='email'),  # subdomain
        _item(3, body='Odbiór RECEPTA jutro'),                                   # case-folded keyword
        _item(4, subject='Planowanie', body=''),                                 # whole word: no match
        _item(5, subject='Nowy plan.', body=''),                                 # whole word: match, split weights
        _item(6, chatbot='Claude', event_type='chat_message'),
        _item(7, event_type='call'),
        _item(8, body='nothing'),
        _item(9, counterparty='office@example.org'),
    ]
    labels = cat.label_items(items, config)
    assert labels[1]['categories'] == {'sprawy_urzedowe': 1.0}
    assert labels[1]['provenance']['rules'][0] == {'rule_id': 'cp', 'rule_version': 2, 'kind': 'counterparty',
                                                   'weights': {'sprawy_urzedowe': 1.0}}
    assert labels[2]['provenance']['rules'][0]['rule_id'] == 'dom'
    assert labels[3]['categories'] == {'zdrowie': 1.0}
    assert labels[4]['path'] == 'default' and labels[4]['categories'] == {'inne': 1.0}
    assert labels[5]['categories'] == {'praca_projekty': 0.6, 'zdrowie': 0.4}
    assert labels[6]['categories'] == {'inne': 0.3, 'praca_projekty': 0.7}
    assert labels[7]['path'] == 'rule' and labels[8]['path'] == 'default'
    assert labels[9]['path'] == 'rule'


def test_combine_max_versus_first():
    rules = [{'id': 'a', 'kind': 'tag', 'values': ['x'], 'weights': {'zdrowie': 0.5}},
             {'id': 'b', 'kind': 'tag', 'values': ['x'], 'weights': {'zdrowie': 1, 'inne': 0.2}}]
    item = [_item(1, tags=['X'])]
    assert cat.label_items(item, _config(rules=rules))[1]['categories'] == {'inne': 0.2, 'zdrowie': 1.0}
    first = cat.label_items(item, _config(rules=rules, combine='first'))[1]
    assert first['categories'] == {'zdrowie': 0.5} and [r['rule_id'] for r in first['provenance']['rules']] == ['a']


def test_annotation_precedence_model_fallback_and_shadowed_visibility():
    config = _config()
    items = [_item(1, counterparty='+48 000 000 101'), _item(2, body='x'), _item(3, body='y')]
    labels = cat.label_items(items, config,
                             annotations={1: {'annotation_id': 9, 'categories': {'zdrowie': 1}},
                                          3: {'annotation_id': 10, 'categories': {}}},
                             model_labels={1: {'run_id': 4, 'categories': {'odpoczynek_x': 1}},
                                           2: {'run_id': 4, 'categories': {'praca_projekty': 1}},
                                           3: {'run_id': 4, 'categories': {'zdrowie': 1}}})
    assert labels[1]['path'] == 'annotation' and labels[1]['categories'] == {'zdrowie': 1.0}
    assert labels[1]['shadowed'][0]['path'] == 'rule'           # rule result kept visible
    assert labels[2]['path'] == 'model' and labels[2]['provenance'] == {'model_run_id': 4}
    assert labels[3]['path'] == 'model'                          # withdrawn correction -> automatic paths
    with pytest.raises(ValueError, match='fields=True'):
        cat.label_items([{'event_id': 1, 'event_type': 'sms'}], config)


def test_resolve_config_path_prefers_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv(cat.ENV_VAR, str(tmp_path / 'env.json'))
    assert cat.resolve_config_path(tmp_path / 'cli.json') == tmp_path / 'cli.json'
    assert cat.resolve_config_path() == tmp_path / 'env.json'
    monkeypatch.delenv(cat.ENV_VAR)
    assert cat.resolve_config_path() is None


def _db(tmp_path, monkeypatch):
    from server.app import db as dbmod, evidence
    from server.app.config import Settings
    s = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', s)
    monkeypatch.setattr(evidence, 'settings', s)
    dbmod.init_db()
    return dbmod, evidence


def test_annotations_and_label_runs_are_append_only(tmp_path, monkeypatch):
    import sqlite3
    dbmod, evidence = _db(tmp_path, monkeypatch)
    src = evidence.ensure_source('test', 'synthetic')
    e1 = evidence.add_event(source_id=src, artifact_id=None, event_type='sms', ts_start='2025-01-01T10:00:00+00:00')
    config = _config()
    with dbmod.session() as db:
        first = cat.add_annotation(db, e1, {'zdrowie': 1}, note='n1')
        second = cat.add_annotation(db, e1, None, note='withdraw')
        with pytest.raises(ValueError):
            cat.add_annotation(db, e1, {'nieobserwowane': 1})
        with pytest.raises(LookupError):
            cat.add_annotation(db, 999, {'zdrowie': 1})
        effective = cat.load_effective_annotations(db)
        assert effective[e1]['annotation_id'] == second and effective[e1]['categories'] == {}
        assert [h['annotation_id'] for h in cat.annotation_history(db, e1)] == [first, second]
        labels = cat.label_items([_item(e1, body='recepta')], config)
        run = cat.record_label_run(db, labels, config, input_sha256='x', code_version='unknown')
        again = cat.record_label_run(db, labels, config, input_sha256='x', code_version='unknown')
        assert again == {**run, 'reused': True} and run['reused'] is False
        row = db.execute('SELECT * FROM analysis_event_labels WHERE label_run_id=?', (run['label_run_id'],)).fetchone()
        assert (row['event_id'], row['category'], row['path'], row['rules']) == (e1, 'zdrowie', 'rule', 'kw@1')
        for sql in ('UPDATE analysis_label_annotations SET note="x"', 'DELETE FROM analysis_label_annotations',
                    'DELETE FROM analysis_event_labels', 'UPDATE analysis_label_runs SET event_count=0'):
            with pytest.raises(sqlite3.DatabaseError, match='append-only'):
                db.execute(sql)

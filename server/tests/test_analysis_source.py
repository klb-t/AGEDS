import pytest

from server.app import timeinfo
from server.app.analysis import source
from server.app.analysis.source import load_items, load_items_interim


def _db(tmp_path, monkeypatch):
    from server.app import db as dbmod, evidence
    from server.app.config import Settings
    s = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', s)
    monkeypatch.setattr(evidence, 'settings', s)
    dbmod.init_db()
    return dbmod, evidence


def _events(dbmod, evidence):
    src = evidence.ensure_source('test', 'synthetic')
    utc_event = evidence.add_event(source_id=src, artifact_id=None, event_type='sms', ts_start='2025-01-01T10:00:00+00:00', body='abc')
    local_event = evidence.add_event(source_id=src, artifact_id=None, event_type='whatsapp', ts_start='2025-01-01 11:00', body='x')
    chat = evidence.add_event(source_id=src, artifact_id=None, event_type='chat_message', body='hello',
                              metadata={'chat': {'chatbot': 'claude', 'role': 'assistant', 'content_kind': 'text'}})
    part = evidence.add_event(source_id=src, artifact_id=None, event_type='chat_message_part', body='tool',
                              metadata={'chat': {'chatbot': 'claude', 'content_kind': 'tool_call'}})
    with dbmod.session() as db:
        timeinfo.record_event_times(db, chat, [timeinfo.from_epoch('created', 1735725600, 'test')])
        timeinfo.record_event_times(db, part, [timeinfo.from_epoch('start', 1735725601, 'test')])
    return utc_event, local_event, chat, part


def test_timeline_seam_keeps_contract_and_reports_unknown_zone_and_hidden(tmp_path, monkeypatch):
    dbmod, evidence = _db(tmp_path, monkeypatch)
    utc_event, local_event, chat, part = _events(dbmod, evidence)
    with dbmod.session() as db:
        result = load_items(db, fields=True)
    assert result['seam'] == 'timeline' and result['rule'].startswith('display_time@') and len(result['rule_sha256']) == 64
    ids = [i['event_id'] for i in result['items']]
    assert ids == [utc_event, chat]
    item = result['items'][1]
    assert item['chatbot'] == 'claude' and item['time_basis']['tz_status'] == 'utc_epoch'
    assert item['chat']['role'] == 'assistant' and item['fields']['body'] == 'hello' and item['t_lo'] == item['t_hi']
    assert [(u['event_id'], u['reason']) for u in result['untimed']] == [(local_event, 'unknown_zone')]
    assert result['hidden_excluded'] == [{'event_id': part, 'event_type': 'chat_message_part'}]
    with dbmod.session() as db:
        with pytest.raises(ValueError, match='assumption_rule'):
            load_items(db, assume_zone='Europe/Amsterdam')
        assumed = load_items(db, assume_zone='Europe/Amsterdam', assumption_rule='test-zone', include_hidden=True)
    placed = {i['event_id']: i for i in assumed['items']}
    assert placed[local_event]['time_basis']['assumed_zone'] == 'Europe/Amsterdam'
    assert placed[local_event]['time_basis']['assumption_rule'] == 'test-zone'
    assert placed[local_event]['t_start'] == placed[utc_event]['t_start']  # 11:00 CET == 10:00 UTC
    assert placed[local_event]['t_hi'] - placed[local_event]['t_lo'] == pytest.approx(60, abs=1e-5)  # minute precision
    assert placed[part]['hidden'] is True


def test_interim_fallback_is_labelled(tmp_path, monkeypatch):
    dbmod, evidence = _db(tmp_path, monkeypatch)
    utc_event, local_event, chat, part = _events(dbmod, evidence)
    with dbmod.session() as db:
        result = load_items_interim(db)
    assert result['seam'] == 'interim_fallback' and result['rule'] == source.RULE_ID
    assert [i['event_id'] for i in result['items']] == [utc_event, chat, part]
    assert result['untimed'] == [{'event_id': local_event, 'event_type': 'whatsapp', 'reason': 'unknown_zone'}]
    with dbmod.session() as db:
        assumed = load_items_interim(db, assume_zone='Europe/Amsterdam')
    placed = {i['event_id']: i for i in assumed['items']}
    assert placed[local_event]['time_basis']['assumed_zone'] == 'Europe/Amsterdam'
    assert placed[local_event]['t_start'] == placed[utc_event]['t_start']


def test_event_item_uses_projection(tmp_path, monkeypatch):
    dbmod, evidence = _db(tmp_path, monkeypatch)
    utc_event, local_event, chat, part = _events(dbmod, evidence)
    with dbmod.session() as db:
        placed = source.event_item(db, utc_event)
        unknown = source.event_item(db, local_event)
        with pytest.raises(LookupError):
            source.event_item(db, 999)
    assert placed['seam'] == 'timeline' and placed['t_start'] == 1735725600 and placed['untimed_reason'] is None
    assert unknown['t_start'] is None and unknown['untimed_reason'] == 'unknown_zone'


def test_dst_fold_keeps_both_instants_in_both_seams(tmp_path, monkeypatch):
    """Regression (review U3): an ambiguous fold is carried with a flag and both instants, never resolved."""
    dbmod, evidence = _db(tmp_path, monkeypatch)
    src = evidence.ensure_source('test', 'synthetic')
    stored = evidence.add_event(source_id=src, artifact_id=None, event_type='sms', body='importer-assumed fold')
    local = evidence.add_event(source_id=src, artifact_id=None, event_type='whatsapp_message', ts_start='2025-10-26 02:30',
                               body='wall clock in the repeated hour')
    folded = timeinfo.assume_zone(timeinfo.from_iso('received', '2025-10-26 02:15', 'test'), 'Europe/Warsaw', rule='importer-test')
    assert folded.metadata['dst'] == 'ambiguous_fold'
    with dbmod.session() as db:
        timeinfo.record_event_times(db, stored, [folded])
    first, second = 1761437700.0, 1761441300.0  # 00:15Z and 01:15Z
    with dbmod.session() as db:
        interim = {i['event_id']: i for i in load_items_interim(db, assume_zone='Europe/Warsaw')['items']}
        timeline = {i['event_id']: i for i in load_items(db, assume_zone='Europe/Warsaw', assumption_rule='op-test')['items']}
    for seam in (interim, timeline):
        item = seam[stored]
        assert item['time_basis']['dst'] == 'ambiguous_fold'
        assert len(item['time_basis']['utc_alternatives']) == 2
        assert item['t_lo'] == first and second <= item['t_hi'] < second + 60  # timeline adds minute precision
        operator = seam[local]
        assert operator['time_basis']['dst'] == 'ambiguous_fold' and len(operator['time_basis']['utc_alternatives']) == 2
        assert 3600 <= operator['t_hi'] - operator['t_lo'] < 3660

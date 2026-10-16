from server.app import timeinfo
from server.app.analysis.source import load_items


def _db(tmp_path, monkeypatch):
    from server.app import db as dbmod, evidence
    from server.app.config import Settings
    s = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', s)
    monkeypatch.setattr(evidence, 'settings', s)
    dbmod.init_db()
    return dbmod, evidence


def test_items_use_utc_candidates_and_exclude_unknown_zone(tmp_path, monkeypatch):
    dbmod, evidence = _db(tmp_path, monkeypatch)
    src = evidence.ensure_source('test', 'synthetic')
    utc_event = evidence.add_event(source_id=src, artifact_id=None, event_type='sms', ts_start='2025-01-01T10:00:00+00:00', body='abc')
    local_event = evidence.add_event(source_id=src, artifact_id=None, event_type='whatsapp', ts_start='2025-01-01 11:00', body='x')
    chat = evidence.add_event(source_id=src, artifact_id=None, event_type='chat_message', body='hello', metadata={'chat': {'chatbot': 'claude'}})
    with dbmod.session() as db:
        timeinfo.record_event_times(db, chat, [timeinfo.from_epoch('created', 1735725600, 'test')])
    with dbmod.session() as db:
        result = load_items(db)
    ids = [i['event_id'] for i in result['items']]
    assert ids == [utc_event, chat]
    assert result['items'][1]['chatbot'] == 'claude' and result['items'][1]['time_basis']['tz_status'] == 'utc_epoch'
    assert result['untimed'] == [{'event_id': local_event, 'event_type': 'whatsapp', 'reason': 'unknown_zone'}]
    with dbmod.session() as db:
        assumed = load_items(db, assume_zone='Europe/Amsterdam')
    placed = {i['event_id']: i for i in assumed['items']}
    assert placed[local_event]['time_basis']['assumed_zone'] == 'Europe/Amsterdam'
    assert placed[local_event]['t_start'] == placed[utc_event]['t_start']  # 11:00 CET == 10:00 UTC

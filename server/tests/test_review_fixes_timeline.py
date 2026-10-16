"""Review finding #7: projection cache keyed by effective zone parameters; bounded number of projections."""
from __future__ import annotations

import pytest

from server.app import evidence
from server.app.timeline import projection as projection_module
from server.tests.test_review_fixes_support import env  # noqa: F401 (fixture)


def _events(count: int = 50) -> None:
    source = evidence.ensure_source('synthetic', 'zones')
    for i in range(count):
        evidence.add_event(source_id=source, artifact_id=None, event_type='whatsapp_message',
                           ts_start=f'2024-01-01T10:{i % 60:02d}:00', body='x', external_id=str(i))


def _counts(env) -> dict:
    return {'projections': env.one('SELECT COUNT(*) FROM timeline_projections'),
            'items': env.one('SELECT COUNT(*) FROM timeline_items'),
            'builds': env.one('SELECT COUNT(*) FROM timeline_projection_builds')}


def test_finding7_free_text_labels_do_not_create_projections(env):
    _events()
    with env.client() as http:
        for i in range(5):
            label = f'typo-{i}'
            density = http.get('/api/timeline/density', params={'assume_zone': 'Europe/Amsterdam', 'assumption_rule': label})
            assert density.status_code == 200, density.text
            assert density.json()['params']['assumption_rule'] == label
            page = http.get('/api/timeline/window', params={'assume_zone': 'Europe/Amsterdam', 'assumption_rule': label,
                                                            'limit': 3}).json()
            assert page['params'] == {'assume_zone': 'Europe/Amsterdam', 'assumption_rule': label}
            flags = page['items'][0]['time']['flags']
            assert flags['assumed_zone'] == 'Europe/Amsterdam' and flags['assumption_rule'] == label
            assert page['items'][0]['time']['basis'] == 'assumed_zone_operator'
            assert any(c['placement'] and c['placement']['flags'].get('assumption_rule') == label
                       for c in page['items'][0]['candidates'])
    assert _counts(env) == {'projections': 1, 'items': 50, 'builds': 1}
    stored = env.rows('SELECT params_json FROM timeline_projections')[0]['params_json']
    assert 'typo' not in stored and 'Europe/Amsterdam' in stored
    assert not env.rows("SELECT 1 FROM timeline_items WHERE flags_json LIKE '%typo%'")


def test_finding7_analysis_items_report_the_requested_label(env):
    from server.app.db import session
    from server.app.timeline.analysis_items import load_items
    _events(3)
    with session() as db:
        first = load_items(db, assume_zone='Europe/Amsterdam', assumption_rule='label-a')
        second = load_items(db, assume_zone='Europe/Amsterdam', assumption_rule='label-b')
    assert {i['time_basis']['assumption_rule'] for i in first['items']} == {'label-a'}
    assert {i['time_basis']['assumption_rule'] for i in second['items']} == {'label-b'}
    assert first['projection']['id'] == second['projection']['id']


def test_finding7_non_default_projections_are_capped_and_evicted(env, monkeypatch):
    monkeypatch.setenv('EW_TIMELINE_MAX_PROJECTIONS', '2')
    _events(10)
    zones = ['Europe/Amsterdam', 'Europe/Warsaw', 'America/New_York', 'Asia/Tokyo']
    with env.client() as http:
        assert http.get('/api/timeline/window').status_code == 200  # the default projection is never evicted
        for zone in zones:
            assert http.get('/api/timeline/window', params={'assume_zone': zone, 'assumption_rule': 'operator'}).status_code == 200
        materialized = env.rows('SELECT DISTINCT projection_id FROM timeline_items')
        assert len(materialized) == 3  # default + the two most recent zone projections
        evicted = env.rows('SELECT projection_id, items_deleted, reason FROM timeline_projection_evictions ORDER BY id')
        assert len(evicted) == 2 and all(e['items_deleted'] == 10 for e in evicted)
        again = http.get('/api/timeline/window', params={'assume_zone': zones[0], 'assumption_rule': 'operator'}).json()
        assert len(again['items']) == 10 and again['projection']['build_mode'] == 'full'
    assert len(env.rows('SELECT DISTINCT projection_id FROM timeline_items')) == 3
    assert env.one('SELECT COUNT(*) FROM timeline_items') == 30


def test_finding7_builds_from_older_code_are_rebuilt(env):
    import json
    _events(3)
    with env.client() as http:
        first = http.get('/api/timeline/window').json()['projection']
        from server.app.db import session
        with session() as db:  # a build log row as written by projection code 1 (no projection_code)
            last = db.execute('SELECT * FROM timeline_projection_builds ORDER BY id DESC LIMIT 1').fetchone()
            marks = json.loads(last['watermarks_json'])
            marks.pop('projection_code')
            db.execute('''INSERT INTO timeline_projection_builds(projection_id,mode,started_at,finished_at,duration_ms,
                          watermarks_json,counts_json) VALUES(?,?,?,?,?,?,?)''',
                       (first['id'], 'full', last['started_at'], last['finished_at'], 0, json.dumps(marks), '{}'))
        again = http.get('/api/timeline/window').json()['projection']
    assert again['status'] == 'refreshed' and again['build_mode'] == 'full'
    assert projection_module.PROJECTION_CODE >= 2


@pytest.mark.parametrize('value', ['0', 'many'])
def test_finding7_cap_setting_is_validated(env, monkeypatch, value):
    monkeypatch.setenv('EW_TIMELINE_MAX_PROJECTIONS', value)
    _events(1)
    with env.client() as http:
        assert http.get('/api/timeline/window', params={'assume_zone': 'Europe/Warsaw', 'assumption_rule': 'x'}).status_code == 200
    assert env.one('SELECT COUNT(*) FROM timeline_projection_evictions') == 0  # invalid → default cap (8)

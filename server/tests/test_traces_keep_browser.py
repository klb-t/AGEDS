"""Google Keep notes and local browser profiles (traces area)."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.config import settings
from server.app.importers import browser_profiles, google_keep
from server.tests.corpus import traces as corpus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_settings = replace(settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store')
    test_settings.store_dir.mkdir(parents=True)
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    manifest = corpus.build(tmp_path / 'corpus')
    return {'keep': manifest['keep'], 'browsers': manifest['browsers'], 'root': tmp_path / 'corpus'}


def rows(sql, params=()):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


def tree(root: Path) -> dict:
    return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in sorted(root.rglob('*'))
            if p.is_file()}


def test_keep_folder(env):
    keep = env['keep']
    result = google_keep.import_keep(Path(keep['root']))
    assert (result['notes'], result['json_not_note'], result['html_variant_not_parsed'], result['trashed']) == (4, 1, 1, 1)
    notes = {e['subject']: e for e in rows("SELECT * FROM events WHERE event_type='note'")}
    shopping = notes['Zakupy']
    assert shopping['body'] == '[x] mleko\n[ ] chleb 🍞' and shopping['ts_start'] == '2020-09-13T12:26:40Z'
    meta = json.loads(shopping['metadata_json'])
    assert meta['note']['labels'] == ['dom'] and meta['note']['checked_items'] == 1
    assert meta['note']['sharees'] == {'count': 2, 'owner_listed': True, 'types': ['WRITER']}
    assert 'ala@example.org' not in shopping['metadata_json']
    times = {t['kind']: t for t in rows('SELECT * FROM event_times WHERE event_id=?', (shopping['id'],))}
    assert times['updated']['utc'] == '2020-09-13T12:35:00Z' and times['created']['precision'] == 'microsecond'
    idea = notes['Pomysł']
    refs = rows('SELECT status, reference_key FROM event_media_references WHERE event_id=? ORDER BY id', (idea['id'],))
    assert [(r['status'], r['reference_key']) for r in refs] == [('present', 'abc.png'), ('missing_from_export', 'missing.jpg')]
    assert json.loads(idea['metadata_json'])['note']['archived'] is True
    wrong = notes['ms in a Usec field']
    issues = json.loads(wrong['metadata_json'])['issues']
    assert 'time_before_service_start:createdTimestampUsec' in issues and 'time_unparsed:userEditedTimestampUsec' in issues
    trashed = [e for e in notes.values() if json.loads(e['metadata_json'])['note']['trashed']]
    assert len(trashed) == 1 and trashed[0]['body'] == 'usunięta'
    before = rows('SELECT COUNT(*) AS n FROM events')[0]['n']
    google_keep.import_keep(Path(keep['root']))
    assert rows('SELECT COUNT(*) AS n FROM events')[0]['n'] == before
    with pytest.raises(ValueError, match='Keep note keys'):
        google_keep.import_keep(Path(keep['not_note']))


def test_browser_profile_requires_opt_in_and_acquires_nothing(env):
    with pytest.raises(PermissionError, match='enable_browser_history'):
        browser_profiles.import_profile(Path(env['browsers']['chrome_dir']))
    assert rows('SELECT COUNT(*) AS n FROM artifacts')[0]['n'] == 0


def test_chromium_profile_with_wal(env):
    b = env['browsers']
    before = tree(env['root'] / 'browsers')
    result = browser_profiles.import_profile(Path(b['chrome_dir']), enable_browser_history=True)
    after = tree(env['root'] / 'browsers')
    assert after == before  # no byte, mtime, -shm or checkpoint touched the original profile
    assert not os.path.exists(b['chrome_history'] + '-shm')
    assert result['browser'] == 'chromium' and result['reading_key'].startswith('wal:') and result['wal_artifact_id']
    assert result['visits'] == 10  # visit 10 exists only in the WAL
    visits = {r['visit_id']: r for r in rows('SELECT * FROM trace_browser_visits ORDER BY visit_id')}
    assert visits[1]['visit_utc'] == '2021-03-01T10:00:00Z' and visits[1]['epoch'] == 'webkit_1601_us'
    assert visits[2]['title'] == 'Zażółć 😀' and visits[4]['url_truncated'] == 1 and len(visits[4]['url']) == 4096
    assert visits[6]['url'] is None
    assert (visits[7]['time_status'], visits[8]['time_status']) == ('time_zero_placeholder', 'time_out_of_range')
    assert visits[9]['time_status'] == 'time_out_of_range'  # unix µs read as WebKit µs lands in 1652: not guessed
    assert result['time_out_of_range'] == 2 and visits[9]['visit_time_raw'] == 1_614_592_800_000_000
    sessions = rows("SELECT * FROM events WHERE event_type='browser_session' ORDER BY ts_start")
    assert [json.loads(s['metadata_json'])['session']['visits'] for s in sessions[-2:]] == [3, 3]
    first = [s for s in sessions if s['ts_start'] == '2021-03-01T10:00:00Z'][0]
    meta = json.loads(first['metadata_json'])
    assert first['ts_end'] == '2021-03-01T10:20:00Z' and meta['session']['rule']['gap_seconds'] == 1800
    assert meta['session']['top_domains'][0] == ['example.org', 2] and meta['signal']['basis'] == 'derived'
    assert first['body'] is None
    child = rows('SELECT parent_artifact_id FROM artifacts WHERE id=?', (result['wal_artifact_id'],))[0]
    assert child['parent_artifact_id'] == result['artifact_id']
    again = browser_profiles.import_profile(Path(b['chrome_dir']), enable_browser_history=True)
    assert rows('SELECT COUNT(*) AS n FROM trace_browser_visits')[0]['n'] == 10 and again['sessions'] == result['sessions']
    wider = browser_profiles.import_profile(Path(b['chrome_dir']), enable_browser_history=True, session_gap_minutes=180)
    assert wider['sessions'] < result['sessions']


def test_immutable_copy_and_firefox(env):
    b = env['browsers']
    nowal = browser_profiles.import_profile(Path(b['chrome_nowal']), enable_browser_history=True)
    assert nowal['reading_key'] == 'immutable' and nowal['visits'] == 10 and nowal['wal_artifact_id'] is None
    firefox = browser_profiles.import_profile(Path(b['firefox_dir']), enable_browser_history=True)
    assert firefox['browser'] == 'firefox' and firefox['visits'] == 4
    visits = {r['visit_id']: r for r in rows("SELECT * FROM trace_browser_visits WHERE browser='firefox'")}
    assert visits[1]['visit_utc'] == '2021-03-01T10:00:00Z' and visits[1]['epoch'] == 'prtime_1970_us'
    assert visits[4]['visit_utc'].startswith('1970-01-19')
    assert firefox['transition_embed'] == 1 and firefox['time_before_service_start'] == 1
    sessions = rows("SELECT * FROM events WHERE event_type='browser_session' AND contact_label='firefox'")
    assert sum(json.loads(s['metadata_json'])['session']['visits'] for s in sessions) == 3  # the embed is not a session visit
    with pytest.raises(ValueError, match='not a Chromium History'):
        browser_profiles.import_profile(Path(b['other_sqlite']), enable_browser_history=True)

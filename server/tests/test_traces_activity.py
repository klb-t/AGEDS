"""Account security/login activity (traces area)."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.config import settings
from server.app.importers import account_activity, instagram
from server.tests.corpus import traces as corpus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_settings = replace(settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store')
    test_settings.store_dir.mkdir(parents=True)
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    return corpus.build(tmp_path / 'corpus')['account_activity']


def rows(sql, params=()):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


def events(artifact_id):
    out = rows("SELECT * FROM events WHERE artifact_id=? AND event_type='account_activity' ORDER BY id", (artifact_id,))
    for e in out:
        e['meta'] = json.loads(e['metadata_json'])
    return out


def test_facebook_account_activity_without_location(env):
    result = account_activity.import_account_activity(Path(env['fb_activity']))
    assert (result['provider'], result['record_kind'], result['events']) == ('facebook', 'account_activity', 3)
    assert result['coverage']['complete'] is False and result['issues']['by_code']['record_not_an_object'] == 1
    first, zero, bad = events(result['artifact_id'])
    activity = first['meta']['account_activity']
    assert first['contact_label'] == 'facebook' and first['subject'] == 'Login' and first['ts_start'] == '2020-09-13T12:26:40Z'
    assert activity['ip'] == {'raw': '192.0.2.10', 'status': 'parsed', 'version': 4, 'is_private': True,
                              'is_global': False, 'geolocation': 'not_performed'}
    assert activity['cookie']['value'] == 'not_stored' and len(activity['cookie']['sha256_prefix']) == 16
    assert activity['provider_location'] == {'present': True, 'fields': ['city', 'country', 'region'],
                                             'values': 'trace_provider_locations (enable_location only)'}
    blob = json.dumps(rows('SELECT metadata_json, body FROM events'), ensure_ascii=False)
    assert 'SYNTHETICcookieVALUE' not in blob and 'Łódź' not in blob and corpus.mojibake('Łódź') not in blob
    assert first['body'] is None
    assert not rows("SELECT 1 FROM search_fts WHERE search_fts MATCH '\"192.0.2.10\"'")
    assert first['meta']['signal']['class'] == 'passive'
    assert 'epoch_zero_placeholder:timestamp' in zero['meta']['issues'] and zero['ts_start'] is None
    assert zero['meta']['account_activity']['ip']['version'] == 6
    assert 'ip_address_not_parsed' in bad['meta']['issues']
    assert rows('SELECT COUNT(*) AS n FROM trace_provider_locations')[0]['n'] == 0


def test_location_opt_in_adds_values_without_changing_events(env):
    first = account_activity.import_account_activity(Path(env['fb_activity']))
    before = rows('SELECT id, metadata_json FROM events ORDER BY id')
    again = account_activity.import_account_activity(Path(env['fb_activity']), enable_location=True)
    assert again['provider_location_stored'] == 1 and rows('SELECT id, metadata_json FROM events ORDER BY id') == before
    stored = rows('SELECT * FROM trace_provider_locations')
    assert len(stored) == 1 and json.loads(stored[0]['values_json'])['city'] == 'Łódź'
    assert stored[0]['basis'] == 'provider_inference'
    account_activity.import_account_activity(Path(env['fb_activity']), enable_location=True)
    assert len(rows('SELECT * FROM trace_provider_locations')) == 1
    assert first['artifact_id'] == again['artifact_id']


def test_sessions_label_values_instagram_and_google_csv(env):
    sessions = account_activity.import_account_activity(Path(env['fb_sessions']))
    session = events(sessions['artifact_id'])[0]
    kinds = {t['kind']: t['utc'] for t in rows('SELECT * FROM event_times WHERE event_id=?', (session['id'],))}
    assert kinds == {'start': '2020-09-01T22:40:00Z', 'updated': '2020-09-13T14:56:40Z'}
    assert session['meta']['account_activity']['device'] == 'Synthetic Phone'
    labels = account_activity.import_account_activity(Path(env['fb_label_values']))
    record = events(labels['artifact_id'])[0]
    assert labels['record_kind'] == 'recognized_device' and record['meta']['account_activity']['device'] == 'Firefox on Linux'
    assert record['meta']['account_activity']['ip']['raw'] == '198.51.100.7'
    assert 'abc' not in json.dumps(record['meta']['raw'])
    login = instagram.import_instagram_activity(Path(env['ig_login']))
    ig = events(login['artifact_id'])[0]
    assert (login['provider'], login['record_kind']) == ('instagram', 'login')
    assert ig['ts_start'] == '2023-01-01T10:00:00Z' and ig['meta']['account_activity']['language'] == 'pl'
    assert 'SYNTHETICIGCOOKIE' not in ig['metadata_json']
    old = account_activity.import_account_activity(Path(env['ig_old']))
    assert events(old['artifact_id'])[0]['ts_start'] == '2019-05-01T06:00:00Z'
    google = account_activity.import_account_activity(Path(env['google_activities']))
    assert (google['provider'], google['variant'], google['events']) == ('google', 'google_access_log_activities_v1', 3)
    rows_ = events(google['artifact_id'])
    assert rows_[0]['ts_start'] == '2023-01-01T10:00:00Z' and rows_[0]['meta']['account_activity']['app_or_site'] == 'Gmail'
    zone_less = rows('SELECT * FROM event_times WHERE event_id=?', (rows_[1]['id'],))[0]
    assert zone_less['tz_status'] == 'unknown' and rows_[1]['ts_start'] is None
    assert 'time_unparsed:Activity Timestamp' in rows_[2]['meta']['issues']
    devices = account_activity.import_account_activity(Path(env['google_devices']))
    device = events(devices['artifact_id'])[0]
    assert device['meta']['account_activity']['device'] == 'Synthetic Model 1'
    assert device['meta']['account_activity']['provider_location']['fields'] == ['Device Last Location']
    assert {t['kind'] for t in rows('SELECT kind FROM event_times WHERE event_id=?', (device['id'],))} == {'start', 'updated'}
    with pytest.raises(ValueError, match='not a known account activity'):
        account_activity.import_account_activity(Path(env['not_activity']))

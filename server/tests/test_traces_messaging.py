"""Messaging/social downloads with chat_message conventions (traces area)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.config import settings
from server.app.importers import facebook, instagram
from server.app.records.report import dedup_report
from server.app.traces.common import decode_latin1_utf8
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
    return {'root': tmp_path / 'corpus', 'm': manifest}


def rows(sql, params=()):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


def messages(platform):
    out = rows("SELECT * FROM events WHERE contact_label=? ORDER BY id", (platform,))
    for event in out:
        event['meta'] = json.loads(event['metadata_json'])
    return out


def hashes(root: Path) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}


def counts():
    return {t: rows(f'SELECT COUNT(*) AS n FROM {t}')[0]['n'] for t in
            ('artifacts', 'events', 'event_times', 'logical_records', 'logical_occurrences', 'event_media_references',
             'record_exports', 'record_export_parses')}


def test_mojibake_rule_statuses():
    assert decode_latin1_utf8(corpus.mojibake('Zażółć 😀')) == ('Zażółć 😀', 'decoded')
    assert decode_latin1_utf8('plain') == ('plain', 'unchanged_ascii')
    assert decode_latin1_utf8('arrow →')[1] == 'not_applicable_codepoint_above_ff'
    assert decode_latin1_utf8('bad Ã end')[1] == 'not_applicable_invalid_utf8'
    assert decode_latin1_utf8('café')[1] == 'not_applicable_invalid_utf8'  # proper Latin-1 text is left alone


def test_facebook_folder_import_layers_media_and_directions(env):
    m = env['m']['facebook']
    before = hashes(env['root'])
    result = facebook.import_messages(Path(m['root']), owner_name=m['owner'])
    assert hashes(env['root']) == before
    assert result['platform'] == 'facebook_messenger' and len(result['files']) == 3
    events = messages('facebook_messenger')
    by_body = {e['body']: e for e in events}
    first = by_body[m['first_text']]
    assert first['sender'] == m['friend'] and first['direction'] == 'incoming'
    assert first['thread_id'] == 'facebook:inbox/alaprzykladowa_10abc'
    layer = first['meta']['text']
    assert layer['rule'] == 'fb_latin1_utf8_v1' and layer['fields']['text']['status'] == 'decoded'
    assert layer['fields']['text']['raw'] == corpus.mojibake(m['first_text'])  # the exported string is kept
    assert first['meta']['chat']['reactions'] == [{'reaction': '😆', 'actor': m['owner'], 'timestamp_raw': None}]
    assert first['meta']['signal'] == {'class': 'active', 'basis': 'raw_sample', 'provider': 'meta_facebook',
                                       'device_or_account': 'account'}
    times = rows('SELECT * FROM event_times WHERE event_id=?', (first['id'],))
    assert [(t['kind'], t['utc'], t['tz_status'], t['precision']) for t in times] == [
        ('created', '2020-09-13T12:27:40Z', 'utc_epoch', 'millisecond')]
    assert first['ts_start'] == '2020-09-13T12:27:40Z'
    # proper text above U+00FF and invalid sequences stay as exported
    arrow = by_body['already proper → arrow']
    assert arrow['meta']['text']['fields']['text']['status'] == 'not_applicable_codepoint_above_ff'
    assert by_body['bad Ã sequence']['meta']['text']['fields']['text']['status'] == 'not_applicable_invalid_utf8'
    surrogate = [e for e in events if e['body'] and e['body'].endswith('�')]
    assert len(surrogate) == 1 and surrogate[0]['meta']['text']['fields']['text'] == {
        'status': 'not_applicable_codepoint_above_ff', 'lone_surrogates_replaced': True,
        'raw_sha256': surrogate[0]['meta']['text']['fields']['text']['raw_sha256']}
    # duplicates keep separate logical records; seconds written as milliseconds are flagged
    twice = [e for e in events if e['body'] == 'twice']
    assert len(twice) == 2 and twice[1]['meta']['chat']['record_id'].endswith('~dup1')
    assert 'time_before_service_start' in by_body['seconds written as ms']['meta']['issues']
    assert not rows('SELECT 1 FROM event_times WHERE event_id=?', (by_body['unparsed time']['id'],))
    assert by_body['unknown type']['meta']['issues'] == ['message_type_unmapped']
    kinds = {e['meta']['chat']['content_kind'] for e in events}
    assert {'text', 'media', 'share', 'unsent', 'call', 'service'} <= kinds
    owner_photo = [e for e in events if e['meta']['chat']['content_kind'] == 'media' and e['direction'] == 'outgoing']
    refs = rows('SELECT * FROM event_media_references WHERE event_id=? ORDER BY id', (owner_photo[0]['id'],))
    assert [r['status'] for r in refs] == ['present', 'missing_from_export']
    child = rows('SELECT * FROM artifacts WHERE id=?', (refs[0]['artifact_id'],))[0]
    assert child['parent_artifact_id'] == owner_photo[0]['artifact_id']
    assert child['sha256'] == hashlib.sha256(Path(m['photo']).read_bytes()).hexdigest()
    issues = result['files'][0]['issues']['by_code'] if result['files'][0]['file_name'] == 'message_1.json' else {}
    group = [e for e in events if e['thread_id'] == 'facebook:archived_threads/grupa_20def']
    assert len(group) == 2 and group[0]['meta']['chat']['thread_id_basis'] == 'export_directory_names'
    parse = [f for f in result['files'] if f['locator'] == m['message_1']][0]
    assert parse['coverage'] == 'partial' and parse['issues']['by_code']['message_not_an_object'] == 1


def test_reimport_idempotent_and_cross_export_dedup(env, tmp_path):
    m = env['m']['facebook']
    facebook.import_messages(Path(m['root']))
    first = counts()
    facebook.import_messages(Path(m['root']))
    assert counts() == {**first, 'record_export_parses': first['record_export_parses']}
    # a second download of the same thread under another export label: same logical records
    second = facebook.import_messages(Path(m['message_2']), export_label='download-2024-02')
    assert second['export']['label'] == 'download-2024-02'
    report = dedup_report(namespace='facebook')
    assert report['schema'].startswith('ageds.logical_dedup_report')
    shared = rows('''SELECT r.id, COUNT(DISTINCT o.export_id) AS exports FROM logical_records r
                     JOIN logical_occurrences o ON o.logical_record_id=r.id
                     WHERE r.namespace='facebook' AND r.record_kind='message' GROUP BY r.id HAVING exports > 1''')
    assert len(shared) == 1  # the one message of message_2.json, observed in both exports


def test_e2ee_secure_storage_is_not_decoded_and_instagram_lane(env):
    m = env['m']['facebook']
    result = facebook.import_messages(Path(m['e2ee']), owner_name=m['owner'])
    events = messages('facebook_messenger')
    assert events[0]['body'] == 'Zażółć — proper UTF-8, never decoded'
    assert events[0]['meta']['text']['rule'] is None and events[0]['meta']['chat']['variant'] == 'e2ee_secure_storage'
    media = [e for e in events if e['meta']['chat']['content_kind'] == 'media'][0]
    assert media['direction'] == 'outgoing'
    assert rows('SELECT status FROM event_media_references WHERE event_id=?', (media['id'],))[0]['status'] == 'not_extracted'
    assert result['files'][0]['coverage'] == 'complete'
    insta = instagram.import_instagram_messages(Path(m['instagram_root']))
    events = messages('instagram')
    assert insta['namespace'] == 'instagram' and events[0]['body'] == 'Hej 👋 co słychać?'
    assert events[0]['meta']['signal']['provider'] == 'meta_instagram'
    assert events[0]['meta']['chat']['reactions'][0]['reaction'] == '❤️'
    assert events[0]['direction'] is None and events[0]['meta']['chat']['direction_basis'] is None


def test_not_a_message_file_is_reported(env, tmp_path):
    bogus = tmp_path / 'message_1.json'
    bogus.write_text('{"something": []}')
    result = facebook.import_messages(bogus)
    assert result['files'][0]['coverage'] == 'failed'
    assert result['files'][0]['issues']['by_code'] == {'file_not_parsed': 1}


def test_telegram_full_export(env):
    from server.app.importers import telegram
    m = env['m']['telegram']
    before = hashes(env['root'])
    result = telegram.import_result(Path(m['root']))
    assert hashes(env['root']) == before
    events = messages('telegram')
    assert {e['thread_id'] for e in events} == {'telegram:6000001', 'telegram:7000001', 'telegram:8000001'}
    by_id = {(e['thread_id'], e['meta']['chat']['message_id']): e for e in events}
    first = by_id[('telegram:6000001', 10)]
    assert first['body'] == 'Cześć 😀 שלום' and first['direction'] == 'incoming'
    assert first['meta']['chat']['direction_basis'] == 'export_personal_information'
    times = {t['source'].rsplit(':', 1)[1]: t for t in rows('SELECT * FROM event_times WHERE event_id=?', (first['id'],))}
    assert times['date_unixtime']['utc'] == '2021-03-01T10:00:00Z'
    assert times['date']['tz_status'] == 'unknown' and times['date']['local'] == '2021-03-01T11:00:00'
    assert first['ts_start'] == '2021-03-01T10:00:00Z'
    edited = by_id[('telegram:6000001', 11)]
    assert edited['body'] == 'See https://example.org and bold' and edited['direction'] == 'outgoing'
    assert edited['meta']['chat']['reply_to'] == {'message_id': 10, 'peer_id': None}
    assert [e['type'] for e in edited['meta']['chat']['text_entities']] == ['link', 'bold']
    assert edited['meta']['signal']['basis'] == 'provider_user_edit'
    assert {t['kind'] for t in rows('SELECT kind FROM event_times WHERE event_id=?', (edited['id'],))} == {'created', 'updated'}
    photo = rows('SELECT * FROM event_media_references WHERE event_id=?', (by_id[('telegram:6000001', 12)]['id'],))
    assert [r['status'] for r in photo] == ['present']
    voice = rows('SELECT * FROM event_media_references WHERE event_id=?', (by_id[('telegram:6000001', 13)]['id'],))
    assert [(r['status'], r['reference_kind']) for r in voice] == [('omitted_by_export', 'voice_message')]
    location = by_id[('telegram:6000001', 14)]
    assert location['meta']['raw']['__location__'] == 'location_present_not_stored'
    assert '30.25' not in location['metadata_json'] and 'shared_location_not_stored' in location['meta']['issues']
    call = by_id[('telegram:6000001', 15)]
    assert call['meta']['chat']['content_kind'] == 'call' and call['meta']['chat']['service']['duration_seconds'] == 61
    forwarded = by_id[('telegram:6000001', 16)]
    assert forwarded['meta']['chat']['forwarded_from']['name'] == 'Kanał Przykładowy' and forwarded['ts_start'] is None
    assert 'time_unparsed:date' in by_id[('telegram:6000001', 17)]['meta']['issues']
    assert 'epoch_implausible_for_unit_s:date_unixtime' in by_id[('telegram:6000001', 18)]['meta']['issues']
    assert by_id[('telegram:6000001', 19)]['meta']['chat']['reactions'] == [{'emoji': '👍', 'type': 'emoji', 'count': 1}]
    assert by_id[('telegram:8000001', 1)]['meta']['chat']['chat_list'] == 'left_chats'
    assert result['files'][0]['coverage'] == 'partial'  # one non-object message
    assert '+48 000 000 101' not in json.dumps(rows('SELECT metadata_json FROM events'))
    single = telegram.import_result(Path(m['single']))
    assert single['files'][0]['coverage'] == 'complete'
    single_events = [e for e in messages('telegram') if e['artifact_id'] == single['files'][0]['artifact_id']]
    assert single_events and all(e['direction'] is None for e in single_events)
    shared = rows('''SELECT r.record_id, COUNT(DISTINCT o.export_id) AS n FROM logical_records r JOIN logical_occurrences o
                     ON o.logical_record_id=r.id WHERE r.namespace='telegram' AND r.record_kind='message'
                     GROUP BY r.id HAVING n > 1''')
    assert sorted(r['record_id'] for r in shared) == ['10', '11']
    with pytest.raises(ValueError, match='HTML exports are not supported'):
        telegram.import_result(Path(m['html_dir']))


def test_google_chat_folder(env):
    from server.app.importers import google_chat
    m = env['m']['google_chat']
    result = google_chat.import_chat(Path(m['root']), owner_id=m['owner'])
    events = messages('google_chat')
    by_body = {e['body']: e for e in events}
    first = by_body['Cześć z Google Chat']
    assert first['thread_id'] == 'google_chat:DM abc123' and first['direction'] == 'incoming'
    assert first['ts_start'] == '2021-03-02T10:00:00Z' and first['meta']['chat']['reactions'][0]['emoji'] == '👍'
    cet = by_body['abbreviated zone, edited']
    assert cet['direction'] == 'outgoing' and cet['ts_start'] is None
    kinds = {t['kind']: t for t in rows('SELECT * FROM event_times WHERE event_id=?', (cet['id'],))}
    assert kinds['created']['tz_status'] == 'unknown' and kinds['updated']['utc'] == '2021-03-02T10:06:00Z'
    assert json.loads(kinds['created']['metadata_json'])['normalization'] == 'google_chat_display_v1'
    refs = rows('SELECT status FROM event_media_references WHERE event_id=? ORDER BY id', (cet['id'],))
    assert [r['status'] for r in refs] == ['present', 'missing_from_export']
    assert 'time_unparsed:created_date' in by_body['localized date not in the month table']['meta']['issues']
    derived = by_body['no message id here']
    assert derived['meta']['chat']['record_id_basis'] == 'content_derived_google_chat_v1'
    assert derived['ts_start'] == '2021-03-02T20:15:00Z'  # GMT+02:00 is a numeric zone
    space = by_body['space message']
    assert space['meta']['chat']['conversation_title'] == 'Projekt testowy'
    assert result['coverage']['complete'] is True


def test_x_archive_folder(env):
    from server.app.importers import twitter_x
    m = env['m']['x']
    result = twitter_x.import_archive(Path(m['root']))
    assert result['options']['owner_basis'] == 'export_account_js'
    posts = rows("SELECT * FROM events WHERE event_type='social_post' ORDER BY id")
    assert len(posts) == 4 and all(p['direction'] == 'outgoing' for p in posts)
    by_body = {p['body']: p for p in posts}
    assert by_body['Hello 👋 #test']['ts_start'] == '2020-09-02T10:00:00Z'
    assert by_body['RT @someone: retweeted text']['ts_start'] == '2020-09-03T18:29:59Z'
    reply = by_body['a reply with a photo']
    assert json.loads(reply['metadata_json'])['chat']['content_kind'] == 'reply' and reply['ts_start'] is None
    assert [r['status'] for r in rows('SELECT status FROM event_media_references WHERE event_id=?', (reply['id'],))] == ['present']
    old = by_body['old layout without the tweet wrapper and no prefix']
    assert old['ts_start'] == '2020-09-04T15:00:00Z'
    noprefix = [f for f in result['files'] if f['file_name'] == 'tweets-part1.js'][0]
    assert noprefix['issues']['by_code'] == {'ytd_assignment_prefix_missing': 1}
    dms = messages('twitter_x')
    dm = {e['meta']['chat'].get('message_id'): e for e in dms if e['event_type'] == 'chat_message'}
    assert dm['5001']['direction'] == 'outgoing' and dm['5002']['direction'] == 'incoming'
    assert dm['5001']['thread_id'] == 'twitter_x:111-222'
    assert [r['status'] for r in rows('SELECT status FROM event_media_references WHERE event_id=?', (dm['5002']['id'],))] == [
        'missing_from_export']
    assert 'time_missing:createdAt' in dm['5003']['meta']['issues']
    group = [e for e in dms if e['thread_id'] == 'twitter_x:9001']
    assert {e['meta']['chat']['content_kind'] for e in group} == {'service', 'text'}
    assert any('message_type_unmapped' in e['meta']['issues'] for e in group)


def test_linkedin_csv(env):
    from server.app.importers import linkedin
    m = env['m']['linkedin']
    result = linkedin.import_messages_csv(Path(m['messages']), owner_name=m['owner'])
    events = messages('linkedin')
    assert len(events) == 6
    by_body = {e['body']: e for e in events}
    first = by_body['Dzień dobry']
    assert first['ts_start'] == '2023-03-01T10:00:00Z' and first['direction'] == 'incoming'
    html = [e for e in events if e['meta']['chat']['content_was_html']][0]
    assert html['body'].startswith('Multi') and 'quotes' in html['body'] and '<p>' not in html['body']
    assert html['direction'] == 'outgoing'
    nozone = by_body['Plain text without zone']
    assert nozone['ts_start'] is None and 'time_zone_not_fixed:DATE' in nozone['meta']['issues']
    assert nozone['subject'] == 'Oferta pracy'
    assert by_body['with offset']['ts_start'] == '2023-03-03T07:00:00Z'
    short = [e for e in events if 'row_width_differs_from_header' in e['meta']['issues']]
    assert len(short) == 1
    before = rows('SELECT COUNT(*) AS n FROM events')[0]['n']
    folder = linkedin.import_messages_csv(Path(m['root']), owner_name=m['owner'])
    assert folder['files'][0]['artifact_id'] == result['files'][0]['artifact_id']  # same file, same locator
    assert rows('SELECT COUNT(*) AS n FROM events')[0]['n'] == before

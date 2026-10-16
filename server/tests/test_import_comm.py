"""Communications/Takeout/media importers against the synthetic hard corpus."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.config import settings
from server.app.importers import (billing, browser_history, call_log, comm_common, email_mbox, exif, ics, location,
                                  recording_names, sms_backup, takeout_activity, vcf, whatsapp_media)
from server.tests.corpus import communications


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_settings = replace(settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store')
    test_settings.store_dir.mkdir(parents=True)
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    manifest = communications.build(tmp_path / 'corpus')
    return {'root': tmp_path / 'corpus', 'm': manifest}


def rows(sql, params=()):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


def events(artifact_id=None, event_type=None):
    sql, params = 'SELECT * FROM events WHERE 1=1', []
    if artifact_id is not None:
        sql += ' AND artifact_id=?'
        params.append(artifact_id)
    if event_type is not None:
        sql += ' AND event_type=?'
        params.append(event_type)
    out = rows(sql + ' ORDER BY id', params)
    for event in out:
        event['meta'] = json.loads(event['metadata_json'])
    return out


def times(event_id):
    out = rows('SELECT * FROM event_times WHERE event_id=? ORDER BY id', (event_id,))
    for t in out:
        t['meta'] = json.loads(t['metadata_json'])
    return out


def snapshot_counts():
    tables = ('artifacts', 'events', 'event_times', 'event_media_references', 'contact_observations',
              'contact_observation_values', 'recording_name_observations')
    return {t: rows(f'SELECT COUNT(*) n FROM {t}')[0]['n'] for t in tables}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Email

def test_mbox_headers_threads_dates_and_attachments(env):
    m = env['m']['mbox']
    result = email_mbox.import_mbox(Path(m['path']))
    assert result['messages'] == m['messages'] == 6
    mails = events(result['artifact_id'], 'email')
    assert [e['meta']['locator']['message_index'] for e in mails] == list(range(6))
    first, reply, no_id, broken, duplicate, html_only = mails
    assert first['subject'] == m['subjects'][0] and reply['subject'] == m['subjects'][1]
    assert first['external_id'] == '<root-1@example.org>'
    assert first['thread_id'] == 'gmail-thrid:1790000000000000001'
    assert first['meta']['thread']['root'] == '<root-1@example.org>'
    assert first['meta']['gmail']['labels'] == ['Inbox', 'Projects, 2024', 'Important']
    assert first['contact_label'] == 'Zośka Przykładowa' and first['phone_or_address'] == 'zoska@example.org'
    assert first['meta']['addresses']['to']['parsed'][0] == {'name': 'Doe, John', 'address': 'john@example.com'}
    assert 'From the archive' in first['body'] and '>From' not in first['body']
    assert first['meta']['locator']['from_escaping']['lines_unescaped'] == 1
    assert first['meta']['html_parts'][0]['rendering'] == 'never_rendered_as_trusted_markup'
    first_times = {(t['kind'], t['source']): t for t in times(first['id'])}
    assert first_times[('header_date', 'email:Date')]['utc'] == '2024-01-05T09:20:00Z'
    assert first['ts_start'] == '2024-01-05T09:20:00Z'
    received = [t for t in first_times.values() if t['kind'] == 'received']
    assert sorted(t['utc'] for t in received) == ['2024-01-05T09:20:01Z', '2024-01-05T09:20:03Z']
    assert first_times[('other', 'mbox:From_line')]['meta']['semantics'] == 'mbox_separator_time_writer_defined'
    # Reply without References: root reconstructed through In-Reply-To inside the file.
    assert reply['thread_id'] == '<root-1@example.org>'
    assert reply['meta']['thread']['method'] == 'in_reply_to_chain_within_file'
    reply_date = [t for t in times(reply['id']) if t['kind'] == 'header_date'][0]
    assert reply_date['tz_status'] == 'unknown' and reply_date['utc'] is None and reply['ts_start'] is None
    assert reply_date['local'] == '2024-01-06T11:00:00'
    # Missing Message-ID: content-derived identity, flagged; -0000 keeps UTC, local zone unknown.
    assert no_id['external_id'].startswith('content-sha256:')
    assert no_id['meta']['message_id']['external_id_kind'] == 'content_derived_missing_message_id'
    no_id_date = [t for t in times(no_id['id']) if t['kind'] == 'header_date'][0]
    assert (no_id_date['utc'], no_id_date['tz']) == ('2024-01-07T08:00:00Z', '-00:00')
    assert no_id_date['meta']['zone_status'] == 'rfc5322_minus_0000_utc_instant_local_zone_unknown'
    # Broken MIME and missing Date stay visible.
    assert 'date_header_missing' in broken['meta']['issues']
    assert not [t for t in times(broken['id']) if t['kind'] == 'header_date']
    assert any('CloseBoundaryNotFoundDefect' in d for d in broken['meta']['mime']['defects'])
    assert broken['thread_id'] == '<root-1@example.org>' and broken['meta']['thread']['method'] == 'references_first'
    # Duplicate Message-ID inside one file: a second occurrence, never a merge.
    assert duplicate['external_id'] == '<root-1@example.org>#occurrence-2'
    cet = [t for t in times(duplicate['id']) if t['kind'] == 'header_date'][0]
    assert cet['tz_status'] == 'unknown' and cet['meta']['zone_raw'] == 'CET'
    assert cet['meta']['rfc5322_4_3_reading'] == {'equivalent': '-0000', 'utc': '2024-01-05T10:20:00Z', 'adopted': False}
    # HTML only: text projection without script, obsolete EST zone and RFC two-digit year.
    assert html_only['body'] == 'Only HTML here' and 'steal' not in html_only['body']
    assert html_only['meta']['body']['source'] == 'text_extracted_from_html'
    assert html_only['ts_start'] == '2024-01-09T12:30:00Z'
    est = [t for t in times(html_only['id']) if t['kind'] == 'header_date'][0]
    assert est['meta']['obsolete_zone'] == 'EST' and est['meta']['year_rule'].startswith('rfc5322_4_3_two_digit')
    assert html_only['subject'].startswith('Nieprawidłowe') and '=?BOGUS-CHARSET?Q?abc?=' in html_only['subject']
    # Attachments are children of the MBOX artifact and share the disk PDF's SHA-256.
    children = rows('SELECT * FROM artifacts WHERE parent_artifact_id=? ORDER BY id', (result['artifact_id'],))
    by_name = {c['original_name']: c for c in children}
    assert by_name['report.pdf']['sha256'] == env['m']['pdf']['sha256'] == m['attachment_sha256']
    assert by_name['logo.png']['sha256'] == m['inline_sha256']
    refs = rows('SELECT * FROM event_media_references WHERE event_id=? ORDER BY id', (first['id'],))
    assert {(r['reference_kind'], r['status']) for r in refs} == {('email_inline', 'present'), ('email_attachment', 'present')}
    assert json.loads(refs[0]['locator_json'])['byte_start'] == 0


def test_same_pdf_on_disk_and_attached_shares_content_identity(env):
    mail = email_mbox.import_mbox(Path(env['m']['mbox']['path']))
    source = evidence.ensure_source('disk', 'synthetic disk')
    disk_artifact = evidence.ingest_file(Path(env['m']['pdf']['path']), source_id=source)
    attachment = rows("SELECT * FROM artifacts WHERE parent_artifact_id=? AND original_name='report.pdf'",
                      (mail['artifact_id'],))[0]
    disk = rows('SELECT * FROM artifacts WHERE id=?', (disk_artifact,))[0]
    assert attachment['id'] != disk['id'] and attachment['sha256'] == disk['sha256']
    assert attachment['stored_path'] == disk['stored_path']  # one stored copy, two provenances
    assert disk['parent_artifact_id'] is None


def test_email_reimport_is_idempotent_and_originals_unchanged(env):
    before_sources = {k: v for k, v in env['m']['source_sha256'].items()}
    first = email_mbox.import_mbox(Path(env['m']['mbox']['path']))
    counts = snapshot_counts()
    second = email_mbox.import_mbox(Path(env['m']['mbox']['path']))
    assert second['artifact_id'] == first['artifact_id'] and snapshot_counts() == counts
    eml = email_mbox.import_eml(Path(env['m']['eml']['path']))
    counts = snapshot_counts()
    assert email_mbox.import_eml(Path(env['m']['eml']['path']))['artifact_id'] == eml['artifact_id']
    assert snapshot_counts() == counts
    after = {str(p.relative_to(env['root'])): sha(p) for p in sorted(env['root'].rglob('*')) if p.is_file()}
    assert after == before_sources


def test_single_eml_rfc2231_filename_and_pst(env):
    result = email_mbox.import_eml(Path(env['m']['eml']['path']))
    event = events(result['artifact_id'], 'email')[0]
    assert event['ts_start'] == '2024-01-10T23:45:00Z'
    child = rows('SELECT * FROM artifacts WHERE parent_artifact_id=?', (result['artifact_id'],))[0]
    assert child['original_name'] == env['m']['eml']['attachment_name']
    assert child['sha256'] == env['m']['pdf']['sha256']


@pytest.mark.parametrize('raw, utc, status', [
    ('Mon, 1 Jan 2024 10:00:00 +0000', '2024-01-01T10:00:00Z', 'explicit_offset'),
    ('Mon, 1 Jan 2024 10:00:00 -0000', '2024-01-01T10:00:00Z', 'explicit_offset'),
    ('Mon, 1 Jan 2024 10:00:00 GMT', '2024-01-01T10:00:00Z', 'explicit_offset'),
    ('Mon, 1 Jan 2024 10:00:00 UT', '2024-01-01T10:00:00Z', 'explicit_offset'),
    ('Mon, 1 Jan 2024 10:00:00 PDT', '2024-01-01T17:00:00Z', 'explicit_offset'),
    ('Mon, 1 Jan 2024 10:00:00 Z', '2024-01-01T10:00:00Z', 'explicit_offset'),
    ('Mon, 1 Jan 2024 10:00:00 A', None, 'unknown'),
    ('Mon, 1 Jan 2024 10:00:00 CEST', None, 'unknown'),
    ('Mon, 1 Jan 2024 10:00:00', None, 'unknown'),
    ('Monday, 01-Jan-24 10:00:00 GMT', '2024-01-01T10:00:00Z', 'explicit_offset'),
    ('Mon Jan  1 10:00:00 2024', None, 'unknown'),
    ('1 Jan 50 10:00 +0100', '1950-01-01T09:00:00Z', 'explicit_offset'),
    ('1 Jan 049 10:00 +0100', '1949-01-01T09:00:00Z', 'explicit_offset'),
    ('Mon, 1 Jan 2024 10:00:00 +1500', '2023-12-31T19:00:00Z', 'explicit_offset'),
])
def test_rfc5322_date_rules(raw, utc, status):
    candidate, meta = email_mbox.parse_mail_date(raw, 'header_date', 'test')
    assert candidate is not None, meta
    assert (candidate.utc, candidate.tz_status) == (utc, status)


@pytest.mark.parametrize('raw, status', [
    ('Mon, 1 Jan 2024 10:00:60 +0000', 'leap_second_not_representable'),
    ('Mon, 31 Feb 2024 10:00:00 +0000', 'invalid_calendar_value'),
    ('Mon, 1 Foo 2024 10:00:00 +0000', 'unknown_month_name'),
    ('yesterday', 'unparsed'),
])
def test_rfc5322_invalid_dates_are_not_guessed(raw, status):
    candidate, meta = email_mbox.parse_mail_date(raw, 'header_date', 'test')
    assert candidate is None and meta['status'] == status


def test_rfc5322_day_of_week_mismatch_and_invalid_zone_minutes():
    candidate, meta = email_mbox.parse_mail_date('Tue, 1 Jan 2024 10:00:00 +0100', 'header_date', 't')
    assert meta['day_of_week_mismatch'] == 'Tue' and candidate.utc == '2024-01-01T09:00:00Z'
    candidate, meta = email_mbox.parse_mail_date('Mon, 1 Jan 2024 10:00:00 +0175', 'header_date', 't')
    assert candidate.tz_status == 'unknown' and meta['zone_status'] == 'invalid_numeric_zone'


def test_rfc2047_adjacent_words_and_failures():
    assert email_mbox.decode_words('=?UTF-8?Q?a?= =?UTF-8?Q?b?=')[0] == 'ab'
    assert email_mbox.decode_words('x =?UTF-8?Q?a?= y')[0] == 'x a y'
    assert email_mbox.decode_words('=?utf-8?b?xYvDs8WCdw?=')[0] == 'ŋółw'
    text, problems = email_mbox.decode_words('=?UTF-8?B?////?=')
    assert problems == ['encoded_word_invalid_bytes:UTF-8']
    text, problems = email_mbox.decode_words('=?nope?Q?abc?=')
    assert text == '=?nope?Q?abc?=' and problems == ['encoded_word_unknown_charset:nope']


def test_mbox_limits_mark_partial_coverage(env):
    limits = comm_common.DEFAULT_LIMITS.replace(max_message_bytes=600, max_attachment_bytes=10, max_records=3)
    result = email_mbox.import_mbox(Path(env['m']['mbox']['path']), limits=limits)
    assert result['messages'] == 3 and result['coverage'] == {'complete': False}
    assert result['issues']['by_code']['record_limit'] == 1
    assert result['truncated_messages'] >= 1
    audit = rows("SELECT details_json FROM audit_log WHERE action='import_run' ORDER BY id DESC LIMIT 1")[0]
    assert json.loads(audit['details_json'])['coverage'] == {'complete': False}


def test_mbox_without_separator_is_reported(env, tmp_path):
    path = tmp_path / 'plain.mbox'
    path.write_bytes(b'not a separator\nFrom someone Mon Jan  1 10:00:00 2024\nSubject: x\n\nbody\n')
    result = email_mbox.import_mbox(path)
    assert result['messages'] == 1
    assert result['issues']['by_code'] == {'date_header_missing': 1, 'mbox_bytes_before_first_separator': 1,
                                           'mbox_separator_without_blank_line': 1,
                                           'message_id_missing_content_derived_id': 1}


# ---------------------------------------------------------------------------
# Calendar

def test_ics_zones_dst_floating_allday_and_recurrence(env):
    result = ics.import_ics(Path(env['m']['calendar']['path']))
    assert result['events'] == env['m']['calendar']['vevents']  # RRULE not expanded on import
    by_uid = {e['meta']['uid']: e for e in events(result['artifact_id'])}
    weekly = [e for e in events(result['artifact_id']) if e['external_id'] == 'uid:weekly-dst@example.org'][0]
    assert weekly['subject'] == 'Weekly meeting, across DST' and weekly['ts_start'] == '2024-03-24T09:00:00Z'
    assert weekly['body'] == 'First line\nsecond line that is folded across physical lines'
    assert weekly['meta']['categories'] == ['work', 'meeting,internal']
    assert weekly['meta']['recurrence']['rrule'] == ['FREQ=WEEKLY;COUNT=4']
    assert weekly['meta']['recurrence']['expanded_on_import'] is False
    start = [t for t in times(weekly['id']) if t['kind'] == 'start'][0]
    assert start['meta']['zone_declaration'] == 'vtimezone_in_file' and start['tz'] == 'Europe/Amsterdam'
    stamp = [t for t in times(weekly['id']) if t['source'] == 'ics:DTSTAMP'][0]
    assert stamp['kind'] == 'other' and stamp['meta']['semantics'] == 'rfc5545_dtstamp_depends_on_method'
    override = [e for e in events(result['artifact_id']) if 'recurrence-id' in e['external_id']][0]
    assert override['thread_id'] == weekly['thread_id'] == 'uid:weekly-dst@example.org'
    assert override['ts_start'] == '2024-03-31T10:00:00Z' and override['ts_end'] == '2024-03-31T10:30:00Z'
    end = [t for t in times(override['id']) if t['kind'] == 'end'][0]
    assert end['meta']['projection'] == 'dtstart_plus_duration'
    floating = [t for t in times(by_uid['floating@example.org']['id'])][0]
    assert floating['tz_status'] == 'unknown' and floating['meta']['zone_status'] == 'floating_time_zone_unknown'
    gap = times(by_uid['gap@example.org']['id'])[0]
    assert gap['tz_status'] == 'unknown' and gap['meta']['dst'] == 'nonexistent_local_time'
    fold_event = by_uid['fold@example.org']
    fold = times(fold_event['id'])[0]
    assert fold['meta']['dst'] == 'ambiguous_fold' and fold_event['ts_start'] is None
    assert fold['meta']['utc_alternatives'] == ['2024-10-27T00:30:00Z', '2024-10-27T01:30:00Z']
    allday = times(by_uid['allday@example.org']['id'])
    assert {t['precision'] for t in allday} == {'day'} and all(t['tz_status'] == 'unknown' for t in allday)
    assert by_uid['utc@example.org']['ts_start'] == '2024-01-15T09:00:00Z'
    no_uid = [e for e in events(result['artifact_id']) if e['external_id'].startswith('content-sha256:')][0]
    assert times(no_uid['id'])[0]['meta']['zone_status'] == 'tzid_unresolved'
    assert result['issues']['by_code']['ics_tzid_unresolved'] == 1
    projection = ics.expand_recurrence(weekly['id'], max_instances=10)
    assert projection['stored'] is False and projection['status'] == 'projected'
    assert [o['utc'] for o in projection['occurrences']] == [
        '2024-03-24T09:00:00Z', '2024-03-31T08:00:00Z', '2024-04-14T08:00:00Z']  # EXDATE 04-07 removed
    assert [o['overridden_by_recurrence_id'] for o in projection['occurrences']] == [False, True, False]
    bounded = ics.expand_recurrence(weekly['id'], max_instances=1)
    assert len(bounded['occurrences']) == 1 and bounded['truncated'] is True
    assert ics.expand_recurrence(by_uid['utc@example.org']['id'])['status'] == 'not_recurring'
    counts = snapshot_counts()
    ics.import_ics(Path(env['m']['calendar']['path']))
    assert snapshot_counts() == counts


def test_ics_vtimezone_wins_over_iana_and_disagreement_is_visible(env, tmp_path):
    path = tmp_path / 'odd.ics'
    path.write_text('\r\n'.join([
        'BEGIN:VCALENDAR', 'BEGIN:VTIMEZONE', 'TZID:Europe/Amsterdam', 'BEGIN:STANDARD', 'DTSTART:19700101T000000',
        'TZOFFSETFROM:+0300', 'TZOFFSETTO:+0300', 'END:STANDARD', 'END:VTIMEZONE',
        'BEGIN:VEVENT', 'UID:odd@example.org', 'DTSTART;TZID=Europe/Amsterdam:20240115T120000', 'END:VEVENT',
        'BEGIN:VEVENT', 'UID:broken@example.org', 'DTSTART:2024011', 'END:VEVENT',
        'END:VEVENT', 'BEGIN:VEVENT', 'NOT A CONTENT LINE', 'UID:open@example.org', 'END:VCALENDAR', '']), encoding='utf-8')
    result = ics.import_ics(path)
    odd = [e for e in events(result['artifact_id']) if e['external_id'] == 'uid:odd@example.org'][0]
    candidate = times(odd['id'])[0]
    assert candidate['utc'] == '2024-01-15T09:00:00Z' and candidate['meta']['iana_alternative_utc'] == '2024-01-15T11:00:00Z'
    codes = result['issues']['by_code']
    assert codes['ics_vtimezone_disagrees_with_iana'] == 1 and codes['ics_unparsable_datetime'] == 1
    assert codes['ics_malformed_content_line'] == 1 and codes['ics_unbalanced_end'] >= 1


# ---------------------------------------------------------------------------
# Contacts

def test_vcf_versions_quoted_printable_and_no_events(env):
    result = vcf.import_vcf(Path(env['m']['contacts']['path']))
    assert result['contacts'] == 4 and events(result['artifact_id']) == []
    cards = rows('SELECT * FROM contact_observations ORDER BY card_index')
    assert cards[0]['formatted_name'] == 'Zośka Przykładowa' and cards[0]['vcard_version'] == '2.1'
    assert json.loads(cards[0]['name_parts_json'])[:2] == ['Przykładowa', 'Zośka']
    assert cards[1]['formatted_name'] == 'Doe, John' and cards[1]['organization'] == 'Example BV; Sales'
    assert cards[1]['external_id_kind'] == 'uid' and cards[0]['external_id_kind'] == 'content_derived'
    raw = json.loads(cards[1]['raw_lines_json'])
    assert 'NOTE:a folded note line' in raw
    values = rows('SELECT v.*, c.card_index FROM contact_observation_values v JOIN contact_observations c '
                  'ON c.id=v.contact_observation_id ORDER BY c.card_index, v.value_kind, v.position')
    phones = [(v['card_index'], v['raw_value'], v['normalized_candidate']) for v in values if v['value_kind'] == 'tel']
    assert phones == [(0, '+48 000 000 101', '+48000000101'), (0, '(+31) 6-0000-0001', '+31600000001'),
                      (1, '+31 6 0000 0002', '+31600000002'), (1, '0600000003', '0600000003'),
                      (2, 'tel:+31-6-0000-0004', '+31600000004')]
    types = {v['raw_value']: json.loads(v['types_json']) for v in values}
    assert types['+48 000 000 101'] == ['cell', 'pref'] and types['tel:+31-6-0000-0004'] == ['cell', 'voice']
    assert [v['normalized_candidate'] for v in values if v['value_kind'] == 'email'][0] == 'zoska@example.org'
    assert result['issues']['by_code'] == {'vcf_unterminated_card': 1}
    counts = snapshot_counts()
    vcf.import_vcf(Path(env['m']['contacts']['path']))
    assert snapshot_counts() == counts
    with pytest.raises(sqlite3.DatabaseError):
        with dbmod.session() as db:
            db.execute("UPDATE contact_observations SET formatted_name='changed'")


# ---------------------------------------------------------------------------
# Call log, billing, recording names

def test_calls_xml_times_codes_and_projection(env):
    m = env['m']['calls']
    result = call_log.import_calls_xml(Path(m['path']))
    calls = events(result['artifact_id'], 'call')
    assert len(calls) == m['calls']
    first = calls[0]
    assert (first['ts_start'], first['ts_end']) == ('2024-01-05T09:15:00Z', '2024-01-05T09:17:05Z')
    assert first['direction'] == 'outgoing' and first['phone_or_address'] == m['with_recording_number']
    end = [t for t in times(first['id']) if t['kind'] == 'end'][0]
    assert end['meta']['projection'] == 'start_plus_duration_seconds' and end['tz_status'] == 'utc_epoch'
    assert first['meta']['readable_date']['interpretation'] == 'display_string_zone_unknown_not_parsed'
    unmapped = calls[2]
    assert unmapped['direction'] == '9' and 'type_code_unmapped' in unmapped['meta']['issues']
    assert unmapped['ts_end'] is None and 'duration_missing' in unmapped['meta']['issues']
    assert unmapped['meta']['presentation']['label'] == 'restricted'
    assert times(calls[3]['id']) == [] and 'timestamp_invalid' in calls[3]['meta']['issues']
    with pytest.raises(ValueError):
        call_log.import_calls_xml(Path(m['entity_path']))
    counts = snapshot_counts()
    call_log.import_calls_xml(Path(m['path']))
    assert snapshot_counts() == counts
    sms_backup.import_sms_backup(Path(m['path']))
    again = call_log.import_calls_xml(Path(m['path']))
    assert again['issues']['by_code']['other_parser_projection_exists'] == 1


def test_billing_day_first_profile_preamble_and_services(env):
    result = billing.import_billing(Path(env['m']['billing']['daymonth']))
    assert result['profiles'][0]['profile_id'] == 'example_semicolon_daymonth_v1'
    assert result['profiles'][0]['delimiter_source'] == "header_match_retry:';'"
    assert result['skipped_service'] == 1 and result['events'] == 4
    found = events(result['artifact_id'])
    assert [e['event_type'] for e in found] == ['call', 'call', 'sms', 'call']
    first = found[0]
    assert first['ts_start'] is None  # billing zone unknown
    start = [t for t in times(first['id']) if t['kind'] == 'start']
    assert len(start) == 1 and start[0]['local'] == '2024-01-05T10:15:00' and start[0]['tz_status'] == 'unknown'
    assert first['meta']['start']['date_order_ambiguous_resolved_by'] == 'profile_or_file_policy'
    assert first['meta']['duration']['seconds'] == 125 and first['direction'] == 'outgoing'
    assert first['meta']['locator']['start_line'] == 3 and '0,10' in first['meta']['raw_record']
    second = found[1]
    assert second['meta']['duration']['seconds'] == 3723
    assert 'three_fields_under_mm_ss_profile_read_as_hh_mm_ss' in second['meta']['issues']
    assert [t['local'] for t in times(second['id']) if t['kind'] == 'start'] == ['2024-01-13T08:00:00']
    last = found[3]
    assert {'duration_unparsed', 'direction_value_unmapped'} <= set(last['meta']['issues'])


def test_billing_ambiguous_dates_keep_both_readings_and_inference(env):
    result = billing.import_billing(Path(env['m']['billing']['infer']))
    assert result['profiles'][0]['date_order_applied'] == 'undetermined'
    found = events(result['artifact_id'])
    starts = sorted((t['source'], t['local']) for t in times(found[0]['id']) if t['kind'] == 'start')
    assert starts == [('billing:example_datetime_infer_order_v1:day_first', '2024-04-03T10:00:00'),
                      ('billing:example_datetime_infer_order_v1:month_first', '2024-03-04T10:00:00')]
    pm = sorted(t['local'] for t in times(found[1]['id']) if t['kind'] == 'start')
    assert pm == ['2024-04-05T23:00:00', '2024-05-04T23:00:00']
    same = [t for t in times(found[2]['id']) if t['kind'] == 'start']
    assert len(same) == 1 and same[0]['local'] == '2024-05-05T09:00:00'
    inferred = billing.import_billing(Path(env['m']['billing']['infer_dayfirst']))
    assert inferred['profiles'][0]['date_order_inference']['decision'] == 'day_first'
    first = events(inferred['artifact_id'])[0]
    assert [t['local'] for t in times(first['id']) if t['kind'] == 'start'] == ['2024-04-03T10:00:00']


def test_billing_xlsx_typed_cells_and_private_profile(env, tmp_path):
    path = env['m']['billing']['xlsx']
    if path is None:
        pytest.skip('openpyxl unavailable: XLSX fixture not generated')
    result = billing.import_billing(Path(path))
    found = events(result['artifact_id'])
    assert len(found) == 4
    assert found[3]['meta']['duration']['seconds'] == 3725
    assert found[3]['meta']['duration']['interpretation'] == 'typed_duration_cell'
    assert [t['local'] for t in times(found[0]['id']) if t['kind'] == 'start'] == ['2024-01-05T10:15:00']
    assert found[0]['meta']['start']['interpretation'] == 'typed_spreadsheet_datetime'
    assert found[0]['meta']['duration']['seconds'] == 125
    assert 'numeric_number_cell_may_have_lost_leading_zero_or_plus' in found[2]['meta']['issues']
    private = tmp_path / 'profiles.json'
    private.write_text(json.dumps({'schema': 'private.billing', 'version': '7', 'profiles': [{
        'id': 'mine', 'columns': {'datetime': ['start'], 'number': ['number'], 'duration': ['duration']},
        'required': ['datetime', 'number', 'duration'], 'date_order': 'month_first', 'duration_format': 'seconds',
        'assumed_zone': 'Europe/Amsterdam'}]}), encoding='utf-8')
    custom = billing.import_billing(Path(env['m']['billing']['infer']), profile_id='mine', profiles_path=private,
                                    source_locator='private-profile-run')
    first = events(custom['artifact_id'])[0]
    by_source = {t['source']: t for t in times(first['id']) if t['kind'] == 'start'}
    assert by_source['billing:mine']['local'] == '2024-03-04T10:00:00'
    assumed = by_source['billing:mine:assumed_zone']
    assert assumed['tz_status'] == 'assumed_zone' and assumed['utc'] == '2024-03-04T09:00:00Z'
    with pytest.raises(ValueError):
        billing.import_billing(Path(env['m']['billing']['infer']), profile_id='does-not-exist')


def test_recording_names_are_hypotheses_and_audio_is_not_read(env, monkeypatch):
    m = env['m']['recordings']
    recordings_dir = Path(m['dir'])
    real_open = Path.open

    def guarded(self, *args, **kwargs):
        if recordings_dir in self.parents:
            raise AssertionError(f'audio file was opened: {self}')
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guarded)
    artifacts_before = snapshot_counts()['artifacts']
    result = recording_names.observe_paths([recordings_dir])
    assert snapshot_counts()['artifacts'] == artifacts_before
    assert result['observed'] == 5 and result['unmatched'] == 1 and result['events'] == 3
    observations = {o['file_name']: o for o in rows('SELECT * FROM recording_name_observations')}
    match = observations[m['names']['matching_call']]
    hypotheses = json.loads(match['hypotheses_json'])
    assert hypotheses['number'] == {'raw': communications.NUMBER_A, 'normalized_candidate': communications.NUMBER_A,
                                    'status': 'correlation_hypothesis_from_file_name'}
    assert hypotheses['name']['raw'] == 'Example A'
    event = events(event_type='recording')[0]
    assert event['phone_or_address'] is None and event['contact_label'] is None and event['direction'] is None
    candidate = times(match['event_id'])[0]
    assert (candidate['kind'], candidate['tz_status'], candidate['local']) == ('filename_derived', 'unknown', '2024-01-05T10:15:00')
    without_log = json.loads(observations[m['names']['without_log']]['hypotheses_json'])
    assert without_log['direction']['label'] == 'outgoing' and without_log['number']['raw'] == communications.NUMBER_C
    invalid = observations[m['names']['invalid_date']]
    assert invalid['event_id'] is None and 'date_or_time_not_a_valid_calendar_value' in json.loads(invalid['metadata_json'])['issues']
    assert observations[m['names']['unmatched']]['status'] == 'unmatched'
    counts = snapshot_counts()
    recording_names.observe_paths([recordings_dir])
    assert snapshot_counts() == counts


def test_recording_call_correlation_inputs_are_present_but_not_decided(env):
    """The planted pairs are discoverable; deciding a match is the timeline track's hypothesis."""
    calls = call_log.import_calls_xml(Path(env['m']['calls']['path']))
    recording_names.observe_paths([Path(env['m']['recordings']['dir'])])
    call_numbers = {e['phone_or_address'] for e in events(calls['artifact_id'], 'call')}
    hypotheses = [json.loads(o['hypotheses_json']).get('number', {}).get('normalized_candidate')
                  for o in rows('SELECT * FROM recording_name_observations')]
    assert communications.NUMBER_A in call_numbers and communications.NUMBER_A in hypotheses
    assert communications.NUMBER_B in call_numbers and communications.NUMBER_B not in hypotheses
    assert communications.NUMBER_C in hypotheses and communications.NUMBER_C not in call_numbers
    from server.app.timeinfo import assume_zone, TimeCandidate
    observation = [o for o in rows('SELECT * FROM recording_name_observations')
                   if communications.NUMBER_A in o['file_name']][0]
    candidate = times(observation['event_id'])[0]
    projected = assume_zone(TimeCandidate('filename_derived', candidate['raw'], candidate['source'],
                                          local=candidate['local'], precision='second'), 'Europe/Amsterdam', rule='test')
    call = [e for e in events(calls['artifact_id'], 'call') if e['phone_or_address'] == communications.NUMBER_A][0]
    assert projected.utc == call['ts_start']
    assert rows("SELECT COUNT(*) n FROM links")[0]['n'] == 0  # no correlation written by importers


def test_recording_pattern_ambiguity_and_artifact_names(env, tmp_path):
    patterns = tmp_path / 'patterns.json'
    patterns.write_text(json.dumps({'schema': 'private.patterns', 'version': '3', 'patterns': [
        {'id': 'a', 'regex': r'^(?P<number>\d{5})_(?P<date>\d{8})_(?P<time>\d{4})', 'date_format': '%Y%m%d', 'time_format': '%H%M'},
        {'id': 'b', 'regex': r'^(?P<name>\d{5})_(?P<date>\d{8})_(?P<time>\d{4})', 'date_format': '%Y%d%m', 'time_format': '%H%M'},
    ]}), encoding='utf-8')
    result = recording_names.observe_names(['00001_20240102_1015.m4a'], patterns_path=patterns)
    assert result['ambiguous'] == 1
    observation = rows('SELECT * FROM recording_name_observations')[0]
    assert observation['status'] == 'ambiguous' and len(json.loads(observation['alternatives_json'])) == 1
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'patterns': [{'id': 'x', 'regex': '(?P<number>\\d+)'}]}), encoding='utf-8')
    with pytest.raises(ValueError):
        recording_names.load_patterns(bad)
    source = evidence.ensure_source('phone', 'synthetic phone')
    audio = tmp_path / env['m']['recordings']['names']['parenthesized']
    audio.write_bytes(b'placeholder')
    artifact = evidence.ingest_file(audio, source_id=source, source_locator='content://synthetic/rec')
    observed = recording_names.observe_artifacts([artifact, 999999])
    assert observed['events'] == 1 and observed['issues']['by_code'] == {'artifact_not_found': 1}
    linked = rows('SELECT * FROM recording_name_observations WHERE artifact_id=?', (artifact,))[0]
    assert events(artifact, 'recording')[0]['id'] == linked['event_id']


# ---------------------------------------------------------------------------
# Takeout

def test_my_activity_json_gemini_and_browser_gating(env):
    gemini = takeout_activity.import_my_activity(Path(env['m']['takeout']['gemini_json']))
    found = events(gemini['artifact_id'], 'activity')
    assert len(found) == 4 and gemini['gemini_records'] == 4
    first = found[0]
    assert first['ts_start'] == '2024-01-05T09:30:12.345000Z'
    candidate = times(first['id'])[0]
    assert candidate['kind'] == 'created' and candidate['precision'] == 'millisecond'
    assert 'Here is a summary.' in first['body'] and 'x()' not in first['body']
    assert first['meta']['raw_record']['safeHtmlItem'][0]['html'].startswith('<p>')
    assert first['meta']['opt_in'] == {'enable_browser_history': False}
    assert 'time_missing' in found[2]['meta']['issues'] and 'time_unparsed' in found[3]['meta']['issues']
    search = takeout_activity.import_my_activity(Path(env['m']['takeout']['search_json']))
    assert search['skipped_browser_history_not_enabled'] == 1 and search['events'] == 1
    enabled = takeout_activity.import_my_activity(Path(env['m']['takeout']['search_json']), enable_browser_history=True,
                                                  source_locator='with-browser-opt-in')
    assert enabled['events'] == 2
    chrome = [e for e in events(enabled['artifact_id']) if e['contact_label'] == 'Chrome'][0]
    assert chrome['meta']['opt_in'] == {'enable_browser_history': True}
    audit = [json.loads(r['details_json']) for r in rows("SELECT details_json FROM audit_log WHERE action='import_run'")]
    assert {'enable_browser_history': True} in [a['options'] for a in audit]


def test_my_activity_html_localized_times_keep_abbreviation_unknown(env):
    result = takeout_activity.import_my_activity(Path(env['m']['takeout']['search_html']))
    assert result['variant'] == 'html' and result['records'] == 4 and result['events'] == 3
    assert result['skipped_browser_history_not_enabled'] == 1
    found = events(result['artifact_id'], 'activity')
    first = found[0]
    assert first['subject'] == 'Searched for weather example'
    assert first['meta']['raw_record']['titleUrl'] == 'https://www.google.com/search?q=weather+example'
    assert first['meta']['raw_record']['products'] == ['Search']
    candidate = times(first['id'])[0]
    same = env['m']['takeout']['same_activity']
    assert (candidate['local'], candidate['tz_status']) == (same['html_local'], 'unknown')
    assert candidate['meta']['zone_abbreviation'] == same['html_zone'] and first['ts_start'] is None
    polish = times(found[1]['id'])[0]
    assert polish['tz_status'] == 'explicit_offset' and polish['utc'] == '2024-01-05T09:16:00Z'
    assert 'time_unparsed' in found[2]['meta']['issues']


def test_location_requires_opt_in_and_stays_coarse(env):
    before = snapshot_counts()
    with pytest.raises(location.LocationOptInRequired):
        location.import_location_history(Path(env['m']['location']['records']))
    assert snapshot_counts() == before  # refused before acquisition
    records = location.import_location_history(Path(env['m']['location']['records']), enable_location=True)
    assert records['events'] == 2 and records['raw_points_aggregated'] == 4
    first, second = events(records['artifact_id'], 'presence')
    assert first['meta']['coarse_location'] == {'lat': 52.37, 'lon': 4.9, 'decimals': 2}
    assert (first['ts_start'], first['ts_end']) == ('2024-01-05T09:00:00Z', '2024-01-05T09:05:00.500000Z')
    assert second['meta']['locator'] == {'artifact_id': records['artifact_id'], 'format': 'records_json',
                                         'first_index': 2, 'last_index': 3}
    semantic = location.import_location_history(Path(env['m']['location']['semantic']), enable_location=True,
                                                coarse_decimals=1)
    visit, activity = events(semantic['artifact_id'], 'presence')
    assert visit['meta']['coarse_location'] == {'lat': 52.4, 'lon': 4.9, 'decimals': 1}
    assert visit['meta']['fields'] == {'placeVisitType': 'SINGLE_PLACE', 'visitConfidence': 92, 'semanticType': 'TYPE_WORK'}
    assert activity['ts_start'] == '2024-01-05T12:00:00Z' and activity['meta']['fields']['activityType'] == 'IN_PASSENGER_VEHICLE'
    timeline = location.import_location_history(Path(env['m']['location']['timeline']), enable_location=True)
    segments = events(timeline['artifact_id'], 'presence')
    assert [s['subject'] for s in segments] == ['visit', 'activity', 'path']
    assert segments[0]['ts_start'] == '2024-01-06T09:00:00Z'
    assert times(segments[0]['id'])[0]['tz'] == '+01:00'
    assert timeline['issues']['by_code'] == {'section_not_imported': 2}
    every = json.dumps([e['metadata_json'] for e in events(event_type='presence')])
    for private in env['m']['location']['private_strings'] + ['523702000', '52.3702']:
        assert private not in every
    with pytest.raises(ValueError):
        location.import_location_history(Path(env['m']['location']['records']), enable_location=True, coarse_decimals=7)


def test_browser_history_opt_in_and_microseconds(env):
    before = snapshot_counts()
    with pytest.raises(browser_history.BrowserHistoryOptInRequired):
        browser_history.import_browser_history(Path(env['m']['browser']['chrome']))
    assert snapshot_counts() == before
    result = browser_history.import_browser_history(Path(env['m']['browser']['chrome']), enable_browser_history=True)
    visit, untimed = events(result['artifact_id'], 'browser_visit')
    candidate = times(visit['id'])[0]
    assert candidate['utc'] == '2024-01-05T09:16:00.123456Z' and candidate['precision'] == 'microsecond'
    assert visit['meta']['opt_in']['enable_browser_history'] is True and visit['body'] == 'https://example.com/'
    assert untimed['meta']['issues'] == ['time_missing']
    youtube = browser_history.import_browser_history(Path(env['m']['takeout']['youtube_json']), enable_browser_history=True)
    assert youtube['delegated_to'] == 'takeout_my_activity' and youtube['events'] == 1


# ---------------------------------------------------------------------------
# WhatsApp media and EXIF

def test_whatsapp_folder_media_present_missing_omitted_unreferenced(env):
    m = env['m']['whatsapp']
    result = whatsapp_media.import_whatsapp_export(Path(m['folder']))
    expected = m['folder_expect']
    assert (result['present'], result['missing_from_export'], result['omitted_by_export'],
            result['unreferenced_media_files']) == (expected['present'], expected['missing_from_export'],
                                                    expected['omitted_by_export'], expected['unreferenced'])
    refs = rows('SELECT r.*, e.body, e.external_id FROM event_media_references r JOIN events e ON e.id=r.event_id ORDER BY r.id')
    present = [r for r in refs if r['status'] == 'present'][0]
    assert present['external_id'] == 'whatsapp_txt:line:2' and json.loads(present['locator_json'])['line'] == 2
    media = rows('SELECT * FROM artifacts WHERE id=?', (present['artifact_id'],))[0]
    assert media['sha256'] == m['photo_sha256'] and media['parent_artifact_id'] == result['chat_artifact_id']
    missing = [r for r in refs if r['status'] == 'missing_from_export'][0]
    assert missing['artifact_id'] is None and missing['reference_name'] == '00000013-VIDEO-2024-01-05-10-16-00.mp4'
    omitted = [r for r in refs if r['status'] == 'omitted_by_export'][0]
    assert json.loads(omitted['locator_json'])['pattern_id'] == 'ios_omitted_en_v1'
    chat_events = events(result['chat_artifact_id'], 'whatsapp_message')
    assert {e['meta']['parser']['version'] for e in chat_events} == {'2'}  # whatsapp_txt semantics unchanged
    counts = snapshot_counts()
    whatsapp_media.import_whatsapp_export(Path(m['folder']))
    assert snapshot_counts() == counts


def test_whatsapp_zip_children_of_container_and_pdf_identity(env):
    m = env['m']['whatsapp']
    result = whatsapp_media.import_whatsapp_export(Path(m['zip']))
    expected = m['zip_expect']
    assert (result['present'], result['missing_from_export'], result['omitted_by_export']) == (
        expected['present'], expected['missing_from_export'], expected['omitted_by_export'])
    chat = rows('SELECT * FROM artifacts WHERE id=?', (result['chat_artifact_id'],))[0]
    assert chat['parent_artifact_id'] == result['container_artifact_id']
    children = {a['original_name']: a for a in rows('SELECT * FROM artifacts WHERE parent_artifact_id=?',
                                                    (result['container_artifact_id'],))}
    assert children['DOC-20240105-WA0003.pdf']['sha256'] == env['m']['pdf']['sha256']
    assert children['IMG-20240105-WA0001.jpg']['source_locator'].endswith('export.zip!/IMG-20240105-WA0001.jpg')
    patterns = {json.loads(r['locator_json'])['pattern_id'] for r in rows('SELECT * FROM event_media_references')}
    assert {'android_file_attached_en_v1', 'android_file_attached_nl_v1', 'android_media_omitted_en_v1'} <= patterns
    counts = snapshot_counts()
    whatsapp_media.import_whatsapp_export(Path(m['zip']))
    assert snapshot_counts() == counts


def test_whatsapp_export_chat_selection_errors(env, tmp_path):
    import zipfile
    path = tmp_path / 'two.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('a.txt', '01/01/2024, 10:00 - A: x\n')
        archive.writestr('b.txt', '01/01/2024, 10:00 - B: y\n')
    with pytest.raises(ValueError, match='chat_member'):
        whatsapp_media.import_whatsapp_export(path)
    chosen = whatsapp_media.import_whatsapp_export(path, chat_member='b.txt', source_locator='two-zip-chosen')
    assert chosen['messages'] == 1 and chosen['options']['chat_member'] == 'b.txt'
    with pytest.raises(ValueError):
        whatsapp_media.import_whatsapp_export(Path(env['m']['pdf']['path']))


def test_exif_candidates_offsets_and_bounds(env):
    images = env['m']['images']
    result = exif.import_images([Path(images[n]) for n in sorted(images)])
    by_name = {Path(images[n]).name: r for n, r in zip(sorted(images), result['results'])}
    assert by_name['with_exif_offset.jpg']['status'] == 'exif_time_observed'
    assert by_name['without_exif.jpg']['status'] == 'no_exif'
    assert by_name['truncated.jpg']['status'] == 'no_exif'
    assert by_name['zero_date_exif.jpg']['status'] == 'exif_without_time'
    event = events(by_name['with_exif_offset.jpg']['artifact_id'], 'image_capture')[0]
    found = {t['source']: t for t in times(event['id'])}
    original = found['exif:DateTimeOriginal']
    assert (original['utc'], original['tz'], original['precision']) == ('2024-01-05T09:15:30.123000Z', '+01:00', 'millisecond')
    assert found['exif:DateTimeDigitized']['tz_status'] == 'unknown' and found['exif:DateTime']['tz_status'] == 'unknown'
    assert event['ts_start'] is None
    assert event['meta']['exif']['Make'] == 'ExampleMake' and event['meta']['gps'] == {'ifd_present': True, 'coordinates_decoded': False}
    no_offset = events(by_name['exif_without_offset.jpg']['artifact_id'], 'image_capture')[0]
    assert times(no_offset['id'])[0]['meta']['zone_status'] == 'no_offset_tag_zone_unknown'
    small = comm_common.DEFAULT_LIMITS.replace(max_exif_bytes=16)
    values, problems, container = exif.read_exif(Path(images['with_exif_offset.jpg']), small)
    assert values is None and container == 'jpeg' and problems
    counts = snapshot_counts()
    exif.import_images([Path(images[n]) for n in sorted(images)])
    assert snapshot_counts() == counts


def test_exif_ifd_loop_png_and_whatsapp_media_artifact(env, tmp_path):
    block = bytearray(communications.tiff_block({0x010F: 'ExampleMake'}, {0x9003: '2024:01:05 10:15:30'}, gps=False))
    count = struct.unpack('<H', block[8:10])[0]
    for i in range(count):  # point the EXIF IFD pointer back at IFD0
        entry = 10 + 12 * i
        if struct.unpack('<H', block[entry:entry + 2])[0] == 0x8769:
            block[entry + 8:entry + 12] = struct.pack('<I', 8)
    looped = tmp_path / 'loop.jpg'
    looped.write_bytes(communications.jpeg(bytes(block)))
    values, problems, _ = exif.read_exif(looped)
    assert 'ifd_offset_invalid_or_loop' in problems and 'DateTimeOriginal' not in values
    tiff = communications.tiff_block({0x010F: 'ExampleMake'}, {0x9003: '2024:02:01 08:00:00', 0x9011: '-05:00'}, gps=False)
    png = b'\x89PNG\r\n\x1a\n' + struct.pack('>I4s', len(tiff), b'eXIf') + tiff + b'\x00\x00\x00\x00' + struct.pack('>I4s', 0, b'IEND') + b'\x00' * 4
    png_path = tmp_path / 'image.png'
    png_path.write_bytes(png)
    values, problems, container = exif.read_exif(png_path)
    assert container == 'png' and values['DateTimeOriginal'] == '2024:02:01 08:00:00'
    candidate = exif.exif_candidates(values, [])[0]
    assert candidate.utc == '2024-02-01T13:00:00Z'
    wa = whatsapp_media.import_whatsapp_export(Path(env['m']['whatsapp']['folder']))
    media = rows('SELECT artifact_id FROM event_media_references WHERE status="present"')[0]['artifact_id']
    observed = exif.observe_artifact(media)
    assert observed['status'] == 'exif_time_observed'
    assert events(media, 'image_capture')[0]['source_id'] == rows('SELECT source_id FROM artifacts WHERE id=?', (media,))[0]['source_id']
    assert wa['chat_artifact_id']


# ---------------------------------------------------------------------------
# Shared contracts

def test_parent_artifact_validation(env, tmp_path):
    source = evidence.ensure_source('test', 'parent checks')
    parent_file = tmp_path / 'parent.bin'
    parent_file.write_bytes(b'parent')
    child_file = tmp_path / 'child.bin'
    child_file.write_bytes(b'child')
    parent = evidence.ingest_file(parent_file, source_id=source, source_locator='p')
    with pytest.raises(evidence.IntegrityError):
        evidence.ingest_file(child_file, source_id=source, source_locator='c', parent_artifact_id=999999)
    child = evidence.ingest_file(child_file, source_id=source, source_locator='c', parent_artifact_id=parent)
    assert rows('SELECT parent_artifact_id FROM artifacts WHERE id=?', (child,))[0]['parent_artifact_id'] == parent
    assert evidence.ingest_file(child_file, source_id=source, source_locator='c') == child  # parent not stated
    other_parent = evidence.ingest_file(child_file, source_id=source, source_locator='other')
    with pytest.raises(evidence.IntegrityError):
        evidence.ingest_file(child_file, source_id=source, source_locator='c', parent_artifact_id=other_parent)
    with dbmod.session() as db:
        case = db.execute("INSERT INTO cases(name) VALUES ('second synthetic case')").lastrowid
    foreign = evidence.ensure_source('test', 'other case', case_id=case)
    with pytest.raises(evidence.IntegrityError):
        evidence.ingest_file(child_file, source_id=foreign, source_locator='x', parent_artifact_id=parent)
    audit = rows("SELECT details_json FROM audit_log WHERE action='ingest' AND object_id=? ORDER BY id LIMIT 1", (child,))
    assert json.loads(audit[0]['details_json'])['parent_artifact_id'] == parent


def test_all_importers_leave_corpus_bytes_unchanged(env):
    m = env['m']
    email_mbox.import_mbox(Path(m['mbox']['path']))
    email_mbox.import_eml(Path(m['eml']['path']))
    ics.import_ics(Path(m['calendar']['path']))
    vcf.import_vcf(Path(m['contacts']['path']))
    call_log.import_calls_xml(Path(m['calls']['path']))
    for key in ('daymonth', 'infer', 'infer_dayfirst', 'xlsx'):
        if m['billing'][key]:
            billing.import_billing(Path(m['billing'][key]))
    recording_names.observe_paths([Path(m['recordings']['dir'])])
    for key in ('gemini_json', 'search_json', 'search_html'):
        takeout_activity.import_my_activity(Path(m['takeout'][key]))
    for key in ('records', 'semantic', 'timeline'):
        location.import_location_history(Path(m['location'][key]), enable_location=True)
    browser_history.import_browser_history(Path(m['browser']['chrome']), enable_browser_history=True)
    whatsapp_media.import_whatsapp_export(Path(m['whatsapp']['folder']))
    whatsapp_media.import_whatsapp_export(Path(m['whatsapp']['zip']))
    exif.import_images([Path(p) for p in m['images'].values()])
    after = {str(p.relative_to(env['root'])): sha(p) for p in sorted(env['root'].rglob('*')) if p.is_file()}
    assert after == m['source_sha256']
    store = Path(evidence.settings.store_dir)
    assert not any(env['root'] in p.parents for p in [store])
    leaked = [p for p in env['root'].rglob('*') if p.name.startswith(('.capture-', 'payload'))]
    assert leaked == []


def test_mbox_long_lines_split_by_read_bound_never_create_separators(env, tmp_path):
    path = tmp_path / 'long.mbox'
    path.write_bytes(b'From a@example.org Mon Jan  1 10:00:00 2024\nMessage-ID: <long@example.org>\n\n'
                     + b'x' * 64 + b'From fake@example.org Mon Jan  1 10:00:00 2024\n'
                     + b'\nFrom b@example.org Tue Jan  2 10:00:00 2024\nMessage-ID: <second@example.org>\n\nok\n')
    limits = comm_common.DEFAULT_LIMITS.replace(max_line_bytes=64)
    result = email_mbox.import_mbox(path, limits=limits)
    assert result['messages'] == 2
    first = events(result['artifact_id'], 'email')[0]
    assert first['body'].startswith('x' * 64 + 'From fake')

"""Review findings #2 (numeric offsets out of range), #11 (ICS EXDATE/until zones), #12 (bound offsets)."""
from __future__ import annotations

from server.app.importers import comm_common, email_mbox, exif, takeout_activity
from server.tests.test_review_fixes_support import env, mbox_message  # noqa: F401 (fixture)


# --------------------------------------------------------------------- finding 2

def test_finding2_out_of_range_numeric_offsets_stay_local_and_flagged():
    for zone in ('+2400', '+9900', '-2400'):
        candidate, meta = email_mbox.parse_mail_date(f'Mon, 1 Jan 2024 10:00:00 {zone}', 'header_date', 'email:Date')
        assert candidate.tz_status == 'unknown' and candidate.utc is None, zone
        assert candidate.local == '2024-01-01T10:00:00'
        assert meta['zone_status'] == 'numeric_offset_out_of_range' and meta['offset_raw'] == zone
    edge, _ = email_mbox.parse_mail_date('Mon, 1 Jan 2024 10:00:00 +2359', 'header_date', 'email:Date')
    assert edge.tz_status == 'explicit_offset' and edge.utc == '2023-12-31T10:01:00Z'
    problems: list[str] = []
    candidates = exif.exif_candidates({'DateTimeOriginal': '2024:02:01 08:00:00', 'OffsetTimeOriginal': '+99:00'}, problems)
    assert [c.tz_status for c in candidates] == ['unknown']
    assert candidates[0].metadata['zone_status'] == 'numeric_offset_out_of_range'
    assert 'exif_offset_out_of_range:OffsetTimeOriginal' in problems
    candidate, meta = takeout_activity.parse_display_time('Jan 5, 2024, 3:04:05 PM GMT+25', takeout_activity.settings(), 'x')
    assert candidate.tz_status == 'unknown' and meta['zone_status'] == 'numeric_offset_out_of_range'
    good, _ = takeout_activity.parse_display_time('Jan 5, 2024, 3:04:05 PM GMT+01:00', takeout_activity.settings(), 'x')
    assert good.utc == '2024-01-05T14:04:05Z'
    # offset_candidate itself never raises on a value no caller range-checked.
    from datetime import datetime
    fallback = comm_common.offset_candidate('other', 'x', 's', datetime(2024, 1, 1), 99 * 60, 'second')
    assert fallback.tz_status == 'unknown' and fallback.metadata['zone_status'] == 'numeric_offset_out_of_range'


def test_finding2_mbox_with_bad_offsets_imports_every_message_with_audit(env):
    mbox = (mbox_message(1, 'first')
            + mbox_message(3, 'spam', extra_headers='Received: from x by y; Mon, 1 Jan 2024 10:00:00 +9900\n')
            + mbox_message(4, 'later', date='Mon, 1 Jan 2024 11:00:00 +2400'))
    with env.client() as http:
        for attempt in (1, 2):
            response = http.post('/api/imports/comm/email', files={'file': ('m.mbox', mbox, 'application/mbox')})
            assert response.status_code == 200, (attempt, response.text)
    assert [r['external_id'] for r in env.rows("SELECT external_id FROM events WHERE event_type='email' ORDER BY id")] == [
        '<1@example.org>', '<3@example.org>', '<4@example.org>']
    assert env.one("SELECT COUNT(*) FROM audit_log WHERE action='import_run'") == 2
    later = env.rows("SELECT e.ts_start, t.tz_status, t.local FROM events e JOIN event_times t ON t.event_id=e.id "
                     "WHERE e.external_id='<4@example.org>' AND t.kind='header_date'")
    assert later == [{'ts_start': None, 'tz_status': 'unknown', 'local': '2024-01-01T11:00:00'}]


# --------------------------------------------------------------------- finding 11

ICS = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:u1@example.org\r\n'
       'DTSTART;TZID=Europe/Amsterdam:20240101T100000\r\nDTEND;TZID=Europe/Amsterdam:20240101T110000\r\n'
       'RRULE:FREQ=DAILY;COUNT=5\r\nEXDATE:20240103T090000Z\r\n'
       'EXDATE;TZID=America/New_York:20240104T040000\r\nSUMMARY:standup\r\nEND:VEVENT\r\n'
       'BEGIN:VEVENT\r\nUID:u2@example.org\r\nDTSTART;TZID=Europe/Amsterdam:20240101T100000\r\n'
       'RRULE:FREQ=DAILY;UNTIL=20240103T090000Z\r\nSUMMARY:until in utc\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n').encode()


def test_finding11_exdate_and_until_are_compared_in_the_dtstart_zone(env):
    with env.client(raise_server_exceptions=False) as http:
        assert http.post('/api/imports/comm/ics', files={'file': ('cal.ics', ICS, 'text/calendar')}).status_code == 200
        first = env.one("SELECT id FROM events WHERE external_id='uid:u1@example.org'")
        second = env.one("SELECT id FROM events WHERE external_id='uid:u2@example.org'")
        page = http.get(f'/api/imports/comm/calendar/{first}/occurrences').json()
        # 09:00Z and 04:00 New York are both 10:00 in Amsterdam: Jan 3 and Jan 4 are cancelled.
        assert [o['local'] for o in page['occurrences']] == ['2024-01-01T10:00:00', '2024-01-02T10:00:00',
                                                              '2024-01-05T10:00:00']
        bounded = http.get(f'/api/imports/comm/calendar/{first}/occurrences', params={'until': '2024-01-02T09:00:00+00:00'})
        assert bounded.status_code == 200, bounded.text
        assert [o['utc'] for o in bounded.json()['occurrences']] == ['2024-01-01T09:00:00Z', '2024-01-02T09:00:00Z']
        assert http.get(f'/api/imports/comm/calendar/{first}/occurrences', params={'until': 'garbage'}).status_code == 422
        # RRULE UNTIL in UTC is inclusive at 10:00 Amsterdam (RFC 5545), not compared as naive 09:00.
        until = http.get(f'/api/imports/comm/calendar/{second}/occurrences').json()
        assert [o['local'] for o in until['occurrences']] == ['2024-01-01T10:00:00', '2024-01-02T10:00:00',
                                                               '2024-01-03T10:00:00']


# --------------------------------------------------------------------- finding 12

def test_finding12_bound_offsets_out_of_range_are_input_errors(env):
    with env.client(raise_server_exceptions=False) as http:
        for bound in ('2024-01-01T00:00+25:00', '2024-01-01T00:00+23:60', '2024-01-01T00:00-24:00'):
            response = http.get('/api/timeline/window', params={'from': bound})
            assert response.status_code == 422, (bound, response.status_code, response.text[:200])
        assert http.get('/api/timeline/window', params={'from': '2024-01-01T00:00+14:00'}).status_code == 200

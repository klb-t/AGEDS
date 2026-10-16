"""Timeline projection: versioned display rule, candidates, uncertainty, cursors (synthetic data only)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from server.app import timeinfo
from server.app.timeline import ruleset
from server.app.timeline.query import QueryInputError, make_filters
from server.app.timeline.timeutil import US, parse_bound, utc_text_to_us

HOUR = 3600 * US


@pytest.fixture
def tl(tmp_path, monkeypatch):
    from server.app import db as dbmod, evidence
    from server.app.config import Settings
    settings = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', settings)
    monkeypatch.setattr(evidence, 'settings', settings)
    dbmod.init_db()
    return Helper(tmp_path, dbmod, evidence)


class Helper:
    def __init__(self, root, dbmod, evidence):
        self.root, self.dbmod, self.evidence = root, dbmod, evidence
        self.source = evidence.ensure_source('synthetic', 'projection tests')
        self.counter = 0

    def event(self, event_type='sms', **kwargs):
        self.counter += 1
        kwargs.setdefault('source_id', self.source)
        kwargs.setdefault('artifact_id', None)
        kwargs.setdefault('external_id', f'test:{self.counter}')
        return self.evidence.add_event(event_type=event_type, **kwargs)

    def times(self, event_id, *candidates):
        with self.dbmod.session() as db:
            timeinfo.record_event_times(db, event_id, list(candidates))

    def projection(self, params=None, rule=None, **kwargs):
        from server.app.timeline.projection import ensure_projection
        with self.dbmod.session() as db:
            return ensure_projection(db, rule, params or {}, **kwargs)

    def window(self, params=None, rule=None, **kwargs):
        from server.app.timeline.query import window
        rule = rule or ruleset.load_rule(kind='display_rule')
        with self.dbmod.session() as db:
            from server.app.timeline.projection import ensure_projection
            projection = ensure_projection(db, rule, params or {})
            return window(db, projection, rule, **kwargs)

    def item(self, key, params=None):
        from server.app.timeline.projection import ensure_projection
        from server.app.timeline.query import item_detail
        rule = ruleset.load_rule(kind='display_rule')
        with self.dbmod.session() as db:
            projection = ensure_projection(db, rule, params or {})
            return item_detail(db, projection, rule, key)

    def rows(self, projection_id=None):
        with self.dbmod.session() as db:
            sql = 'SELECT * FROM timeline_items' + (' WHERE projection_id=?' if projection_id else '') + ' ORDER BY item_kind, ref_id, sub_index'
            return [dict(r) for r in db.execute(sql, (projection_id,) if projection_id else ())]


def sms(tl, iso_utc, epoch_ms, **kwargs):
    return tl.event('sms', ts_start=iso_utc, direction=kwargs.pop('direction', 'incoming'),
                    metadata={'timestamp': {'raw': str(epoch_ms), 'format': 'unix_milliseconds'},
                              'raw_attributes': kwargs.pop('raw_attributes', {'date': str(epoch_ms)})}, **kwargs)


def whatsapp(tl, local, raw_time=None, **kwargs):
    return tl.event('whatsapp_message', ts_start=local, thread_id=kwargs.pop('thread_id', 'czat'),
                    metadata={'timestamp': {'raw_time': raw_time or local[11:16], 'timezone_status': 'unknown'}}, **kwargs)


def test_released_rules_load_and_are_pinned():
    rules = {r.ref: r for r in ruleset.released_rules()}
    assert {'display_time@1', 'recording_call_log@1', 'email_chat@1', 'same_conversation@1'} <= set(rules)
    for rule in rules.values():
        assert ruleset.RELEASED_SHA256.get((rule.rule_id, rule.version)) == rule.sha256, f'{rule.ref} must be pinned'
    assert ruleset.load_rule(kind='display_rule').ref == 'display_time@1'
    with pytest.raises(ruleset.RuleError):
        ruleset.load_rule('display_time@99')
    with pytest.raises(ruleset.RuleError):
        ruleset.load_rule('bad ref!')


def test_edited_released_rule_is_refused(monkeypatch):
    monkeypatch.setitem(ruleset.RELEASED_SHA256, ('display_time', 1), '0' * 64)
    with pytest.raises(ruleset.RuleIntegrityError):
        ruleset.load_rule(kind='display_rule')


def test_experimental_rule_cannot_reuse_released_version():
    data = dict(ruleset.load_rule(kind='display_rule').data)
    with pytest.raises(ruleset.RuleError):
        ruleset.experimental_rule(data)
    data['version'] = 2
    assert ruleset.experimental_rule(data).origin == 'experimental'


def test_assumed_zone_requires_named_assumption():
    with pytest.raises(ruleset.RuleError):
        ruleset.display_params('Europe/Amsterdam', None)
    with pytest.raises(ruleset.RuleError):
        ruleset.display_params('Mars/Olympus', 'op-1')
    with pytest.raises(ruleset.RuleError):
        ruleset.display_params(None, 'op-1')
    assert ruleset.display_params('Europe/Amsterdam', ' op-1 ') == {'assume_zone': 'Europe/Amsterdam', 'assumption_rule': 'op-1'}


def test_legacy_sms_uses_epoch_metadata_and_keeps_every_candidate(tl):
    event = sms(tl, '2024-03-31T00:30:00+00:00', 1711845000000,
                raw_attributes={'date': '1711845000000', 'date_sent': '1711844990000'})
    item = tl.item(f'event:{event}')
    assert item['time']['basis'] == 'utc_epoch'
    assert item['time']['display'] == '2024-03-31T00:30:00Z'
    assert item['time']['candidate_kind'] == 'received'
    refs = {c['ref']: c for c in item['candidates']}
    selected = refs['derived:metadata.timestamp.raw']
    assert selected['selected'] and selected['origin'] == 'derived_legacy'
    assert 'events.ts_start' in selected['derived_from']
    sent = refs['derived:metadata.raw_attributes.date_sent']
    assert sent['kind'] == 'sent' and sent['utc'] == '2024-03-31T00:29:50Z' and not sent['selected']


def test_unknown_zone_is_sorted_local_as_utc_with_full_offset_interval(tl):
    event = whatsapp(tl, '2024-03-05T10:00:00', raw_time='10:00')
    item = tl.item(f'event:{event}')
    t = item['time']
    base = utc_text_to_us('2024-03-05T10:00:00Z')
    assert t['basis'] == 'local_as_utc_unknown_zone'
    assert t['precision'] == 'minute'
    assert t['interval']['from_us'] == base - 14 * HOUR
    assert t['interval']['to_us'] == base + 12 * HOUR + 60 * US - 1
    candidate = item['candidates'][0]
    assert candidate['tz_status'] == 'unknown' and candidate['utc'] is None and candidate['local'] == '2024-03-05T10:00:00'


def test_mixed_offsets_sort_by_instant_not_text_and_import_time_is_ignored(tl):
    from fastapi.testclient import TestClient
    from server.app import main
    undated = tl.event('sms', body='no time')  # earliest created_at
    late_text = tl.event('call', ts_start='2024-01-01T10:00:00+01:00')     # 09:00Z, sorts lexicographically later
    early_text = tl.event('call', ts_start='2024-01-01T09:30:00+00:00')    # 09:30Z
    local = whatsapp(tl, '2024-01-01T09:45:00')                          # unknown zone, local-as-UTC 09:45
    window = tl.window(limit=10, candidates=False)
    assert [i['event_id'] for i in window['items']] == [late_text, early_text, local]
    assert window['undated_count'] == 1
    with TestClient(main.app) as client:
        response = client.get('/api/timeline')
    assert response.status_code == 200
    assert [r['id'] for r in response.json()] == [local, early_text, late_text, undated]
    assert set(response.json()[0]) == {'id', 'artifact_id', 'source_id', 'event_type', 'ts_start', 'ts_end', 'direction', 'sender',
                                       'recipients', 'contact_label', 'phone_or_address', 'subject', 'body', 'external_id',
                                       'thread_id', 'confidence', 'metadata_json', 'created_at'}
    assert response.headers['x-ageds-timeline-order'].startswith('projected-time-desc')


def test_stored_candidates_outrank_legacy_derivation_but_stay_visible(tl):
    event = tl.event('chat_message', ts_start='2020-01-01T00:00:00+00:00', metadata={'chatbot': 'claude'})
    tl.times(event, timeinfo.from_epoch('created', 1700000000, 'synthetic:create_time'))
    item = tl.item(f'event:{event}')
    assert item['time']['candidate_ref'].startswith('stored:')
    assert item['time']['display'] == '2023-11-14T22:13:20Z'
    derived = next(c for c in item['candidates'] if c['ref'] == 'derived:ts_start')
    assert not derived['eligible'] and derived['not_eligible_reason'] == 'stored_candidate_present'
    assert item['chatbot'] == 'claude'


def test_operator_assumed_zone_reports_fold_and_gap(tl):
    fold = whatsapp(tl, '2025-10-26T02:30:00')
    gap = whatsapp(tl, '2025-03-30T02:30:00')
    params = ruleset.display_params('Europe/Amsterdam', 'synthetic-assumption-1')
    folded = tl.item(f'event:{fold}', params)
    assert folded['time']['basis'] == 'assumed_zone_operator'
    assert folded['time']['display'] == '2025-10-26T00:30:00Z'
    assert folded['time']['interval']['from'] == '2025-10-26T00:30:00Z'
    assert folded['time']['interval']['to'] == '2025-10-26T01:30:59.999999Z'
    assert folded['time']['flags']['dst'] == 'ambiguous_fold'
    assert folded['time']['flags']['assumption_rule'] == 'synthetic-assumption-1'
    missing = tl.item(f'event:{gap}', params)
    assert missing['time']['basis'] == 'local_as_utc_unknown_zone'
    assert missing['time']['flags']['dst'] == 'nonexistent_local_time'
    # Without the assumption the same events stay unknown-zone: nothing is applied silently.
    assert tl.item(f'event:{fold}')['time']['basis'] == 'local_as_utc_unknown_zone'


def test_zone_by_source_overrides_default(tl):
    other = tl.evidence.ensure_source('synthetic', 'second phone')
    a = whatsapp(tl, '2024-06-01T12:00:00')
    b = whatsapp(tl, '2024-06-01T12:00:00', source_id=other)
    params = ruleset.display_params('Europe/Warsaw', 'per-source-1', {str(other): 'America/New_York'})
    assert tl.item(f'event:{a}', params)['time']['display'] == '2024-06-01T10:00:00Z'
    assert tl.item(f'event:{b}', params)['time']['display'] == '2024-06-01T16:00:00Z'


def test_day_precision_widens_interval(tl):
    event = tl.event('calendar_event')
    tl.times(event, timeinfo.from_iso('start', '2024-03-05', 'synthetic:ics'))
    t = tl.item(f'event:{event}')['time']
    base = utc_text_to_us('2024-03-05T00:00:00Z')
    assert t['precision'] == 'day' and t['sort_us'] == base
    assert t['interval']['from_us'] == base - 14 * HOUR
    assert t['interval']['to_us'] == base + 86400 * US + 12 * HOUR - 1


def test_undated_events_are_listed_separately_with_reason(tl):
    two_digit = tl.event('whatsapp_message', body='x', metadata={'timestamp': {'status': 'unknown_century'}})
    garbage = tl.event('sms', ts_start='not a date')
    from server.app.timeline.projection import ensure_projection
    from server.app.timeline.query import undated
    rule = ruleset.load_rule(kind='display_rule')
    with tl.dbmod.session() as db:
        page = undated(db, ensure_projection(db, rule, {}), rule, limit=10)
    reasons = {i['event_id']: i['time']['flags'] for i in page['items']}
    assert reasons[two_digit] == {'undated_reason': 'no_candidates', 'timestamp_status': 'unknown_century'}
    assert reasons[garbage]['undated_reason'] == 'no_usable_candidate'
    assert 'not a time order' in page['order']
    assert tl.window(candidates=False)['items'] == []


def test_rule_versions_are_separate_and_build_log_is_append_only(tl):
    event = whatsapp(tl, '2024-01-01T12:00:00')
    v1 = tl.projection()
    data = json.loads(json.dumps(ruleset.load_rule(kind='display_rule').data))
    data['version'] = 2
    data['unknown_zone']['offset_hours_min'] = -1
    data['unknown_zone']['offset_hours_max'] = 1
    v2_rule = ruleset.experimental_rule(data)
    v2 = tl.projection(rule=v2_rule)
    assert v1['id'] != v2['id'] and v2['rule']['origin'] == 'experimental'
    rows = {r['projection_id']: r for r in tl.rows() if r['ref_id'] == event}
    assert rows[v1['id']]['lo_us'] == utc_text_to_us('2024-01-01T12:00:00Z') - 14 * HOUR
    assert rows[v2['id']]['lo_us'] == utc_text_to_us('2024-01-01T12:00:00Z') - 1 * HOUR
    assert tl.projection()['status'] == 'fresh'
    with tl.dbmod.session() as db, pytest.raises(sqlite3.DatabaseError):
        db.execute('UPDATE timeline_projection_builds SET mode=mode')
    with tl.dbmod.session() as db, pytest.raises(sqlite3.DatabaseError):
        db.execute('DELETE FROM timeline_projection_builds')


def test_incremental_refresh_and_full_rebuild_after_deletion(tl):
    first = whatsapp(tl, '2024-01-01T12:00:00')
    initial = tl.projection()
    assert initial['build']['mode'] == 'full'
    tl.times(first, timeinfo.from_iso('sent', '2024-01-01T12:00:00+01:00', 'synthetic:later-parser'))
    second = sms(tl, '2024-01-02T00:00:00+00:00', 1704153600000)
    refreshed = tl.projection()
    assert refreshed['build']['mode'] == 'incremental'
    assert refreshed['build']['counts']['recomputed_event_ids'] == 2
    assert tl.item(f'event:{first}')['time']['basis'] == 'explicit_offset'
    assert tl.item(f'event:{second}')['time']['basis'] == 'utc_epoch'
    with tl.dbmod.session() as db:
        db.execute('DELETE FROM events WHERE id=?', (second,))
    rebuilt = tl.projection()
    assert rebuilt['build']['mode'] == 'full'
    assert {r['ref_id'] for r in tl.rows(rebuilt['id'])} == {first}
    with tl.dbmod.session() as db:
        assert db.execute('SELECT COUNT(*) FROM timeline_projection_builds').fetchone()[0] == 3


def test_cursor_pages_are_stable_under_ties_and_new_rows(tl):
    for _ in range(9):
        sms(tl, '2024-05-01T10:00:00+00:00', 1714557600000)
    sms(tl, '2024-05-01T09:00:00+00:00', 1714554000000)

    def paged(order, limit=3, first_page=None, between=None):
        keys, page = [], first_page or tl.window(limit=limit, order=order, candidates=False)
        while True:
            keys += [i['key'] for i in page['items']]
            if between:
                between()
                between = None
            if not page['has_more']:
                return keys
            page = tl.window(limit=limit, order=order, cursor=page['next_cursor'], candidates=False)
    for order in ('asc', 'desc'):
        full = [i['key'] for i in tl.window(limit=100, order=order, candidates=False)['items']]
        assert len(full) == 10 and paged(order) == full
    full = [i['key'] for i in tl.window(limit=100, candidates=False)['items']]
    added = []
    keys = paged('asc', between=lambda: added.append(sms(tl, '2024-05-01T10:00:00+00:00', 1714557600000)))
    assert len(keys) == len(set(keys)) == 11
    assert keys[:10] == full and keys[-1] == f'event:{added[0]}'
    page = tl.window(limit=3, candidates=False)
    with pytest.raises(QueryInputError):
        tl.window(limit=3, cursor=page['next_cursor'], filters=make_filters(event_types=['call']))
    with pytest.raises(QueryInputError):
        tl.window(limit=3, cursor='!!notbase64!!')


def test_ties_order_by_kind_then_id(tl):
    a = sms(tl, '2024-05-01T10:00:00+00:00', 1714557600000)
    b = sms(tl, '2024-05-01T10:00:00+00:00', 1714557600000)
    items = tl.window(candidates=False)['items']
    assert [i['event_id'] for i in items] == [a, b]
    assert [i['event_id'] for i in tl.window(order='desc', candidates=False)['items']] == [b, a]


def test_overlap_match_includes_uncertain_items(tl):
    local = whatsapp(tl, '2024-01-01T23:00:00')
    exact = sms(tl, '2024-01-02T03:00:00+00:00', 1704164400000)
    start, end = parse_bound('2024-01-02T00:00:00Z'), parse_bound('2024-01-02T06:00:00Z')
    display = tl.window(start_us=start, end_us=end, candidates=False)
    overlap = tl.window(start_us=start, end_us=end, match='overlap', candidates=False)
    assert [i['event_id'] for i in display['items']] == [exact]
    assert [i['event_id'] for i in overlap['items']] == [local, exact]


def test_filters_and_lanes(tl):
    other = tl.evidence.ensure_source('synthetic', 'second source')
    with tl.dbmod.session() as db:
        case_id = db.execute('SELECT id FROM cases').fetchone()[0]
        collection = db.execute("INSERT INTO collections(case_id,label) VALUES(?, 'Kolekcja X')", (case_id,)).lastrowid
    sample = tl.root / 'export.json'
    sample.write_text('{}')
    artifact = tl.evidence.ingest_file(sample, source_id=other, source_locator='synthetic:export', collection_id=collection)
    call = tl.event('call', ts_start='2024-02-01T10:00:00+00:00', phone_or_address='+48 000-000 101')
    chat_a = tl.event('chat_message', source_id=other, artifact_id=artifact, thread_id='conv-a', body='alpha beta',
                      metadata={'chatbot': 'chatgpt'})
    chat_b = tl.event('chat_message', source_id=other, artifact_id=artifact, thread_id='conv-b', body='gamma',
                      metadata={'chatbot': 'claude'})
    for event, second in ((chat_a, 1706781600), (chat_b, 1706781660)):
        tl.times(event, timeinfo.from_epoch('created', second, 'synthetic:ct'))
    with tl.dbmod.session() as db:
        tag = db.execute("INSERT INTO tags(name) VALUES('ważne')").lastrowid
        db.execute('INSERT INTO event_tags(event_id,tag_id) VALUES(?,?)', (chat_b, tag))

    def ids(**kwargs):
        return [i['event_id'] for i in tl.window(filters=make_filters(**kwargs), candidates=False)['items']]
    assert ids(event_types=['call']) == [call]
    assert ids(counterparties=['+48 000 000 101']) == [call]
    assert ids(chatbots=['claude']) == [chat_b]
    assert ids(threads=['conv-a']) == [chat_a]
    assert ids(collections=[str(collection)]) == [chat_a, chat_b]
    assert ids(sources=[str(other)]) == [chat_a, chat_b]
    assert ids(tags=['ważne']) == [chat_b]
    assert ids(q='alpha') == [chat_a]
    assert ids(kinds=['transcript_segment']) == []
    lanes = tl.window(group='chatbot', candidates=False)['lanes']['lanes']
    assert [(lane['key'], lane['count']) for lane in lanes] == [('chatgpt', 1), ('claude', 1), (None, 1)]
    with pytest.raises(QueryInputError):
        make_filters(kinds=['bogus'])
    with pytest.raises(QueryInputError):
        make_filters(q='"unterminated')
    with pytest.raises(QueryInputError):
        make_filters(sources=['x'])


def test_density_uses_calendar_buckets_and_bounds(tl):
    from server.app.timeline.projection import ensure_projection
    from server.app.timeline.query import QueryLimitError, density
    sms(tl, '2024-01-31T23:59:59+00:00', 1706745599000)
    sms(tl, '2024-02-01T00:00:00+00:00', 1706745600000)
    sms(tl, '2023-12-31T23:00:00+00:00', 1704063600000)
    whatsapp(tl, '2024-02-01T00:30:00')
    rule = ruleset.load_rule(kind='display_rule')
    with tl.dbmod.session() as db:
        projection = ensure_projection(db, rule, {})
        month = density(db, projection, bucket='month', group='type')
        lanes = {lane['key']: lane for lane in month['lanes']}
        assert [b[0] for b in lanes['sms']['buckets']] == ['2023-12-01T00:00:00Z', '2024-01-01T00:00:00Z', '2024-02-01T00:00:00Z']
        assert lanes['whatsapp_message']['buckets'][0][3] == 1  # unknown-zone count
        year = density(db, projection, bucket='year', group=None)
        assert [(b[0], b[2]) for b in year['lanes'][0]['buckets']] == [('2023-01-01T00:00:00Z', 1), ('2024-01-01T00:00:00Z', 3)]
        week = density(db, projection, bucket='week', group=None)
        assert all(b[0].startswith(('2023-12-25', '2024-01-29')) for b in week['lanes'][0]['buckets'])
        hour = density(db, projection, bucket='hour', group=None, start_us=parse_bound('2024-01-31T23:00Z'),
                       end_us=parse_bound('2024-02-01T01:00Z'))
        assert [(b[0], b[2]) for b in hour['lanes'][0]['buckets']] == [('2024-01-31T23:00:00Z', 1), ('2024-02-01T00:00:00Z', 2)]
        minute = density(db, projection, bucket='minute', group=None, start_us=parse_bound('2024-01-31T23:59Z'),
                         end_us=parse_bound('2024-02-01T00:01Z'))
        assert [b[2] for b in minute['lanes'][0]['buckets']] == [1, 1]
        assert density(db, projection, bucket='auto', group=None, max_buckets=10)['bucket'] == 'week'
        assert density(db, projection, bucket='auto', group=None, max_buckets=3)['bucket'] == 'month'
        with pytest.raises(QueryLimitError):
            density(db, projection, bucket='minute', group=None)
        with pytest.raises(QueryInputError):
            density(db, projection, bucket='fortnight')


def test_transcript_segments_inherit_recording_start_and_version_choice(tl):
    recording_source = tl.evidence.ensure_source('synthetic', 'recordings')
    audio = tl.root / 'rec.wav'
    audio.write_bytes(b'RIFF\x00\x00\x00\x00WAVE')
    artifact = tl.evidence.ingest_file(audio, source_id=recording_source, source_locator='synthetic:upload', mime_type='audio/wav',
                                       metadata={'filesystem_stat_scope': 'server_temporary_upload_not_original_source',
                                                 'acquisition_kind': 'uploaded_stream'})
    no_start = tl.evidence.ingest_file(tl.root / 'rec.wav', source_id=recording_source, source_locator='synthetic:other',
                                       mime_type='audio/wav', metadata={'acquisition_kind': 'uploaded_stream'})
    old = tl.evidence.add_derived_text(artifact, 'transcript', 'old', segments=[{'start': 0, 'end': 1, 'text': 'old'}])
    new = tl.evidence.add_derived_text(artifact, 'transcript', 'a b', language='pl', segments=[
        {'start': 0.5, 'end': 2.0, 'text': 'Dzień dobry', 'language': 'pl'},
        {'start': 2.0, 'end': 3.25, 'text': 'Goedemiddag', 'language': 'nl'},
        {'start': 'bad', 'end': 4, 'text': 'broken offset'}])
    tl.evidence.add_derived_text(no_start, 'transcript', 'x', segments=[{'start': 0, 'end': 1, 'text': 'x'}])
    # Before any recording observation the only time candidate is the temporary-upload mtime: unusable.
    undated = tl.window(filters=make_filters(kinds=['transcript_segment']), candidates=False)
    assert undated['items'] == [] and undated['undated_count'] == 4
    recording = tl.event('recording', source_id=recording_source, artifact_id=artifact, metadata={'filename_number': '000000101'})
    tl.times(recording, timeinfo.from_iso('filename_derived', '2024-04-01 14:00:00', 'synthetic:recording_name:v1'))
    page = tl.window(filters=make_filters(kinds=['transcript_segment']))
    items = page['items']
    assert [i['key'] for i in items] == [f'segment:{new}:0', f'segment:{new}:1']
    first = items[0]
    base = utc_text_to_us('2024-04-01T14:00:00Z')
    assert first['time']['sort_us'] == base + US // 2
    assert first['time']['interval']['from_us'] == base + US // 2 - 14 * HOUR
    assert first['time']['end_us'] == base + 2 * US
    assert first['language'] == 'pl' and items[1]['language'] == 'nl' and first['language_basis'] == 'segment'
    assert first['transcript_version'] == {'id': new, 'is_latest': True, 'model': None, 'run_id': None}
    refs = {c['ref']: c for c in first['candidates']}
    mtime = next(c for c in first['candidates'] if c['ref'].startswith('observation:'))
    assert not mtime['usable'] and mtime['unusable_reason'] == 'file_mtime_of_temporary_upload'
    assert any(r.startswith('artifact_time:') and c['selected'] for r, c in refs.items())
    every = tl.window(filters=make_filters(kinds=['transcript_segment'], transcript_versions='all'), candidates=False)
    assert {i['derived_text_id'] for i in every['items']} == {old, new}
    pinned = tl.window(filters=make_filters(kinds=['transcript_segment'], transcript_versions=str(old)), candidates=False)
    assert [i['key'] for i in pinned['items']] == [f'segment:{old}:0']
    assert page['undated_count'] == 2  # the broken offset segment and the recording without a start


def test_project_window_function_for_other_areas(tl):
    from server.app.timeline import iter_projected_items, project_window
    sms(tl, '2024-01-01T10:00:00+00:00', 1704103200000)
    whatsapp(tl, '2024-01-01T11:00:00')
    tl.event('sms', body='undated')
    result = project_window('2024-01-01', '2024-01-01')
    assert [i['time']['basis'] for i in result['items']] == ['utc_epoch', 'local_as_utc_unknown_zone']
    assert result['complete'] and result['undated_count'] == 1 and result['rule']['rule_id'] == 'display_time'
    streamed = list(iter_projected_items('2024-01-01T10:30Z', '2024-01-02', batch=1))
    assert len(streamed) == 1 and streamed[0]['time']['interval']['from_us'] < streamed[0]['time']['sort_us']


def test_export_guards_formulas_in_csv_but_json_is_verbatim(tl):
    from server.app.timeline.projection import ensure_projection
    from server.app.timeline.query import export_window
    sms(tl, '2024-01-01T10:00:00+00:00', 1704103200000, body='=HYPERLINK("x")')
    rule = ruleset.load_rule(kind='display_rule')
    with tl.dbmod.session() as db:
        projection = ensure_projection(db, rule, {})
        csv_bytes, meta = export_window(db, projection, rule, fmt='csv')
        json_bytes, _ = export_window(db, projection, rule, fmt='json')
        limited, limited_meta = export_window(db, projection, rule, fmt='json', max_rows=1)
    assert "'=HYPERLINK" in csv_bytes.decode() and meta['complete']
    assert json.loads(json_bytes)['items'][0]['text'] == '=HYPERLINK("x")'
    assert limited_meta['complete'] is True and json.loads(limited)['row_count'] == 1


def test_scan_plan_and_index_plan_return_identical_pages(tl, monkeypatch):
    from server.app.timeline import query as query_module
    for minute in range(12):
        sms(tl, f'2024-05-01T10:{minute:02d}:00+00:00', 1714557600000 + minute * 60000)
        tl.event('call', ts_start=f'2024-05-01T10:{minute:02d}:00+00:00')
    filters = make_filters(event_types=['sms'])

    def pages():
        keys, cursor, plans = [], None, set()
        while True:
            page = tl.window(limit=5, filters=filters, cursor=cursor, candidates=False)
            keys += [i['key'] for i in page['items']]
            plans.add(page['notes']['plan'])
            if not page['has_more']:
                return keys, plans
            cursor = page['next_cursor']
    index_keys, index_plans = pages()
    monkeypatch.setattr(query_module, 'SCAN_THRESHOLD', 0)
    scan_keys, scan_plans = pages()
    assert index_plans == {'sort_index'} and scan_plans == {'primary_key_scan'}
    assert index_keys == scan_keys and len(scan_keys) == 12

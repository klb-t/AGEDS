"""Correlation hypotheses: per-criterion evidence, ambiguity, DST/AM-PM, revocable review (synthetic data)."""
from __future__ import annotations

import json
import sqlite3
import struct
import wave
from datetime import datetime, timezone

import pytest

from server.app import timeinfo
from server.app.timeline.correlation import CorrelationError, TransitionError


@pytest.fixture
def cx(tmp_path, monkeypatch):
    from server.app import db as dbmod, evidence
    from server.app.config import Settings
    settings = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', settings)
    monkeypatch.setattr(evidence, 'settings', settings)
    dbmod.init_db()
    return Corpus(tmp_path, dbmod, evidence)


def epoch_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() * 1000)


class Corpus:
    def __init__(self, root, dbmod, evidence):
        self.root, self.dbmod, self.evidence = root, dbmod, evidence
        self.phone = evidence.ensure_source('synthetic', 'call log')
        self.recordings = evidence.ensure_source('synthetic', 'recordings')
        self.n = 0

    def call(self, start_iso_z: str, seconds: int, number: str):
        self.n += 1
        start = datetime.fromisoformat(start_iso_z.replace('Z', '+00:00'))
        end = datetime.fromtimestamp(start.timestamp() + seconds, timezone.utc)
        return self.evidence.add_event(source_id=self.phone, artifact_id=None, event_type='call', direction='incoming',
                                       ts_start=start.isoformat(), ts_end=end.isoformat(), phone_or_address=number,
                                       external_id=f'call:{self.n}',
                                       metadata={'timestamp': {'raw': str(epoch_ms(start_iso_z))}, 'duration': {'seconds': seconds}})

    def recording(self, local: str, seconds: int | None, number: str | None = None, **candidate_metadata):
        self.n += 1
        path = self.root / f'rec-{self.n}.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 1, 100, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x80' * 100 * (seconds or 1))
        artifact = self.evidence.ingest_file(path, source_id=self.recordings, source_locator=f'synthetic:rec:{self.n}',
                                             mime_type='audio/wav')
        metadata = {'filename_number': number} if number else {}
        if seconds is None:
            metadata['duration_seconds'] = 'unknown'
        event = self.evidence.add_event(source_id=self.recordings, artifact_id=artifact, event_type='recording',
                                        external_id=f'rec:{self.n}', metadata=metadata)
        candidate = timeinfo.from_iso('filename_derived', local, 'synthetic:recording_name:v1', metadata=candidate_metadata)
        with self.dbmod.session() as db:
            timeinfo.record_event_times(db, event, [candidate])
        return artifact

    def run(self, rule='recording_call_log', params=None):
        from server.app.timeline.correlation import run_rule
        with self.dbmod.session() as db:
            return run_rule(db, rule, params, actor='tester')

    def hypotheses(self, run_id):
        from server.app.timeline.correlation import list_hypotheses
        with self.dbmod.session() as db:
            return list_hypotheses(db, run_id=run_id)['hypotheses']

    def review(self, hid, action, **kwargs):
        from server.app.timeline.correlation import review
        with self.dbmod.session() as db:
            return review(db, hid, action, reason=kwargs.pop('reason', 'synthetic review'), actor='tester', **kwargs)

    def segment_time(self, artifact):
        from server.app.timeline.projection import ensure_projection
        from server.app.timeline.query import item_detail
        from server.app.timeline.ruleset import load_rule
        rule = load_rule(kind='display_rule')
        with self.dbmod.session() as db:
            version = db.execute("SELECT MAX(id) FROM derived_text WHERE artifact_id=?", (artifact,)).fetchone()[0]
            return item_detail(db, ensure_projection(db, rule, {}), rule, f'segment:{version}:0')['time']


def test_recording_matches_call_with_offset_duration_and_number_evidence(cx):
    call = cx.call('2024-06-03T08:15:00Z', 95, '+48 000 000 123')
    recording = cx.recording('2024-06-03 10:15:04', 95, '000000123')
    run = cx.run()
    assert run['status'] == 'completed' and run['rule_id'] == 'recording_call_log' and run['rule_version'] == 1
    [h] = cx.hypotheses(run['id'])
    assert (h['left_kind'], h['left_id'], h['right_kind'], h['right_id']) == ('artifact', recording, 'event', call)
    assert h['status'] == 'proposed' and h['candidate_count'] == 1
    c = h['criteria']
    assert c['time']['mode'] == 'unknown_zone_offset_search' and c['time']['implied_utc_offset_minutes'] == 120
    assert c['time']['residual_seconds'] == 4.0 and c['time']['recording_candidate']['kind'] == 'filename_derived'
    assert c['duration']['status'] == 'agree' and c['duration']['recording_duration_source'].startswith('wav_header:')
    assert c['number']['status'] == 'suffix' and c['gate'] == {'time': True, 'corroboration_required': True, 'corroborated': True}
    assert 'probability' not in c


def test_two_calls_within_tolerance_stay_ambiguous(cx):
    first = cx.call('2024-06-03T08:15:00Z', 60, '+48 000 000 123')
    second = cx.call('2024-06-03T08:16:00Z', 60, '+48 000 000 123')
    recording = cx.recording('2024-06-03T10:15:30', 60, '000000123')
    run = cx.run()
    hypotheses = cx.hypotheses(run['id'])
    assert {h['right_id'] for h in hypotheses} == {first, second}
    assert {h['ambiguity_group'] for h in hypotheses} == {f'recording_call_log:artifact:{recording}'}
    assert all(h['candidate_count'] == 2 and h['status'] == 'proposed' for h in hypotheses)
    assert run['result']['counts']['ambiguous_recordings'] == 1
    from server.app.timeline.correlation import hypothesis_detail
    with cx.dbmod.session() as db:
        detail = hypothesis_detail(db, hypotheses[0]['id'])
    assert [s['right_id'] for s in detail['siblings']] == [hypotheses[1]['right_id']] and detail['probability'] is None


def test_unmatched_recording_and_unmatched_call_are_reported(cx):
    lonely_call = cx.call('2024-06-05T08:00:00Z', 30, '+48 000 000 555')
    lonely_recording = cx.recording('2024-06-09 22:00:00', 30, '000000777')
    no_time = cx.evidence.ingest_file(cx.root / 'rec-2.wav', source_id=cx.recordings, source_locator='synthetic:no-time',
                                      mime_type='audio/wav', metadata={'acquisition_kind': 'uploaded_stream'})
    run = cx.run()
    assert run['hypothesis_count'] == 0
    unmatched = run['result']['unmatched']
    assert {'left_kind': 'artifact', 'left_id': lonely_recording, 'reason': 'no_call_within_tolerance',
            'evaluated_time_points': 1} in unmatched['recordings']
    assert {'left_kind': 'artifact', 'left_id': no_time, 'reason': 'no_usable_time_candidate'} in unmatched['recordings']
    assert unmatched['calls'] == [lonely_call]


def test_uncorroborated_unknown_zone_match_is_not_proposed(cx):
    cx.call('2024-06-03T08:15:00Z', 300, '+48 000 000 999')
    recording = cx.recording('2024-06-03 10:15:00', 20, '000000123')
    run = cx.run()
    assert run['hypothesis_count'] == 0
    assert {'left_kind': 'artifact', 'left_id': recording,
            'reason': 'only_uncorroborated_unknown_zone_matches'} in run['result']['unmatched']['recordings']
    relaxed = cx.run(params={'unknown_zone_requires_corroboration': False})
    [h] = cx.hypotheses(relaxed['id'])
    assert h['criteria']['duration']['status'] == 'disagree' and h['criteria']['number']['status'] == 'mismatch'


def test_dst_change_between_filename_and_utc_log(cx):
    before = cx.call('2025-03-30T00:59:00Z', 40, '+48 000 000 321')   # 01:59 CET (UTC+1)
    after = cx.call('2025-03-30T01:01:30Z', 40, '+48 000 000 321')    # 03:01:30 CEST (UTC+2)
    rec_before = cx.recording('2025-03-30 01:59:00', 40, '000000321')
    rec_after = cx.recording('2025-03-30 03:01:30', 40, '000000321')
    run = cx.run()
    by_left = {}
    for h in cx.hypotheses(run['id']):
        by_left.setdefault(h['left_id'], {})[h['right_id']] = h['criteria']['time']
    assert by_left[rec_before][before]['implied_utc_offset_minutes'] == 60
    assert by_left[rec_after][after]['implied_utc_offset_minutes'] == 120
    # 2.5 minutes apart: the other call would need a residual of 150 s (> 120 s tolerance) under any real offset.
    assert set(by_left[rec_before]) == {before} and set(by_left[rec_after]) == {after}


def test_dst_fold_under_assumed_zone_tests_both_instants(cx):
    early = cx.call('2025-10-26T00:30:00Z', 50, '+31 6 0000 0001')
    late = cx.call('2025-10-26T01:30:00Z', 50, '+31 6 0000 0001')
    recording = cx.recording('2025-10-26 02:30:00', 50, '0600000001')
    with pytest.raises(CorrelationError):
        cx.run(params={'assume_zone': 'Europe/Amsterdam'})
    run = cx.run(params={'assume_zone': 'Europe/Amsterdam', 'assumption_rule': 'synthetic-zone-1'})
    hypotheses = cx.hypotheses(run['id'])
    assert {h['right_id'] for h in hypotheses} == {early, late}
    for h in hypotheses:
        time = h['criteria']['time']
        assert time['mode'] == 'assumed_zone' and time['tested_point']['dst'] == 'ambiguous_fold'
        assert time['tested_point']['assumption_rule'] == 'synthetic-zone-1' and time['residual_seconds'] == 0
        assert h['candidate_count'] == 2 and h['left_id'] == recording


def test_ampm_ambiguity_tests_the_twelve_hour_alternative(cx):
    call = cx.call('2024-05-01T12:15:00Z', 70, '+48 000 000 444')
    cx.recording('2024-05-01 02:15:00', 70, '000000444', ampm_ambiguous=True)
    # Unknown zone: 02:15 at UTC-10 and 14:15 at UTC+2 both explain the log; both are reported, none is picked.
    [h] = cx.hypotheses(cx.run()['id'])
    time = h['criteria']['time']
    readings = [(time['tested_point']['ampm_alternative'], time['implied_utc_offset_minutes'])]
    readings += [(a['ampm_alternative'], a['implied_utc_offset_minutes']) for a in time['other_matching_points']]
    assert sorted(readings) == [(False, -600), (True, 120)]
    zoned = cx.run(params={'assume_zone': 'Europe/Warsaw', 'assumption_rule': 'synthetic-zone-2'})
    [h] = cx.hypotheses(zoned['id'])
    assert h['right_id'] == call
    assert h['criteria']['time']['tested_point']['ampm_alternative'] is True
    assert h['criteria']['time']['tested_point']['local'] == '2024-05-01T14:15:00'
    assert h['criteria']['time']['other_matching_points'] == []
    off = cx.run(params={'consider_ampm_ambiguity': False, 'assume_zone': 'Europe/Warsaw', 'assumption_rule': 'synthetic-zone-2'})
    assert off['hypothesis_count'] == 0


def test_review_history_links_and_projection_effect(cx):
    call = cx.call('2024-06-03T08:15:00Z', 95, '+48 000 000 123')
    recording = cx.recording('2024-06-03 10:15:04', 95, '000000123')
    cx.evidence.add_derived_text(recording, 'transcript', 'x', segments=[{'start': 1.0, 'end': 2.0, 'text': 'x'}])
    assert cx.segment_time(recording)['basis'] == 'local_as_utc_unknown_zone'
    [h] = cx.hypotheses(cx.run()['id'])
    accepted = cx.review(h['id'], 'accept', reason='durations and number agree')
    link_id = accepted['history'][-1]['link_id']
    with cx.dbmod.session() as db:
        link = dict(db.execute('SELECT * FROM links WHERE id=?', (link_id,)).fetchone())
    assert (link['left_kind'], link['left_id'], link['right_kind'], link['right_id']) == ('artifact', recording, 'event', call)
    assert link['confidence'] is None and json.loads(link['rationale'])['hypothesis_id'] == h['id']
    accepted_time = cx.segment_time(recording)
    assert accepted_time['basis'] == 'utc_epoch' and accepted_time['candidate_ref'] == f"correlation:{h['id']}"
    assert accepted_time['display'] == '2024-06-03T08:15:01Z'
    with pytest.raises(TransitionError):
        cx.review(h['id'], 'accept')
    revoked = cx.review(h['id'], 'revoke', reason='wrong call')
    assert [s['status'] for s in revoked['history']] == ['proposed', 'accepted', 'revoked']
    with cx.dbmod.session() as db:
        assert db.execute('SELECT 1 FROM links WHERE id=?', (link_id,)).fetchone() is None
        audit = db.execute("SELECT details_json FROM audit_log WHERE action='timeline_hypothesis_revoke'").fetchone()
        assert json.loads(audit[0])['removed_link']['id'] == link_id
    assert cx.segment_time(recording)['basis'] == 'local_as_utc_unknown_zone'
    rejected = cx.review(h['id'], 'reject', reason='re-decided after revoke')
    assert rejected['status'] == 'rejected'
    no_link = cx.review(h['id'], 'revoke', reason='undo')
    assert no_link['status'] == 'revoked'
    plain = cx.review(h['id'], 'accept', reason='accept without link', create_link=False)
    assert plain['history'][-1]['link_id'] is None
    with pytest.raises(CorrelationError):
        cx.review(h['id'], 'approve')
    with pytest.raises(CorrelationError):
        cx.review(h['id'], 'revoke', reason='  ')
    with pytest.raises(LookupError):
        cx.review(999999, 'accept')


def test_hypothesis_tables_are_append_only_and_recompute_is_a_new_run(cx):
    cx.call('2024-06-03T08:15:00Z', 95, '+48 000 000 123')
    cx.recording('2024-06-03 10:15:04', 95, '000000123')
    first = cx.run()
    [old] = cx.hypotheses(first['id'])
    cx.review(old['id'], 'reject', reason='first pass')
    second = cx.run(params={'time_tolerance_seconds': 30})
    [new] = cx.hypotheses(second['id'])
    assert new['id'] != old['id'] and second['params']['time_tolerance_seconds'] == 30
    assert cx.hypotheses(first['id'])[0]['status'] == 'rejected' and new['status'] == 'proposed'
    with cx.dbmod.session() as db:
        for sql in ('UPDATE timeline_hypotheses SET relation=relation', 'DELETE FROM timeline_hypotheses',
                    'UPDATE timeline_hypothesis_status SET reason=reason', 'DELETE FROM timeline_hypothesis_status',
                    'UPDATE timeline_correlation_runs SET status=status', 'DELETE FROM timeline_correlation_runs'):
            with pytest.raises(sqlite3.DatabaseError):
                db.execute(sql)


def test_parameter_validation(cx):
    with pytest.raises(CorrelationError):
        cx.run(params={'no_such_parameter': 1})
    with pytest.raises(CorrelationError):
        cx.run(params={'time_tolerance_seconds': 'soon'})
    with pytest.raises(CorrelationError):
        cx.run(params={'time_tolerance_seconds': -1})
    from server.app.timeline.ruleset import RuleError
    with pytest.raises(RuleError):
        cx.run(rule='display_time')


def test_email_chat_needs_proximity_and_shared_text(cx):
    email = cx.evidence.add_event(source_id=cx.phone, artifact_id=None, event_type='email', subject='Wezwanie',
                                  body='Prosimy o przesłanie dokumentów do dnia 15 marca w sprawie numer 12',
                                  external_id='mail:1')
    with cx.dbmod.session() as db:
        timeinfo.record_event_times(db, email, [timeinfo.from_iso('header_date', '2024-03-01T09:00:00+01:00', 'synthetic:mbox')])
    quoting = cx.evidence.add_event(source_id=cx.phone, artifact_id=None, event_type='chat_message', thread_id='c1',
                                    body='Dostałem: "prosimy o przesłanie dokumentów do dnia 15 marca" — co robić?',
                                    external_id='chat:1', metadata={'chatbot': 'claude'})
    unrelated = cx.evidence.add_event(source_id=cx.phone, artifact_id=None, event_type='chat_message', thread_id='c1',
                                      body='Zupełnie inna rozmowa o pogodzie', external_id='chat:2', metadata={'chatbot': 'claude'})
    earlier = cx.evidence.add_event(source_id=cx.phone, artifact_id=None, event_type='chat_message', thread_id='c2',
                                    body='prosimy o przesłanie dokumentów do dnia 15 marca', external_id='chat:3',
                                    metadata={'chatbot': 'chatgpt'})
    with cx.dbmod.session() as db:
        timeinfo.record_event_times(db, quoting, [timeinfo.from_epoch('created', 1709290800, 'synthetic:ct')])     # 11:00Z
        timeinfo.record_event_times(db, unrelated, [timeinfo.from_epoch('created', 1709290860, 'synthetic:ct')])
        timeinfo.record_event_times(db, earlier, [timeinfo.from_epoch('created', 1709200000, 'synthetic:ct')])     # a day before
    run = cx.run('email_chat')
    pairs = {(h['left_id'], h['right_id']): h['criteria'] for h in cx.hypotheses(run['id'])}
    assert set(pairs) == {(email, quoting), (email, earlier)}
    overlap = pairs[(email, quoting)]['quote_overlap']
    assert overlap['status'] == 'shared' and overlap['shared_ngrams'] >= 1 and overlap['example']
    assert pairs[(email, quoting)]['proximity']['min_possible_gap_seconds'] == 10800
    directed = cx.run('email_chat', {'direction': 'email_first'})
    assert {h['right_id'] for h in cx.hypotheses(directed['id'])} == {quoting}
    loose = cx.run('email_chat', {'require_quote_overlap': False, 'max_lag_seconds': 7200 * 2})
    assert {h['right_id'] for h in cx.hypotheses(loose['id'])} == {quoting, unrelated}


def test_same_conversation_across_exports(cx):
    exports = []
    for n in range(2):
        path = cx.root / f'conversations-{n}.json'
        path.write_text(json.dumps({'export': n}))
        exports.append(cx.evidence.ingest_file(path, source_id=cx.phone, source_locator=f'synthetic:export:{n}'))
    skipped = cx.run('same_conversation')
    assert skipped['status'] == 'skipped' and skipped['hypothesis_count'] == 0
    for n, artifact in enumerate(exports):
        for message in range(3 + n):
            event = cx.evidence.add_event(source_id=cx.phone, artifact_id=artifact, event_type='chat_message', thread_id='conv-x',
                                          body=f'm{message}', external_id=f'msg-{message}', metadata={'chatbot': 'chatgpt'})
            with cx.dbmod.session() as db:
                timeinfo.record_event_times(db, event, [timeinfo.from_epoch('created', 1700000000 + message, 'synthetic:ct')])
    run = cx.run('same_conversation')
    [h] = cx.hypotheses(run['id'])
    assert (h['left_id'], h['right_id']) == tuple(exports)
    assert h['criteria']['mode'] == 'thread_id_fallback'
    assert h['criteria']['shared_external_ids']['count'] == 3
    assert h['criteria']['message_times']['right']['events'] == 4

"""Hard synthetic activity data for time budgets and feature series (generated, never committed).

``build(root)`` writes a generic category configuration (``categories.json``, the
same shape a user keeps privately outside the repository) and a tiny generated
WAV, and returns the manifest of planted cases. ``populate(manifest)`` inserts
the events into the database configured in ``server.app.db.settings``. Zone of
the planted wall clock: Europe/Warsaw. Only fictional numbers (+48 000 000 xxx)
and example.org addresses are used.

Planted cases (local dates):

* 2025-03-27 parallel Claude (10:00–10:40) and ChatGPT (10:20–11:00)
  conversations, both ``praca_projekty``: the union must be counted once;
* 2025-03-28 an incoming official call 09:00 (1200 s) overlapping a Claude
  conversation 09:10–09:30 (overlap rule matters), an outgoing call 14:00
  (600 s, default category), a missed call (0 s), a calendar entry 16:00–17:00
  whose rule splits it 0.5/0.5, a 2 s WAV recording at 18:00 (header duration),
  an SMS with a health keyword that a human later re-labels, a reasoning part and
  a hidden chat message (both filtered), a night message at 23:30;
* 2025-03-29 one lone incoming SMS at 12:00 (point event: only the upper bound
  gets time);
* 2025-03-30 DST spring-forward day (23 h) with one outgoing SMS at 12:00; an
  unknown-zone wall clock 02:30 (nonexistent under the zone) and an unknown-zone
  18:00 (excluded unless a zone is assumed);
* 2025-03-31 … 2025-04-02 no data at all (must be ``nieobserwowane``);
* 2025-04-03 ten outgoing SMS (400 chars, hourly, ``praca_projekty``) versus five
  incoming Gemini answers (3333 chars, every 2 h, ``zdrowie``): the top category
  flips under the typing/reading grid;
* 2025-10-26 DST fall-back day (25 h) with one call 01:30 UTC+1 lasting 600 s.
"""
from __future__ import annotations

import json
import struct
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ZONE = 'Europe/Warsaw'
OFFICIAL_NUMBER = '+48 000 000 101'
OTHER_NUMBER = '+48 000 000 202'
OFFICIAL_EMAIL = 'desk@urzad.example.org'

CATEGORIES = {
    'schema': 'ageds.activity_categories.v1',
    'version': 'corpus-activity-1',
    'categories': [
        {'id': 'praca_projekty', 'label': 'Praca / projekty', 'color': '#4c78a8'},
        {'id': 'sprawy_urzedowe', 'label': 'Sprawy urzędowe', 'color': '#e45756'},
        {'id': 'zdrowie', 'label': 'Zdrowie', 'color': '#54a24b'},
        {'id': 'odpoczynek', 'label': 'Odpoczynek', 'color': '#b279a2'},
        {'id': 'inne', 'label': 'Inne', 'color': '#f58518'},
    ],
    'default_category': 'inne',
    'combine': 'max',
    'official_categories': ['sprawy_urzedowe'],
    'rules': [
        {'id': 'urzedy-numery', 'version': 1, 'kind': 'counterparty', 'values': [OFFICIAL_NUMBER],
         'weights': {'sprawy_urzedowe': 1}},
        {'id': 'urzedy-domeny', 'version': 1, 'kind': 'email_domain', 'values': ['urzad.example.org'],
         'weights': {'sprawy_urzedowe': 1}},
        {'id': 'zdrowie-slowa', 'version': 1, 'kind': 'keyword', 'values': ['lekarz', 'recepta'],
         'weights': {'zdrowie': 1}},
        {'id': 'projekt-zdrowotny', 'version': 1, 'kind': 'keyword', 'values': ['projekt zdrowotny'],
         'fields': ['subject'], 'weights': {'praca_projekty': 0.5, 'zdrowie': 0.5}},
        {'id': 'chatboty-praca', 'version': 1, 'kind': 'chatbot', 'values': ['claude', 'chatgpt'],
         'weights': {'praca_projekty': 1}},
        {'id': 'sms-praca', 'version': 1, 'kind': 'keyword', 'values': ['raport'], 'fields': ['body'],
         'event_types': ['sms'], 'weights': {'praca_projekty': 1}},
    ],
}


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(ZONE))


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def epoch(text: str) -> float:
    return local(text).timestamp()


def _chat(conv: str, chatbot: str, start: str, turns: int, step_min: int, *, user_chars=20, answer_chars=500,
          subject='synthetic'):
    rows, t = [], local(start)
    for i in range(turns):
        rows.append({'kind': 'chat', 'chatbot': chatbot, 'conv': conv, 'role': 'user', 'at': t,
                     'body': 'u' * user_chars, 'subject': subject, 'id': f'{conv}-u{i}'})
        rows.append({'kind': 'chat', 'chatbot': chatbot, 'conv': conv, 'role': 'assistant',
                     'at': t + timedelta(minutes=1), 'body': 'a' * answer_chars, 'subject': subject,
                     'id': f'{conv}-a{i}'})
        t += timedelta(minutes=step_min)
    return rows


def build(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / 'categories.json'
    config_path.write_text(json.dumps(CATEGORIES, ensure_ascii=False, indent=2), encoding='utf-8')
    wav_path = root / 'synthetic-recording.wav'
    with wave.open(str(wav_path), 'wb') as audio:
        audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        audio.writeframes(struct.pack('<h', 0) * 16000)  # 2.0 s of silence
    claude_parallel = _chat('conv-claude-1', 'claude', '2025-03-27T10:00', 5, 10)      # 10:00..10:41
    chatgpt_parallel = _chat('conv-chatgpt-1', 'chatgpt', '2025-03-27T10:20', 5, 10)   # 10:20..11:01
    claude_during_call = _chat('conv-claude-2', 'claude', '2025-03-28T09:10', 3, 10)   # 09:10..09:31
    gemini = [{'kind': 'chat', 'chatbot': 'gemini', 'conv': 'conv-gemini-1', 'role': 'assistant',
               'at': local(f'2025-04-03T{8 + 2 * i:02d}:30'), 'body': 'lekarz ' + 'g' * 3326,
               'subject': 'synthetic', 'id': f'gem-{i}'} for i in range(5)]
    sms_flip = [{'kind': 'sms', 'direction': 'outgoing', 'at': local(f'2025-04-03T{8 + i:02d}:00'),
                 'number': OTHER_NUMBER, 'body': 'raport ' + 'x' * 393, 'id': f'flip-{i}'} for i in range(10)]
    rows = claude_parallel + chatgpt_parallel + claude_during_call + gemini + sms_flip + [
        {'kind': 'call', 'direction': 'incoming', 'at': local('2025-03-28T09:00'), 'duration': 1200,
         'number': OFFICIAL_NUMBER, 'id': 'call-official'},
        {'kind': 'call', 'direction': 'outgoing', 'at': local('2025-03-28T14:00'), 'duration': 600,
         'number': OTHER_NUMBER, 'id': 'call-out'},
        {'kind': 'call', 'direction': 'missed', 'at': local('2025-03-28T15:00'), 'duration': 0,
         'number': OTHER_NUMBER, 'id': 'call-missed'},
        {'kind': 'calendar', 'at': local('2025-03-28T16:00'), 'end': local('2025-03-28T17:00'),
         'subject': 'Projekt zdrowotny: spotkanie', 'id': 'cal-split'},
        {'kind': 'recording', 'at': local('2025-03-28T18:00'), 'id': 'rec-1'},
        {'kind': 'sms', 'direction': 'incoming', 'at': local('2025-03-28T19:00'), 'number': OTHER_NUMBER,
         'body': 'Recepta do odbioru', 'id': 'sms-override'},
        {'kind': 'email', 'direction': 'incoming', 'at': local('2025-03-28T20:00'), 'sender': OFFICIAL_EMAIL,
         'subject': 'Pismo', 'body': 'Synthetic letter', 'id': 'mail-official'},
        {'kind': 'sms', 'direction': 'incoming', 'at': local('2025-03-28T23:30'), 'number': OFFICIAL_NUMBER,
         'body': 'night', 'id': 'sms-night-official'},
        {'kind': 'chat', 'chatbot': 'claude', 'conv': 'conv-claude-2', 'role': 'assistant',
         'at': local('2025-03-28T09:12'), 'body': 'thinking ' * 50, 'subject': 'synthetic',
         'content_kind': 'reasoning', 'id': 'reasoning-part'},
        {'kind': 'chat', 'chatbot': 'claude', 'conv': 'conv-claude-2', 'role': 'system',
         'at': local('2025-03-28T09:13'), 'body': 'hidden', 'subject': 'synthetic', 'hidden': True,
         'id': 'hidden-system'},
        {'kind': 'sms', 'direction': 'incoming', 'at': local('2025-03-29T12:00'), 'number': OTHER_NUMBER,
         'body': 'lone message', 'id': 'sms-lone'},
        {'kind': 'sms', 'direction': 'outgoing', 'at': local('2025-03-30T12:00'), 'number': OTHER_NUMBER,
         'body': 'y' * 100, 'id': 'sms-dst'},
        {'kind': 'unknown_zone', 'local': '2025-03-30 02:30', 'id': 'wa-nonexistent'},
        {'kind': 'unknown_zone', 'local': '2025-03-30 18:00', 'id': 'wa-unknown'},
        {'kind': 'call', 'direction': 'incoming', 'at': datetime(2025, 10, 26, 0, 30, tzinfo=timezone.utc),
         'duration': 600, 'number': OTHER_NUMBER, 'id': 'call-fallback'},
    ]
    return {
        'zone': ZONE, 'config_path': str(config_path), 'wav_path': str(wav_path), 'rows': rows,
        'window': {'from': '2025-03-27', 'to': '2025-04-04'},
        'flip_window': {'from': '2025-04-03', 'to': '2025-04-04'},
        'fallback_window': {'from': '2025-10-26', 'to': '2025-10-27'},
        'expect': {
            'parallel_day': '2025-03-27', 'call_day': '2025-03-28', 'lone_day': '2025-03-29',
            'dst_short_day': '2025-03-30', 'empty_days': ['2025-03-31', '2025-04-01', '2025-04-02'],
            'flip_day': '2025-04-03', 'dst_long_day': '2025-10-26',
            'official_call_s': 1200, 'recording_s': 2.0, 'calendar_split': {'praca_projekty': 1800, 'zdrowie': 1800},
            'untimed_reasons': {'unknown_zone': 2},
            'untimed_reasons_assumed_zone': {'unknown_zone': 1},  # 02:30 does not exist on the DST day
            'filtered': {'chat_content_kind:reasoning': 1, 'hidden_by_projection': 1},
        },
    }


def populate(manifest: dict) -> dict[str, int]:
    """Insert the planted events; returns ``{row id: event id}``."""
    from server.app import evidence, timeinfo
    from server.app.db import session
    src = evidence.ensure_source('synthetic', 'activity corpus')
    wav = evidence.ingest_file(Path(manifest['wav_path']), source_id=src, source_locator='synthetic:activity-recording',
                               mime_type='audio/wav')
    ids = {}
    for row in manifest['rows']:
        kind, rid = row['kind'], row['id']
        if kind == 'chat':
            meta = {'chat': {'chatbot': row['chatbot'], 'conversation_id': row['conv'], 'role': row['role'],
                             'content_kind': row.get('content_kind', 'text'), 'hidden': row.get('hidden', False)}}
            direction = {'user': 'outgoing', 'assistant': 'incoming'}.get(row['role'])
            eid = evidence.add_event(source_id=src, artifact_id=None, event_type='chat_message', ts_start=utc_iso(row['at']),
                                     direction=direction, sender=row['role'], contact_label=row['chatbot'],
                                     subject=row['subject'], body=row['body'], thread_id=row['conv'],
                                     external_id=f'activity/{rid}', metadata=meta)
            with session() as db:
                timeinfo.record_event_times(db, eid, [timeinfo.from_epoch('created', row['at'].timestamp(), 'synthetic')])
        elif kind == 'sms':
            eid = evidence.add_event(source_id=src, artifact_id=None, event_type='sms', ts_start=utc_iso(row['at']),
                                     direction=row['direction'], phone_or_address=row['number'], body=row['body'],
                                     external_id=f'activity/{rid}')
        elif kind == 'call':
            end = row['at'] + timedelta(seconds=row['duration'])
            eid = evidence.add_event(source_id=src, artifact_id=None, event_type='call', ts_start=utc_iso(row['at']),
                                     ts_end=utc_iso(end) if row['duration'] else None, direction=row['direction'],
                                     phone_or_address=row['number'], external_id=f'activity/{rid}',
                                     metadata={'duration': {'raw': str(row['duration']), 'seconds': row['duration'],
                                                            'unit': 'seconds'}})
        elif kind == 'calendar':
            eid = evidence.add_event(source_id=src, artifact_id=None, event_type='calendar_event',
                                     ts_start=utc_iso(row['at']), ts_end=utc_iso(row['end']), subject=row['subject'],
                                     external_id=f'activity/{rid}')
        elif kind == 'recording':
            eid = evidence.add_event(source_id=src, artifact_id=wav, event_type='recording', ts_start=utc_iso(row['at']),
                                     external_id=f'activity/{rid}')
        elif kind == 'email':
            eid = evidence.add_event(source_id=src, artifact_id=None, event_type='email', ts_start=utc_iso(row['at']),
                                     direction=row['direction'], sender=row['sender'], subject=row['subject'],
                                     body=row['body'], external_id=f'activity/{rid}')
        elif kind == 'unknown_zone':
            eid = evidence.add_event(source_id=src, artifact_id=None, event_type='whatsapp_message', ts_start=row['local'],
                                     body='wall clock without zone', external_id=f'activity/{rid}')
        else:
            raise ValueError(kind)
        ids[rid] = eid
    return ids


def serve() -> None:
    """Browser fixture: temporary database with this corpus, two runs and a correction."""
    import os
    import tempfile
    with tempfile.TemporaryDirectory(prefix='ageds-activity-browser-') as directory:
        root = Path(directory)
        os.environ.update(EW_DATA_DIR=str(root), EW_DB_PATH=str(root / 'evidence.sqlite'), EW_STORE_DIR=str(root / 'store'))
        import uvicorn
        from server.app import db
        from server.app.analysis import budget, categories
        db.init_db()
        manifest = build(root / 'activity')
        ids = populate(manifest)
        config = categories.load_config(manifest['config_path'])
        with db.session() as connection:
            categories.add_annotation(connection, ids['sms-override'], {'odpoczynek': 1}, note='synthetic correction')
            first = budget.run_budget(connection, config, window_from=manifest['window']['from'],
                                      window_to=manifest['window']['to'], zone=ZONE)
            second = budget.run_budget(connection, config, window_from=manifest['window']['from'],
                                       window_to=manifest['window']['to'], zone=ZONE,
                                       params={'overlap_rule': 'count_both', 'granularities': ['day', 'week']})
        os.environ['AGEDS_ACTIVITY_CATEGORIES'] = manifest['config_path']
        print(json.dumps({'runs': [first['run']['run_id'], second['run']['run_id']], 'events': ids,
                          'zone': ZONE, 'window': manifest['window']}), flush=True)
        uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    serve()

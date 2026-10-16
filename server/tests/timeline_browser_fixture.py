"""Isolated synthetic timeline fixture for real-Chromium checks; never a live catalog.

All names, numbers and texts are generated (fictional zero-block numbers,
example.org addresses). ``AGEDS_TIMELINE_BULK`` adds that many bulk events
through a fast SQL path that mirrors the ``events``/``event_times``/FTS columns
written by ``evidence.add_event`` (audit rows are skipped for speed and the
receipt says so).
"""
from __future__ import annotations

import json
import os
import random
import struct
import tempfile
import time
import wave
from pathlib import Path

LITERAL = '<img src=x onerror="window.__timelineSourceExecuted=1">'


def _wav(path: Path, seconds: int = 90):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        audio.writeframes(struct.pack('<h', 0) * 8000 * seconds)


def build_small(root: Path) -> dict:
    """Hand-made cases: legacy SMS/WhatsApp, two chatbots in parallel, call + recording + transcript, undated."""
    from server.app import db, evidence, timeinfo
    phone = evidence.ensure_source('synthetic', 'Telefon A (syntetyczny)')
    chats = evidence.ensure_source('synthetic', 'Eksport czatów (syntetyczny)')
    with db.session() as connection:
        case_id = connection.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()[0]
        collection = connection.execute("INSERT INTO collections(case_id,label) VALUES(?, 'Kolekcja syntetyczna 1')", (case_id,)).lastrowid
    sample = root / 'sms-sample.xml'
    sample.write_text('<smses count="0"></smses>', encoding='utf-8')
    sms_artifact = evidence.ingest_file(sample, source_id=phone, source_locator='synthetic:sms', collection_id=collection)
    ids = {}
    ids['sms'] = evidence.add_event(source_id=phone, artifact_id=sms_artifact, event_type='sms', direction='incoming',
                                    ts_start='2025-10-26T00:30:00+00:00', phone_or_address='+48 000 000 101',
                                    body='SMS ' + LITERAL, external_id='sms:1',
                                    metadata={'timestamp': {'raw': '1761438600000', 'format': 'unix_milliseconds'},
                                              'raw_attributes': {'date': '1761438600000', 'date_sent': '1761438590000'}})
    ids['whatsapp_fold'] = evidence.add_event(source_id=phone, artifact_id=sms_artifact, event_type='whatsapp_message',
                                              ts_start='2025-10-26T02:30:00', sender='Osoba B', thread_id='Czat syntetyczny',
                                              body='WhatsApp ' + LITERAL, external_id='wa:1',
                                              metadata={'timestamp': {'raw_time': '2:30', 'timezone_status': 'unknown'}})
    base = 1761475200  # 2025-10-26T10:40:00Z
    for i in range(6):
        for bot, offset in (('chatgpt', 0), ('claude', 90)):
            role = 'user' if i % 2 == 0 else 'assistant'
            event = evidence.add_event(source_id=chats, artifact_id=None, event_type='chat_message', thread_id=f'{bot}-conv-1',
                                       sender=role, contact_label=bot, body=f'{bot} wiadomość {i} ' + LITERAL,
                                       external_id=f'{bot}:{i}', metadata={'chat': {'chatbot': bot, 'role': role, 'hidden': False}})
            with db.session() as connection:
                timeinfo.record_event_times(connection, event, [
                    timeinfo.from_epoch('created', base + offset + i * 120, f'synthetic:{bot}:create_time')])
            ids.setdefault(bot, []).append(event)
    part = evidence.add_event(source_id=chats, artifact_id=None, event_type='chat_message_part', thread_id='claude-conv-1',
                              contact_label='claude', body='claude wiadomość — część (myślenie) ' + LITERAL, external_id='claude:part:1',
                              metadata={'chat': {'chatbot': 'claude', 'content_kind': 'reasoning'}})
    with db.session() as connection:
        timeinfo.record_event_times(connection, part, [timeinfo.from_epoch('start', base + 95, 'synthetic:claude:block_start')])
    ids['hidden_part'] = part
    ids['call'] = evidence.add_event(source_id=phone, artifact_id=sms_artifact, event_type='call', direction='incoming',
                                     ts_start='2025-10-26T11:00:00+00:00', ts_end='2025-10-26T11:01:30+00:00',
                                     phone_or_address='+48 000 000 101', external_id='call:1',
                                     metadata={'timestamp': {'raw': '1761476400000'}, 'duration': {'seconds': 90}})
    recordings = evidence.ensure_source('synthetic', 'Nagrania (syntetyczne)')
    wav = root / 'rec.wav'
    _wav(wav, 90)
    recording = evidence.ingest_file(wav, source_id=recordings, source_locator='synthetic:rec', mime_type='audio/wav',
                                     collection_id=collection)
    rec_event = evidence.add_event(source_id=recordings, artifact_id=recording, event_type='recording', external_id='rec:1',
                                   metadata={'filename_number': '000000101'})
    with db.session() as connection:
        timeinfo.record_event_times(connection, rec_event, [
            timeinfo.from_iso('filename_derived', '2025-10-26 12:00:04', 'synthetic:recording_name:v1')])
    transcript = evidence.add_derived_text(recording, 'transcript', 'Dzień dobry. ' + LITERAL, segments=[
        {'start': 0.0, 'end': 4.0, 'text': 'Dzień dobry.', 'language': 'pl'},
        {'start': 4.0, 'end': 9.5, 'text': 'Goedemiddag ' + LITERAL, 'language': 'nl'}])
    ids['undated'] = evidence.add_event(source_id=phone, artifact_id=sms_artifact, event_type='whatsapp_message',
                                        sender='Osoba B', thread_id='Czat syntetyczny', body='bez daty ' + LITERAL,
                                        external_id='wa:undated',
                                        metadata={'timestamp': {'status': 'unknown_century', 'raw_date': '01.02.24'}})
    with db.session() as connection:
        tag = connection.execute("INSERT INTO tags(name) VALUES('syntetyczny-tag')").lastrowid
        connection.execute('INSERT INTO event_tags(event_id,tag_id) VALUES(?,?)', (ids['sms'], tag))
    # Workbench bookmarks (human annotations) over the chatbot day: a point linked to an event, a range,
    # one far outside any window and one without a zone (no UTC instant, never placed).
    from server.app.workbench import bookmarks
    marks = {
        'point': bookmarks.create_bookmark('2025-10-26T10:45:00Z', label='Zakładka ' + LITERAL, note='notatka ' + LITERAL,
                                           ref={'kind': 'event', 'id': ids['chatgpt'][0]})['id'],
        'range': bookmarks.create_bookmark('2025-10-26T11:46:00+01:00', '2025-10-26T11:48:00+01:00', label='Zakres')['id'],
        'outside': bookmarks.create_bookmark('2023-01-01T00:00:00Z', label='Poza oknem')['id'],
        'unzoned': bookmarks.create_bookmark('2025-10-26T10:47:00', label='Bez strefy')['id'],
    }
    return {'ids': ids, 'recording': recording, 'transcript': transcript, 'collection': collection, 'literal': LITERAL,
            'bookmarks': marks}


EVENT_MIX = ('sms', 'whatsapp_message', 'chat_message', 'call', 'email')


def bulk_events(db_path: Path, count: int, *, seed: int = 20261016, start: int = 1640995200, span_days: int = 1095,
                source_label: str = 'Masowe syntetyczne') -> dict:
    """Insert ``count`` synthetic events directly (same columns as add_event, no audit rows)."""
    import sqlite3
    from server.app.timeinfo import format_utc
    from datetime import datetime, timezone
    rng = random.Random(seed)
    connection = sqlite3.connect(str(db_path))
    connection.execute('PRAGMA foreign_keys=ON')
    started = time.perf_counter()
    case_id = connection.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()[0]
    sources = [connection.execute('INSERT INTO sources(case_id,kind,label) VALUES(?,?,?)',
                                  (case_id, 'synthetic', f'{source_label} {n}')).lastrowid for n in range(4)]
    first = (connection.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]) + 1
    events, times, fts = [], [], []
    for offset in range(count):
        event_id = first + offset
        kind = EVENT_MIX[offset % len(EVENT_MIX)]
        moment = start + rng.randrange(span_days * 86400)
        number = f'+48 000 000 {rng.randrange(300):03d}'
        body = f'syntetyczna treść {offset} wątek {offset % 97}'
        metadata, ts_start, ts_end, thread, direction = {}, None, None, None, rng.choice(('incoming', 'outgoing'))
        undated = offset % 100 == 99
        if kind == 'sms' and not undated:
            ts_start = datetime.fromtimestamp(moment, timezone.utc).isoformat()
            metadata = {'timestamp': {'raw': str(moment * 1000), 'format': 'unix_milliseconds'}}
        elif kind == 'whatsapp_message' and not undated:
            ts_start = datetime.fromtimestamp(moment, timezone.utc).replace(tzinfo=None).isoformat()
            metadata = {'timestamp': {'raw_time': ts_start[11:16], 'timezone_status': 'unknown'}}
            thread = f'czat-{offset % 40}'
        elif kind == 'call' and not undated:
            ts_start = datetime.fromtimestamp(moment, timezone.utc).isoformat()
            ts_end = datetime.fromtimestamp(moment + 60 + offset % 600, timezone.utc).isoformat()
            metadata = {'timestamp': {'raw': str(moment * 1000)}, 'duration': {'seconds': 60 + offset % 600}}
        elif kind == 'chat_message':
            bot = ('chatgpt', 'claude', 'gemini')[offset % 3]
            thread = f'{bot}-conv-{offset % 500}'
            metadata = {'chat': {'chatbot': bot}}
            if not undated:
                times.append((event_id, 'created', str(moment), format_utc(datetime.fromtimestamp(moment, timezone.utc)), None,
                              'utc_epoch', 'UTC', 'second', 'bulk:create_time', '{"epoch_unit": "s"}'))
        elif kind == 'email' and not undated:
            raw = datetime.fromtimestamp(moment, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
            times.append((event_id, 'header_date', raw, format_utc(datetime.fromtimestamp(moment, timezone.utc)),
                          raw[:19], 'explicit_offset', '+00:00', 'second', 'bulk:email_date', '{}'))
            number = f'osoba{offset % 50}@example.org'
        events.append((event_id, sources[offset % 4], kind, ts_start, ts_end, direction, number, body, thread,
                       f'bulk:{event_id}', json.dumps(metadata, sort_keys=True)))
        fts.append(('event', event_id, f'{kind} | {number}', body))
        if len(events) >= 20000:
            _flush(connection, events, times, fts)
    _flush(connection, events, times, fts)
    connection.commit()
    connection.close()
    return {'inserted': count, 'first_id': first, 'seconds': round(time.perf_counter() - started, 3),
            'undated_share': '1/100', 'audit_rows': 'skipped_in_bulk_fixture'}


def _flush(connection, events, times, fts):
    connection.executemany('''INSERT INTO events(id,source_id,event_type,ts_start,ts_end,direction,phone_or_address,body,
                              thread_id,external_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)''', events)
    connection.executemany('''INSERT INTO event_times(event_id,kind,raw,utc,local,tz_status,tz,precision,source,metadata_json)
                              VALUES(?,?,?,?,?,?,?,?,?,?)''', times)
    connection.executemany('INSERT INTO search_fts(object_kind,object_id,title,content) VALUES(?,?,?,?)', fts)
    events.clear(), times.clear(), fts.clear()


def main():
    with tempfile.TemporaryDirectory(prefix='ageds-timeline-browser-') as directory:
        root = Path(directory)
        os.environ.update(EW_DATA_DIR=str(root), EW_DB_PATH=str(root / 'evidence.sqlite'), EW_STORE_DIR=str(root / 'store'))
        from server.app import db
        import uvicorn
        db.init_db()
        info = build_small(root)
        bulk = int(os.environ.get('AGEDS_TIMELINE_BULK', '0') or 0)
        if bulk:
            info['bulk'] = bulk_events(root / 'evidence.sqlite', bulk)
        print(json.dumps(info), flush=True)
        uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    main()

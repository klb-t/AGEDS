"""Timed items for analyses: the single seam between observations and analysis.

``load_items`` returns plain dicts with UTC epoch seconds. It is an interim
projection (rule ``analysis_source.v1``) over ``events`` + ``event_times`` +
legacy ``ts_start``/``ts_end``; the timeline area's versioned display rule will
replace the body of ``_choose_time`` without changing the item contract.

Wall-clock times with an unknown zone are NOT placed on the UTC axis unless the
caller passes ``assume_zone`` (recorded on every affected item); otherwise they
are returned in ``untimed`` with the reason, so analyses can report them as
excluded instead of silently shifting them.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..timeinfo import TimeCandidate, assume_zone as _assume_zone, from_iso

RULE_ID = 'analysis_source.v1'
START_KINDS = ('start', 'sent', 'received', 'created', 'header_date', 'exif', 'filename_derived',
               'observed', 'updated', 'file_mtime', 'export_time', 'other')
END_KINDS = ('end',)


def _epoch(utc_text: str) -> float:
    return datetime.fromisoformat(utc_text.replace('Z', '+00:00')).astimezone(timezone.utc).timestamp()


def _legacy_candidate(kind: str, raw: str | None) -> TimeCandidate | None:
    if not raw:
        return None
    try:
        return from_iso(kind, raw, f'events.ts_{kind}')
    except ValueError:
        return None


def _choose_time(rows: list[sqlite3.Row], legacy_start: str | None, legacy_end: str | None,
                 assume_zone: str | None) -> dict:
    """Pick start/end candidates by kind priority; report basis and exclusions."""
    candidates = [TimeCandidate(r['kind'], r['raw'], r['source'], utc=r['utc'], local=r['local'],
                                tz_status=r['tz_status'], tz=r['tz'], precision=r['precision'],
                                metadata=json.loads(r['metadata_json'] or '{}')) for r in rows]
    for kind, raw in (('start', legacy_start), ('end', legacy_end)):
        legacy = _legacy_candidate(kind, raw)
        if legacy is not None:
            candidates.append(legacy)

    def pick(kinds):
        ordered = sorted((c for c in candidates if c.kind in kinds), key=lambda c: kinds.index(c.kind))
        for c in ordered:
            if c.utc:
                return c, None
        for c in ordered:
            if c.local and assume_zone:
                assumed = _assume_zone(c, assume_zone, rule=RULE_ID)
                if assumed.utc:
                    return assumed, None
                return None, assumed.metadata.get('dst', 'assume_zone_failed')
        if ordered:
            return None, 'unknown_zone' if any(c.local for c in ordered) else 'no_parseable_time'
        return None, 'no_time_candidate'

    start, reason = pick(START_KINDS)
    end, _ = pick(END_KINDS) if start else (None, None)
    return {'start': start, 'end': end, 'reason': reason}


FIELD_BODY_CHARS = 65_536


def _confirmed_duration(metadata: dict) -> float | None:
    """Source-declared duration in seconds (call logs ``metadata.duration``), else None."""
    duration = metadata.get('duration')
    if isinstance(duration, dict):
        seconds = duration.get('seconds')
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 0 <= seconds < 10 ** 9:
            return float(seconds)
    return None


def _wav_duration(db: sqlite3.Connection, artifact_id: int, cache: dict) -> float | None:
    """Declared WAV duration from the content-store copy's header prefix (never the original)."""
    if artifact_id in cache:
        return cache[artifact_id]
    from ..wav_header import MAX_PREFIX_BYTES, probe_wav_header
    duration = None
    row = db.execute('SELECT stored_path, size_bytes, mime_type, original_name FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    if row and row['stored_path'] and ((row['mime_type'] or '').startswith('audio/') or
                                       (row['original_name'] or '').lower().endswith('.wav')):
        path = Path(row['stored_path'])
        try:
            if path.is_file() and not path.is_symlink():
                with path.open('rb') as handle:
                    probe = probe_wav_header(handle.read(MAX_PREFIX_BYTES), row['size_bytes'])
                value = probe.get('declared_duration_sec')
                if type(value) in (int, float) and math.isfinite(value) and value > 0:
                    duration = float(value)
        except (OSError, ValueError):
            duration = None
    cache[artifact_id] = duration
    return duration


def load_items(db: sqlite3.Connection, *, start_utc: str | None = None, end_utc: str | None = None,
               event_types: tuple[str, ...] | list[str] | None = None, assume_zone: str | None = None,
               limit: int = 1_000_000, fields: bool = False, media_durations: bool = False) -> dict:
    """Return ``{'rule', 'items', 'untimed', 'truncated'}``; items sorted by (t_start, event_id).

    Item keys: event_id, event_type, source_id, artifact_id, collection_id, thread_id,
    counterparty, chatbot, direction, t_start, t_end (or None), time_basis (kind,
    tz_status, source, precision, assumed_zone), text_chars, tags; additive since
    E1: ``chat`` (role, content_kind, hidden, conversation_id, model or None),
    ``end_basis`` (kind/source of the end candidate or None), ``declared_duration_s``
    (source-declared duration, e.g. call logs; with ``media_durations=True`` also
    the declared WAV header duration of the event's audio artifact, with
    ``duration_basis``) and, with ``fields=True``,
    ``fields`` (sender, recipients, subject, contact_label, phone_or_address and
    the first ``FIELD_BODY_CHARS`` characters of body with ``body_truncated``).
    """
    db.row_factory = sqlite3.Row
    has_event_times = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_times'").fetchone()
    where, args = [], []
    if event_types:
        where.append('e.event_type IN (%s)' % ','.join('?' * len(event_types)))
        args.extend(event_types)
    sql = ('SELECT e.*, (SELECT o.collection_id FROM source_observations o WHERE o.artifact_id=e.artifact_id '
           'ORDER BY o.id LIMIT 1) AS collection_id FROM events e')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY e.id LIMIT ?'
    lo = _epoch(start_utc) if start_utc else None
    hi = _epoch(end_utc) if end_utc else None
    items, untimed, wav_cache = [], [], {}
    rows = db.execute(sql, (*args, limit + 1)).fetchall()
    truncated = len(rows) > limit
    for e in rows[:limit]:
        times = db.execute('SELECT * FROM event_times WHERE event_id=? ORDER BY id', (e['id'],)).fetchall() if has_event_times else []
        chosen = _choose_time(times, e['ts_start'], e['ts_end'], assume_zone)
        start = chosen['start']
        if start is None:
            untimed.append({'event_id': e['id'], 'event_type': e['event_type'], 'reason': chosen['reason']})
            continue
        t0 = _epoch(start.utc)
        t1 = _epoch(chosen['end'].utc) if chosen['end'] else None
        if t1 is not None and t1 < t0:
            t1 = None
        if (lo is not None and (t1 or t0) < lo) or (hi is not None and t0 >= hi):
            continue
        metadata = json.loads(e['metadata_json'] or '{}')
        chat = metadata.get('chat') if isinstance(metadata.get('chat'), dict) else {}
        tags = [r[0] for r in db.execute('SELECT t.name FROM event_tags et JOIN tags t ON t.id=et.tag_id WHERE et.event_id=? ORDER BY t.name', (e['id'],))]
        end = chosen['end'] if t1 is not None else None
        item = {
            'event_id': e['id'], 'event_type': e['event_type'], 'source_id': e['source_id'],
            'artifact_id': e['artifact_id'], 'collection_id': e['collection_id'], 'thread_id': e['thread_id'],
            'counterparty': e['contact_label'] or e['phone_or_address'], 'chatbot': chat.get('chatbot'),
            'direction': e['direction'], 't_start': t0, 't_end': t1,
            'time_basis': {'kind': start.kind, 'tz_status': start.tz_status, 'source': start.source,
                           'precision': start.precision, 'assumed_zone': start.metadata.get('assumed_zone')},
            'text_chars': len(e['body']) if e['body'] is not None else None, 'tags': tags,
            'chat': {'role': chat.get('role'), 'content_kind': chat.get('content_kind'), 'hidden': chat.get('hidden'),
                     'conversation_id': chat.get('conversation_id'), 'model': chat.get('model')} if chat else None,
            'end_basis': {'kind': end.kind, 'source': end.source, 'tz_status': end.tz_status} if end else None,
            'declared_duration_s': _confirmed_duration(metadata),
            'duration_basis': 'source_declared' if _confirmed_duration(metadata) is not None else None,
        }
        if item['declared_duration_s'] is None and t1 is None and media_durations and e['artifact_id']:
            wav = _wav_duration(db, e['artifact_id'], wav_cache)
            if wav is not None:
                item['declared_duration_s'], item['duration_basis'] = wav, 'wav_header_declared'
        if fields:
            body = e['body']
            item['fields'] = {'sender': e['sender'], 'recipients': e['recipients'], 'subject': e['subject'],
                              'contact_label': e['contact_label'], 'phone_or_address': e['phone_or_address'],
                              'body': body[:FIELD_BODY_CHARS] if body is not None else None,
                              'body_truncated': body is not None and len(body) > FIELD_BODY_CHARS}
        items.append(item)
    items.sort(key=lambda i: (i['t_start'], i['event_id']))
    return {'rule': RULE_ID, 'assume_zone': assume_zone, 'items': items, 'untimed': untimed, 'truncated': truncated}

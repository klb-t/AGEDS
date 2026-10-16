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
import sqlite3
from datetime import datetime, timezone

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


def load_items(db: sqlite3.Connection, *, start_utc: str | None = None, end_utc: str | None = None,
               event_types: tuple[str, ...] | list[str] | None = None, assume_zone: str | None = None,
               limit: int = 1_000_000) -> dict:
    """Return ``{'rule', 'items', 'untimed', 'truncated'}``; items sorted by (t_start, event_id).

    Item keys: event_id, event_type, source_id, artifact_id, collection_id, thread_id,
    counterparty, chatbot, direction, t_start, t_end (or None), time_basis (kind,
    tz_status, source, precision, assumed_zone), text_chars, tags.
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
    items, untimed = [], []
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
        items.append({
            'event_id': e['id'], 'event_type': e['event_type'], 'source_id': e['source_id'],
            'artifact_id': e['artifact_id'], 'collection_id': e['collection_id'], 'thread_id': e['thread_id'],
            'counterparty': e['contact_label'] or e['phone_or_address'], 'chatbot': chat.get('chatbot'),
            'direction': e['direction'], 't_start': t0, 't_end': t1,
            'time_basis': {'kind': start.kind, 'tz_status': start.tz_status, 'source': start.source,
                           'precision': start.precision, 'assumed_zone': start.metadata.get('assumed_zone')},
            'text_chars': len(e['body']) if e['body'] is not None else None, 'tags': tags,
        })
    items.sort(key=lambda i: (i['t_start'], i['event_id']))
    return {'rule': RULE_ID, 'assume_zone': assume_zone, 'items': items, 'untimed': untimed, 'truncated': truncated}

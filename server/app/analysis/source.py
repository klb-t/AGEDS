"""Timed items for analyses: the single seam between observations and analyses.

``load_items`` delegates to the timeline projection
(``server.app.timeline.analysis_items.load_items``: versioned display rule
``display_time@N`` with its SHA-256, materialized projection, uncertainty
interval ``t_lo``/``t_hi``, ``basis``, ``hidden``) and enriches every item with
the analysis keys added by track E1 (``chat``, ``end_basis``,
``declared_duration_s``/``duration_basis`` and optional ``fields``). The result
names the seam (``timeline``) and the rule id and hash, which every analysis run
records.

The interim projection (rule ``analysis_source.v1`` over ``events`` +
``event_times`` + legacy ``ts_start``/``ts_end``) remains only as a clearly
labelled fallback (``seam: interim_fallback``) for a checkout without the
timeline package; ``load_items_interim`` keeps it testable.

Wall-clock times with an unknown zone are never placed on the UTC axis unless
the caller names an operator zone (and, through the timeline, an
``assumption_rule`` id); otherwise they are returned in ``untimed`` with the
reason, so analyses report them as excluded instead of silently shifting them.
Items hidden by the projection (chat parts, hidden chat messages) are reported
in ``hidden_excluded`` unless ``include_hidden=True``.
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


def _fold_alternatives(candidate: TimeCandidate) -> list[float]:
    """Both instants of a DST fold (never silently resolved: they stay in time_basis and t_lo/t_hi)."""
    values = candidate.metadata.get('utc_alternatives')
    if not isinstance(values, list):
        return []
    out = []
    for value in values:
        try:
            out.append(_epoch(value))
        except (TypeError, ValueError):
            continue
    return out


def _confirmed_duration(metadata: dict) -> float | None:
    """Source-declared duration in seconds (call logs ``metadata.duration``), else None."""
    duration = metadata.get('duration')
    if isinstance(duration, dict):
        seconds = duration.get('seconds')
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 0 <= seconds < 10 ** 9:
            return float(seconds)
    return None


def _signal(metadata: dict) -> dict | None:
    """``metadata.signal`` class/basis of the digital-traces contract (2026-10-17), else None."""
    signal = metadata.get('signal')
    if not isinstance(signal, dict):
        return None
    return {'class': signal.get('class'), 'basis': signal.get('basis')}


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


def _signal_keys(metadata: dict) -> dict:
    """Additive keys from the digital-traces contract (location area L1, 2026-10-17).

    ``signal``: ``{'class', 'basis'}`` from ``metadata.signal`` (passive/active/declared;
    raw_sample/provider_inference/provider_user_edit/derived) or None; ``place_ids``:
    private place ids an event refers to (``metadata.place.id`` of ``place_presence``).
    """
    signal = metadata.get('signal') if isinstance(metadata.get('signal'), dict) else None
    place = metadata.get('place') if isinstance(metadata.get('place'), dict) else None
    return {'signal': {'class': signal.get('class'), 'basis': signal.get('basis')} if signal else None,
            'place_ids': [place['id']] if place and isinstance(place.get('id'), str) else []}


def _describe(db: sqlite3.Connection, e: sqlite3.Row, fields: bool) -> dict:
    """Item keys that do not depend on the time choice."""
    metadata = json.loads(e['metadata_json'] or '{}')
    chat = metadata.get('chat') if isinstance(metadata.get('chat'), dict) else {}
    tags = [r[0] for r in db.execute('SELECT t.name FROM event_tags et JOIN tags t ON t.id=et.tag_id WHERE et.event_id=? ORDER BY t.name', (e['id'],))]
    declared = _confirmed_duration(metadata)
    item = {
        'event_id': e['id'], 'event_type': e['event_type'], 'source_id': e['source_id'],
        'artifact_id': e['artifact_id'], 'collection_id': e['collection_id'], 'thread_id': e['thread_id'],
        'counterparty': e['contact_label'] or e['phone_or_address'], 'chatbot': chat.get('chatbot'),
        'direction': e['direction'],
        'text_chars': len(e['body']) if e['body'] is not None else None, 'tags': tags,
        'chat': {'role': chat.get('role'), 'content_kind': chat.get('content_kind'), 'hidden': chat.get('hidden'),
                 'conversation_id': chat.get('conversation_id'), 'model': chat.get('model')} if chat else None,
        'declared_duration_s': declared,
        'duration_basis': 'source_declared' if declared is not None else None,
        **_signal_keys(metadata),
    }
    if fields:
        body = e['body']
        item['fields'] = {'sender': e['sender'], 'recipients': e['recipients'], 'subject': e['subject'],
                          'contact_label': e['contact_label'], 'phone_or_address': e['phone_or_address'],
                          'body': body[:FIELD_BODY_CHARS] if body is not None else None,
                          'body_truncated': body is not None and len(body) > FIELD_BODY_CHARS}
    return item


_EVENT_SQL = ('SELECT e.*, (SELECT o.collection_id FROM source_observations o WHERE o.artifact_id=e.artifact_id '
              'ORDER BY o.id LIMIT 1) AS collection_id FROM events e')


def event_item(db: sqlite3.Connection, event_id: int) -> dict:
    """One event as an item with ``fields`` and its projected time (timeline seam, default rule).

    Time keys are None and ``untimed_reason`` is set when the event has no usable or
    only an unknown-zone time. Without the timeline package the interim rule is used
    and named in ``rule``.
    """
    db.row_factory = sqlite3.Row
    e = db.execute(_EVENT_SQL + ' WHERE e.id=?', (event_id,)).fetchone()
    if e is None:
        raise LookupError(f'event {event_id} not found')
    item = _describe(db, e, True)
    try:
        from ..timeline.projection import ensure_projection
        from ..timeline.ruleset import display_params, load_rule
    except ImportError:
        times = db.execute('SELECT * FROM event_times WHERE event_id=? ORDER BY id', (event_id,)).fetchall()
        chosen = _choose_time(times, e['ts_start'], e['ts_end'], None)
        start, end = chosen['start'], chosen['end']
        t0 = _epoch(start.utc) if start else None
        t1 = _epoch(end.utc) if start and end else None
        alternatives = _fold_alternatives(start) if start else []
        item.update({'t_start': t0, 't_end': t1 if t1 is not None and t1 >= t0 else None,
                     't_lo': min([t0, *alternatives]) if start else None, 't_hi': max([t0, *alternatives]) if start else None,
                     'dst': start.metadata.get('dst') if start else None,
                     'basis': start.tz_status if start else 'none', 'untimed_reason': chosen['reason'],
                     'rule': RULE_ID, 'seam': 'interim_fallback'})
        return item
    rule = load_rule(None, kind='display_rule')
    projection = ensure_projection(db, rule, display_params())
    row = db.execute("SELECT * FROM timeline_items WHERE projection_id=? AND item_kind='event' AND ref_id=?",
                     (projection['id'], event_id)).fetchone()
    us = 1_000_000
    placed = row is not None and row['sort_us'] is not None and row['basis'] != 'local_as_utc_unknown_zone'
    reason = None
    if not placed:
        reason = 'unknown_zone' if row is not None and row['sort_us'] is not None else \
            json.loads((row['flags_json'] if row is not None else None) or '{}').get('undated_reason', 'no_time_candidate')
    item.update({'t_start': row['sort_us'] / us if placed else None,
                 't_end': row['end_us'] / us if placed and row['end_us'] is not None and row['end_us'] >= row['sort_us'] else None,
                 't_lo': row['lo_us'] / us if placed else None, 't_hi': row['hi_us'] / us if placed else None,
                 'basis': row['basis'] if row is not None else 'none', 'hidden': bool(row['hidden']) if row is not None else None,
                 'untimed_reason': reason, 'rule': f'{rule.rule_id}@{rule.version}', 'rule_sha256': rule.sha256,
                 'seam': 'timeline'})
    return item


def load_items_interim(db: sqlite3.Connection, *, start_utc: str | None = None, end_utc: str | None = None,
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
    sql = _EVENT_SQL
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
        end = chosen['end'] if t1 is not None else None
        item = _describe(db, e, fields)
        alternatives = _fold_alternatives(start)
        item.update({
            't_start': t0, 't_end': t1,
            't_lo': min([t0, *alternatives]), 't_hi': max([t0, *alternatives]),
            'time_basis': {'kind': start.kind, 'tz_status': start.tz_status, 'source': start.source,
                           'precision': start.precision, 'assumed_zone': start.metadata.get('assumed_zone'),
                           'dst': start.metadata.get('dst'),
                           'utc_alternatives': start.metadata.get('utc_alternatives')},
            'end_basis': {'kind': end.kind, 'source': end.source, 'tz_status': end.tz_status} if end else None,
        })
        if item['declared_duration_s'] is None and t1 is None and media_durations and e['artifact_id']:
            wav = _wav_duration(db, e['artifact_id'], wav_cache)
            if wav is not None:
                item['declared_duration_s'], item['duration_basis'] = wav, 'wav_header_declared'
        items.append(item)
    items.sort(key=lambda i: (i['t_start'], i['event_id']))
    for item in items:
        item.setdefault('t_lo', item['t_start'])
        item.setdefault('t_hi', item['t_start'])
        item.setdefault('hidden', False)
    return {'rule': RULE_ID, 'rule_sha256': None, 'seam': 'interim_fallback', 'assume_zone': assume_zone,
            'items': items, 'untimed': untimed, 'hidden_excluded': [], 'truncated': truncated}


def _enrich(db: sqlite3.Connection, items: list[dict], fields: bool, media_durations: bool) -> None:
    """Add the E1 keys to timeline items (batched lookups of the events rows)."""
    wav_cache: dict = {}
    by_id = {it['event_id']: it for it in items}
    ids = list(by_id)
    for offset in range(0, len(ids), 500):
        chunk = ids[offset:offset + 500]
        rows = db.execute('SELECT id, sender, recipients, subject, body, contact_label, phone_or_address, metadata_json '
                          'FROM events WHERE id IN (%s)' % ','.join('?' * len(chunk)), chunk).fetchall()
        for e in rows:
            item = by_id[e['id']]
            metadata = json.loads(e['metadata_json'] or '{}')
            chat = metadata.get('chat') if isinstance(metadata.get('chat'), dict) else {}
            declared = _confirmed_duration(metadata)
            item['chat'] = {'role': chat.get('role'), 'content_kind': chat.get('content_kind'), 'hidden': chat.get('hidden'),
                            'conversation_id': chat.get('conversation_id'), 'model': chat.get('model')} if chat else None
            item['end_basis'] = {'kind': 'end', 'source': 'display_rule_end'} if item['t_end'] is not None else None
            item['declared_duration_s'] = declared
            item['duration_basis'] = 'source_declared' if declared is not None else None
            item.update(_signal_keys(metadata))
            if declared is None and item['t_end'] is None and media_durations and item.get('artifact_id'):
                wav = _wav_duration(db, item['artifact_id'], wav_cache)
                if wav is not None:
                    item['declared_duration_s'], item['duration_basis'] = wav, 'wav_header_declared'
            if fields:
                body = e['body']
                item['fields'] = {'sender': e['sender'], 'recipients': e['recipients'], 'subject': e['subject'],
                                  'contact_label': e['contact_label'], 'phone_or_address': e['phone_or_address'],
                                  'body': body[:FIELD_BODY_CHARS] if body is not None else None,
                                  'body_truncated': body is not None and len(body) > FIELD_BODY_CHARS}


def load_items(db: sqlite3.Connection, *, start_utc: str | None = None, end_utc: str | None = None,
               event_types: tuple[str, ...] | list[str] | None = None, assume_zone: str | None = None,
               assumption_rule: str | None = None, include_hidden: bool = False, limit: int = 1_000_000,
               fields: bool = False, media_durations: bool = False, timeline_rule: str | None = None,
               refresh: bool = True) -> dict:
    """Return ``{'seam', 'rule', 'rule_sha256', 'items', 'untimed', 'hidden_excluded', 'truncated', ...}``.

    Items are sorted by (t_start, event_id) and keep the interim contract: event_id,
    event_type, source_id, artifact_id, collection_id, thread_id, counterparty,
    chatbot, direction, t_start, t_end (or None), time_basis, text_chars, tags;
    timeline keys t_lo, t_hi, basis, candidate_ref, hidden; E1 keys chat,
    end_basis, declared_duration_s, duration_basis and, with ``fields=True``,
    ``fields`` (sender, recipients, subject, contact_label, phone_or_address and the
    first ``FIELD_BODY_CHARS`` characters of body with ``body_truncated``); additive
    since 2026-10-17 (digital traces): ``signal`` and ``place_ids`` (``_signal_keys``).

    ``refresh=False`` is for read-only snapshots (reports): the timeline projection is
    not refreshed (no write); a stale projection raises
    ``timeline.analysis_items.ProjectionNotFresh`` instead of being read silently.
    """
    try:
        from ..timeline.analysis_items import load_items as timeline_load_items
    except ImportError:  # labelled fallback: a checkout without the timeline package
        return load_items_interim(db, start_utc=start_utc, end_utc=end_utc, event_types=event_types,
                                  assume_zone=assume_zone, limit=limit, fields=fields, media_durations=media_durations)
    db.row_factory = sqlite3.Row
    # One read snapshot for every timeline read below (review 2 finding 4): the projection may be
    # evicted by another request between statements; inside the snapshot it stays readable.
    own_snapshot = refresh and not db.in_transaction
    if own_snapshot:
        from ..timeline.analysis_items import open_snapshot
        from ..timeline.ruleset import display_params, load_rule
        open_snapshot(db, load_rule(timeline_rule, kind='display_rule'), display_params(assume_zone, assumption_rule))
    try:
        return _load_timeline_items(db, timeline_load_items, start_utc=start_utc, end_utc=end_utc,
                                    event_types=event_types, assume_zone=assume_zone, assumption_rule=assumption_rule,
                                    include_hidden=include_hidden, limit=limit, fields=fields,
                                    media_durations=media_durations, timeline_rule=timeline_rule, refresh=refresh)
    finally:
        if own_snapshot:
            db.rollback()  # read-only snapshot opened above


def _load_timeline_items(db, timeline_load_items, *, start_utc, end_utc, event_types, assume_zone, assumption_rule,
                         include_hidden, limit, fields, media_durations, timeline_rule, refresh) -> dict:
    try:
        loaded = timeline_load_items(db, start_utc=start_utc, end_utc=end_utc, event_types=event_types,
                                     assume_zone=assume_zone, assumption_rule=assumption_rule, include_hidden=True,
                                     rule=timeline_rule, limit=limit, refresh=refresh)
    except sqlite3.OperationalError as exc:
        if 'within a transaction' not in str(exc):
            raise
        raise ValueError('the timeline projection needs its own refresh transaction: load analysis items before '
                         'writing in this connection (or commit first)') from exc
    folds = [it['event_id'] for it in loaded['items'] if it['time_basis'].get('dst') == 'ambiguous_fold']
    if folds:
        for offset in range(0, len(folds), 500):
            chunk = folds[offset:offset + 500]
            flags = {r[0]: json.loads(r[1] or '{}') for r in db.execute(
                "SELECT ref_id, flags_json FROM timeline_items WHERE projection_id=? AND item_kind='event' "
                'AND ref_id IN (%s)' % ','.join('?' * len(chunk)), (loaded['projection']['id'], *chunk))}
            for it in loaded['items']:
                if it['event_id'] in flags:
                    it['time_basis']['utc_alternatives'] = flags[it['event_id']].get('utc_alternatives')
    if start_utc and end_utc:
        # An unknown-zone wall clock may fall in the window under any real offset: report every such
        # item whose offset interval [t_lo, t_hi] overlaps the window, not only those whose wall clock
        # read as UTC does.
        lo_us, hi_us = int(_epoch(start_utc) * 1_000_000), int(_epoch(end_utc) * 1_000_000)
        seen = {u['event_id'] for u in loaded['untimed']}
        from ..timeline.analysis_items import unknown_zone_reason
        for row in db.execute(
                "SELECT ref_id, event_type, sort_us, flags_json, hidden FROM timeline_items WHERE projection_id=? "
                "AND item_kind='event' AND basis='local_as_utc_unknown_zone' AND lo_us<? AND hi_us>=? ORDER BY ref_id",
                (loaded['projection']['id'], hi_us, lo_us)):
            if row[0] not in seen and (not event_types or row[1] in event_types):
                loaded['untimed'].append({'event_id': row[0], 'event_type': row[1],
                                          'reason': unknown_zone_reason(json.loads(row[3] or '{}')),
                                          'local_as_utc': row[2] / 1_000_000, 'hidden': bool(row[4])})
        loaded['untimed'].sort(key=lambda u: u['event_id'])
    items, hidden = [], []
    for item in loaded['items']:
        if item['hidden'] and not include_hidden:
            hidden.append({'event_id': item['event_id'], 'event_type': item['event_type']})
        else:
            items.append(item)
    _enrich(db, items, fields, media_durations)
    return {**loaded, 'seam': 'timeline', 'assumption_rule': assumption_rule, 'items': items,
            'hidden_excluded': hidden}

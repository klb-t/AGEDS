"""Scoped events for the report (shared by several sections; not a section).

Time placement uses the analysis source seam, which delegates to the timeline
projection (display rule ``display_time@N`` with its SHA-256; the interim rule
``analysis_source.v1`` only without the timeline package). The read never
writes: the report service refreshes the projection (a derived cache) before
its read-only snapshot and the seam refuses a stale cache. An event is on the
UTC axis only when a candidate has a known offset (or the caller passed
``assume_zone`` with a named ``assumption_rule``, recorded on the item).
Untimed events are kept with their reason and reported as exclusions, never
shifted or dropped silently.

Filters are literal: ``counterparty`` is a case-folded substring of
contact label / address / sender / recipients; ``topic`` a case-folded
substring of subject or body. No semantic matching, no diacritics folding.
Hidden chat parts (hidden by the timeline display rule: ``chat_message_part``
events and ``metadata.chat.hidden``) are excluded unless
``include_hidden_chat_parts`` is set; their count is reported.
"""
from __future__ import annotations

import json

from ...analysis.source import RULE_ID, load_items

CHUNK = 500


def _rows_by_id(db, table: str, ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start:start + CHUNK]
        for row in db.execute(f'SELECT * FROM {table} WHERE id IN ({",".join("?" * len(chunk))})', chunk):
            out[row['id']] = dict(row)
    return out


def _hidden(row: dict) -> bool:
    try:
        metadata = json.loads(row.get('metadata_json') or '{}')
    except ValueError:
        return False
    chat = metadata.get('chat') if isinstance(metadata, dict) else None
    return bool(isinstance(chat, dict) and chat.get('hidden') is True)


def _matches(row: dict, counterparty: str | None, topic: str | None) -> bool:
    if counterparty:
        needle = counterparty.casefold()
        if not any(needle in str(row.get(field) or '').casefold()
                   for field in ('contact_label', 'phone_or_address', 'sender', 'recipients')):
            return False
    if topic:
        needle = topic.casefold()
        if not any(needle in str(row.get(field) or '').casefold() for field in ('subject', 'body')):
            return False
    return True


def scoped_events(ctx) -> dict:
    """Cached per report: ``{rule, items, rows, untimed, exclusions, truncated}``."""
    cached = ctx.scope.get('_events')
    if cached is not None:
        return cached
    params = ctx.params
    loaded = load_items(ctx.db, start_utc=params['from'], end_utc=params['to'],
                        event_types=params['event_types'] or None, assume_zone=params['assume_zone'],
                        assumption_rule=params['assumption_rule'], include_hidden=True,
                        limit=params['max_events'], refresh=False)
    timed_ids = [item['event_id'] for item in loaded['items']]
    untimed_ids = [item['event_id'] for item in loaded['untimed']]
    rows = _rows_by_id(ctx.db, 'events', timed_ids + untimed_ids)
    exclusions = {'filtered_by_counterparty_or_topic': 0, 'hidden_chat_parts_excluded': 0}
    items, untimed = [], []
    for item in loaded['items']:
        row = rows[item['event_id']]
        if not _matches(row, params['counterparty'], params['topic']):
            exclusions['filtered_by_counterparty_or_topic'] += 1
            continue
        if (item.get('hidden') or _hidden(row)) and not params['include_hidden_chat_parts']:
            exclusions['hidden_chat_parts_excluded'] += 1
            continue
        items.append(item)
    for item in loaded['untimed']:
        row = rows[item['event_id']]
        if not _matches(row, params['counterparty'], params['topic']):
            exclusions['filtered_by_counterparty_or_topic'] += 1
            continue
        if (item.get('hidden') or _hidden(row)) and not params['include_hidden_chat_parts']:
            exclusions['hidden_chat_parts_excluded'] += 1
            continue
        untimed.append(item)
    for item in items:
        # Literal grouping (docs/REPORTS.md): the source's label/address as written. The timeline
        # lane key (separators removed from phone-like values) stays available as counterparty_key.
        row = rows[item['event_id']]
        item['counterparty_key'] = item.get('counterparty')
        item['counterparty'] = row['contact_label'] or row['phone_or_address']
    reasons: dict[str, int] = {}
    for item in untimed:
        reasons[item['reason']] = reasons.get(item['reason'], 0) + 1
    for reason, count in sorted(reasons.items()):
        exclusions[f'untimed:{reason}'] = count
    exclusions['event_scan_limit_hit'] = bool(loaded['truncated'])
    used = [item['event_id'] for item in items] + [item['event_id'] for item in untimed]
    times: dict[int, list[dict]] = {}
    if used and ctx.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_times'").fetchone():
        for start in range(0, len(used), CHUNK):
            chunk = used[start:start + CHUNK]
            for row in ctx.db.execute(f'SELECT * FROM event_times WHERE event_id IN ({",".join("?" * len(chunk))}) ORDER BY id', chunk):
                times.setdefault(row['event_id'], []).append(dict(row))
    artifact_ids = sorted({rows[i]['artifact_id'] for i in used if rows[i]['artifact_id'] is not None})
    artifacts = _rows_by_id(ctx.db, 'artifacts', artifact_ids)
    result = {'rule': loaded['rule'] or RULE_ID, 'rule_sha256': loaded.get('rule_sha256'),
              'seam': loaded.get('seam', 'interim_fallback'), 'projection': loaded.get('projection'),
              'assumption_rule': loaded.get('assumption_rule'), 'assume_zone': loaded['assume_zone'], 'items': items,
              'untimed': untimed, 'rows': rows, 'times': times, 'artifacts': artifacts,
              'exclusions': exclusions, 'truncated': bool(loaded['truncated'])}
    ctx.scope['_events'] = result
    ctx.scope.setdefault('event_ids', set()).update(used)
    ctx.scope.setdefault('artifact_ids', set()).update(artifact_ids)
    return result


def record_event_inputs(section, events: dict, event_ids) -> None:
    """Rows behind the listed events: events, their time candidates, artifacts."""
    for event_id in event_ids:
        row = events['rows'][event_id]
        section.input_row('events', row)
        for time_row in events['times'].get(event_id, ()):
            section.input_row('event_times', time_row)
        artifact = events['artifacts'].get(row['artifact_id'])
        if artifact is not None:
            section.input_row('artifacts', artifact)

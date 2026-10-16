"""Projected event times for analyses (time budgets, event studies).

``load_items`` keeps the item contract of the interim seam
``server/app/analysis/source.py`` (``rule``, ``items``, ``untimed``,
``truncated``; item keys ``event_id``, ``event_type``, ``source_id``,
``artifact_id``, ``collection_id``, ``thread_id``, ``counterparty``,
``chatbot``, ``direction``, ``t_start``, ``t_end``, ``time_basis``,
``text_chars``, ``tags``) but takes the time from the versioned display rule
(``display_time@N``) and adds ``t_lo``/``t_hi`` (uncertainty interval, epoch
seconds), ``basis``, ``candidate_ref`` and ``hidden``.

Unknown zones are never shifted silently: by default (``unknown_zone='exclude'``)
such items go to ``untimed`` with reason ``unknown_zone``; ``'interval'`` keeps
them with the full offset interval; an operator zone needs ``assume_zone`` and a
named ``assumption_rule``, recorded on every item.
"""
from __future__ import annotations

import json
import sqlite3

from .projection import ensure_projection, label_flags
from .ruleset import Rule, display_params, load_rule
from .timeutil import US, parse_bound

UNKNOWN = 'local_as_utc_unknown_zone'


class ProjectionNotFresh(RuntimeError):
    """The projection cache is stale and the caller asked for a read without refresh."""


def read_projection(db: sqlite3.Connection, rule: Rule, params: dict) -> dict:
    """The projection description, read without writing (``PRAGMA query_only`` for the call).

    A fresh projection is returned as ``ensure_projection`` would. A stale or missing one
    would need a cache refresh (a write): that is refused with ``ProjectionNotFresh`` so a
    read-only snapshot (a report) never writes and never reads a stale cache silently.
    Refresh first with ``ensure_projection`` in a write transaction, then read.
    """
    previous = int(db.execute('PRAGMA query_only').fetchone()[0])
    db.execute('PRAGMA query_only=1')
    try:
        return ensure_projection(db, rule, params)
    except sqlite3.OperationalError as exc:
        if 'readonly' not in str(exc) and 'read-only' not in str(exc):
            raise
        raise ProjectionNotFresh(
            f'timeline projection {rule.rule_id}@{rule.version} with params {params} is not fresh in this read-only '
            'snapshot; refresh it in a write transaction first (timeline.projection.ensure_projection)') from exc
    finally:
        if not previous:
            db.execute('PRAGMA query_only=0')


SNAPSHOT_ATTEMPTS = 5


def open_snapshot(db: sqlite3.Connection, rule: Rule, params: dict, *, attempts: int = SNAPSHOT_ATTEMPTS) -> dict:
    """Refresh the projection, then open a read snapshot in which it is fresh and not evicted.

    ``ensure_projection`` commits its refresh; another request may evict (or refresh) the
    projection before the caller reads ``timeline_items`` (review 2 finding 4). This opens a
    read transaction (``BEGIN``) and re-checks the projection inside it with
    ``read_projection``: evicted or stale in that snapshot means another refresh and another
    snapshot, up to ``attempts`` times, then ``ProjectionNotFresh`` — never a silently empty
    read. Every read in the same transaction sees one consistent state (WAL snapshot), even
    if the projection is evicted meanwhile. The read transaction stays open: end it with
    ``db.rollback()``/``db.commit()`` (``db.session()`` does). Inside a caller's transaction
    the caller owns the snapshot and the refresh joins it (savepoint), as before.
    """
    if db.in_transaction:
        return ensure_projection(db, rule, params)
    for attempt in range(1, attempts + 1):
        refreshed = ensure_projection(db, rule, params)
        db.execute('BEGIN')
        try:
            current = read_projection(db, rule, params)  # first read fixes the snapshot
        except ProjectionNotFresh:
            db.rollback()
            if attempt == attempts:
                raise
            continue
        if refreshed['status'] == 'refreshed' and refreshed['build']['id'] == current['build']['id']:
            current = {**current, 'status': 'refreshed'}  # this call built what the snapshot reads
        return current
    raise RuntimeError('unreachable')  # pragma: no cover


# Projection reasons mapped to the analysis item contract (analysis/source.py).
_ANALYSIS_REASON = {'no_candidates': 'no_time_candidate', 'no_usable_candidate': 'no_parseable_time'}


def unknown_zone_reason(flags: dict) -> str:
    """Reason of an item left on the unknown-zone basis, in the analysis item contract.

    An operator zone that cannot place a wall clock (DST gap) leaves the item on the
    unknown-zone interval with ``flags.dst``; the analysis contract (and the interim
    seam) names that ``nonexistent_local_time`` rather than a plain ``unknown_zone``.
    """
    dst = flags.get('dst') if isinstance(flags, dict) else None
    if flags.get('assumed_zone') and isinstance(dst, str) and dst != 'ambiguous_fold':
        return dst
    return 'unknown_zone'


def undated_reason(flags: dict) -> str:
    """Analysis-contract reason of an item without a usable time (projection ``undated_reason``)."""
    reason = flags.get('undated_reason') if isinstance(flags, dict) else None
    return _ANALYSIS_REASON.get(reason, reason or 'no_time_candidate')


def load_items(db: sqlite3.Connection, *, start_utc: str | None = None, end_utc: str | None = None,
               event_types=None, assume_zone: str | None = None, assumption_rule: str | None = None,
               unknown_zone: str = 'exclude', include_hidden: bool = False, rule: Rule | str | None = None,
               limit: int = 1_000_000, refresh: bool = True) -> dict:
    """Events overlapping ``[start_utc, end_utc)`` sorted by (t_start, event_id); see module docstring.

    ``refresh=False`` never writes: the projection must already be fresh in this
    connection's snapshot, otherwise ``ProjectionNotFresh`` is raised (read-only callers).
    With ``refresh=True`` all reads happen in one snapshot (``open_snapshot``), closed here
    unless the caller already had a transaction open.
    """
    if unknown_zone not in ('exclude', 'interval'):
        raise ValueError("unknown_zone must be 'exclude' or 'interval'")
    resolved = rule if isinstance(rule, Rule) else load_rule(rule, kind='display_rule')
    params = display_params(assume_zone, assumption_rule)
    db.row_factory = sqlite3.Row
    own_snapshot = refresh and not db.in_transaction
    projection = open_snapshot(db, resolved, params) if refresh else read_projection(db, resolved, params)
    try:
        return _read_items(db, resolved, params, projection, start_utc=start_utc, end_utc=end_utc,
                           event_types=event_types, assume_zone=assume_zone, unknown_zone=unknown_zone,
                           include_hidden=include_hidden, limit=limit)
    finally:
        if own_snapshot:
            db.rollback()  # read-only snapshot opened above


def _read_items(db, resolved: Rule, params: dict, projection: dict, *, start_utc, end_utc, event_types, assume_zone,
                unknown_zone, include_hidden, limit) -> dict:
    lo, hi = parse_bound(start_utc), parse_bound(end_utc, end=True)
    clauses, args = ["ti.projection_id=?", "ti.item_kind='event'"], [projection['id']]
    if not include_hidden:
        clauses.append('ti.hidden=0')
    if event_types:
        clauses.append('ti.event_type IN (SELECT value FROM json_each(?))')
        args.append(json.dumps(list(event_types)))
    where = ' AND '.join(clauses)
    timed = [*clauses, 'ti.sort_us IS NOT NULL']
    targs = list(args)
    if hi is not None:
        timed.append('+ti.sort_us<?')
        targs.append(hi)
    if lo is not None:
        timed.append('COALESCE(ti.end_us, ti.sort_us)>=?')
        targs.append(lo)
    rows = db.execute(f'''SELECT ti.*, e.direction, e.contact_label, e.phone_or_address, length(e.body) AS text_chars,
          (SELECT json_group_array(t.name) FROM (SELECT t.name FROM event_tags et JOIN tags t ON t.id=et.tag_id
             WHERE et.event_id=e.id ORDER BY t.name) t) AS tags_json
        FROM timeline_items ti JOIN events e ON e.id=ti.ref_id WHERE {' AND '.join(timed)}
        ORDER BY +ti.sort_us, ti.ref_id LIMIT ?''', (*targs, limit + 1)).fetchall()
    truncated = len(rows) > limit
    items, untimed = [], []
    for row in rows[:limit]:
        flags = label_flags(json.loads(row['flags_json'] or '{}'), params)
        if row['basis'] == UNKNOWN and unknown_zone == 'exclude':
            untimed.append({'event_id': row['ref_id'], 'event_type': row['event_type'],
                            'reason': unknown_zone_reason(flags), 'local_as_utc': row['sort_us'] / US,
                            'hidden': bool(row['hidden'])})
            continue
        t0 = row['sort_us'] / US
        t1 = row['end_us'] / US if row['end_us'] is not None and row['end_us'] >= row['sort_us'] else None
        items.append({
            'event_id': row['ref_id'], 'event_type': row['event_type'], 'source_id': row['source_id'],
            'artifact_id': row['artifact_id'], 'collection_id': row['collection_id'], 'thread_id': row['thread_id'],
            'counterparty': row['counterparty'], 'chatbot': row['chatbot'], 'direction': row['direction'],
            't_start': t0, 't_end': t1, 't_lo': row['lo_us'] / US, 't_hi': row['hi_us'] / US,
            'basis': row['basis'], 'candidate_ref': row['candidate_ref'], 'hidden': bool(row['hidden']),
            'time_basis': {'kind': row['candidate_kind'], 'tz_status': _tz_status(row['basis']), 'source': row['candidate_ref'],
                           'precision': row['precision'], 'assumed_zone': flags.get('assumed_zone'),
                           'assumption_rule': flags.get('assumption_rule'), 'dst': flags.get('dst')},
            'text_chars': row['text_chars'], 'tags': json.loads(row['tags_json'] or '[]'),
        })
    for row in db.execute(f'SELECT ref_id, event_type, flags_json, hidden FROM timeline_items ti WHERE {where} '
                          'AND ti.sort_us IS NULL ORDER BY ti.ref_id LIMIT ?', (*args, limit + 1)):
        untimed.append({'event_id': row['ref_id'], 'event_type': row['event_type'],
                        'reason': undated_reason(json.loads(row['flags_json'] or '{}')), 'hidden': bool(row['hidden'])})
    return {'rule': f'{resolved.rule_id}@{resolved.version}', 'rule_sha256': resolved.sha256, 'params': params,
            'projection': {'id': projection['id'], 'build_id': projection['build']['id']},
            'assume_zone': assume_zone, 'unknown_zone': unknown_zone, 'items': items, 'untimed': untimed,
            'truncated': truncated}


def _tz_status(basis: str) -> str:
    return {'explicit_offset': 'explicit_offset', 'utc_epoch': 'utc_epoch', 'assumed_zone': 'assumed_zone',
            'assumed_zone_operator': 'assumed_zone', UNKNOWN: 'unknown'}.get(basis, 'unknown')

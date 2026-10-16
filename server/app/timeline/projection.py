"""Materialized timeline projection: one cache per display rule version + params.

``ensure_projection`` compares data watermarks with the last build and
refreshes the cache incrementally (new/changed events, affected recordings)
or fully (after deletions or on first use). Every build is logged append-only
with its watermarks and counts. The cache is derived data: it can be dropped
and rebuilt from events, ``event_times``, transcripts, observations and
accepted correlation hypotheses at any time.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone

from .candidates import (Candidate, _iso_candidate, counterparty, legacy_event_candidates, select,
                         stored_candidate)
from .ruleset import Rule, canonical_json, load_rule, params_sha256
from .timeutil import US

BATCH = 5000
SEGMENT_FLAGS = ('hidden_reason', 'dst', 'date_order_ambiguous', 'ampm_ambiguous', 'hour_12h_ambiguous', 'assumption_rule',
                 'assumed_zone', 'end_before_start', 'conflicting_correlations')
_JSON_PATH = re.compile(r'^\$(\.[A-Za-z_][A-Za-z0-9_]{0,63}){1,8}$')
INSERT = '''INSERT INTO timeline_items(projection_id,item_kind,ref_id,sub_index,sort_us,lo_us,hi_us,end_us,basis,
  precision,candidate_ref,candidate_kind,event_type,source_id,artifact_id,collection_id,counterparty,chatbot,
  thread_id,is_latest,hidden,flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metadata(text) -> dict:
    try:
        value = json.loads(text or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _paths(paths) -> list[str]:
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and _JSON_PATH.match(p) for p in paths):
        raise ValueError('rule metadata paths must be simple JSON paths like $.chat.chatbot')
    return paths


def _chatbot_sql(rule: Rule) -> str:
    paths = _paths(rule.data['lanes'].get('chatbot_metadata_paths', ['$.chatbot']))
    return 'COALESCE(' + ','.join(f"json_extract(metadata_json,'{p}')" for p in paths) + ')' if len(paths) > 1 \
        else f"json_extract(metadata_json,'{paths[0]}')"


def _hidden_sql(rule: Rule) -> str:
    paths = rule.data.get('visibility', {}).get('hidden_metadata_flags') or []
    if not paths:
        return '0'
    return '(' + ' OR '.join(f"json_extract(metadata_json,'{p}') IN (1,'true')" for p in _paths(paths)) + ')'


def hidden_reason(rule: Rule, event) -> str | None:
    visibility = rule.data.get('visibility', {})
    if event['event_type'] in (visibility.get('hidden_event_types') or []):
        return f"event_type:{event['event_type']}"
    if event['hidden_flag']:
        return 'metadata_flag'
    return None


def projection_id(db: sqlite3.Connection, rule: Rule, params: dict) -> int:
    params_json = canonical_json(params)
    digest = params_sha256(params)
    row = db.execute('''SELECT id FROM timeline_projections WHERE rule_id=? AND rule_version=? AND rule_sha256=?
                        AND params_sha256=?''', (rule.rule_id, rule.version, rule.sha256, digest)).fetchone()
    if row:
        return int(row['id'])
    cur = db.execute('''INSERT INTO timeline_projections(rule_id,rule_version,rule_sha256,params_json,params_sha256)
                        VALUES(?,?,?,?,?)''', (rule.rule_id, rule.version, rule.sha256, params_json, digest))
    return int(cur.lastrowid)


# Bumped when the projection code changes what it derives from unchanged data
# (2: stat-scoped file_mtime candidates, review finding 1); a build made by
# other code is rebuilt in full.
PROJECTION_CODE = 2


def watermarks(db: sqlite3.Connection) -> dict:
    def one(sql):
        return int(db.execute(sql).fetchone()[0])
    return {
        'projection_code': PROJECTION_CODE,
        'events_max_id': one('SELECT COALESCE(MAX(id),0) FROM events'),
        'events_count': one('SELECT COUNT(*) FROM events'),
        'event_times_max_id': one('SELECT COALESCE(MAX(id),0) FROM event_times'),
        'derived_text_max_id': one('SELECT COALESCE(MAX(id),0) FROM derived_text'),
        'derived_text_count': one('SELECT COUNT(*) FROM derived_text'),
        'observations_max_id': one('SELECT COALESCE(MAX(id),0) FROM source_observations'),
        'hypothesis_status_max_id': one('SELECT COALESCE(MAX(id),0) FROM timeline_hypothesis_status'),
    }


def collection_map(db: sqlite3.Connection, artifact_ids=None) -> dict[int, int]:
    """First (earliest observation) collection of each artifact; other memberships stay filterable."""
    sql = 'SELECT artifact_id, collection_id FROM source_observations WHERE collection_id IS NOT NULL'
    args: tuple = ()
    if artifact_ids is not None:
        sql += ' AND artifact_id IN (SELECT value FROM json_each(?))'
        args = (json.dumps(sorted(artifact_ids)),)
    mapping: dict[int, int] = {}
    for row in db.execute(sql + ' ORDER BY id', args):
        mapping.setdefault(int(row['artifact_id']), int(row['collection_id']))
    return mapping


def event_candidates(event, time_rows, rule: Rule, *, include_ineligible: bool = True) -> list[Candidate]:
    stored = [stored_candidate(r) for r in time_rows]
    if not include_ineligible and any(c.usable for c in stored):
        return stored
    return stored + legacy_event_candidates(event, _metadata(event['metadata_json']), rule)


def _flags(placement_flags: dict, extra: dict | None = None) -> str:
    flags = {k: v for k, v in placement_flags.items() if k in SEGMENT_FLAGS or k == 'utc_alternatives'}
    if extra:
        flags.update(extra)
    return json.dumps(flags, ensure_ascii=False, sort_keys=True) if flags else '{}'


def _event_row(pid: int, event, time_rows, rule: Rule, params: dict, collections: dict):
    candidates = event_candidates(event, time_rows, rule, include_ineligible=False)
    selection = select(candidates, event['event_type'], rule, params, event['source_id'])
    placement, chosen = selection['placement'], selection['candidate']
    lanes = (event['event_type'], event['source_id'], event['artifact_id'],
             collections.get(event['artifact_id']) if event['artifact_id'] is not None else None,
             counterparty(event, rule), event['chatbot'] if isinstance(event['chatbot'], str) else None,
             event['thread_id'])
    reason = hidden_reason(rule, event)
    hidden = {'hidden_reason': reason} if reason else {}
    if placement is None:
        status = _metadata(event['metadata_json']).get('timestamp')
        extra = {'undated_reason': 'no_candidates' if not candidates else 'no_usable_candidate', **hidden}
        if isinstance(status, dict) and status.get('status'):
            extra['timestamp_status'] = status['status']
        return (pid, 'event', event['id'], 0, None, None, None, None, 'none', 'unknown', None, None, *lanes, 1,
                1 if reason else 0, json.dumps(extra, sort_keys=True))
    return (pid, 'event', event['id'], 0, placement.sort_us, placement.lo_us, placement.hi_us, selection['end_us'],
            placement.basis, placement.precision, chosen.ref, chosen.kind, *lanes, 1, 1 if reason else 0,
            _flags(placement.flags, hidden))


def _events_query(rule: Rule) -> str:
    return f'''SELECT id,event_type,ts_start,ts_end,direction,source_id,artifact_id,phone_or_address,contact_label,
      sender,recipients,thread_id,metadata_json,
      CASE WHEN json_valid(metadata_json) THEN {_chatbot_sql(rule)} END AS chatbot,
      CASE WHEN json_valid(metadata_json) THEN {_hidden_sql(rule)} ELSE 0 END AS hidden_flag
      FROM events'''


def _times_for(db, event_ids: list[int]) -> dict[int, list]:
    grouped: dict[int, list] = {}
    for row in db.execute('SELECT * FROM event_times WHERE event_id IN (SELECT value FROM json_each(?)) ORDER BY event_id,id',
                          (json.dumps(event_ids),)):
        grouped.setdefault(int(row['event_id']), []).append(row)
    return grouped


def _build_events(db, pid: int, rule: Rule, params: dict, event_ids: list[int] | None) -> dict:
    counts = {'events': 0, 'undated_events': 0}
    collections = collection_map(db)
    base = _events_query(rule)
    last = 0
    position = 0
    while True:
        if event_ids is None:
            chunk = db.execute(base + ' WHERE id>? ORDER BY id LIMIT ?', (last, BATCH)).fetchall()
        else:
            ids = event_ids[position:position + BATCH]
            position += BATCH
            if not ids:
                break
            chunk = db.execute(base + ' WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id', (json.dumps(ids),)).fetchall()
        if not chunk:
            break
        times = _times_for(db, [int(r['id']) for r in chunk])
        rows = [_event_row(pid, e, times.get(int(e['id']), []), rule, params, collections) for e in chunk]
        db.executemany(INSERT, rows)
        counts['events'] += len(rows)
        counts['undated_events'] += sum(1 for r in rows if r[4] is None)
        last = int(chunk[-1]['id'])
        if event_ids is None and len(chunk) < BATCH:
            break
    return counts


def event_selection(db, event_id: int, rule: Rule, params: dict) -> dict | None:
    event = db.execute(_events_query(rule) + ' WHERE id=?', (event_id,)).fetchone()
    if event is None:
        return None
    times = db.execute('SELECT * FROM event_times WHERE event_id=? ORDER BY id', (event_id,)).fetchall()
    candidates = event_candidates(event, times, rule)
    selection = select(candidates, event['event_type'], rule, params, event['source_id'])
    return {'event': event, 'candidates': candidates, **selection}


def _accepted_correlations(db, artifact_id: int, rule_ids: list[str]):
    return db.execute('''SELECT h.* FROM timeline_hypotheses h
        WHERE h.left_kind='artifact' AND h.left_id=? AND h.right_kind='event'
          AND h.rule_id IN (SELECT value FROM json_each(?))
          AND (SELECT s.status FROM timeline_hypothesis_status s WHERE s.hypothesis_id=h.id ORDER BY s.id DESC LIMIT 1)='accepted'
        ORDER BY h.id''', (artifact_id, json.dumps(rule_ids))).fetchall()


_TEMPORARY_SOURCE_KEYS = ('extracted_from', 'catalog_entry_id')


def observation_mtime_scope(locator: str | None, meta: dict) -> tuple[str, str | None]:
    """What an observation's ``source_modified_at`` describes, and why it is unusable (or None).

    Rows since 2026-10-16 carry ``filesystem_stat_scope`` (``evidence.STAT_SCOPES``):
    only ``source_path`` (a stat of the original location) and ``declared_by_caller``
    (with its own basis) are usable. A temporary-upload marker in the caller's
    metadata (``acquisition_kind: uploaded_stream`` or a ``…temporary…`` scope)
    makes any row unusable: conflicting signals resolve to "not the source".
    Older rows are judged by the markers they have: those, an
    ``upload:``/``client-upload:`` locator (server upload routes), a
    container-member locator (``…!/…``), extraction or catalog-ingest metadata.
    Older rows without any marker stay usable and are labelled
    ``legacy_unmarked_assumed_source_path`` (residual ambiguity).
    """
    scope = meta.get('filesystem_stat_scope')
    source_meta = meta.get('source_metadata') if isinstance(meta.get('source_metadata'), dict) else {}
    declared = str(source_meta.get('filesystem_stat_scope') or '')
    if 'temporary' in declared or source_meta.get('acquisition_kind') == 'uploaded_stream':
        return declared or 'uploaded_stream', 'file_mtime_of_temporary_upload'
    if isinstance(scope, str):
        if scope in ('source_path', 'declared_by_caller'):
            return scope, None
        return scope, 'file_mtime_of_temporary_copy'
    locator = locator or ''
    if locator.startswith(('upload:', 'client-upload:')):
        return 'legacy_server_upload', 'file_mtime_of_temporary_upload'
    if '!/' in locator or any(key in source_meta for key in _TEMPORARY_SOURCE_KEYS):
        return 'legacy_temporary_copy', 'file_mtime_of_temporary_copy'
    return 'legacy_unmarked_assumed_source_path', None


def recording_candidates(db, artifact_id: int, rule: Rule, params: dict) -> list[Candidate]:
    """Start-time candidates of a recorded artifact (all visible, each labelled)."""
    out: list[Candidate] = []
    recording = rule.data['recording']
    events = db.execute(_events_query(rule) + ''' WHERE artifact_id=? AND event_type IN (SELECT value FROM json_each(?))
                        ORDER BY id''', (artifact_id, json.dumps(recording['recording_event_types']))).fetchall()
    for event in events:
        rows = db.execute('SELECT * FROM event_times WHERE event_id=? ORDER BY id', (event['id'],)).fetchall()
        stored = [stored_candidate(r, origin='derived_artifact', ref_prefix='artifact_time') for r in rows]
        for c in stored:
            c.metadata = {**c.metadata, 'event_id': int(event['id']), 'event_type': event['event_type']}
            c.derived_from = [f"event_times:{c.ref.split(':', 1)[1]}", f"events:{event['id']}"]
        out.extend(stored)
        if not any(c.usable for c in stored):
            for c in legacy_event_candidates(event, _metadata(event['metadata_json']), rule, origin='derived_artifact',
                                             ref_prefix=f"artifact_event:{event['id']}"):
                c.metadata = {**c.metadata, 'event_id': int(event['id']), 'event_type': event['event_type']}
                out.append(c)
    for obs in db.execute('''SELECT id,source_locator,source_modified_at,acquisition_kind,metadata_json FROM source_observations
                             WHERE artifact_id=? ORDER BY id''', (artifact_id,)):
        if not obs['source_modified_at']:
            continue
        c = _iso_candidate('file_mtime', obs['source_modified_at'], f"derived:source_observations:{obs['id']}.source_modified_at",
                           f"observation:{obs['id']}", 'derived_observation', ['source_observations.source_modified_at'])
        meta = _metadata(obs['metadata_json'])
        scope, reason = observation_mtime_scope(obs['source_locator'], meta)
        c.metadata['acquisition_kind'] = obs['acquisition_kind']
        c.metadata['filesystem_stat_scope'] = scope
        c.metadata['semantics'] = 'filesystem modification time of the acquired copy; copying can change it'
        if scope == 'declared_by_caller':
            c.metadata['source_modified_at_basis'] = meta.get('source_modified_at_basis')
            c.metadata['semantics'] = 'modification time declared by the acquiring code; see source_modified_at_basis'
        if reason is not None:
            c.usable, c.unusable_reason = False, reason
        out.append(c)
    for hypothesis in _accepted_correlations(db, artifact_id, recording.get('correlation_rule_ids', [])):
        call = event_selection(db, int(hypothesis['right_id']), rule, params)
        if call is None or call['candidate'] is None:
            continue
        source = call['candidate']
        criteria = _metadata(hypothesis['criteria_json'])
        time_criterion = criteria.get('time') if isinstance(criteria.get('time'), dict) else {}
        out.append(Candidate(
            f"correlation:{hypothesis['id']}", 'derived_correlation', 'start', source.raw,
            f"correlation:{hypothesis['rule_id']}@{hypothesis['rule_version']}:hypothesis:{hypothesis['id']}",
            source.utc, source.local, source.tz_status, source.tz, source.precision,
            {'hypothesis_id': int(hypothesis['id']), 'call_event_id': int(hypothesis['right_id']),
             'call_candidate_ref': source.ref, 'call_counterparty': counterparty(call['event'], rule),
             'hypothesis_time_delta_seconds': time_criterion.get('delta_seconds'),
             'semantics': 'call-log start of an accepted hypothesis; the recording may start later than the call'},
            [f"timeline_hypotheses:{hypothesis['id']}", f"events:{hypothesis['right_id']}"]))
    return out


def recording_selection(db, artifact_id: int, rule: Rule, params: dict) -> dict:
    artifact = db.execute('SELECT id,source_id FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    candidates = recording_candidates(db, artifact_id, rule, params)
    selection = select(candidates, 'transcript_segment', rule, params, artifact['source_id'] if artifact else None)
    correlated = [c.ref for c in candidates if c.origin == 'derived_correlation']
    if selection['placement'] is not None and len(correlated) > 1:
        selection['placement'].flags['conflicting_correlations'] = correlated
    return {'artifact': artifact, 'candidates': candidates, **selection}


def _finite_seconds(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float('inf'), float('-inf')) or value < 0 or value > 10**7:
        return None
    return float(value)


def _build_segments(db, pid: int, rule: Rule, params: dict, artifact_ids: list[int] | None) -> dict:
    counts = {'transcript_versions': 0, 'segments': 0, 'undated_segments': 0, 'malformed_transcript_versions': 0}
    if artifact_ids is None:
        targets = [int(r[0]) for r in db.execute("SELECT DISTINCT artifact_id FROM derived_text WHERE kind='transcript' ORDER BY artifact_id")]
    else:
        targets = sorted(set(artifact_ids))
    collections = collection_map(db, targets)
    for artifact_id in targets:
        versions = db.execute("""SELECT id,segments_json FROM derived_text WHERE artifact_id=? AND kind='transcript'
                                 ORDER BY id""", (artifact_id,)).fetchall()
        if not versions:
            continue
        selection = recording_selection(db, artifact_id, rule, params)
        artifact = selection['artifact']
        placement, chosen = selection['placement'], selection['candidate']
        cp = chosen.metadata.get('call_counterparty') if chosen is not None else None
        latest = int(versions[-1]['id'])
        rows = []
        for version in versions:
            counts['transcript_versions'] += 1
            try:
                segments = json.loads(version['segments_json'] or '[]')
                if not isinstance(segments, list):
                    raise ValueError
            except ValueError:
                counts['malformed_transcript_versions'] += 1
                continue
            for index, segment in enumerate(segments):
                start = _finite_seconds(segment.get('start')) if isinstance(segment, dict) else None
                end = _finite_seconds(segment.get('end')) if isinstance(segment, dict) else None
                common = ('transcript_segment', artifact['source_id'] if artifact else None, artifact_id,
                          collections.get(artifact_id), cp, None, None, 1 if int(version['id']) == latest else 0, 0)
                if placement is None or start is None:
                    reason = 'no_recording_start' if placement is None else 'segment_offset_invalid'
                    rows.append((pid, 'transcript_segment', int(version['id']), index, None, None, None, None, 'none',
                                 'unknown', None, None, *common, json.dumps({'undated_reason': reason})))
                    continue
                offset = int(round(start * US))
                end_us = placement.sort_us + int(round(end * US)) if end is not None and end >= start else None
                rows.append((pid, 'transcript_segment', int(version['id']), index, placement.sort_us + offset,
                             placement.lo_us + offset, placement.hi_us + offset, end_us, placement.basis,
                             placement.precision, chosen.ref, chosen.kind, *common,
                             _flags(placement.flags, {'offset_seconds': start})))
        db.executemany(INSERT, rows)
        counts['segments'] += len(rows)
        counts['undated_segments'] += sum(1 for r in rows if r[4] is None)
    return counts


def _affected(db, last: dict, current: dict) -> tuple[list[int], list[int]] | None:
    """Event ids and recording artifact ids to recompute, or None for a full rebuild."""
    new_events = int(db.execute('SELECT COUNT(*) FROM events WHERE id>?', (last['events_max_id'],)).fetchone()[0])
    if current['events_count'] != last['events_count'] + new_events:
        return None
    new_texts = int(db.execute('SELECT COUNT(*) FROM derived_text WHERE id>?', (last['derived_text_max_id'],)).fetchone()[0])
    if current['derived_text_count'] != last['derived_text_count'] + new_texts:
        return None
    if any(current[k] < last.get(k, 0) for k in current):
        return None
    events = {int(r[0]) for r in db.execute('SELECT id FROM events WHERE id>?', (last['events_max_id'],))}
    events |= {int(r[0]) for r in db.execute('SELECT DISTINCT event_id FROM event_times WHERE id>?', (last['event_times_max_id'],))}
    new_obs_artifacts = {int(r[0]) for r in db.execute('SELECT DISTINCT artifact_id FROM source_observations WHERE id>?',
                                                       (last['observations_max_id'],))}
    if new_obs_artifacts:
        events |= {int(r[0]) for r in db.execute('SELECT id FROM events WHERE artifact_id IN (SELECT value FROM json_each(?))',
                                                  (json.dumps(sorted(new_obs_artifacts)),))}
    artifacts = set(new_obs_artifacts)
    artifacts |= {int(r[0]) for r in db.execute("SELECT DISTINCT artifact_id FROM derived_text WHERE id>? AND kind='transcript'",
                                                (last['derived_text_max_id'],))}
    if events:
        payload = json.dumps(sorted(events))
        artifacts |= {int(r[0]) for r in db.execute('''SELECT DISTINCT artifact_id FROM events WHERE artifact_id IS NOT NULL
                                                        AND id IN (SELECT value FROM json_each(?))''', (payload,))}
        artifacts |= {int(r[0]) for r in db.execute('''SELECT DISTINCT left_id FROM timeline_hypotheses WHERE left_kind='artifact'
                                                        AND right_kind='event' AND right_id IN (SELECT value FROM json_each(?))''', (payload,))}
    artifacts |= {int(r[0]) for r in db.execute('''SELECT DISTINCT h.left_id FROM timeline_hypothesis_status s
        JOIN timeline_hypotheses h ON h.id=s.hypothesis_id WHERE s.id>? AND h.left_kind='artifact' ''',
                                                (last['hypothesis_status_max_id'],))}
    return sorted(events), sorted(artifacts)


def _last_build(db, pid: int):
    return db.execute('SELECT * FROM timeline_projection_builds WHERE projection_id=? ORDER BY id DESC LIMIT 1', (pid,)).fetchone()


def ensure_projection(db: sqlite3.Connection, rule: Rule | None = None, params: dict | None = None, *,
                      force_full: bool = False) -> dict:
    """Return a fresh projection description, refreshing the cache when data changed."""
    rule = rule or load_rule(kind='display_rule')
    params = params or {}
    db.execute('PRAGMA busy_timeout=60000')
    row = db.execute('''SELECT id FROM timeline_projections WHERE rule_id=? AND rule_version=? AND rule_sha256=?
                        AND params_sha256=?''', (rule.rule_id, rule.version, rule.sha256, params_sha256(params))).fetchone()
    if row is not None and not force_full:
        build = _last_build(db, int(row['id']))
        if build is not None and json.loads(build['watermarks_json']) == watermarks(db):
            return _describe(rule, params, int(row['id']), build, 'fresh')
    started_at, started = _now(), time.perf_counter()
    db.execute('BEGIN IMMEDIATE')
    try:
        pid = projection_id(db, rule, params)
        build = _last_build(db, pid)
        current = watermarks(db)
        last = json.loads(build['watermarks_json']) if build is not None else None
        if last == current and not force_full:
            db.commit()
            return _describe(rule, params, pid, build, 'fresh')
        stale_code = last is not None and last.get('projection_code') != PROJECTION_CODE
        plan = None if (last is None or force_full or stale_code) else _affected(db, last, current)
        if plan is None:
            mode = 'full'
            db.execute('DELETE FROM timeline_items WHERE projection_id=?', (pid,))
            counts = _build_events(db, pid, rule, params, None)
            counts.update(_build_segments(db, pid, rule, params, None))
        else:
            mode = 'incremental'
            events, artifacts = plan
            counts = {'recomputed_event_ids': len(events), 'recomputed_recordings': len(artifacts)}
            for start in range(0, len(events), BATCH):
                db.execute("DELETE FROM timeline_items WHERE projection_id=? AND item_kind='event' AND ref_id IN (SELECT value FROM json_each(?))",
                           (pid, json.dumps(events[start:start + BATCH])))
            counts.update(_build_events(db, pid, rule, params, events))
            if artifacts:
                db.execute("""DELETE FROM timeline_items WHERE projection_id=? AND item_kind='transcript_segment'
                              AND artifact_id IN (SELECT value FROM json_each(?))""", (pid, json.dumps(artifacts)))
                counts.update(_build_segments(db, pid, rule, params, artifacts))
        counts['max_halfwidth_us'] = _max_halfwidth(db, pid)
        duration_ms = int((time.perf_counter() - started) * 1000)
        cur = db.execute('''INSERT INTO timeline_projection_builds(projection_id,mode,started_at,finished_at,duration_ms,
                            watermarks_json,counts_json) VALUES(?,?,?,?,?,?,?)''',
                         (pid, mode, started_at, _now(), duration_ms, canonical_json(current), canonical_json(counts)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    build = db.execute('SELECT * FROM timeline_projection_builds WHERE id=?', (cur.lastrowid,)).fetchone()
    return _describe(rule, params, pid, build, 'refreshed')


def _describe(rule: Rule, params: dict, pid: int, build, status: str) -> dict:
    return {'id': pid, 'status': status, 'rule': rule.describe(), 'params': params,
            'build': {'id': int(build['id']), 'mode': build['mode'], 'finished_at': build['finished_at'],
                      'duration_ms': int(build['duration_ms']), 'counts': json.loads(build['counts_json']),
                      'watermarks': json.loads(build['watermarks_json'])}}


def _max_halfwidth(db, pid: int) -> int:
    """Largest distance between an item's sort time and its interval bounds (for overlap queries)."""
    row = db.execute('''SELECT MAX(MAX(sort_us-lo_us, hi_us-sort_us)) FROM timeline_items
                        WHERE projection_id=? AND sort_us IS NOT NULL''', (pid,)).fetchone()
    return int(row[0] or 0)

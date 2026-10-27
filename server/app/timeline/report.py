"""Timeline section for reports (area C, wired into area F's provider contract).

``report_section`` reads the materialized projection inside the caller's
read-only snapshot. It never refreshes the cache: a stale projection raises
``analysis_items.ProjectionNotFresh`` (the report service refreshes the derived
cache explicitly, in its own write transaction, before opening the snapshot).
Every value is computed here; nothing is formatted for display.

* ``rule`` / ``projection`` — display rule id, version and SHA-256, operator
  parameters with their SHA-256, projection id, build id, mode and finish time.
* ``placement`` — the scoped events (the report's material, timed or not) by
  basis and precision, with the widest uncertainty interval; ``flags`` counts
  DST folds/gaps, ambiguous day order or AM/PM, ends before starts.
* ``segments`` — transcript segments in the window through the timeline window
  query (latest version per recording by default), with version id, offsets,
  uncertainty interval and language basis. Segments carry no counterparty.
* ``correlations`` — correlation hypotheses touching the scoped events or
  artifacts with their current review status. They are hypotheses with
  per-criterion evidence and a human decision; no probability, never facts.
* ``inputs`` — the rows read (``SELECT *``) for the report's method record.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter

from .analysis_items import read_projection, undated_reason
from .query import iter_window, make_filters
from .ruleset import canonical_json, display_params, load_rule
from .timeutil import US, parse_bound

VERSION = 'timeline.report.v1'
CORRELATION_SEMANTICS = ('correlation hypothesis: per-criterion evidence and a human review status; '
                         'no probability is computed; not a fact')
FLAG_KEYS = ('dst', 'date_order_ambiguous', 'ampm_ambiguous', 'hour_12h_ambiguous', 'end_before_start',
             'conflicting_correlations', 'hidden_reason')
CHUNK = 500


def _ids(values) -> list[int]:
    return sorted({int(v) for v in values or () if v is not None})


def _rows(db, sql: str, ids: list[int], *args) -> list[sqlite3.Row]:
    out: list[sqlite3.Row] = []
    for start in range(0, len(ids), CHUNK):
        out.extend(db.execute(sql, (*args, json.dumps(ids[start:start + CHUNK]))).fetchall())
    return out


def _placement(db, pid: int, event_ids: list[int]) -> tuple[list[dict], list[dict]]:
    groups: dict[tuple, dict] = {}
    flags: Counter = Counter()
    for row in _rows(db, '''SELECT basis, precision, sort_us, lo_us, hi_us, flags_json FROM timeline_items
                            WHERE projection_id=? AND item_kind='event' AND ref_id IN (SELECT value FROM json_each(?))''',
                     event_ids, pid):
        timed = row['sort_us'] is not None
        key = (row['basis'] if timed else 'none', row['precision'] if timed else 'unknown')
        entry = groups.setdefault(key, {'basis': key[0], 'precision': key[1], 'items': 0, 'with_interval': 0,
                                        'max_uncertainty_s': 0.0})
        entry['items'] += 1
        if timed and row['hi_us'] > row['lo_us']:
            entry['with_interval'] += 1
            entry['max_uncertainty_s'] = max(entry['max_uncertainty_s'], (row['hi_us'] - row['lo_us']) / US)
        try:
            item_flags = json.loads(row['flags_json'] or '{}')
        except ValueError:
            item_flags = {'unreadable_flags': True}
        for name in FLAG_KEYS:
            value = item_flags.get(name)
            if value not in (None, False, ''):
                flags[(name, value if isinstance(value, str) else 'true')] += 1
        if not timed:
            flags[('undated_reason', undated_reason(item_flags))] += 1  # analysis/report vocabulary
    placement = sorted(groups.values(), key=lambda g: (-g['items'], g['basis'], g['precision']))
    return placement, [{'flag': name, 'value': value, 'items': count} for (name, value), count in sorted(flags.items())]


def _segments(db, projection: dict, rule, *, lo, hi, topic: str | None, limit: int,
              max_scan: int) -> tuple[list[dict], dict]:
    filters = make_filters(kinds=['transcript_segment'])
    rows, scanned, filtered = [], 0, 0
    needle = topic.casefold() if topic else None
    for item in iter_window(db, projection, rule, start_us=lo, end_us=hi, filters=filters, match='display',
                            candidates=False, group=None, max_rows=max_scan + 1):
        if scanned == max_scan:
            return rows, {'segments_in_window': None, 'segments_filtered_by_topic': filtered,
                          'segments_listed': len(rows), 'segments_truncated': True, 'segments_scan_limit_hit': True}
        scanned += 1
        if needle and needle not in str(item.get('text') or '').casefold():
            filtered += 1
            continue
        if len(rows) < limit:
            time = item['time']
            rows.append({'time_utc': time.get('display'), 'interval_from': (time.get('interval') or {}).get('from'),
                         'interval_to': (time.get('interval') or {}).get('to'), 'basis': time.get('basis'),
                         'precision': time.get('precision'), 'artifact_id': item.get('artifact_id'),
                         'derived_text_id': item.get('derived_text_id'), 'segment_index': item.get('segment_index'),
                         'segment_start_s': (item.get('segment') or {}).get('start_seconds'),
                         'segment_end_s': (item.get('segment') or {}).get('end_seconds'),
                         'transcript_is_latest': (item.get('transcript_version') or {}).get('is_latest'),
                         'run_id': (item.get('transcript_version') or {}).get('run_id'),
                         'model': (item.get('transcript_version') or {}).get('model'),
                         'language': item.get('language'), 'language_basis': item.get('language_basis'),
                         'text': item.get('text'), 'text_truncated': item.get('text_truncated')})
    return rows, {'segments_in_window': scanned - filtered, 'segments_filtered_by_topic': filtered,
                  'segments_listed': len(rows), 'segments_truncated': scanned - filtered > len(rows),
                  'segments_scan_limit_hit': False}


def _correlations(db, event_ids: list[int], artifact_ids: list[int], limit: int) -> tuple[list[dict], dict, dict]:
    if not event_ids and not artifact_ids:
        return [], {'hypotheses_total': 0, 'hypotheses_listed': 0, 'truncated': False}, {}
    refs = json.dumps({'event': event_ids, 'artifact': artifact_ids})
    rows = db.execute('''WITH refs(kind, id) AS (
          SELECT 'event', value FROM json_each(json_extract(?, '$.event'))
          UNION ALL SELECT 'artifact', value FROM json_each(json_extract(?, '$.artifact')))
        SELECT DISTINCT h.* FROM timeline_hypotheses h JOIN refs r
          ON (h.left_kind=r.kind AND h.left_id=r.id) OR (h.right_kind=r.kind AND h.right_id=r.id)
        ORDER BY h.id''', (refs, refs)).fetchall()
    hypotheses, statuses, runs = [], [], {}
    by_status: Counter = Counter()
    for h in rows:
        history = db.execute('SELECT * FROM timeline_hypothesis_status WHERE hypothesis_id=? ORDER BY id', (h['id'],)).fetchall()
        current = history[-1] if history else None
        status = current['status'] if current else 'unknown'
        by_status[status] += 1
        if len(hypotheses) >= limit:
            continue
        statuses.extend(history)
        if h['run_id'] not in runs:
            runs[h['run_id']] = db.execute('SELECT * FROM timeline_correlation_runs WHERE id=?', (h['run_id'],)).fetchone()
        run = runs[h['run_id']]
        try:
            criteria = json.loads(h['criteria_json'])
        except ValueError:
            criteria = None
        hypotheses.append({
            'hypothesis_id': h['id'], 'status': status, 'status_at': current['created_at'] if current else None,
            'status_actor': current['actor'] if current else None, 'status_reason': current['reason'] if current else None,
            'decisions': len(history), 'relation': h['relation'], 'left': f"{h['left_kind']}:{h['left_id']}",
            'right': f"{h['right_kind']}:{h['right_id']}", 'rule_id': h['rule_id'], 'rule_version': h['rule_version'],
            'rule_sha256': run['rule_sha256'] if run is not None else None, 'run_id': h['run_id'],
            'ambiguity_group': h['ambiguity_group'], 'candidate_count': h['candidate_count'],
            'criteria': sorted(criteria) if isinstance(criteria, dict) else None,
            'criteria_sha256': hashlib.sha256(h['criteria_json'].encode('utf-8')).hexdigest(),
            'probability': None, 'semantics': CORRELATION_SEMANTICS,
        })
    summary = {'hypotheses_total': len(rows), 'hypotheses_listed': len(hypotheses), 'truncated': len(rows) > len(hypotheses),
               **{f'status_{name}': count for name, count in sorted(by_status.items())}}
    listed = {item['hypothesis_id'] for item in hypotheses}
    inputs = {'timeline_hypotheses': [dict(h) for h in rows if h['id'] in listed],
              'timeline_hypothesis_status': [dict(s) for s in statuses],
              'timeline_correlation_runs': [dict(r) for r in runs.values() if r is not None]}
    return hypotheses, summary, inputs


def report_section(db: sqlite3.Connection, *, from_utc: str | None = None, to_utc: str | None = None,
                   event_ids=None, artifact_ids=None, assume_zone: str | None = None,
                   assumption_rule: str | None = None, include_segments: bool = True, topic: str | None = None,
                   limit: int = 2000, max_scan: int = 200_000) -> dict:
    """Computed timeline values for a report over the caller's snapshot (see module docstring)."""
    db.row_factory = sqlite3.Row
    rule = load_rule(None, kind='display_rule')
    params = display_params(assume_zone, assumption_rule)
    projection = read_projection(db, rule, params)
    build = projection['build']
    events, artifacts = _ids(event_ids), _ids(artifact_ids)
    placement, flags = _placement(db, projection['id'], events)
    lo, hi = parse_bound(from_utc), parse_bound(to_utc, end=True)
    segments, segment_summary = ([], {'segments_in_window': None, 'segments_listed': 0, 'segments_truncated': False,
                                      'segments_filtered_by_topic': 0, 'segments_scan_limit_hit': False})
    if include_segments:
        segments, segment_summary = _segments(db, projection, rule, lo=lo, hi=hi, topic=topic, limit=limit,
                                              max_scan=max_scan)
    segment_artifacts = _ids(row['artifact_id'] for row in segments)
    correlations, correlation_summary, inputs = _correlations(db, events, sorted(set(artifacts) | set(segment_artifacts)),
                                                              limit)
    projection_row = db.execute('SELECT * FROM timeline_projections WHERE id=?', (projection['id'],)).fetchone()
    build_row = db.execute('SELECT * FROM timeline_projection_builds WHERE id=?', (build['id'],)).fetchone()
    inputs = {'timeline_projections': [dict(projection_row)], 'timeline_projection_builds': [dict(build_row)], **inputs}
    return {
        'schema': 'ageds.timeline-report-section/v1', 'version': VERSION,
        'rule': {'id': f'{rule.rule_id}@{rule.version}', 'rule_id': rule.rule_id, 'version': rule.version,
                 'sha256': rule.sha256, 'origin': rule.origin},
        # The stored projection identity (zone parameters; the free-text assumption label is per request).
        'projection': {'id': projection['id'], 'status': projection['status'], 'params': canonical_json(params),
                       'key_params': projection_row['params_json'], 'params_sha256': projection_row['params_sha256'],
                       'build_id': build['id'], 'build_mode': build['mode'],
                       'build_finished_at': build['finished_at'], 'max_halfwidth_us': build['counts'].get('max_halfwidth_us')},
        'window': {'from': from_utc, 'to': to_utc, 'to_exclusive': True, 'axis': 'UTC', 'match': 'display'},
        'scope': {'events': len(events), 'artifacts': len(artifacts)},
        'placement': placement, 'flags': flags,
        'segments': segments, 'segment_summary': segment_summary,
        'correlations': correlations, 'correlation_summary': correlation_summary,
        'inputs': inputs,
    }

"""Correlation hypotheses between derived presence and other events (rules as data, no probability).

Rules live in ``rules/<rule_id>.v<N>.json``; released files are pinned by SHA-256
(``RELEASED_SHA256``): an edited released rule is refused (add a new version).
A run names one presence run, the rule (id, version, hash), the merged
parameters (declared names only, same types), the timeline rule that placed the
other events, counts and the unmatched lists; each hypothesis stores the pair,
an ambiguity group (all candidates of one interval) and per-criterion evidence.
Review is an append-only status history (``accepted`` / ``rejected`` /
``revoked``) with a self-declared actor and a reason. Nothing here writes links
or changes events.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
import unicodedata
from pathlib import Path

from ..db import session
from . import presence, store

RULES_DIR = Path(__file__).parent / 'rules'
RELEASED_SHA256 = {
    ('presence_calendar', 1): '8c534faee9db413094fe37684bc7f632a8ba790cd26266541f6aacb7fe281c93',
    ('presence_communication', 1): 'c456e6dc526bbb4a68f67246213d5fc3b4ed7c48e8bf48c6d4d3e9a372687bb3',
}
_REF = re.compile(r'^(?P<rule_id>[a-z][a-z0-9_]{0,63})(?:@(?P<version>[1-9][0-9]{0,5}))?$')
TRANSITIONS = {None: {'accepted', 'rejected'}, 'accepted': {'revoked'}, 'rejected': {'revoked'},
               'revoked': {'accepted', 'rejected'}}


class RuleError(ValueError):
    pass


class RuleIntegrityError(RuntimeError):
    pass


def released_rules() -> list[dict]:
    out = []
    for path in sorted(RULES_DIR.glob('*.v*.json')):
        raw = path.read_bytes()
        data = json.loads(raw)
        digest = hashlib.sha256(raw).hexdigest()
        key = (data.get('rule_id'), data.get('version'))
        pinned = RELEASED_SHA256.get(key)
        if pinned is None or pinned != digest:
            raise RuleIntegrityError(f'{path.name}: released rule hash mismatch; add a new version file instead')
        out.append({**data, 'sha256': digest})
    return out


def load_rule(ref: str) -> dict:
    if not isinstance(ref, str) or not _REF.match(ref.strip()):
        raise RuleError('rule reference must look like rule_id or rule_id@version')
    match = _REF.match(ref.strip())
    found = [r for r in released_rules() if r['rule_id'] == match['rule_id']]
    if match['version']:
        found = [r for r in found if r['version'] == int(match['version'])]
    if not found:
        raise RuleError(f'unknown location correlation rule {ref!r}')
    return max(found, key=lambda r: r['version'])


def merge_params(rule: dict, params: dict | None) -> dict:
    merged = dict(rule['parameters'])
    for key, value in (params or {}).items():
        if key not in merged:
            raise RuleError(f'unknown parameter {key!r} for {rule["rule_id"]}')
        default = merged[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise RuleError(f'{key} must be true or false')
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10 ** 9:
                raise RuleError(f'{key} must be a non-negative integer')
        merged[key] = value
    return merged


def _fold(text) -> str:
    return unicodedata.normalize('NFKC', text).casefold() if isinstance(text, str) else ''


def run_correlation(rule_ref: str, presence_run_id: int, *, params: dict | None = None, actor: str = 'unknown') -> dict:
    from ..analysis.source import load_items
    rule = load_rule(rule_ref)
    merged = merge_params(rule, params)
    run = presence.get_run(int(presence_run_id), limit=1_000_000)
    intervals = run['intervals']
    labels = {p['id']: p.get('label') or p['id'] for p in run.get('places', [])}
    tolerance = merged['tolerance_s']
    counts = {'intervals': len(intervals), 'right_items': 0, 'right_untimed': 0, 'hypotheses': 0,
              'intervals_with_candidates': 0, 'candidates_truncated_intervals': 0, 'hypotheses_truncated': False}
    hypotheses, unmatched_right = [], set()
    source_rule = source_sha = None
    if intervals:
        lo = min(i['outer_start_us'] if i['outer_start_us'] is not None else i['start_us'] for i in intervals)
        hi = max(i['outer_end_us'] if i['outer_end_us'] is not None else i['end_us'] for i in intervals)
        margin = (tolerance + 86400) * 1_000_000
        with session() as db:
            loaded = load_items(db, start_utc=store.us_to_iso(lo - margin), end_utc=store.us_to_iso(hi + margin),
                                event_types=rule['right']['event_types'], limit=2_000_000)
            meta = {}
            if merged.get('label_match'):
                ids = [it['event_id'] for it in loaded['items']]
                for offset in range(0, len(ids), 500):
                    chunk = ids[offset:offset + 500]
                    for eid, text in db.execute(f"SELECT id, json_extract(metadata_json,'$.location') FROM events "
                                                f"WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                        meta[eid] = text
        source_rule, source_sha = loaded.get('rule'), loaded.get('rule_sha256')
        counts['right_untimed'] = len(loaded.get('untimed') or [])
        right = []
        for it in loaded['items']:
            start = it['t_lo'] if it.get('t_lo') is not None else it['t_start']
            end = max(x for x in (it.get('t_end'), it.get('t_hi'), it['t_start']) if x is not None)
            right.append((start * 1_000_000, end * 1_000_000, it))
        right.sort(key=lambda r: (r[0], r[2]['event_id']))
        counts['right_items'] = len(right)
        unmatched_right = {r[2]['event_id'] for r in right}
        starts = [r[0] for r in right]
        longest = max((r[1] - r[0] for r in right), default=0)
        tol_us = tolerance * 1_000_000
        for interval in intervals:
            s, e = interval['start_us'], interval['end_us']
            os_, oe = interval['outer_start_us'], interval['outer_end_us']
            group = f'interval:{interval["id"]}'
            found = []
            lower = bisect.bisect_left(starts, s - tol_us - longest)
            upper = bisect.bisect_right(starts, e + tol_us)
            for r_start, r_end, it in right[lower:upper]:
                if r_end < s - tol_us:
                    continue
                overlap = max(0, min(e, r_end) - max(s, r_start))
                gap = 0 if overlap > 0 or (r_start <= e and r_end >= s) else min(abs(r_start - e), abs(s - r_end))
                if gap > tol_us or (merged['require_inner_overlap'] and overlap <= 0 and not (s <= r_start <= e)):
                    continue
                within_outer = (os_ is None or r_start >= os_) and (oe is None or r_end <= oe)
                evidence = {'inner_overlap_s': overlap / 1e6, 'inner_gap_s': gap / 1e6,
                            'within_outer_bounds': within_outer,
                            'outer_bounds_known': {'start': os_ is not None, 'end': oe is not None},
                            'interval_status': interval['status'], 'time_basis': it.get('basis'),
                            'right_signal': it.get('signal')}
                if merged.get('label_match'):
                    text = meta.get(it['event_id'])
                    label = labels.get(interval['place_id'])
                    evidence['location_text_contains_place_label'] = (
                        None if not text else bool(label) and _fold(label) in _fold(text))
                if rule['rule_id'] == 'presence_communication':
                    evidence['same_stream_hint'] = 'unknown'
                found.append((it['event_id'], evidence))
            if not found:
                continue
            counts['intervals_with_candidates'] += 1
            if len(found) > merged['max_candidates_per_interval']:
                counts['candidates_truncated_intervals'] += 1
                found = found[:merged['max_candidates_per_interval']]
            for event_id, evidence in found:
                if len(hypotheses) >= merged['max_hypotheses']:
                    counts['hypotheses_truncated'] = True
                    break
                unmatched_right.discard(event_id)
                hypotheses.append((interval['id'], event_id, group, len(found), evidence))
    counts['hypotheses'] = len(hypotheses)
    unmatched = {'intervals_without_candidates': sorted({i['id'] for i in intervals} - {h[0] for h in hypotheses})[:1000],
                 'right_events_without_interval': sorted(unmatched_right)[:1000],
                 'right_events_without_interval_total': len(unmatched_right)}
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        cur = db.execute('''INSERT INTO location_correlation_runs(rule_id,rule_version,rule_sha256,presence_run_id,
                            params_json,source_rule,source_rule_sha256,counts_json,unmatched_json,actor)
                            VALUES(?,?,?,?,?,?,?,?,?,?)''',
                         (rule['rule_id'], rule['version'], rule['sha256'], run['run_id'], store.canonical(merged),
                          source_rule, source_sha, store.canonical(counts), store.canonical(unmatched),
                          actor.strip()[:200] or 'unknown'))
        run_id = int(cur.lastrowid)
        db.executemany('''INSERT INTO location_correlation_hypotheses(run_id,interval_id,event_id,relation,ambiguity_group,
                          candidate_count,evidence_json) VALUES(?,?,?,?,?,?,?)''',
                       [(run_id, h[0], h[1], rule['relation'], h[2], h[3], store.canonical(h[4])) for h in hypotheses])
    return get_correlation_run(run_id)


def get_correlation_run(run_id: int, *, limit: int = 5000) -> dict:
    with session() as db:
        r = db.execute('SELECT * FROM location_correlation_runs WHERE id=?', (run_id,)).fetchone()
        if r is None:
            raise LookupError(f'correlation run {run_id} not found')
        rows = db.execute('''SELECT h.*, (SELECT status FROM location_correlation_status s WHERE s.hypothesis_id=h.id
                             ORDER BY s.id DESC LIMIT 1) AS status FROM location_correlation_hypotheses h
                             WHERE h.run_id=? ORDER BY h.id LIMIT ?''', (run_id, limit + 1)).fetchall()
    return {'run_id': r['id'], 'rule': {'rule_id': r['rule_id'], 'version': r['rule_version'], 'sha256': r['rule_sha256']},
            'presence_run_id': r['presence_run_id'], 'params': json.loads(r['params_json']),
            'source_rule': r['source_rule'], 'source_rule_sha256': r['source_rule_sha256'],
            'counts': json.loads(r['counts_json']), 'unmatched': json.loads(r['unmatched_json']), 'actor': r['actor'],
            'created_at': r['created_at'],
            'hypotheses': [{'id': h['id'], 'interval_id': h['interval_id'], 'event_id': h['event_id'],
                            'relation': h['relation'], 'ambiguity_group': h['ambiguity_group'],
                            'candidate_count': h['candidate_count'], 'evidence': json.loads(h['evidence_json']),
                            'status': h['status'] or 'proposed'} for h in rows[:limit]],
            'hypotheses_listed_complete': len(rows) <= limit,
            'semantics': 'hypotheses for human review; no probability; a temporal coincidence is not attendance'}


def list_correlation_runs(limit: int = 50) -> list[dict]:
    with session() as db:
        rows = db.execute('SELECT id, rule_id, rule_version, presence_run_id, counts_json, created_at FROM '
                          'location_correlation_runs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [{'run_id': r[0], 'rule': f'{r[1]}@{r[2]}', 'presence_run_id': r[3], 'counts': json.loads(r[4]),
             'created_at': r[5]} for r in rows]


def review(hypothesis_id: int, action: str, *, reason: str, actor: str) -> dict:
    status = {'accept': 'accepted', 'reject': 'rejected', 'revoke': 'revoked'}.get(action)
    if status is None:
        raise ValueError('action must be accept, reject or revoke')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 10_000:
        raise ValueError('a non-empty reason is required')
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise ValueError('actor must be a non-blank string up to 200 characters')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM location_correlation_hypotheses WHERE id=?', (hypothesis_id,)).fetchone():
            raise LookupError(f'hypothesis {hypothesis_id} not found')
        current = db.execute('SELECT status FROM location_correlation_status WHERE hypothesis_id=? ORDER BY id DESC LIMIT 1',
                             (hypothesis_id,)).fetchone()
        current = current[0] if current else None
        if status not in TRANSITIONS[current]:
            raise ValueError(f'transition {current or "proposed"} -> {status} is not allowed')
        cur = db.execute('INSERT INTO location_correlation_status(hypothesis_id,status,actor,reason) VALUES(?,?,?,?)',
                         (hypothesis_id, status, actor.strip(), reason.strip()))
        db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES (?,?,?,?,?)",
                   (actor.strip(), 'location_correlation_review', 'location_correlation_hypothesis', hypothesis_id,
                    json.dumps({'from': current or 'proposed', 'to': status}, sort_keys=True)))
        return {'hypothesis_id': hypothesis_id, 'status_id': int(cur.lastrowid), 'from': current or 'proposed',
                'to': status, 'actor_verified': False}

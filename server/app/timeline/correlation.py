"""Cross-source correlation hypotheses from versioned rules.

A run applies one rule version with explicit parameters and appends a new run
row, its hypotheses and an initial ``proposed`` status for each. Nothing is
overwritten: recomputing is a new run. Each hypothesis stores per-criterion
evidence (deltas, matched fields, reasons) and its ambiguity group; no single
probability is computed. Decisions are appended to the status history and are
revocable; an accepted hypothesis may create a ``links`` row whose rationale
names the hypothesis, and revoking removes that link with an audit record.
"""
from __future__ import annotations

import bisect
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .. import timeinfo
from ..wav_header import MAX_PREFIX_BYTES, probe_wav_header
from .candidates import Candidate, place
from .projection import ensure_projection, recording_candidates
from .ruleset import Rule, RuleError, canonical_json, load_rule
from .timeutil import US, iso, local_text_to_us, utc_text_to_us

STATUSES = ('proposed', 'accepted', 'rejected', 'revoked')
TRANSITIONS = {'accept': ({'proposed', 'revoked'}, 'accepted'), 'reject': ({'proposed', 'revoked'}, 'rejected'),
               'revoke': ({'proposed', 'accepted', 'rejected'}, 'revoked')}
MAX_REASON = 2000
MAX_ACTOR = 200


class CorrelationError(ValueError):
    """Invalid rule parameters, review action or state transition."""


class TransitionError(CorrelationError):
    """The requested decision does not apply to the current status (HTTP 409)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def merge_params(rule: Rule, override: dict | None) -> dict:
    """Rule defaults overlaid by explicitly named parameters of the same type."""
    params = json.loads(json.dumps(rule.data['parameters']))
    for key, value in (override or {}).items():
        if key not in params:
            raise CorrelationError(f'unknown parameter for {rule.ref}: {key}')
        default = params[key]
        if default is not None and value is not None:
            numeric = isinstance(default, (int, float)) and not isinstance(default, bool)
            if numeric and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise CorrelationError(f'parameter {key} must be a number')
            if not numeric and type(value) is not type(default):
                raise CorrelationError(f'parameter {key} must be {type(default).__name__}')
            if numeric and (value != value or value < 0):
                raise CorrelationError(f'parameter {key} must be a finite non-negative number')
        params[key] = value
    if rule.rule_id == 'recording_call_log' and params.get('assume_zone'):
        if not params.get('assumption_rule'):
            raise CorrelationError('assume_zone requires assumption_rule')
        try:
            timeinfo.assume_zone(timeinfo.TimeCandidate('other', None, 'check', local='2000-01-01T00:00:00'),
                                 params['assume_zone'], rule='check')
        except ValueError as exc:
            raise CorrelationError(str(exc)) from exc
    return params


def _digits(value) -> str:
    return re.sub(r'\D', '', str(value)) if value is not None else ''


def _number_evidence(recording_number, recording_source, call_number, suffix: int) -> dict:
    left, right = _digits(recording_number), _digits(call_number)
    evidence = {'recording_number_raw': recording_number, 'recording_number_source': recording_source,
                'call_number_raw': call_number, 'suffix_digits': suffix,
                'semantics': 'filename-derived number is a hypothesis, not a verified identity'}
    if not left or not right:
        evidence['status'] = 'not_available'
    elif left == right:
        evidence['status'] = 'exact'
    elif len(left) >= suffix and len(right) >= suffix and left[-suffix:] == right[-suffix:]:
        evidence['status'] = 'suffix'
    else:
        evidence['status'] = 'mismatch'
    return evidence


def _duration_evidence(recording_seconds, recording_source, call_seconds, params) -> dict:
    evidence = {'recording_seconds': recording_seconds, 'recording_duration_source': recording_source,
                'call_seconds': call_seconds}
    if recording_seconds is None or call_seconds is None:
        evidence['status'] = 'not_evaluated'
        return evidence
    tolerance = max(params['duration_tolerance_seconds'], params['duration_tolerance_ratio'] * call_seconds)
    delta = recording_seconds - call_seconds
    evidence.update({'delta_seconds': round(delta, 3), 'tolerance_seconds': round(tolerance, 3),
                     'status': 'agree' if abs(delta) <= tolerance else 'disagree'})
    return evidence


def _json_path_value(text, path: str):
    try:
        value = json.loads(text or '{}')
    except ValueError:
        return None
    for part in path.lstrip('$').strip('.').split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                return None
        else:
            return None
    if value != value or value in (float('inf'), float('-inf')) or value < 0:
        return None
    return float(value)


def _recording_facts(db, artifact, params: dict, recording_types: list) -> dict:
    """Number hypothesis and duration of one recording, each with its source."""
    events = db.execute('''SELECT * FROM events WHERE artifact_id=? AND event_type IN (SELECT value FROM json_each(?))
                           ORDER BY id''', (artifact['id'], json.dumps(recording_types))).fetchall()
    number = number_source = duration = duration_source = None
    for event in events:
        for path in params['recording_number_paths']:
            value = _json_path_value(event['metadata_json'], path)
            if number is None and isinstance(value, (str, int)) and _digits(value):
                number, number_source = str(value), f"events:{event['id']}:metadata{path[1:]}"
        for column in params['recording_number_columns']:
            if number is None and column in event.keys() and event[column] and _digits(event[column]):
                number, number_source = event[column], f"events:{event['id']}:{column}"
        for path in params['recording_duration_paths']:
            value = _finite(_json_path_value(event['metadata_json'], path))
            if duration is None and value is not None:
                duration, duration_source = value, f"events:{event['id']}:metadata{path[1:]}"
        if duration is None and event['ts_start'] and event['ts_end']:
            try:
                start, end = (timeinfo.from_iso('start', event['ts_start'], 'x'), timeinfo.from_iso('end', event['ts_end'], 'x'))
                a = utc_text_to_us(start.utc) if start.utc else local_text_to_us(start.local)
                b = utc_text_to_us(end.utc) if end.utc else local_text_to_us(end.local)
                if b >= a and (start.utc is None) == (end.utc is None):
                    duration, duration_source = (b - a) / US, f"events:{event['id']}:ts_end-ts_start"
            except (ValueError, OverflowError):
                pass
    if duration is None and params.get('use_wav_header_duration') and artifact['stored_path']:
        path = Path(artifact['stored_path'])
        try:
            with path.open('rb') as handle:
                prefix = handle.read(MAX_PREFIX_BYTES)
            size = artifact['size_bytes'] if isinstance(artifact['size_bytes'], int) else None
            probe = probe_wav_header(prefix, size, end_of_input=size is not None and len(prefix) >= size)
            if probe['status'] == 'observed' and probe['declared_duration_sec'] is not None:
                duration, duration_source = float(probe['declared_duration_sec']), 'wav_header:' + probe['duration_basis']
        except (OSError, ValueError):
            pass
    return {'number': number, 'number_source': number_source, 'duration': duration, 'duration_source': duration_source}


def _quarter_offset(delta_us: int, params: dict) -> tuple[int | None, int]:
    """Nearest allowed UTC offset (minutes) for local - utc = delta and the residual."""
    low, high = params['unknown_zone_offset_hours_min'] * 60, params['unknown_zone_offset_hours_max'] * 60
    minutes = delta_us / (60 * US)
    if params['unknown_zone_offsets_minutes'] == 'quarter_hours':
        nearest = int(round(minutes / 15.0)) * 15
    elif params['unknown_zone_offsets_minutes'] == 'whole_hours':
        nearest = int(round(minutes / 60.0)) * 60
    else:
        raise CorrelationError('unknown_zone_offsets_minutes must be quarter_hours or whole_hours')
    if not low <= nearest <= high:
        return None, abs(delta_us)
    return nearest, abs(delta_us - nearest * 60 * US)


def _ampm_alternatives(candidate: Candidate, params: dict) -> list[tuple[str, bool]]:
    locals_ = [(candidate.local, False)]
    flagged = any(candidate.metadata.get(k) for k in params['ampm_ambiguity_metadata_keys'])
    if params['consider_ampm_ambiguity'] and flagged and candidate.local:
        moment = datetime.fromisoformat(candidate.local)
        shifted = moment.replace(hour=(moment.hour + 12) % 24)
        locals_.append((shifted.isoformat(), True))
    return locals_


def _recording_time_points(candidate: Candidate, params: dict) -> list[dict]:
    """Instants (or unknown-zone wall clocks) to test for one recording candidate."""
    points = []
    if candidate.tz_status in ('explicit_offset', 'utc_epoch', 'assumed_zone') and candidate.utc:
        instants = [candidate.utc] + [a for a in candidate.metadata.get('utc_alternatives', []) if a != candidate.utc]
        for value in instants:
            points.append({'mode': 'absolute', 'us': utc_text_to_us(value), 'utc': value, 'ampm_alternative': False,
                           'dst': candidate.metadata.get('dst')})
        return points
    for local, ampm in _ampm_alternatives(candidate, params):
        if params.get('assume_zone'):
            tc = timeinfo.TimeCandidate('other', candidate.raw, candidate.source, local=local,
                                        precision=candidate.precision if candidate.precision in timeinfo.PRECISIONS else 'unknown')
            assumed = timeinfo.assume_zone(tc, params['assume_zone'], rule=params['assumption_rule'])
            if assumed.utc:
                alternatives = assumed.metadata.get('utc_alternatives') or [assumed.utc]
                for value in alternatives:
                    points.append({'mode': 'assumed_zone', 'us': utc_text_to_us(value), 'utc': value, 'local': local,
                                   'zone': params['assume_zone'], 'assumption_rule': params['assumption_rule'],
                                   'dst': assumed.metadata.get('dst'), 'ampm_alternative': ampm})
                continue
            points.append({'mode': 'unknown_zone_offset_search', 'us': local_text_to_us(local), 'local': local,
                           'dst': assumed.metadata.get('dst'), 'zone_tried': params['assume_zone'],
                           'ampm_alternative': ampm})
            continue
        points.append({'mode': 'unknown_zone_offset_search', 'us': local_text_to_us(local), 'local': local,
                       'ampm_alternative': ampm, 'dst': None})
    return points


def _alternative(evidence: dict) -> dict:
    point = evidence['tested_point']
    return {'mode': evidence['mode'], 'residual_seconds': evidence['residual_seconds'],
            'implied_utc_offset_minutes': evidence.get('implied_utc_offset_minutes'),
            'ampm_alternative': point.get('ampm_alternative'), 'dst': point.get('dst'),
            'recording_candidate_ref': evidence['recording_candidate']['ref'],
            'tested': point.get('utc') or point.get('local')}


def _calls(db, projection: dict, params: dict, event_types: list[str]) -> tuple[list, bool]:
    rows = db.execute('''SELECT ti.ref_id, ti.sort_us, ti.end_us, ti.basis, ti.candidate_ref, e.phone_or_address,
                           e.metadata_json, e.direction FROM timeline_items ti JOIN events e ON e.id=ti.ref_id
                         WHERE ti.projection_id=? AND ti.item_kind='event' AND ti.sort_us IS NOT NULL
                           AND ti.event_type IN (SELECT value FROM json_each(?))
                         ORDER BY ti.sort_us, ti.ref_id LIMIT ?''',
                      (projection['id'], json.dumps(event_types), params['max_calls'] + 1)).fetchall()
    return rows[:params['max_calls']], len(rows) > params['max_calls']


def _call_seconds(call) -> float | None:
    value = _finite(_json_path_value(call['metadata_json'], '$.duration.seconds'))
    if value is None:
        value = _finite(_json_path_value(call['metadata_json'], '$.duration_seconds'))
    if value is None and call['end_us'] is not None:
        value = (call['end_us'] - call['sort_us']) / US
    return value


def _recording_call_log(db, rule: Rule, params: dict, projection: dict) -> tuple[list, dict]:
    display_rule = load_rule(kind='display_rule')
    calls, calls_truncated = _calls(db, projection, params, rule.data['right']['event_types'])
    keys = [c['sort_us'] for c in calls]
    recordings = db.execute('''SELECT id,original_name,mime_type,stored_path,size_bytes,source_id FROM artifacts
                               WHERE mime_type LIKE 'audio/%' OR mime_type LIKE 'video/%' ORDER BY id LIMIT ?''',
                            (params['max_recordings'] + 1,)).fetchall()
    recordings_truncated = len(recordings) > params['max_recordings']
    recordings = recordings[:params['max_recordings']]
    tolerance = int(params['time_tolerance_seconds'] * US)
    recording_types = display_rule.data['recording']['recording_event_types']
    widest = int(max(abs(params['unknown_zone_offset_hours_min']), abs(params['unknown_zone_offset_hours_max'])) * 3600 * US)
    proposals, unmatched_recordings = [], []
    matched_calls: dict[int, int] = {}
    truncated_candidates = 0
    for recording in recordings:
        candidates = [c for c in recording_candidates(db, recording['id'], display_rule, {})
                      if c.origin != 'derived_correlation' and c.usable and c.kind in params['recording_time_kinds']
                      and (c.utc or c.local)]
        if not candidates:
            unmatched_recordings.append({'artifact_id': recording['id'], 'reason': 'no_usable_time_candidate'})
            continue
        facts = _recording_facts(db, recording, params, recording_types)
        best: dict[int, dict] = {}
        evaluated = 0
        for candidate in candidates:
            try:
                points = _recording_time_points(candidate, params)
            except (ValueError, OverflowError):
                continue
            for point in points:
                evaluated += 1
                lo, hi = bisect.bisect_left(keys, point['us'] - widest - tolerance), bisect.bisect_right(keys, point['us'] + widest + tolerance)
                recording_unknown = point['mode'] == 'unknown_zone_offset_search'
                for call in calls[lo:hi]:
                    call_us = call['sort_us']
                    call_unknown = call['basis'] == 'local_as_utc_unknown_zone'
                    if recording_unknown != call_unknown:
                        # One side is a wall clock without zone: some real UTC offset must explain the difference.
                        local_minus_utc = (point['us'] - call_us) if recording_unknown else (call_us - point['us'])
                        offset, residual = _quarter_offset(local_minus_utc, params)
                        if offset is None or residual > tolerance:
                            continue
                        time_evidence = {'mode': 'unknown_zone_offset_search' if recording_unknown else 'call_unknown_zone_offset_search',
                                         'implied_utc_offset_minutes': offset, 'residual_seconds': residual / US}
                    else:
                        delta = point['us'] - call_us
                        if abs(delta) > tolerance:
                            continue
                        mode = 'same_unknown_zone_assumed' if recording_unknown else point['mode']
                        time_evidence = {'mode': mode, 'residual_seconds': abs(delta) / US}
                    time_evidence.update({
                        'delta_seconds': (point['us'] - call_us) / US, 'tolerance_seconds': params['time_tolerance_seconds'],
                        'recording_candidate': {'ref': candidate.ref, 'origin': candidate.origin, 'kind': candidate.kind,
                                                'raw': candidate.raw, 'utc': candidate.utc, 'local': candidate.local,
                                                'tz_status': candidate.tz_status, 'precision': candidate.precision,
                                                'source': candidate.source},
                        'tested_point': {k: v for k, v in point.items() if k != 'us'} | {'utc_or_local_as_utc': iso(point['us'])},
                        'call_time': {'display': iso(call_us), 'basis': call['basis'], 'candidate_ref': call['candidate_ref']},
                        'within_tolerance': True})
                    if mode_note := {'same_unknown_zone_assumed': 'both wall clocks lack a zone; compared as if in the same zone'}.get(time_evidence['mode']):
                        time_evidence['note'] = mode_note
                    previous = best.get(call['ref_id'])
                    if previous is None:
                        best[call['ref_id']] = {'time': time_evidence, 'call': call, 'alternatives': []}
                        continue
                    if time_evidence['residual_seconds'] < previous['time']['residual_seconds']:
                        time_evidence, previous['time'] = previous['time'], time_evidence
                    if len(previous['alternatives']) < 10:
                        previous['alternatives'].append(_alternative(time_evidence))
        if not best:
            unmatched_recordings.append({'artifact_id': recording['id'], 'reason': 'no_call_within_tolerance',
                                         'evaluated_time_points': evaluated})
            continue
        accepted = []
        for call_id, match in best.items():
            call = match['call']
            duration = _duration_evidence(facts['duration'], facts['duration_source'], _call_seconds(call), params)
            number = _number_evidence(facts['number'], facts['number_source'], call['phone_or_address'],
                                      params['number_suffix_digits'])
            corroborated = duration['status'] == 'agree' or number['status'] in ('exact', 'suffix')
            uncertain_mode = match['time']['mode'] in ('unknown_zone_offset_search', 'same_unknown_zone_assumed',
                                                       'call_unknown_zone_offset_search')
            gate = {'time': True, 'corroboration_required': bool(uncertain_mode and params['unknown_zone_requires_corroboration']),
                    'corroborated': corroborated}
            if gate['corroboration_required'] and not corroborated:
                continue
            match['time']['evaluated_time_points'] = evaluated
            match['time']['other_matching_points'] = match['alternatives']
            accepted.append((match['time']['residual_seconds'], call_id,
                             {'time': match['time'], 'duration': duration, 'number': number, 'gate': gate,
                              'call_direction': call['direction']}))
        if not accepted:
            unmatched_recordings.append({'artifact_id': recording['id'], 'reason': 'only_uncorroborated_unknown_zone_matches'})
            continue
        accepted.sort(key=lambda item: (item[0], item[1]))
        if len(accepted) > params['max_candidates_per_recording']:
            truncated_candidates += 1
            accepted = accepted[:params['max_candidates_per_recording']]
        for _, call_id, criteria in accepted:
            matched_calls[call_id] = matched_calls.get(call_id, 0) + 1
            proposals.append({'left_kind': 'artifact', 'left_id': recording['id'], 'right_kind': 'event', 'right_id': call_id,
                              'ambiguity_group': f"recording_call_log:artifact:{recording['id']}",
                              'candidate_count': len(accepted), 'criteria': criteria})
    for proposal in proposals:
        proposal['criteria']['call_matched_by_recordings'] = matched_calls[proposal['right_id']]
    unmatched_calls = [c['ref_id'] for c in calls if c['ref_id'] not in matched_calls]
    limit = params['max_unmatched_listed']
    result = {
        'counts': {'recordings_considered': len(recordings), 'calls_considered': len(calls), 'hypotheses': len(proposals),
                   'recordings_with_candidates': len({p['left_id'] for p in proposals}),
                   'ambiguous_recordings': len({p['left_id'] for p in proposals if p['candidate_count'] > 1}),
                   'recordings_without_call': len(unmatched_recordings), 'calls_without_recording': len(unmatched_calls)},
        'unmatched': {'recordings': unmatched_recordings[:limit], 'calls': unmatched_calls[:limit],
                      'listed_limit': limit},
        'truncated': {'recordings': recordings_truncated, 'calls': calls_truncated,
                      'recordings_with_candidate_cap': truncated_candidates},
    }
    return proposals, result


_WORD = re.compile(r'\w+', re.UNICODE)


def _ngrams(text: str | None, n: int, max_chars: int) -> list[tuple]:
    if not text:
        return []
    normalized = unicodedata.normalize('NFKC', text[:max_chars]).casefold()
    words = _WORD.findall(normalized)
    return [tuple(words[i:i + n]) for i in range(0, max(0, len(words) - n + 1))]


def _email_chat(db, rule: Rule, params: dict, projection: dict) -> tuple[list, dict]:
    def items(types, cap):
        rows = db.execute('''SELECT ti.ref_id, ti.sort_us, ti.lo_us, ti.hi_us, ti.basis, ti.event_type FROM timeline_items ti
                             WHERE ti.projection_id=? AND ti.item_kind='event' AND ti.sort_us IS NOT NULL
                               AND ti.event_type IN (SELECT value FROM json_each(?)) ORDER BY ti.sort_us, ti.ref_id LIMIT ?''',
                          (projection['id'], json.dumps(types), cap + 1)).fetchall()
        return rows[:cap], len(rows) > cap
    left, left_truncated = items(rule.data['left']['event_types'], params['max_left'])
    right, right_truncated = items(rule.data['right']['event_types'], params['max_right'])
    keys = [r['sort_us'] for r in right]
    halfwidth = max([max(r['sort_us'] - r['lo_us'], r['hi_us'] - r['sort_us']) for r in right] or [0])
    lag = int(params['max_lag_seconds'] * US)
    n, chars = params['ngram_words'], params['max_text_chars']
    if not isinstance(n, int) or n < 1 or n > 50:
        raise CorrelationError('ngram_words must be an integer from 1 to 50')
    text_cache: dict[int, list] = {}

    def grams(event_id):
        if event_id not in text_cache:
            row = db.execute('SELECT subject, body FROM events WHERE id=?', (event_id,)).fetchone()
            text = ' '.join(x for x in (row['subject'], row['body']) if x) if row else ''
            text_cache[event_id] = _ngrams(text, n, chars)
        return text_cache[event_id]
    proposals, evaluated, pairs_truncated = [], 0, False
    for email in left:
        lo = bisect.bisect_left(keys, email['lo_us'] - lag - halfwidth)
        hi = bisect.bisect_right(keys, email['hi_us'] + lag + halfwidth)
        found = []
        email_grams = None
        for chat in right[lo:hi]:
            if chat['ref_id'] == email['ref_id']:
                continue
            evaluated += 1
            if evaluated > params['max_pairs_evaluated']:
                pairs_truncated = True
                break
            if chat['hi_us'] < email['lo_us']:
                gap = email['lo_us'] - chat['hi_us']
            elif chat['lo_us'] > email['hi_us']:
                gap = chat['lo_us'] - email['hi_us']
            else:
                gap = 0
            if gap > lag:
                continue
            if params['direction'] == 'email_first' and chat['hi_us'] < email['lo_us']:
                continue
            if email_grams is None:
                email_grams = grams(email['ref_id'])
            chat_grams = set(grams(chat['ref_id']))
            shared = [g for g in dict.fromkeys(email_grams) if g in chat_grams]
            overlap = {'method': 'word_ngrams_nfkc_casefold', 'ngram_words': n, 'shared_ngrams': len(shared),
                       'min_shared_ngrams': params['min_shared_ngrams'],
                       'example': ' '.join(shared[0]) if shared else None,
                       'status': 'shared' if len(shared) >= params['min_shared_ngrams'] else 'insufficient'}
            if params['require_quote_overlap'] and overlap['status'] != 'shared':
                continue
            found.append((gap, chat['ref_id'], {
                'proximity': {'min_possible_gap_seconds': gap / US, 'sort_delta_seconds': (chat['sort_us'] - email['sort_us']) / US,
                              'max_lag_seconds': params['max_lag_seconds'], 'direction': params['direction'],
                              'email_basis': email['basis'], 'chat_basis': chat['basis'], 'within_lag': True},
                'quote_overlap': overlap, 'chat_event_type': chat['event_type']}))
        if pairs_truncated:
            break
        found.sort(key=lambda item: (item[0], item[1]))
        found = found[:params['max_candidates_per_left']]
        for _, chat_id, criteria in found:
            proposals.append({'left_kind': 'event', 'left_id': email['ref_id'], 'right_kind': 'event', 'right_id': chat_id,
                              'ambiguity_group': f"email_chat:event:{email['ref_id']}", 'candidate_count': len(found),
                              'criteria': criteria})
    return proposals, {'counts': {'emails_considered': len(left), 'chat_messages_considered': len(right),
                                  'pairs_evaluated': min(evaluated, params['max_pairs_evaluated']),
                                  'hypotheses': len(proposals)},
                       'truncated': {'emails': left_truncated, 'chat_messages': right_truncated, 'pairs': pairs_truncated}}


def _same_conversation(db, rule: Rule, params: dict, projection: dict) -> tuple[list, dict]:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    logical = [t for t in params['logical_record_tables'] if t in tables]
    rows = db.execute('''SELECT thread_id, CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json,'$.chatbot') END AS chatbot,
                           artifact_id, COUNT(*) AS n, MIN(id) AS first_id, MAX(id) AS last_id
                         FROM events WHERE event_type IN (SELECT value FROM json_each(?)) AND thread_id IS NOT NULL
                           AND artifact_id IS NOT NULL GROUP BY thread_id, chatbot, artifact_id
                         ORDER BY thread_id, chatbot, artifact_id''', (json.dumps(params['event_types']),)).fetchall()
    threads: dict = {}
    for row in rows:
        threads.setdefault((row['thread_id'], row['chatbot']), []).append(row)
    shared = {k: v for k, v in threads.items() if len(v) >= 2}
    threads_truncated = len(shared) > params['max_threads']
    proposals = []
    for (thread_id, chatbot), members in list(shared.items())[:params['max_threads']]:
        members = members[:params['max_artifacts_per_thread']]
        extents = {}
        for m in members:
            extent = db.execute('''SELECT MIN(ti.sort_us), MAX(ti.sort_us) FROM timeline_items ti JOIN events e ON e.id=ti.ref_id
                                   WHERE ti.projection_id=? AND ti.item_kind='event' AND e.artifact_id=? AND e.thread_id=?''',
                                (projection['id'], m['artifact_id'], thread_id)).fetchone()
            extents[m['artifact_id']] = {'messages': int(m['n']), 'first': iso(extent[0]), 'last': iso(extent[1])}
        pair_count = len(members) * (len(members) - 1) // 2
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                common = db.execute('''SELECT COUNT(*) FROM (SELECT external_id FROM events WHERE artifact_id=? AND thread_id=?
                                         AND external_id IS NOT NULL INTERSECT SELECT external_id FROM events WHERE artifact_id=?
                                         AND thread_id=? AND external_id IS NOT NULL)''',
                                    (a['artifact_id'], thread_id, b['artifact_id'], thread_id)).fetchone()[0]
                proposals.append({'left_kind': 'artifact', 'left_id': a['artifact_id'], 'right_kind': 'artifact',
                                  'right_id': b['artifact_id'], 'ambiguity_group': f'same_conversation:{chatbot}:{thread_id}',
                                  'candidate_count': pair_count,
                                  'criteria': {'thread_id': {'value': thread_id, 'chatbot': chatbot, 'status': 'identical'},
                                               'shared_external_ids': {'count': int(common)},
                                               'message_counts': {'left': extents[a['artifact_id']],
                                                                  'right': extents[b['artifact_id']]},
                                               'logical_record_tables': {'present': logical,
                                                                         'interpreted': False}}})
    result = {'counts': {'threads_seen': len(threads), 'threads_in_several_exports': len(shared), 'hypotheses': len(proposals)},
              'truncated': {'threads': threads_truncated}, 'logical_record_tables_present': logical}
    if not shared:
        result['skipped_reason'] = 'no thread_id of the configured event types appears in two artifacts'
    return proposals, result


RUNNERS = {'recording_call_log': _recording_call_log, 'email_chat': _email_chat, 'same_conversation': _same_conversation}


def run_rule(db, rule_ref: str | Rule, params_override: dict | None = None, *, actor: str = 'unknown') -> dict:
    """Execute one correlation rule version; appends a run, its hypotheses and their ``proposed`` status."""
    rule = rule_ref if isinstance(rule_ref, Rule) else load_rule(rule_ref, kind='correlation_rule')
    if rule.kind != 'correlation_rule' or rule.rule_id not in RUNNERS:
        raise RuleError(f'no runner for {rule.ref}')
    actor = _actor(actor)
    params = merge_params(rule, params_override)
    started_at = _now()
    projection = ensure_projection(db, load_rule(kind='display_rule'), {})
    proposals, result = RUNNERS[rule.rule_id](db, rule, params, projection)
    truncated = any(v for v in result.get('truncated', {}).values())
    status = 'skipped' if result.get('skipped_reason') else 'partial' if truncated else 'completed'
    result.update({'parameters': params, 'display_projection': {'id': projection['id'], 'rule': projection['rule'],
                                                                 'build_id': projection['build']['id']}})
    db.execute('BEGIN IMMEDIATE')
    try:
        run_id = int(db.execute('''INSERT INTO timeline_correlation_runs(rule_id,rule_version,rule_sha256,params_json,
            display_projection_id,actor,started_at,finished_at,status,result_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (rule.rule_id, rule.version, rule.sha256, canonical_json(params), projection['id'], actor, started_at, _now(),
             status, canonical_json(result))).lastrowid)
        for p in proposals:
            hid = int(db.execute('''INSERT INTO timeline_hypotheses(run_id,rule_id,rule_version,relation,left_kind,left_id,
                right_kind,right_id,ambiguity_group,candidate_count,criteria_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (run_id, rule.rule_id, rule.version, rule.data['relation'], p['left_kind'], p['left_id'], p['right_kind'],
                 p['right_id'], p['ambiguity_group'], p['candidate_count'], canonical_json(p['criteria']))).lastrowid)
            db.execute('''INSERT INTO timeline_hypothesis_status(hypothesis_id,status,actor,reason,details_json)
                          VALUES(?,?,?,?,?)''', (hid, 'proposed', f'rule:{rule.ref}', f'proposed by correlation run {run_id}',
                                                 canonical_json({'run_actor': actor})))
        db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)",
                   (actor, 'timeline_correlation_run', 'timeline_correlation_run', run_id,
                    canonical_json({'rule': rule.ref, 'status': status, 'hypotheses': len(proposals)})))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return run_detail(db, run_id)


def _actor(actor) -> str:
    if not isinstance(actor, str) or not actor.strip() or len(actor) > MAX_ACTOR:
        raise CorrelationError(f'actor must be non-empty text of at most {MAX_ACTOR} characters')
    return actor.strip()


def run_detail(db, run_id: int) -> dict | None:
    row = db.execute('SELECT * FROM timeline_correlation_runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out['params'] = json.loads(out.pop('params_json'))
    out['result'] = json.loads(out.pop('result_json'))
    out['hypothesis_count'] = int(db.execute('SELECT COUNT(*) FROM timeline_hypotheses WHERE run_id=?', (run_id,)).fetchone()[0])
    return out


def list_runs(db, *, limit: int = 50, before_id: int | None = None) -> dict:
    if not 1 <= limit <= 200:
        raise CorrelationError('limit must be 1..200')
    rows = db.execute('''SELECT id,rule_id,rule_version,rule_sha256,actor,started_at,finished_at,status,
                           (SELECT COUNT(*) FROM timeline_hypotheses h WHERE h.run_id=r.id) AS hypothesis_count
                         FROM timeline_correlation_runs r WHERE (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?''',
                      (before_id, before_id, limit + 1)).fetchall()
    return {'runs': [dict(r) for r in rows[:limit]], 'has_more': len(rows) > limit}


def _status_rows(db, hypothesis_id: int) -> list[dict]:
    rows = db.execute('SELECT * FROM timeline_hypothesis_status WHERE hypothesis_id=? ORDER BY id', (hypothesis_id,)).fetchall()
    return [dict(r) | {'details': json.loads(r['details_json'])} for r in rows]


def hypothesis_detail(db, hypothesis_id: int) -> dict | None:
    row = db.execute('SELECT * FROM timeline_hypotheses WHERE id=?', (hypothesis_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out['criteria'] = json.loads(out.pop('criteria_json'))
    history = _status_rows(db, hypothesis_id)
    for entry in history:
        entry.pop('details_json', None)
    out['history'] = history
    out['status'] = history[-1]['status'] if history else None
    out['siblings'] = [dict(r) for r in db.execute('''SELECT id,right_kind,right_id,left_kind,left_id FROM timeline_hypotheses
        WHERE run_id=? AND ambiguity_group=? AND id<>? ORDER BY id LIMIT 50''', (row['run_id'], row['ambiguity_group'], hypothesis_id))]
    out['probability'] = None
    out['semantics'] = 'hypothesis with per-criterion evidence; no probability is computed'
    return out


def list_hypotheses(db, *, run_id: int | None = None, rule_id: str | None = None, status: str | None = None,
                    item_kind: str | None = None, item_id: int | None = None, after_id: int | None = None,
                    limit: int = 100) -> dict:
    if not 1 <= limit <= 500:
        raise CorrelationError('limit must be 1..500')
    if status is not None and status not in STATUSES:
        raise CorrelationError(f'status must be one of {", ".join(STATUSES)}')
    clauses, args = ['1=1'], []
    if run_id is not None:
        clauses.append('h.run_id=?')
        args.append(run_id)
    if rule_id:
        clauses.append('h.rule_id=?')
        args.append(rule_id)
    if item_kind and item_id is not None:
        clauses.append('((h.left_kind=? AND h.left_id=?) OR (h.right_kind=? AND h.right_id=?))')
        args += [item_kind, item_id, item_kind, item_id]
    if after_id is not None:
        clauses.append('h.id>?')
        args.append(after_id)
    current = '(SELECT s.status FROM timeline_hypothesis_status s WHERE s.hypothesis_id=h.id ORDER BY s.id DESC LIMIT 1)'
    if status:
        clauses.append(f'{current}=?')
        args.append(status)
    rows = db.execute(f'''SELECT h.*, {current} AS status FROM timeline_hypotheses h WHERE {' AND '.join(clauses)}
                          ORDER BY h.id LIMIT ?''', (*args, limit + 1)).fetchall()
    items = []
    for r in rows[:limit]:
        entry = dict(r)
        entry['criteria'] = json.loads(entry.pop('criteria_json'))
        items.append(entry)
    return {'hypotheses': items, 'has_more': len(rows) > limit,
            'next_after_id': items[-1]['id'] if len(rows) > limit else None}


def review(db, hypothesis_id: int, action: str, *, reason: str, actor: str, create_link: bool = True) -> dict:
    """Append a decision; ``accept`` may create a links row, ``revoke`` removes the link it created."""
    if action not in TRANSITIONS:
        raise CorrelationError('action must be accept, reject or revoke')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
        raise CorrelationError(f'reason must be non-empty text of at most {MAX_REASON} characters')
    actor = _actor(actor)
    allowed, new_status = TRANSITIONS[action]
    db.execute('BEGIN IMMEDIATE')
    try:
        hypothesis = db.execute('SELECT * FROM timeline_hypotheses WHERE id=?', (hypothesis_id,)).fetchone()
        if hypothesis is None:
            raise LookupError('hypothesis not found')
        last = db.execute('SELECT * FROM timeline_hypothesis_status WHERE hypothesis_id=? ORDER BY id DESC LIMIT 1',
                          (hypothesis_id,)).fetchone()
        if last['status'] not in allowed:
            raise TransitionError(f'cannot {action} a hypothesis whose status is {last["status"]}')
        details = {'previous_status': last['status'], 'actor_verified': False}
        link_id = None
        if action == 'accept' and create_link:
            rationale = canonical_json({'hypothesis_id': hypothesis_id, 'rule': f"{hypothesis['rule_id']}@{hypothesis['rule_version']}",
                                        'run_id': hypothesis['run_id'], 'reason': reason.strip(), 'actor': actor,
                                        'semantics': 'accepted correlation hypothesis; revocable; not a verified identity'})
            link_id = int(db.execute('''INSERT INTO links(left_kind,left_id,right_kind,right_id,relation,confidence,rationale)
                                        VALUES(?,?,?,?,?,NULL,?)''',
                                     (hypothesis['left_kind'], hypothesis['left_id'], hypothesis['right_kind'],
                                      hypothesis['right_id'], hypothesis['relation'], rationale)).lastrowid)
        if action == 'revoke':
            accepted = db.execute('''SELECT link_id FROM timeline_hypothesis_status WHERE hypothesis_id=? AND status='accepted'
                                     AND link_id IS NOT NULL ORDER BY id DESC LIMIT 1''', (hypothesis_id,)).fetchone()
            if last['status'] == 'accepted' and accepted is not None:
                link = db.execute('SELECT * FROM links WHERE id=?', (accepted['link_id'],)).fetchone()
                if link is not None:
                    details['removed_link'] = dict(link)
                    db.execute('DELETE FROM links WHERE id=?', (accepted['link_id'],))
        cur = db.execute('''INSERT INTO timeline_hypothesis_status(hypothesis_id,status,actor,reason,link_id,details_json)
                            VALUES(?,?,?,?,?,?)''', (hypothesis_id, new_status, actor, reason.strip(), link_id,
                                                     canonical_json(details)))
        db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)",
                   (actor, f'timeline_hypothesis_{action}', 'timeline_hypothesis', hypothesis_id,
                    canonical_json({'status_id': cur.lastrowid, 'link_id': link_id, **details})))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return hypothesis_detail(db, hypothesis_id)

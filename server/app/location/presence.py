"""Place presence from stored samples and a private place set (rule ``place_presence.v1``).

An interpretation, not an observation: *the device's stored positions were
inside (or possibly inside) a geofence during this span*. Never a person's
presence. Runs are append-only (``location_presence_runs``) and record the rule,
parameters, the place-set hash and version, a fingerprint of every sample used
and a hash of the result; an identical recomputation returns the stored run.

Per stream (device/account), samples with a fixed UTC instant are taken in time
order and classified against every place with the accuracy-aware rule of
``places.classify`` (``inside`` / ``uncertain`` / ``outside``). A presence
interval is a maximal run of consecutive ``inside``/``uncertain`` samples of one
place whose neighbours are at most ``max_gap_s`` apart; an ``outside`` sample or
a longer gap ends it. Its status is ``inside`` when it holds at least
``min_inside_fixes`` inside samples, else ``uncertain``. Bounds:

* ``[start, end]`` — first and last matching sample (observed span);
* ``[outer_start, outer_end]`` — the neighbouring samples of the stream before and
  after the run (any status, ``null`` at the edge of the data or window): presence
  outside these is contradicted by no sample of this stream, inside the inner span
  it is supported by every sample.

Intervals shorter than ``min_dwell_s`` are dropped and counted. Publishing
(``publish=True``) additionally writes one ``place_presence`` event per interval
(``signal.class=passive``, ``basis=derived``) so the timeline, time budget place
rules and correlations can use them; events are keyed by rule, parameters,
place-set hash and the interval, so the same interval is never duplicated.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import evidence
from ..db import session
from ..timeinfo import from_iso, record_event_times
from . import geo, places as places_mod, store

RULE = 'place_presence.v1'
EVENT_TYPE = 'place_presence'
SOURCE_KIND = 'location_presence'
SOURCE_LABEL = 'Obecność w miejscach (pochodna)'
SOURCE_LOCATOR = 'ageds:location-presence'
BASES = ('raw_sample', 'provider_inference', 'provider_user_edit', 'declared')
SEMANTICS = ('derived presence of a DEVICE within a geofence of a private place set (rule place_presence.v1); an '
             'interpretation of stored samples, not proof of a person\'s presence')


@dataclass(frozen=True)
class PresenceParams:
    min_dwell_s: int = 300
    max_gap_s: int = 3600
    min_inside_fixes: int = 1
    accuracy_multiplier: float = 1.0
    missing_accuracy_m: float | None = None
    max_accuracy_m: float | None = None
    signal_bases: tuple[str, ...] = ('raw_sample',)
    fix_kinds: tuple[str, ...] | None = None
    streams: tuple[str, ...] | None = None
    merge_streams: bool = False
    max_fixes: int = 20_000_000
    max_intervals: int = 200_000

    def __post_init__(self):
        def integer(name, low, high):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f'{name} must be an integer from {low} to {high}')
        integer('min_dwell_s', 0, 365 * 86400)
        integer('max_gap_s', 1, 365 * 86400)
        integer('min_inside_fixes', 1, 1_000_000)
        integer('max_fixes', 1, 1_000_000_000)
        integer('max_intervals', 1, 10_000_000)
        for name in ('accuracy_multiplier',):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100 or \
                    not math.isfinite(value):
                raise ValueError(f'{name} must be a number from 0 to 100')
        for name in ('missing_accuracy_m', 'max_accuracy_m'):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                      not math.isfinite(value) or not 0 <= value <= 1e7):
                raise ValueError(f'{name} must be null or a number from 0 to 1e7 metres')
        if not self.signal_bases or any(b not in BASES for b in self.signal_bases):
            raise ValueError(f'signal_bases must be a non-empty subset of {BASES}')
        for name in ('fix_kinds', 'streams'):
            value = getattr(self, name)
            if value is not None and (not value or len(value) > 1000 or
                                      not all(isinstance(v, str) and 0 < len(v) <= 256 for v in value)):
                raise ValueError(f'{name} must be null or a non-empty list of strings')
        if not isinstance(self.merge_streams, bool):
            raise ValueError('merge_streams must be true or false')

    @classmethod
    def from_dict(cls, raw: dict | None) -> 'PresenceParams':
        raw = dict(raw or {})
        unknown = sorted(set(raw) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError('unknown presence parameters: ' + ', '.join(unknown))
        for name in ('signal_bases', 'fix_kinds', 'streams'):
            if isinstance(raw.get(name), list):
                raw[name] = tuple(sorted(set(raw[name]), key=str))
        return cls(**raw)

    def as_dict(self) -> dict:
        out = asdict(self)
        for name in ('signal_bases', 'fix_kinds', 'streams'):
            if out[name] is not None:
                out[name] = sorted(out[name])
        return out


def code_version() -> str:
    try:
        root = Path(__file__).resolve().parents[3]
        return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True,
                              timeout=5).stdout.strip() or 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


class _PlaceIndex:
    """Brute force for small sets; a latitude-sorted index otherwise (exact: no place is missed)."""

    def __init__(self, place_set: places_mod.PlaceSet):
        self.places = list(place_set.places)
        self.max_radius = max((p.radius_m for p in self.places), default=0.0)
        self.by_lat = sorted(self.places, key=lambda p: p.lat)
        self.lats = [p.lat for p in self.by_lat]

    def candidates(self, lat: float, reach_m: float):
        if len(self.places) <= 64:
            return self.places
        span = (reach_m + self.max_radius) / geo.METERS_PER_DEGREE + 1e-9
        low = bisect.bisect_left(self.lats, lat - span)
        high = bisect.bisect_right(self.lats, lat + span)
        return self.by_lat[low:high]


def ensure_place_set(db, place_set: places_mod.PlaceSet) -> int:
    row = db.execute('SELECT id FROM location_place_sets WHERE sha256=?', (place_set.sha256,)).fetchone()
    if row:
        return int(row[0])
    places_json = store.canonical([p.as_dict() for p in place_set.places])
    cur = db.execute('''INSERT INTO location_place_sets(sha256,schema_id,version,origin,file_sha256,places_json)
                        VALUES(?,?,?,?,?,?)''', (place_set.sha256, places_mod.SCHEMA, place_set.version,
                                                 place_set.origin, place_set.file_sha256, places_json))
    return int(cur.lastrowid)


def _window_us(value: str | None) -> int | None:
    if value is None:
        return None
    candidate = from_iso('other', value, 'presence-window')
    if candidate.utc is None:
        raise ValueError('window bounds need an explicit offset (e.g. 2024-01-15T00:00:00Z)')
    return store.utc_us(candidate)


def compute(db, place_set: places_mod.PlaceSet, params: PresenceParams, *, start_us: int | None = None,
            end_us: int | None = None) -> dict:
    """Pure read: intervals, counts and the input fingerprint (nothing is written)."""
    match = places_mod.MatchParams(accuracy_multiplier=float(params.accuracy_multiplier),
                                   missing_accuracy_m=params.missing_accuracy_m)
    index = _PlaceIndex(place_set)
    where = ['t_us IS NOT NULL', f'signal_basis IN ({",".join("?" * len(params.signal_bases))})']
    args: list = list(params.signal_bases)
    if start_us is not None:
        where.append('t_us >= ?')
        args.append(start_us)
    if end_us is not None:
        where.append('t_us < ?')
        args.append(end_us)
    if params.streams:
        where.append(f'stream_key IN ({",".join("?" * len(params.streams))})')
        args.extend(params.streams)
    if params.fix_kinds:
        where.append(f'fix_kind IN ({",".join("?" * len(params.fix_kinds))})')
        args.extend(params.fix_kinds)
    order = 't_us, id' if params.merge_streams else 'stream_key, t_us, id'
    sql = (f'SELECT id, stream_key, t_us, lat, lon, decimals, source_decimals, accuracy_m, fix_kind, signal_basis '
           f'FROM location_fixes WHERE {" AND ".join(where)} ORDER BY {order} LIMIT ?')
    digest = hashlib.sha256()
    counts = Counter()
    intervals: list[dict] = []
    dropped = Counter()
    open_runs: dict[str, dict] = {}
    stream = None
    previous_t = None
    truncated = False
    statuses = Counter()

    def close(place_id: str, ended_by: str, next_t: int | None):
        run = open_runs.pop(place_id)
        run['outer_end_us'] = next_t
        run['evidence']['ended_by'] = ended_by
        if run['end_us'] - run['start_us'] < params.min_dwell_s * 1_000_000:
            dropped['shorter_than_min_dwell'] += 1
            return
        run['status'] = 'inside' if run['inside_fixes'] >= params.min_inside_fixes else 'uncertain'
        if len(intervals) >= params.max_intervals:
            dropped['max_intervals'] += 1
            return
        intervals.append(run)

    rows = db.execute(sql, (*args, params.max_fixes + 1))
    for n, row in enumerate(rows):
        if n >= params.max_fixes:
            truncated = True
            break
        fid, key, t_us, lat, lon, decimals, source_decimals, accuracy, kind, basis = tuple(row)
        key = 'merged' if params.merge_streams else key
        digest.update(f'{fid}:{t_us}:{lat!r}:{lon!r}:{accuracy!r};'.encode())
        counts['fixes'] += 1
        if key != stream:
            for place_id in list(open_runs):
                close(place_id, 'stream_end', None)
            stream, previous_t = key, None
        if params.max_accuracy_m is not None and accuracy is not None and accuracy > params.max_accuracy_m:
            counts['fixes_skipped_accuracy_over_max'] += 1
            continue
        if previous_t is not None and t_us - previous_t > params.max_gap_s * 1_000_000:
            for place_id in list(open_runs):
                close(place_id, 'gap', t_us)
        rounding = geo.rounding_error_m(lat, decimals, source_decimals)
        reach = (accuracy or params.missing_accuracy_m or 0.0) * params.accuracy_multiplier + rounding
        hits = {}
        for place in index.candidates(lat, reach):
            result = places_mod.classify(place, lat, lon, accuracy_m=accuracy, rounding_m=rounding, params=match)
            statuses[result['status']] += 1
            if result['status'] != 'outside':
                hits[place.id] = result
        for place_id in list(open_runs):
            if place_id not in hits:
                close(place_id, 'outside_fix', t_us)
        for place_id, result in hits.items():
            run = open_runs.get(place_id)
            if run is None:
                run = open_runs[place_id] = {
                    'place_id': place_id, 'stream_key': key, 'start_us': t_us, 'end_us': t_us,
                    'outer_start_us': previous_t, 'outer_end_us': None, 'fixes': 0, 'inside_fixes': 0,
                    'uncertain_fixes': 0, 'min_distance_m': None, 'max_uncertainty_m': 0.0,
                    'evidence': {'first_fix_id': fid, 'last_fix_id': fid, 'max_internal_gap_s': 0.0,
                                 'accuracy_basis': {}, 'max_rounding_m': 0.0,
                                 'started_after': 'stream_start' if previous_t is None else 'outside_or_gap'}}
            else:
                run['evidence']['max_internal_gap_s'] = max(run['evidence']['max_internal_gap_s'],
                                                            (t_us - run['end_us']) / 1e6)
            run['end_us'] = t_us
            run['evidence']['last_fix_id'] = fid
            run['fixes'] += 1
            run['inside_fixes' if result['status'] == 'inside' else 'uncertain_fixes'] += 1
            distance = result['distance_m']
            run['min_distance_m'] = distance if run['min_distance_m'] is None else min(run['min_distance_m'], distance)
            run['max_uncertainty_m'] = max(run['max_uncertainty_m'], result['uncertainty_m'])
            basis_counts = run['evidence']['accuracy_basis']
            basis_counts[result['accuracy_basis']] = basis_counts.get(result['accuracy_basis'], 0) + 1
            run['evidence']['max_rounding_m'] = max(run['evidence']['max_rounding_m'], rounding)
        previous_t = t_us
    for place_id in list(open_runs):
        close(place_id, 'stream_end' if not truncated else 'max_fixes', None)
    for run in intervals:
        run['min_distance_m'] = round(run['min_distance_m'], 3) if run['min_distance_m'] is not None else None
        run['max_uncertainty_m'] = round(run['max_uncertainty_m'], 3)
        run['evidence']['max_rounding_m'] = round(run['evidence']['max_rounding_m'], 3)
        run['evidence']['max_internal_gap_s'] = round(run['evidence']['max_internal_gap_s'], 6)
        run['evidence']['window_edge'] = {'start': start_us is not None and run['outer_start_us'] is None,
                                          'end': end_us is not None and run['outer_end_us'] is None}
    intervals.sort(key=lambda r: (r['start_us'], r['place_id'], r['stream_key']))
    return {'intervals': intervals, 'input_sha256': digest.hexdigest(),
            'counts': {'fixes': counts['fixes'], 'fixes_skipped_accuracy_over_max': counts['fixes_skipped_accuracy_over_max'],
                       'classifications': dict(sorted(statuses.items())), 'intervals': len(intervals),
                       'intervals_by_status': dict(sorted(Counter(r['status'] for r in intervals).items())),
                       'dropped': dict(sorted(dropped.items()))},
            'coverage': {'complete': not truncated and not dropped.get('max_intervals'), 'max_fixes_reached': truncated}}


def _interval_event(interval: dict, place: places_mod.Place, place_set: places_mod.PlaceSet, params_sha: str,
                    source_id: int) -> tuple[dict, list]:
    start = from_iso('start', store.us_to_iso(interval['start_us']), f'location_presence:{RULE}',
                     metadata={'derived_from': 'first_matching_sample'})
    end = from_iso('end', store.us_to_iso(interval['end_us']), f'location_presence:{RULE}',
                   metadata={'derived_from': 'last_matching_sample'})
    metadata = {
        'parser': {'name': 'location_presence', 'version': RULE},
        'signal': {'class': 'passive', 'basis': 'derived', 'provider': f'ageds:{RULE}',
                   'device_or_account': interval['stream_key']},
        'place': {'id': place.id, 'label': place.label, 'kind': place.kind, 'radius_m': place.radius_m},
        'place_set': {'sha256': place_set.sha256, 'version': place_set.version, 'schema': places_mod.SCHEMA},
        'derivation': {'rule': RULE, 'params_sha256': params_sha, 'match_rule': places_mod.MATCH_RULE,
                       'distance_model': geo.DISTANCE_MODEL},
        'presence': {'status': interval['status'], 'outer_start_utc': store.us_to_iso(interval['outer_start_us']),
                     'outer_end_utc': store.us_to_iso(interval['outer_end_us']), 'fixes': interval['fixes'],
                     'inside_fixes': interval['inside_fixes'], 'uncertain_fixes': interval['uncertain_fixes'],
                     'min_distance_m': interval['min_distance_m'], 'max_uncertainty_m': interval['max_uncertainty_m'],
                     'evidence': interval['evidence']},
        'semantics': SEMANTICS,
    }
    external = (f'{RULE}:{place_set.sha256[:16]}:{params_sha[:16]}:{interval["stream_key"]}:{place.id}:'
                f'{interval["start_us"]}:{interval["end_us"]}')
    event = {'source_id': source_id, 'artifact_id': None, 'event_type': EVENT_TYPE, 'ts_start': start.utc,
             'ts_end': end.utc, 'subject': place.label, 'external_id': external, 'confidence': None,
             'metadata': metadata}
    times = [start, end] if interval['end_us'] != interval['start_us'] else [start]
    return event, times


def run_presence(place_set: places_mod.PlaceSet, *, params: PresenceParams | dict | None = None,
                 start_utc: str | None = None, end_utc: str | None = None, publish: bool = False,
                 actor: str = 'unknown') -> dict:
    """Compute and store a presence run (append-only); identical recomputations are reused."""
    if isinstance(params, dict) or params is None:
        params = PresenceParams.from_dict(params)
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise ValueError('actor must be a non-blank string up to 200 characters')
    start_us, end_us = _window_us(start_utc), _window_us(end_utc)
    if start_us is not None and end_us is not None and end_us <= start_us:
        raise ValueError('window needs from < to')
    params_dict = params.as_dict()
    params_sha = store.sha256_text(store.canonical(params_dict))
    with session() as db:
        result = compute(db, place_set, params, start_us=start_us, end_us=end_us)
    window = {'from_utc': store.us_to_iso(start_us), 'to_utc': store.us_to_iso(end_us)}
    result_sha = store.sha256_text(store.canonical(result['intervals']))
    source_id = evidence.ensure_source(SOURCE_KIND, SOURCE_LABEL, SOURCE_LOCATOR) if publish else None
    by_id = place_set.by_id()
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        set_id = ensure_place_set(db, place_set)
        existing = db.execute('''SELECT id FROM location_presence_runs WHERE place_set_id=? AND params_sha256=?
                                 AND input_sha256=? AND result_sha256=? AND published=? AND window_json=? AND rule=?
                                 ORDER BY id LIMIT 1''',
                              (set_id, params_sha, result['input_sha256'], result_sha, int(publish),
                               store.canonical(window), RULE)).fetchone()
        if existing:
            db.rollback()
            return {**get_run(int(existing[0]), with_intervals=False), 'reused': True}
        cur = db.execute('''INSERT INTO location_presence_runs(rule,place_set_id,params_json,params_sha256,input_sha256,
                            window_json,counts_json,coverage_json,result_sha256,published,code_version,actor)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (RULE, set_id, store.canonical(params_dict), params_sha, result['input_sha256'],
                          store.canonical(window), store.canonical(result['counts']),
                          store.canonical(result['coverage']), result_sha, int(publish), code_version(), actor.strip()))
        run_id = int(cur.lastrowid)
        rows = []
        for interval in result['intervals']:
            event_id = None
            if publish:
                event, times = _interval_event(interval, by_id[interval['place_id']], place_set, params_sha, source_id)
                event_id = evidence.add_event(db=db, **event)
                record_event_times(db, event_id, times)
            rows.append((run_id, interval['place_id'], interval['stream_key'], interval['status'], interval['start_us'],
                         interval['end_us'], interval['outer_start_us'], interval['outer_end_us'], interval['fixes'],
                         interval['inside_fixes'], interval['uncertain_fixes'], interval['min_distance_m'],
                         interval['max_uncertainty_m'], store.canonical(interval['evidence']), event_id))
        db.executemany('''INSERT INTO location_presence_intervals(run_id,place_id,stream_key,status,start_us,end_us,
                          outer_start_us,outer_end_us,fixes,inside_fixes,uncertain_fixes,min_distance_m,
                          max_uncertainty_m,evidence_json,event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('location_presence_run',"
                   "'location_presence_run',?,?)",
                   (run_id, json.dumps({'rule': RULE, 'place_set_sha256': place_set.sha256, 'params_sha256': params_sha,
                                        'published': publish, 'intervals': len(rows), 'actor': actor.strip()},
                                       sort_keys=True)))
    return {**get_run(run_id, with_intervals=False), 'reused': False}


def _interval_row(r) -> dict:
    return {'id': r['id'], 'place_id': r['place_id'], 'stream_key': r['stream_key'], 'status': r['status'],
            'start_utc': store.us_to_iso(r['start_us']), 'end_utc': store.us_to_iso(r['end_us']),
            'outer_start_utc': store.us_to_iso(r['outer_start_us']), 'outer_end_utc': store.us_to_iso(r['outer_end_us']),
            'start_us': r['start_us'], 'end_us': r['end_us'], 'outer_start_us': r['outer_start_us'],
            'outer_end_us': r['outer_end_us'], 'duration_s': (r['end_us'] - r['start_us']) / 1e6,
            'fixes': r['fixes'], 'inside_fixes': r['inside_fixes'], 'uncertain_fixes': r['uncertain_fixes'],
            'min_distance_m': r['min_distance_m'], 'max_uncertainty_m': r['max_uncertainty_m'],
            'evidence': json.loads(r['evidence_json']), 'event_id': r['event_id']}


def get_run(run_id: int, *, with_intervals: bool = True, start_us: int | None = None, end_us: int | None = None,
            limit: int = 10_000) -> dict:
    with session() as db:
        r = db.execute('''SELECT r.*, s.sha256 AS set_sha256, s.version AS set_version, s.origin AS set_origin,
                          s.file_sha256 AS set_file_sha256, s.places_json FROM location_presence_runs r
                          JOIN location_place_sets s ON s.id=r.place_set_id WHERE r.id=?''', (run_id,)).fetchone()
        if r is None:
            raise LookupError(f'presence run {run_id} not found')
        out = {'run_id': r['id'], 'rule': r['rule'], 'params': json.loads(r['params_json']),
               'params_sha256': r['params_sha256'], 'input_sha256': r['input_sha256'],
               'window': json.loads(r['window_json']), 'counts': json.loads(r['counts_json']),
               'coverage': json.loads(r['coverage_json']), 'result_sha256': r['result_sha256'],
               'published': bool(r['published']), 'code_version': r['code_version'], 'actor': r['actor'],
               'created_at': r['created_at'],
               'place_set': {'id': r['place_set_id'], 'sha256': r['set_sha256'], 'version': r['set_version'],
                             'origin': r['set_origin'], 'file_sha256': r['set_file_sha256'],
                             'schema': places_mod.SCHEMA},
               'semantics': SEMANTICS}
        if with_intervals:
            where, args = ['run_id=?'], [run_id]
            if start_us is not None:
                where.append('end_us >= ?')
                args.append(start_us)
            if end_us is not None:
                where.append('start_us < ?')
                args.append(end_us)
            rows = db.execute(f'SELECT * FROM location_presence_intervals WHERE {" AND ".join(where)} '
                              f'ORDER BY start_us, id LIMIT ?', (*args, limit + 1)).fetchall()
            out['intervals'] = [_interval_row(x) for x in rows[:limit]]
            out['intervals_truncated'] = len(rows) > limit
            out['places'] = json.loads(r['places_json'])
    return out


def list_runs(limit: int = 50) -> list[dict]:
    with session() as db:
        rows = db.execute('''SELECT r.id, r.rule, r.published, r.created_at, r.actor, r.counts_json, r.window_json,
                             s.sha256, s.version FROM location_presence_runs r JOIN location_place_sets s
                             ON s.id=r.place_set_id ORDER BY r.id DESC LIMIT ?''', (limit,)).fetchall()
    return [{'run_id': r['id'], 'rule': r['rule'], 'published': bool(r['published']), 'created_at': r['created_at'],
             'actor': r['actor'], 'counts': json.loads(r['counts_json']), 'window': json.loads(r['window_json']),
             'place_set': {'sha256': r['sha256'], 'version': r['version']}} for r in rows]


def fixes_loader(db, start_us, end_us, limit):
    """Geotag-source loader for this area's own samples (see ``places.register_geotag_source``)."""
    where, args = ['t_us IS NOT NULL'], []
    if start_us is not None:
        where.append('t_us >= ?')
        args.append(start_us)
    if end_us is not None:
        where.append('t_us < ?')
        args.append(end_us)
    for r in db.execute(f'SELECT id, t_us, lat, lon, decimals, source_decimals, accuracy_m FROM location_fixes '
                        f'WHERE {" AND ".join(where)} ORDER BY t_us, id LIMIT ?', (*args, limit)):
        yield {'id': r[0], 't_us': r[1], 'lat': r[2], 'lon': r[3], 'decimals': r[4], 'source_decimals': r[5],
               'accuracy_m': r[6], 'ref': f'location_fixes/{r[0]}'}


if 'location_fixes' not in places_mod.GEOTAG_SOURCES:
    places_mod.register_geotag_source('location_fixes', label='Próbki położenia (location_fixes)',
                                      signal_class='passive', basis='raw_sample', loader=fixes_loader)

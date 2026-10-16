"""Synthetic event series with planted temporal structure for the hypothesis analyzer.

Everything is generated from fixed seeds; no real person, number or record is
used (event bodies are generic Polish/English words). Each scenario is a list
of events with a role, a type and either a UTC epoch time, a wall-clock time
without zone (unknown zone) or no time at all, plus the expectations that the
tests assert. ``build(root)`` writes ``scenarios.json``; ``populate`` inserts a
scenario into the current test database through the normal evidence API.

Scenarios
---------
* ``planted_effect``: outcomes planted 0.5–2.5 days after each stimulus on top
  of uniform outcomes and background documentation → detectable after-effect.
* ``density_burst_null``: NO effect. Documentation bursts ten-fold during
  eight irregular "conflict" periods, outcomes are a fixed share of all documentation and
  stimuli fall inside the bursts → the raw count looks significant, the
  density-normalized excess does not.
* ``weekday_confound``: stimuli on Mondays 09:00–10:00 UTC, outcomes on every
  Monday independently of stimuli → a circular-shift null shows an effect, the
  weekday×hour stratified null does not.
* ``clustered_stimuli``: five clusters of six stimuli within 12 hours; windows
  of 2 days overlap (15 pairs per cluster); outcomes are uniform.
* ``unknown_zone``: stimuli/outcomes with wall-clock times without zone, one
  nonexistent local time (DST gap in Europe/Warsaw) and one event without any
  time → excluded and counted; an assumed zone places all but the gap and the
  timeless event.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

DAY = 86400
START = int(datetime(2025, 1, 6, tzinfo=timezone.utc).timestamp())  # a Monday, 00:00 UTC


def _ev(role: str, event_type: str, t: float | None = None, *, local: str | None = None, body: str | None = None) -> dict:
    return {'role': role, 'event_type': event_type, 't': None if t is None else int(t), 'local': local,
            'body': body or {'letter': 'pismo z urzędu', 'health': 'wpis zdrowotny wizyta', 'note': 'notatka robocza'}[event_type]}


def _spaced(r: random.Random, n: int, lo: int, hi: int, min_gap: int) -> list[int]:
    """Irregular times (no period a circular shift could re-align) with a minimum gap."""
    slack = hi - lo - (n - 1) * min_gap
    if slack <= 0:
        raise ValueError('interval too short for the requested spacing')
    extras = sorted(r.randrange(slack) for _ in range(n))
    return [lo + extra + index * min_gap for index, extra in enumerate(extras)]


def planted_effect(seed: int = 101) -> dict:
    r = random.Random(seed)
    days = 182
    events = [_ev('background', 'note', START + r.randrange(days * DAY)) for _ in range(days * 6)]
    events += [_ev('baseline_outcome', 'health', START + r.randrange(days * DAY)) for _ in range(40)]
    stimuli = _spaced(r, 24, START + 4 * DAY, START + (days - 4) * DAY, 4 * DAY)
    for s in stimuli:
        events.append(_ev('stimulus', 'letter', s))
        for _ in range(2):
            events.append(_ev('planted_outcome', 'health', s + DAY // 2 + r.randrange(2 * DAY)))
    return {'name': 'planted_effect', 'events': events,
            'windows': [{'label': 'before_3d', 'start_s': -3 * DAY, 'end_s': 0},
                        {'label': 'after_3d', 'start_s': 0, 'end_s': 3 * DAY}],
            'expect': {'stimuli': 24, 'outcomes': 88, 'effect_window': 'after_3d', 'null_window': 'before_3d'}}


BURSTS = ((15, 4), (27, 4), (63, 5), (71, 5), (80, 5), (100, 4), (116, 4), (130, 5))  # (first day, length): low circular autocorrelation


def density_burst_null(seed: int = 202) -> dict:
    r = random.Random(seed)
    days = 180
    events = []
    for day in range(days):
        burst = any(b <= day < b + length for b, length in BURSTS)
        for _ in range(30 if burst else 3):
            t = START + day * DAY + r.randrange(DAY)
            events.append(_ev('documentation', 'health' if r.random() < 0.12 else 'note', t))
    for b, length in BURSTS:
        for _ in range(2):
            events.append(_ev('stimulus', 'letter', START + (b + 1) * DAY + r.randrange((length - 2) * DAY)))
    return {'name': 'density_burst_null', 'events': events,
            'windows': [{'label': 'before_3d', 'start_s': -3 * DAY, 'end_s': 0},
                        {'label': 'after_3d', 'start_s': 0, 'end_s': 3 * DAY}],
            'expect': {'stimuli': 16, 'outcome_share': 0.12, 'bursts': [list(b) for b in BURSTS], 'burst_rate_factor': 10}}


def weekday_confound(seed: int = 303) -> dict:
    r = random.Random(seed)
    weeks = 26
    events = [_ev('background', 'note', START + r.randrange(weeks * 7 * DAY)) for _ in range(weeks * 7 * 6)]
    for week in range(weeks):
        monday = START + week * 7 * DAY
        events += [_ev('monday_outcome', 'health', monday + r.randrange(DAY)) for _ in range(4)]
    for week in sorted(r.sample(range(weeks), 14)):
        events.append(_ev('stimulus', 'letter', START + week * 7 * DAY + 9 * 3600 + r.randrange(3600)))
    return {'name': 'weekday_confound', 'events': events,
            'windows': [{'label': 'after_1d', 'start_s': 0, 'end_s': DAY}],
            'expect': {'stimuli': 14, 'stimulus_weekday': 0, 'stimulus_hour_utc': 9}}


def clustered_stimuli(seed: int = 404) -> dict:
    r = random.Random(seed)
    days = 120
    events = [_ev('background', 'note', START + r.randrange(days * DAY)) for _ in range(days * 5)]
    events += [_ev('uniform_outcome', 'health', START + r.randrange(days * DAY)) for _ in range(80)]
    stimuli = []
    for day in (15, 35, 55, 75, 95):
        base = START + day * DAY
        stimuli += sorted(base + r.randrange(12 * 3600) for _ in range(6))
    events += [_ev('stimulus', 'letter', s) for s in stimuli]
    width = 2 * DAY
    pairs = sum(1 for i, a in enumerate(stimuli) for b in stimuli[i + 1:] if abs(b - a) < width)
    return {'name': 'clustered_stimuli', 'events': events,
            'windows': [{'label': 'after_2d', 'start_s': 0, 'end_s': width}],
            'expect': {'stimuli': 30, 'overlapping_pairs': pairs, 'stimuli_with_overlap': 30}}


def unknown_zone(seed: int = 505) -> dict:
    r = random.Random(seed)
    events = [_ev('background', 'note', START + r.randrange(60 * DAY)) for _ in range(200)]
    events += [_ev('stimulus', 'letter', START + (5 + 10 * k) * DAY + 3600) for k in range(5)]
    events += [_ev('outcome', 'health', START + (6 + 10 * k) * DAY) for k in range(5)]
    events += [_ev('local_stimulus', 'letter', local=f'2025-02-{d:02d} 10:00') for d in (3, 13, 24)]
    events += [_ev('local_outcome', 'health', local=f'2025-02-{d:02d} 18:30') for d in (4, 14)]
    events.append(_ev('dst_gap_outcome', 'health', local='2025-03-30 02:30'))
    events.append(_ev('timeless_outcome', 'health'))
    return {'name': 'unknown_zone', 'events': events,
            'windows': [{'label': 'after_2d', 'start_s': 0, 'end_s': 2 * DAY}],
            'expect': {'untimed_without_zone': {'unknown_zone': 6, 'no_time_candidate': 1},
                       'untimed_with_warsaw': {'nonexistent_local_time': 1, 'no_time_candidate': 1},
                       'timed_stimuli': 5, 'timed_stimuli_with_warsaw': 8}}


SCENARIOS = {f.__name__: f for f in (planted_effect, density_burst_null, weekday_confound, clustered_stimuli, unknown_zone)}


def definition_for(scenario: dict, **analysis) -> dict:
    """Registered definition used by the tests: letters → health entries."""
    windows = scenario['windows']
    low = min(w['start_s'] for w in windows)
    high = max(w['end_s'] for w in windows)
    return {
        'title': f'Synthetic: {scenario["name"]}',
        'text': 'After a letter event, health entries become more frequent (synthetic test hypothesis).',
        'stimulus': {'event_types': ['letter']},
        'outcome': {'event_types': ['health']},
        'windows': windows,
        'lags': {'min_s': min(low, -high), 'max_s': high, 'bin_s': max(3600, (high - min(low, -high)) // 24)},
        'analysis': {'permutations': 299, 'seed': 12345, **analysis},
        'dossier': {'queries': ['wizyta']},
    }


def populate(scenario: dict, *, label: str | None = None) -> dict:
    """Insert scenario events into the patched test database; return ids by role."""
    from server.app import db, evidence, timeinfo
    source = evidence.ensure_source('synthetic', label or f'hypotheses corpus: {scenario["name"]}')
    ids: dict[str, list[int]] = {}
    with db.session() as connection:
        connection.execute('BEGIN IMMEDIATE')
        for index, event in enumerate(scenario['events']):
            event_id = evidence.add_event(source_id=source, artifact_id=None, event_type=event['event_type'],
                                          ts_start=event['local'], body=event['body'],
                                          external_id=f'{scenario["name"]}:{index}', db=connection)
            if event['t'] is not None:
                timeinfo.record_event_times(connection, event_id, [timeinfo.from_epoch('created', event['t'], 'synthetic')])
            ids.setdefault(event['role'], []).append(event_id)
    return ids


def build(root: Path) -> dict:
    scenarios = {name: factory() for name, factory in SCENARIOS.items()}
    path = Path(root) / 'scenarios.json'
    path.write_text(json.dumps(scenarios, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    return {'file': str(path), 'scenarios': {name: {'events': len(s['events']), 'expect': s['expect']}
                                             for name, s in scenarios.items()}}

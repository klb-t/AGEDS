"""Bounded read views for the map, lanes and summaries (no writes except the timeline cache refresh).

Coordinates are returned at their stored precision unless ``display_decimals``
asks for a coarser display (presence matching is never affected by display
rounding). Large windows are thinned by a deterministic stride over time order
and the response says so (``fixes.thinned``); nothing is silently dropped.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import presence, store

LANES_PATH = Path(__file__).parent / 'data' / 'lanes.v1.json'
LOCATION_EVENT_TYPES = ('location_visit', 'location_segment', 'location_track', 'location_route', 'location_waypoint',
                        'location_samples', 'location_profile', 'place_presence', 'presence')
MAX_POINTS = 20_000
MAX_EVENTS = 5_000


def lanes_config() -> dict:
    raw = LANES_PATH.read_bytes()
    data = json.loads(raw)
    data['sha256'] = store.sha256_text(raw.decode('utf-8'))
    return data


def _round(value, decimals):
    if value is None or decimals is None:
        return value
    return round(value, decimals)


def summary(db) -> dict:
    streams = [{'stream_key': r[0], 'fixes': r[1], 'first_utc': store.us_to_iso(r[2]), 'last_utc': store.us_to_iso(r[3]),
                'untimed_fixes': r[4]}
               for r in db.execute('''SELECT stream_key, COUNT(*), MIN(t_us), MAX(t_us), SUM(t_us IS NULL)
                                      FROM location_fixes GROUP BY stream_key ORDER BY stream_key LIMIT 500''')]
    imports = []
    for r in db.execute('''SELECT i.id, i.artifact_id, i.importer, i.parser_version, i.format, i.stream_key,
                                  i.params_json, i.created_at, a.original_name,
                                  (SELECT counts_json FROM location_import_runs x WHERE x.import_id=i.id ORDER BY x.id DESC LIMIT 1),
                                  (SELECT coverage_json FROM location_import_runs x WHERE x.import_id=i.id ORDER BY x.id DESC LIMIT 1)
                           FROM location_imports i JOIN artifacts a ON a.id=i.artifact_id ORDER BY i.id DESC LIMIT 200'''):
        counts = json.loads(r[9]) if r[9] else {}
        imports.append({'id': r[0], 'artifact_id': r[1], 'importer': r[2], 'parser_version': r[3], 'format': r[4],
                        'stream_key': r[5], 'params': json.loads(r[6]), 'created_at': r[7], 'original_name': r[8],
                        'counts': {k: counts.get(k) for k in ('fixes_observed', 'fixes_new', 'signals_observed', 'events',
                                                              'sampling_sessions')},
                        'coverage': json.loads(r[10]) if r[10] else None})
    events = {r[0]: r[1] for r in db.execute(
        f'SELECT event_type, COUNT(*) FROM events WHERE event_type IN ({",".join("?" * len(LOCATION_EVENT_TYPES))}) '
        f'GROUP BY event_type', LOCATION_EVENT_TYPES)}
    extent = db.execute('SELECT MIN(t_us), MAX(t_us) FROM location_fixes WHERE t_us IS NOT NULL').fetchone()
    sets = [{'id': r[0], 'sha256': r[1], 'version': r[2], 'origin': r[3], 'places': len(json.loads(r[4]))}
            for r in db.execute('SELECT id, sha256, version, origin, places_json FROM location_place_sets ORDER BY id DESC LIMIT 50')]
    return {'streams': streams, 'imports': imports, 'events_by_type': dict(sorted(events.items())),
            'extent': {'from_utc': store.us_to_iso(extent[0]), 'to_utc': store.us_to_iso(extent[1])},
            'place_sets': sets, 'presence_runs': presence.list_runs(20), 'lanes': lanes_config(),
            'semantics': 'device/account locations and provider inferences; not proof of a person\'s presence'}


def window(db, *, start_us: int | None, end_us: int | None, streams: list[str] | None = None,
           max_points: int = 5000, display_decimals: int | None = None, presence_run_id: int | None = None,
           bases: list[str] | None = None) -> dict:
    if not 1 <= max_points <= MAX_POINTS:
        raise ValueError(f'max_points must be 1..{MAX_POINTS}')
    if display_decimals is not None and not 0 <= display_decimals <= 9:
        raise ValueError('display_decimals must be 0..9')
    where, args = ['t_us IS NOT NULL'], []
    if start_us is not None:
        where.append('t_us >= ?')
        args.append(start_us)
    if end_us is not None:
        where.append('t_us < ?')
        args.append(end_us)
    if streams:
        where.append(f'stream_key IN ({",".join("?" * len(streams))})')
        args.extend(streams)
    if bases:
        where.append(f'signal_basis IN ({",".join("?" * len(bases))})')
        args.extend(bases)
    clause = ' AND '.join(where)
    total = db.execute(f'SELECT COUNT(*) FROM location_fixes WHERE {clause}', args).fetchone()[0]
    stride = max(1, -(-total // max_points))
    rows = db.execute(f'''SELECT id, stream_key, t_us, lat, lon, decimals, accuracy_m, fix_kind, signal_basis
                          FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY t_us, id) - 1 AS rn FROM location_fixes WHERE {clause})
                          WHERE rn % ? = 0 ORDER BY t_us, id LIMIT ?''', (*args, stride, max_points)).fetchall()
    fixes = [{'id': r[0], 'stream': r[1], 't_us': r[2], 't': store.us_to_iso(r[2]), 'lat': _round(r[3], display_decimals),
              'lon': _round(r[4], display_decimals),
              'decimals': r[5] if display_decimals is None else min(r[5], display_decimals),
              'accuracy_m': r[6], 'kind': r[7], 'basis': r[8]} for r in rows]
    untimed = db.execute('SELECT COUNT(*) FROM location_fixes WHERE t_us IS NULL').fetchone()[0]
    ev_where = [f'e.event_type IN ({",".join("?" * len(LOCATION_EVENT_TYPES))})']
    ev_args: list = list(LOCATION_EVENT_TYPES)
    if start_us is not None:
        ev_where.append('COALESCE(e.ts_end, e.ts_start) >= ?')
        ev_args.append(store.us_to_iso(start_us))
    if end_us is not None:
        ev_where.append('e.ts_start < ?')
        ev_args.append(store.us_to_iso(end_us))
    ev_rows = db.execute(f'''SELECT e.id, e.event_type, e.ts_start, e.ts_end, e.subject, e.metadata_json FROM events e
                             WHERE {" AND ".join(ev_where)} AND e.ts_start IS NOT NULL ORDER BY e.ts_start, e.id LIMIT ?''',
                         (*ev_args, MAX_EVENTS + 1)).fetchall()
    events = []
    for r in ev_rows[:MAX_EVENTS]:
        meta = json.loads(r[5] or '{}')
        location = meta.get('location') if isinstance(meta.get('location'), dict) else None
        end_location = meta.get('end_location') if isinstance(meta.get('end_location'), dict) else None
        signal = meta.get('signal') if isinstance(meta.get('signal'), dict) else {}
        events.append({'id': r[0], 'type': r[1], 'start': r[2], 'end': r[3], 'subject': r[4],
                       'signal': {'class': signal.get('class'), 'basis': signal.get('basis')},
                       'location': _point(location, display_decimals), 'end_location': _point(end_location, display_decimals),
                       'place': meta.get('place') if r[1] == 'place_presence' else None,
                       'provider_labels': meta.get('provider_labels') if r[1] in ('location_visit', 'location_segment') else None,
                       'track': {k: (meta.get('track') or {}).get(k) for k in ('points', 'distance_m', 'duration_s')}
                       if meta.get('track') else None,
                       'user_edit': meta.get('user_edit')})
    run_id = presence_run_id
    if run_id is None:
        row = db.execute('SELECT MAX(id) FROM location_presence_runs').fetchone()
        run_id = row[0] if row else None
    run = None
    if run_id is not None:
        run = presence.get_run(int(run_id), start_us=start_us, end_us=end_us)
        for place in run.get('places', []):
            place['lat'], place['lon'] = _round(place['lat'], display_decimals), _round(place['lon'], display_decimals)
    return {'window': {'from_utc': store.us_to_iso(start_us), 'to_utc': store.us_to_iso(end_us)},
            'fixes': {'total_in_window': total, 'returned': len(fixes), 'stride': stride, 'thinned': stride > 1,
                      'untimed_total': untimed, 'items': fixes},
            'events': {'items': events, 'truncated': len(ev_rows) > MAX_EVENTS,
                       'time_basis': 'events.ts_start/ts_end (UTC fixed by the source); unknown-zone events are not placed'},
            'presence': run, 'display_decimals': display_decimals,
            'semantics': 'device/account positions and provider inferences; not proof of a person\'s presence'}


def _point(location, decimals):
    if not location or location.get('lat') is None:
        return None
    return {'lat': _round(location['lat'], decimals), 'lon': _round(location['lon'], decimals),
            'decimals': location.get('decimals') if decimals is None else min(location.get('decimals') or 9, decimals)}


def lanes(db, *, start_utc: str | None, end_utc: str | None, limit: int = 2000) -> dict:
    """Lane items through the analysis seam (timeline projection): times with their uncertainty interval."""
    from ..analysis.source import load_items
    config = lanes_config()
    types = sorted({t for lane in config['lanes'] for t in lane['event_types']})
    loaded = load_items(db, start_utc=start_utc, end_utc=end_utc, event_types=types, limit=limit * 4)
    by_type = {}
    for lane in config['lanes']:
        for event_type in lane['event_types']:
            by_type[event_type] = lane['id']
    out = {lane['id']: {'id': lane['id'], 'label': lane['label'], 'event_types': lane['event_types'], 'items': [],
                        'note': lane.get('note')} for lane in config['lanes']}
    for item in loaded['items']:
        lane = out[by_type[item['event_type']]]
        if len(lane['items']) >= limit:
            lane['truncated'] = True
            continue
        lane['items'].append({'event_id': item['event_id'], 'event_type': item['event_type'], 't_start': item['t_start'],
                              't_end': item.get('t_end'), 't_lo': item.get('t_lo'), 't_hi': item.get('t_hi'),
                              'basis': item.get('basis'), 'signal': item.get('signal')})
    return {'config': {'schema': config['schema'], 'version': config['version'], 'sha256': config['sha256']},
            'source_rule': loaded.get('rule'), 'source_rule_sha256': loaded.get('rule_sha256'), 'seam': loaded.get('seam'),
            'lanes': list(out.values()), 'untimed': len(loaded.get('untimed') or []),
            'truncated': bool(loaded.get('truncated'))}

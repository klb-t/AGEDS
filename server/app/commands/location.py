"""CLI subcommands owned by the location area (task L1).

Module import stays free of settings/database imports (``cli`` import contract).
Every importer needs ``--enable-location`` (recorded per run). Places come from
``--places PATH`` or the operator's ``AGEDS_LOCATION_PLACES``; the file stays
outside the repository and only its hash and version are recorded. Outputs are
one JSON object on stdout; ``location-window`` writes a NEW file only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

COMMANDS = ('location-import', 'places-check', 'presence-run', 'presence-show', 'location-window', 'location-correlate',
            'location-correlation-review', 'location-status')


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def _json_object(value: str) -> dict:
    try:
        data = json.loads(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'not JSON: {exc}') from exc
    if not isinstance(data, dict):
        raise argparse.ArgumentTypeError('must be a JSON object')
    return data


def register(subparsers) -> None:
    p = subparsers.add_parser('location-import', help='import a location file (Google exports, GPX, KML/KMZ, GeoJSON, '
                                                       'FIT); requires --enable-location')
    p.add_argument('path', type=Path)
    p.add_argument('--enable-location', action='store_true', help='explicit per-run opt-in (recorded)')
    p.add_argument('--format', choices=('auto', 'google', 'gpx', 'kml', 'geojson', 'fit'), default='auto',
                   help='auto = detected from the bytes')
    p.add_argument('--store-decimals', type=int, choices=range(0, 10), metavar='0-9',
                   help='stored coordinate precision (default: as the source states it)')
    p.add_argument('--stream', help='device/account stream key for deduplication (default: collection:<id> or source)')
    p.add_argument('--provider-place-text', choices=('keep', 'omit'), default='keep',
                   help='Google: keep or omit provider place names/addresses (place ids are kept raw)')
    p.add_argument('--gpx-naive-time', choices=('unknown', 'utc_per_schema'), default='unknown')
    p.add_argument('--geojson-options', type=_json_object, help='JSON: time_properties, coord_times_properties, ...')
    p.add_argument('--sample-session-gap-s', type=_positive, default=1800)
    p.add_argument('--label')
    p.add_argument('--collection-id', type=int)
    p.add_argument('--source-locator')
    p.add_argument('--max-fixes', type=_positive)
    p.add_argument('--max-events', type=_positive)
    p.add_argument('--max-element-bytes', type=_positive)
    check = subparsers.add_parser('places-check', help='validate a private places file and print its hash (no write)')
    check.add_argument('--places', type=Path, help='places JSON (default: AGEDS_LOCATION_PLACES)')
    run = subparsers.add_parser('presence-run', help='derive place presence from stored samples (append-only run)')
    run.add_argument('--places', type=Path, help='places JSON (default: AGEDS_LOCATION_PLACES)')
    run.add_argument('--from', dest='from_', help='UTC ISO bound with offset (inclusive)')
    run.add_argument('--to', help='UTC ISO bound with offset (exclusive)')
    run.add_argument('--params', type=_json_object, default={}, help='presence parameters (JSON object)')
    run.add_argument('--publish', action='store_true', help='also write place_presence events for the timeline')
    run.add_argument('--actor', default='cli-operator')
    show = subparsers.add_parser('presence-show', help='print a stored presence run with its intervals')
    show.add_argument('run_id', type=_positive)
    window = subparsers.add_parser('location-window', help='export fixes/events/presence of a window to a NEW json file')
    window.add_argument('output', type=Path)
    window.add_argument('--from', dest='from_')
    window.add_argument('--to')
    window.add_argument('--stream', action='append')
    window.add_argument('--max-points', type=_positive, default=5000)
    window.add_argument('--display-decimals', type=int, choices=range(0, 10), metavar='0-9')
    window.add_argument('--presence-run', type=_positive)
    window.add_argument('--max-output-bytes', type=_positive, default=256 * 1024 * 1024)
    corr = subparsers.add_parser('location-correlate', help='presence <-> calendar/communication hypotheses (new run)')
    corr.add_argument('--rule', required=True, help='presence_calendar | presence_communication [@version]')
    corr.add_argument('--presence-run', type=_positive, required=True)
    corr.add_argument('--params', type=_json_object, default={})
    corr.add_argument('--actor', default='cli-operator')
    review = subparsers.add_parser('location-correlation-review', help='append accept/reject/revoke to a hypothesis')
    review.add_argument('hypothesis_id', type=_positive)
    review.add_argument('action', choices=('accept', 'reject', 'revoke'))
    review.add_argument('--reason', required=True)
    review.add_argument('--actor', default='cli-operator')
    subparsers.add_parser('location-status', help='summary of streams, imports, place sets, runs and adapters')


def _us(value: str | None):
    if value is None:
        return None
    from ..location import store
    from ..timeinfo import from_iso
    candidate = from_iso('other', value, 'cli-window')
    if candidate.utc is None:
        raise ValueError('window bounds need an explicit offset (e.g. 2024-01-15T00:00:00Z)')
    return store.utc_us(candidate)


def _places(path):
    from ..location import places
    found = places.resolve_places(path)
    if found is None:
        raise ValueError('no places file: pass --places PATH or set AGEDS_LOCATION_PLACES')
    return found


def _run(args) -> dict:
    command = args.command
    if command == 'location-import':
        if not args.path.exists():
            raise FileNotFoundError(f'input not found: {args.path}')
        from ..location import formats
        from ..location.ingest import DEFAULT_LIMITS
        limits = DEFAULT_LIMITS.replace(max_fixes=args.max_fixes, max_events=args.max_events,
                                        max_element_bytes=args.max_element_bytes)
        options = {'provider_place_text': args.provider_place_text, 'gpx_naive_time': args.gpx_naive_time,
                   'geojson': args.geojson_options}
        return formats.import_file(args.path, fmt=args.format, enable_location=args.enable_location,
                                   store_decimals=args.store_decimals, stream=args.stream,
                                   sample_session_gap_s=args.sample_session_gap_s, source_locator=args.source_locator,
                                   original_name=args.path.name, collection_id=args.collection_id, label=args.label,
                                   limits=limits, format_options=options)
    if command == 'places-check':
        from ..location import places
        return places.summary(_places(args.places))
    if command == 'presence-run':
        from ..location import presence
        return presence.run_presence(_places(args.places), params=args.params, start_utc=args.from_, end_utc=args.to,
                                     publish=args.publish, actor=args.actor)
    if command == 'presence-show':
        from ..location import presence
        return presence.get_run(args.run_id)
    if command == 'location-window':
        from ..cli import _write_new_json
        from ..db import session
        from ..location import query
        with session() as db:
            value = query.window(db, start_us=_us(args.from_), end_us=_us(args.to), streams=args.stream,
                                 max_points=args.max_points, display_decimals=args.display_decimals,
                                 presence_run_id=args.presence_run)
        _write_new_json(value, args.output, max_bytes=args.max_output_bytes)
        return {'output': str(args.output.resolve()), 'fixes': value['fixes']['returned'],
                'fixes_in_window': value['fixes']['total_in_window'], 'events': len(value['events']['items'])}
    if command == 'location-correlate':
        from ..location import correlate
        return correlate.run_correlation(args.rule, args.presence_run, params=args.params, actor=args.actor)
    if command == 'location-correlation-review':
        from ..location import correlate
        return correlate.review(args.hypothesis_id, args.action, reason=args.reason, actor=args.actor)
    from ..db import session
    from ..importers import fit
    from ..location import places, query
    with session() as db:
        summary = query.summary(db)
    return {**summary, 'adapters': {'fit': fit.status()}, 'geotag_sources': places.geotag_sources()}


def handle(args) -> int | None:
    if getattr(args, 'command', None) not in COMMANDS:
        return None
    from ..db import init_db
    init_db()
    try:
        result = _run(args)
    except LookupError as exc:
        raise ValueError(str(exc)) from exc
    print(json.dumps({'command': args.command, **result}, ensure_ascii=False, allow_nan=False, default=str),
          file=sys.stdout)
    return 0

"""CLI subcommands owned by the traces area (personal-data exports, photo/document metadata).

``import-trace KIND PATH`` runs one importer of ``traces.dispatch.KINDS`` (list
them with ``traces-kinds``). Inputs are read-only; everything is acquired into
the configured store. Location and browser-history data need their explicit
flags (``--enable-location``, ``--enable-browser-history``), recorded per run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

COMMANDS = ('import-trace', 'traces-kinds', 'traces-summary', 'traces-geotags')


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError('must be a positive integer')
    return number


def register(subparsers) -> None:
    p = subparsers.add_parser('import-trace', help='import a personal-data export or file metadata (see traces-kinds)')
    p.add_argument('kind')
    p.add_argument('path', type=Path)
    p.add_argument('--label', help='source label (private labels belong outside the repository)')
    p.add_argument('--collection-id', type=int, help='existing collection to record on the acquisitions')
    p.add_argument('--source-locator', help='stable locator to record instead of the local path')
    p.add_argument('--max-records', type=_positive, help='record limit; reaching it marks coverage partial')
    p.add_argument('--enable-location', action='store_true', help='store coordinates (photo geotags); off by default')
    p.add_argument('--coordinate-decimals', type=int, help='round stored coordinates (0-8); default source precision')
    p.add_argument('--enable-browser-history', action='store_true', help='read local browser profiles; off by default')
    p.add_argument('--owner-name', help='account owner display name (sets message direction); not stored as identity')
    p.add_argument('--owner-id', help='account owner id in the export (sets message direction)')
    p.add_argument('--export-label', help='logical export label for cross-export record identity')
    p.add_argument('--session-gap-minutes', type=_positive, help='browser session gap (default 30)')
    subparsers.add_parser('traces-kinds', help='list the trace import kinds and their options')
    p = subparsers.add_parser('traces-summary', help='counts of trace observations by kind and status')
    p = subparsers.add_parser('traces-geotags', help='stored photo geotags (only rows stored under enable_location)')
    p.add_argument('--since')
    p.add_argument('--until')
    p.add_argument('--limit', type=_positive, default=100)


_OPTION_FLAGS = ('label', 'collection_id', 'source_locator', 'enable_location', 'coordinate_decimals',
                 'enable_browser_history', 'owner_name', 'owner_id', 'export_label', 'session_gap_minutes')


def run(args) -> dict:
    from ..traces import dispatch, queries
    if args.command == 'traces-kinds':
        return {'kinds': dispatch.describe()}
    if args.command == 'traces-summary':
        return queries.summary()
    if args.command == 'traces-geotags':
        from ..traces.geotags import iter_geotags
        return {'geotags': list(iter_geotags(since=args.since, until=args.until, limit=args.limit)),
                'semantics': 'file/account claims of a position; not proof of presence'}
    from ..importers.comm_common import DEFAULT_LIMITS
    options = {name: getattr(args, name) for name in _OPTION_FLAGS}
    if args.max_records:
        options['limits'] = DEFAULT_LIMITS.replace(max_records=args.max_records)
    return {'kind': args.kind, **dispatch.run(args.kind, args.path, **options)}


def handle(args) -> int | None:
    if getattr(args, 'command', None) not in COMMANDS:
        return None
    if args.command == 'import-trace' and not args.path.exists():
        raise FileNotFoundError(f'input not found: {args.path}')
    from ..db import init_db
    init_db()
    result = run(args)
    print(json.dumps({'command': args.command, 'source_bytes_written': False, **result}, ensure_ascii=False,
                     allow_nan=False, default=str), file=sys.stdout)
    return 0

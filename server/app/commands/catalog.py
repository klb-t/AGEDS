"""CLI subcommands owned by the catalog area (AGEDS-TASK-20261016-A).

Each command prints one JSON object on stdout (errors on stderr, exit 2).
Scans read sources only; exports and sidecars are new files outside every
scanned source root.
"""
from __future__ import annotations

import argparse
from pathlib import Path

COMMANDS: tuple[str, ...] = (
    'catalog-collection-add', 'catalog-collection-list', 'catalog-collection-update', 'catalog-scan',
    'catalog-scans', 'catalog-entries', 'catalog-coverage', 'catalog-duplicates', 'catalog-collisions',
    'catalog-export', 'catalog-describe', 'catalog-promote', 'catalog-mark', 'catalog-ingest',
)
_TEXT_FIELDS = ('device_or_account', 'export_date_raw', 'acquisition_method', 'operator', 'notes')


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def _collection_fields(parser, *, update: bool):
    for name in _TEXT_FIELDS:
        parser.add_argument('--' + name.replace('_', '-'), default=None)
    parser.add_argument('--priority', type=int, default=None)
    parser.add_argument('--parent', type=_positive, default=None, dest='parent_collection_id')
    if update:
        parser.add_argument('--label', default=None)
        parser.add_argument('--clear', action='append', default=[], choices=(*_TEXT_FIELDS, 'priority', 'parent_collection_id'),
                            help='set a field to null (repeatable)')
    parser.add_argument('--actor', default='cli')


def register(subparsers) -> None:
    from ..catalog.scan import LIMIT_NAMES
    add = subparsers.add_parser('catalog-collection-add', help='create a collection (device/account/export batch)')
    add.add_argument('label')
    add.add_argument('--case-id', type=_positive, default=None)
    _collection_fields(add, update=False)
    listing = subparsers.add_parser('catalog-collection-list', help='list collections as a tree')
    listing.add_argument('--case-id', type=_positive, default=None)
    update = subparsers.add_parser('catalog-collection-update', help='edit collection fields; previous values go to the audit log')
    update.add_argument('collection_id', type=_positive)
    _collection_fields(update, update=True)

    scan = subparsers.add_parser('catalog-scan', help='read-only catalog scan of a directory or one archive file')
    scan.add_argument('root', type=Path)
    scan.add_argument('--collection', type=_positive, required=True)
    scan.add_argument('--temp-dir', type=Path, default=None, help='spool directory for nested archives (outside the source)')
    scan.add_argument('--expand-document-containers', action='store_true')
    for name in LIMIT_NAMES:
        scan.add_argument('--' + name.replace('_', '-'), type=_positive, default=None)

    scans = subparsers.add_parser('catalog-scans', help='list catalog scans')
    scans.add_argument('--collection', type=_positive, default=None)
    scans.add_argument('--limit', type=_positive, default=50)

    entries = subparsers.add_parser('catalog-entries', help='one bounded page of catalog entries')
    entries.add_argument('--collection', type=_positive, default=None)
    entries.add_argument('--scan-id', type=_positive, default=None)
    entries.add_argument('--all-scans', action='store_true')
    entries.add_argument('--type', default=None)
    entries.add_argument('--status', default=None)
    entries.add_argument('--conflict', action='store_true')
    entries.add_argument('--duplicates-only', action='store_true')
    entries.add_argument('--group-id', type=_positive, default=None)
    entries.add_argument('--q', default=None)
    entries.add_argument('--after-id', type=_positive, default=None)
    entries.add_argument('--limit', type=_positive, default=100)

    cov = subparsers.add_parser('catalog-coverage', help='coverage per collection x type with gaps')
    cov.add_argument('--collection', type=_positive, default=None)
    cov.add_argument('--scan-id', type=_positive, action='append', default=None)
    cov.add_argument('--min-gap-days', type=_positive, default=30)

    dup = subparsers.add_parser('catalog-duplicates', help='content groups observed in more than one place')
    dup.add_argument('--collection', type=_positive, default=None)
    dup.add_argument('--include-empty', action='store_true')
    dup.add_argument('--limit', type=_positive, default=100)
    dup.add_argument('--group-id', type=_positive, default=None, help='show every observation of one group')
    col = subparsers.add_parser('catalog-collisions', help='same file name with different bytes')
    col.add_argument('--collection', type=_positive, default=None)
    col.add_argument('--limit', type=_positive, default=100)

    export = subparsers.add_parser('catalog-export', help='export the catalog to a NEW csv/xlsx/html file')
    export.add_argument('output', type=Path)
    export.add_argument('--format', choices=('csv', 'xlsx', 'html'), required=True)
    export.add_argument('--collection', type=_positive, default=None)
    export.add_argument('--scan-id', type=_positive, action='append', default=None)
    export.add_argument('--max-rows', type=_positive, default=1_000_000)
    export.add_argument('--min-gap-days', type=_positive, default=30)

    describe = subparsers.add_parser('catalog-describe', help='deterministic profile; optional hypothesis via a provider')
    describe.add_argument('--collection', type=_positive, required=True)
    describe.add_argument('--provider', default='none', help='none (default), fake, or a registered adapter')
    describe.add_argument('--output-dir', type=Path, default=None, help='sidecar directory (outside sources)')
    describe.add_argument('--allow-external', action='store_true', help='explicit opt-in for this run only')
    describe.add_argument('--param', action='append', default=[], help='KEY=VALUE passed to the provider and recorded')
    describe.add_argument('--min-gap-days', type=_positive, default=30)

    promote = subparsers.add_parser('catalog-promote', help='human decision on a hypothesis')
    promote.add_argument('annotation_id', type=_positive)
    promote.add_argument('--decision', choices=('promote', 'reject'), required=True)
    promote.add_argument('--actor', required=True)
    promote.add_argument('--reason', default=None)

    mark = subparsers.add_parser('catalog-mark', help='append a priority/note mark for a locator')
    mark.add_argument('collection_id', type=_positive)
    mark.add_argument('locator')
    mark.add_argument('--priority', type=int, default=None)
    mark.add_argument('--note', default=None)
    mark.add_argument('--actor', default='cli')

    ingest = subparsers.add_parser('catalog-ingest', help='acquire catalog entries into the evidence store')
    ingest.add_argument('entry_ids', type=_positive, nargs='+')
    ingest.add_argument('--temp-dir', type=Path, default=None)
    ingest.add_argument('--actor', default='cli')


def _params(pairs: list[str]) -> dict:
    result = {}
    for pair in pairs:
        key, sep, value = pair.partition('=')
        if not sep or not key:
            raise ValueError(f'--param must be KEY=VALUE: {pair!r}')
        result[key] = value
    return result


def handle(args) -> int | None:
    if args.command not in COMMANDS:
        return None
    from ..cli import _emit
    from ..catalog import coverage, describe, export, ingest, store
    from ..catalog.scan import LIMIT_NAMES, CatalogLimits, run_catalog_scan
    from ..db import init_db
    try:
        init_db()
        command = args.command
        if command == 'catalog-collection-add':
            fields = {k: getattr(args, k) for k in (*_TEXT_FIELDS, 'priority', 'parent_collection_id') if getattr(args, k) is not None}
            result = store.create_collection(args.label, case_id=args.case_id, actor=args.actor, **fields)
        elif command == 'catalog-collection-list':
            result = {'collections': store.list_collections(args.case_id)}
        elif command == 'catalog-collection-update':
            changes = {k: getattr(args, k) for k in ('label', *_TEXT_FIELDS, 'priority', 'parent_collection_id')
                       if getattr(args, k) is not None}
            changes.update({k: None for k in args.clear})
            result = store.update_collection(args.collection_id, changes, actor=args.actor)
        elif command == 'catalog-scan':
            values = {n: getattr(args, n) for n in LIMIT_NAMES if getattr(args, n) is not None}
            limits = CatalogLimits(**values, expand_document_containers=args.expand_document_containers)
            result = run_catalog_scan(args.root, args.collection, limits=limits, temp_dir=args.temp_dir)
            result['source_bytes_written'] = False
        elif command == 'catalog-scans':
            result = {'scans': store.list_scans(args.collection, args.limit)}
        elif command == 'catalog-entries':
            filters = {'collection_id': args.collection, 'scan_id': args.scan_id, 'all_scans': args.all_scans, 'type': args.type,
                       'status': args.status, 'conflict': args.conflict, 'duplicates_only': args.duplicates_only,
                       'group_id': args.group_id, 'q': args.q}
            result = store.list_entries(filters, after_id=args.after_id, limit=args.limit)
        elif command == 'catalog-coverage':
            result = coverage.coverage_report(args.collection, scan_ids=args.scan_id, min_gap_days=args.min_gap_days)
        elif command == 'catalog-duplicates':
            result = store.get_group(args.group_id) if args.group_id else store.list_groups(
                collection_id=args.collection, include_empty=args.include_empty, limit=args.limit)
        elif command == 'catalog-collisions':
            result = store.list_collisions(collection_id=args.collection, limit=args.limit)
        elif command == 'catalog-export':
            result = export.export_catalog(args.output, args.format, collection_id=args.collection, scan_ids=args.scan_id,
                                           max_rows=args.max_rows, min_gap_days=args.min_gap_days)
        elif command == 'catalog-describe':
            result = describe.explain(args.collection, provider=args.provider, output_dir=args.output_dir,
                                      allow_external=args.allow_external, params=_params(args.param),
                                      min_gap_days=args.min_gap_days, actor='cli')
        elif command == 'catalog-promote':
            decision = 'promoted' if args.decision == 'promote' else 'rejected'
            result = describe.decide(args.annotation_id, decision, actor=args.actor, reason=args.reason)
        elif command == 'catalog-mark':
            result = store.add_mark(args.collection_id, args.locator, priority=args.priority, note=args.note, actor=args.actor)
        else:
            result = ingest.ingest_entries(args.entry_ids, actor=args.actor, temp_dir=args.temp_dir)
        _emit({'command': command, **result})
        return 0
    except (store.NotFound, PermissionError, FileExistsError, ValueError, OSError) as exc:
        _emit({'command': args.command, 'error': type(exc).__name__, 'message': str(exc)}, error=True)
        return 2

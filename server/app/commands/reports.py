"""CLI subcommands owned by the reports area.

``report-create`` writes a NEW report directory below ``--output`` (refused
inside the repository, the content store or a scan root) and registers it.
``report-verify`` and ``citation-bundle-inspect`` read only the given files:
no database, no configuration, no source access (like ``metadata-verify``).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

COMMANDS = ('report-create', 'report-list', 'report-verify', 'report-types', 'citation-bundle-export',
            'citation-bundle-inspect')


def _pair(value: str) -> tuple[int, int]:
    try:
        artifact, anchor = (int(part) for part in value.split(':', 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError('expected ARTIFACT_ID:ANCHOR_ID') from exc
    if artifact < 1 or anchor < 1:
        raise argparse.ArgumentTypeError('ids must be positive')
    return artifact, anchor


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def register(subparsers) -> None:
    create = subparsers.add_parser('report-create', help='generate a report (HTML, JSON, CSV/XLSX, optional PDF) into a NEW directory')
    create.add_argument('--type', required=True, dest='report_type',
                        help='catalog | timeline | time_budget | hypothesis_dossier | lawyer_package')
    create.add_argument('--params', default='{}', help='JSON object of parameters (see report-types)')
    create.add_argument('--params-file', type=Path, default=None, help='JSON file with parameters (instead of --params)')
    create.add_argument('--output', required=True, type=Path, help='private output directory (a new subdirectory is created)')
    listing = subparsers.add_parser('report-list', help='list registered reports (append-only register)')
    listing.add_argument('--type', dest='report_type', default=None)
    listing.add_argument('--limit', type=_positive, default=100)
    verify = subparsers.add_parser('report-verify', help='recompute report file hashes and check the manifest structure')
    verify.add_argument('manifest', type=Path, help='manifest.json of a report directory')
    verify.add_argument('--expected-manifest-sha256', default=None,
                        help='SHA-256 recorded in the register (report-list) to compare the manifest bytes with')
    subparsers.add_parser('report-types', help='describe report types, sections and parameter defaults')
    bundle = subparsers.add_parser('citation-bundle-export', help='export several pinned citations as a bundle (NEW directory)')
    bundle.add_argument('output', type=Path, help='new bundle directory; existing paths are never reused')
    group = bundle.add_mutually_exclusive_group(required=True)
    group.add_argument('--report', type=_positive, default=None, help='all citations of a registered report')
    group.add_argument('--citation', type=_pair, action='append', default=None, metavar='ARTIFACT:ANCHOR')
    inspect = subparsers.add_parser('citation-bundle-inspect', help='independently inspect a citation bundle')
    inspect.add_argument('bundle', type=Path, help='bundle directory or its citation-bundle.json')


def _print(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


def handle(args) -> int | None:
    command = getattr(args, 'command', None)
    if command not in COMMANDS:
        return None
    if command == 'report-verify':
        from ..reports.manifest import verify_report
        result = verify_report(args.manifest, expected_manifest_sha256=args.expected_manifest_sha256)
        _print({'command': command, **result})
        return 0 if result['valid'] else 1
    if command == 'citation-bundle-inspect':
        from ..reports.bundle_consumer import inspect_bundle
        result = inspect_bundle(args.bundle)
        _print({'command': command, **result})
        return 0 if result['valid'] else 1
    if command == 'report-types':
        from ..reports.definitions import describe_types
        _print({'command': command, **describe_types()})
        return 0
    from ..db import init_db
    init_db()
    if command == 'report-create':
        from ..reports.common import ReportInputError
        from ..reports.service import create_report
        try:
            raw = args.params_file.read_text(encoding='utf-8') if args.params_file else args.params
            params = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise ReportInputError(f'parameters are not a readable JSON object: {exc}') from exc
        result = create_report(args.report_type, params, args.output)
        _print({'command': command, 'source_bytes_written': False, **result})
        if any(status in ('unavailable', 'error', 'partial') for status in result['sections'].values()):
            print(json.dumps({'command': command, 'warning': 'incomplete_sections',
                              'sections': {k: v for k, v in result['sections'].items() if v != 'ok'}}), file=sys.stderr)
        return 0
    if command == 'report-list':
        from ..reports.service import listing
        _print({'command': command, 'reports': listing(report_type=args.report_type, limit=args.limit)})
        return 0
    if command == 'citation-bundle-export':
        from ..reports.bundle import export_bundle
        from ..reports.paths import check_output_dir
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError(f'Output already exists: {args.output}; choose a new directory')
        parent = check_output_dir(args.output.parent if str(args.output.parent) else Path('.'))
        target = check_output_dir(parent / args.output.name)
        parent.mkdir(parents=True, exist_ok=True)
        report_uid = None
        if args.report is not None:
            from ..reports.service import registered
            row = registered(args.report)
            pairs = [tuple(pair) for pair in row['citations']]
            report_uid = row['report_uid']
        else:
            pairs = args.citation
        manifest = export_bundle(pairs, target, report_uid=report_uid)
        _print({'command': command, 'output': str(target), 'packets': len(manifest['packets']),
                'excluded': manifest['excluded'], 'payload_sha256': manifest['integrity']['payload_sha256'],
                'signed': False, 'source_bytes_written': False})
        return 0
    return None

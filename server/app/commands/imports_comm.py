"""CLI for communication, Takeout and media importers (task B part 2).

Every command acquires its input through ``ingest_file`` into the configured
store (``EW_DATA_DIR``/``EW_STORE_DIR``); nothing is written next to the source.
Opt-in importers refuse to run without their explicit flag.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

COMMANDS = (
    'import-email', 'import-ics', 'import-vcf', 'import-calls-xml', 'import-billing', 'observe-recording-names',
    'import-takeout-activity', 'import-location-history', 'import-browser-history', 'import-whatsapp-export',
    'import-exif', 'expand-calendar-recurrence',
)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError('must be a positive integer')
    return number


def _common(parser, *, locator: bool = True) -> None:
    parser.add_argument('--label', help='source label (private labels belong outside the repository)')
    parser.add_argument('--collection-id', type=int, help='existing collection to record on the acquisition')
    if locator:
        parser.add_argument('--source-locator', help='stable locator to record instead of the local path')
    parser.add_argument('--max-records', type=_positive, help='record limit; reaching it marks coverage partial')


def register(subparsers) -> None:
    p = subparsers.add_parser('import-email', help='import an MBOX (e.g. Gmail Takeout) or a single EML message')
    p.add_argument('path', type=Path)
    p.add_argument('--format', choices=('auto', 'mbox', 'eml'), default='auto')
    p.add_argument('--max-attachment-bytes', type=_positive)
    _common(p)
    p = subparsers.add_parser('import-ics', help='import an iCalendar file (recurrences kept raw, not expanded)')
    p.add_argument('path', type=Path)
    _common(p)
    p = subparsers.add_parser('import-vcf', help='import vCard contacts as contact observations (not events)')
    p.add_argument('path', type=Path)
    _common(p)
    p = subparsers.add_parser('import-calls-xml', help='import an SMS Backup & Restore calls.xml as call events')
    p.add_argument('path', type=Path)
    _common(p)
    p = subparsers.add_parser('import-billing', help='import an operator billing CSV/XLSX via a column-mapping profile')
    p.add_argument('path', type=Path)
    p.add_argument('--profile', help='profile id (default: unique header match)')
    p.add_argument('--profiles', type=Path, help='private profile file (default: shipped examples)')
    _common(p)
    p = subparsers.add_parser('observe-recording-names',
                              help='parse recording file names (no audio is read) into hypotheses and time candidates')
    p.add_argument('paths', type=Path, nargs='*')
    p.add_argument('--artifact-id', type=int, action='append', default=[], help='observe an acquired artifact name')
    p.add_argument('--patterns', type=Path, help='private pattern file (default: shipped examples)')
    p.add_argument('--extension', action='append', help='file extension to include when walking directories')
    p.add_argument('--label')
    p = subparsers.add_parser('import-takeout-activity', help='import Google Takeout My Activity (JSON or HTML)')
    p.add_argument('path', type=Path)
    p.add_argument('--enable-browser-history', action='store_true',
                   help='also import Chrome/YouTube records (disabled by default)')
    _common(p)
    p = subparsers.add_parser('import-location-history',
                              help='import Takeout location history as coarse presence events (opt-in, local only)')
    p.add_argument('path', type=Path)
    p.add_argument('--enable-location-history', action='store_true', help='required explicit opt-in')
    p.add_argument('--coarse-decimals', type=int, default=2, help='coordinate rounding in event metadata (0-4)')
    _common(p)
    p = subparsers.add_parser('import-browser-history', help='import Chrome/YouTube history (opt-in)')
    p.add_argument('path', type=Path)
    p.add_argument('--enable-browser-history', action='store_true', help='required explicit opt-in')
    _common(p)
    p = subparsers.add_parser('import-whatsapp-export', help='import a WhatsApp export folder or zip with media')
    p.add_argument('path', type=Path)
    p.add_argument('--chat-label')
    p.add_argument('--chat-member', help='chat .txt inside the export when several exist')
    p.add_argument('--collection-id', type=int)
    p.add_argument('--source-locator')
    p = subparsers.add_parser('import-exif', help='acquire images and record EXIF time candidates')
    p.add_argument('paths', type=Path, nargs='*')
    p.add_argument('--artifact-id', type=int, action='append', default=[], help='read EXIF of an acquired artifact')
    p.add_argument('--label')
    p.add_argument('--collection-id', type=int)
    p = subparsers.add_parser('expand-calendar-recurrence',
                              help='bounded, unsaved projection of a recurring calendar event')
    p.add_argument('event_id', type=int)
    p.add_argument('--max-instances', type=_positive, default=100)
    p.add_argument('--until', help='local wall-clock bound, ISO format')


def _limits(args):
    from ..importers.comm_common import DEFAULT_LIMITS
    return DEFAULT_LIMITS.replace(max_records=getattr(args, 'max_records', None),
                                  max_attachment_bytes=getattr(args, 'max_attachment_bytes', None))


def _kwargs(args) -> dict:
    return {'source_locator': args.source_locator, 'collection_id': args.collection_id, 'label': args.label,
            'limits': _limits(args)}


def _sniff_mbox(path: Path) -> bool:
    with path.open('rb') as handle:
        return handle.read(5) == b'From '


def run(args) -> dict:
    from ..importers import (billing, browser_history, call_log, email_mbox, exif, ics, location, recording_names,
                             takeout_activity, vcf, whatsapp_media)
    command = args.command
    if command == 'import-email':
        fmt = args.format if args.format != 'auto' else ('mbox' if _sniff_mbox(args.path) else 'eml')
        importer = email_mbox.import_mbox if fmt == 'mbox' else email_mbox.import_eml
        return {'format': fmt, **importer(args.path, **_kwargs(args))}
    if command == 'import-ics':
        return ics.import_ics(args.path, **_kwargs(args))
    if command == 'import-vcf':
        return vcf.import_vcf(args.path, **_kwargs(args))
    if command == 'import-calls-xml':
        return call_log.import_calls_xml(args.path, **_kwargs(args))
    if command == 'import-billing':
        return billing.import_billing(args.path, profile_id=args.profile, profiles_path=args.profiles, **_kwargs(args))
    if command == 'observe-recording-names':
        result = {}
        if args.paths:
            result['paths'] = recording_names.observe_paths(
                args.paths, label=args.label, patterns_path=args.patterns,
                extensions=tuple(args.extension) if args.extension else None)
        if args.artifact_id:
            result['artifacts'] = recording_names.observe_artifacts(args.artifact_id, patterns_path=args.patterns)
        if not result:
            raise ValueError('give at least one path or --artifact-id')
        return result
    if command == 'import-takeout-activity':
        return takeout_activity.import_my_activity(args.path, enable_browser_history=args.enable_browser_history,
                                                   **_kwargs(args))
    if command == 'import-location-history':
        return location.import_location_history(args.path, enable_location=args.enable_location_history,
                                                coarse_decimals=args.coarse_decimals, **_kwargs(args))
    if command == 'import-browser-history':
        return browser_history.import_browser_history(args.path, enable_browser_history=args.enable_browser_history,
                                                      **_kwargs(args))
    if command == 'import-whatsapp-export':
        return whatsapp_media.import_whatsapp_export(args.path, chat_label=args.chat_label,
                                                     chat_member=args.chat_member, source_locator=args.source_locator,
                                                     collection_id=args.collection_id)
    if command == 'import-exif':
        result = {}
        if args.paths:
            result['paths'] = exif.import_images(args.paths, label=args.label, collection_id=args.collection_id)
        if args.artifact_id:
            result['artifacts'] = [exif.observe_artifact(a) for a in args.artifact_id]
        if not result:
            raise ValueError('give at least one path or --artifact-id')
        return result
    if command == 'expand-calendar-recurrence':
        return ics.expand_recurrence(args.event_id, max_instances=args.max_instances, until=args.until)
    raise ValueError(f'unknown command {command}')


def handle(args) -> int | None:
    if getattr(args, 'command', None) not in COMMANDS:
        return None
    for name in ('path',):
        value = getattr(args, name, None)
        if value is not None and not value.exists():
            raise FileNotFoundError(f'input not found: {value}')
    from ..db import init_db
    init_db()
    result = run(args)
    print(json.dumps({'command': args.command, 'source_bytes_written': False, **result}, ensure_ascii=False,
                     allow_nan=False, default=str), file=sys.stdout)
    return 0

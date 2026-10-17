"""CLI subcommands owned by the ledger area (financial, transport and health records).

Every import acquires its input through ``ingest_file`` into the configured
store; nothing is written next to the source. ``health-import`` refuses to run
without ``--enable-health`` (special-category data, local processing only).
Output: one JSON object on stdout; errors on stderr with exit code 2.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

COMMANDS = ('ledger-import', 'camt-import', 'mt940-import', 'health-import', 'ledger-profiles', 'ledger-entries',
            'ledger-statements', 'health-sessions')
HEALTH_FORMATS = ('auto', 'apple', 'google-fit', 'samsung', 'fitbit')


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError('must be a positive integer')
    return number


def _common(parser) -> None:
    parser.add_argument('--label', help='source label (private labels belong outside the repository)')
    parser.add_argument('--collection-id', type=int, help='existing collection to record on the acquisition')
    parser.add_argument('--source-locator', help='stable locator to record instead of the local path')
    parser.add_argument('--max-records', type=_positive, help='record limit; reaching it marks coverage partial')


def register(subparsers) -> None:
    p = subparsers.add_parser('ledger-import', help='import a bank/transit/ride table (CSV/TSV/TXT/XLSX/XLS) via a profile')
    p.add_argument('path', type=Path)
    p.add_argument('--profile', help='profile id (default: unique header match among the profiles)')
    p.add_argument('--profiles', type=Path, help='private profile file (default: shipped EXAMPLE profiles)')
    p.add_argument('--encoding', help='explicit encoding (utf-8, utf-8-sig, cp1250, iso-8859-2, cp1252); default: recorded hypothesis')
    p.add_argument('--delimiter', choices=(';', ',', 'tab', '|'), help='explicit delimiter (default: profile or hypothesis)')
    _common(p)
    p = subparsers.add_parser('camt-import', help='import an ISO 20022 CAMT.053 bank statement (XML)')
    p.add_argument('path', type=Path)
    _common(p)
    p = subparsers.add_parser('mt940-import', help='import a SWIFT MT940 statement file')
    p.add_argument('path', type=Path)
    p.add_argument('--two-digit-year-base', type=int, default=2000, help='century of YYMMDD dates (recorded; default 2000)')
    p.add_argument('--encoding', help='explicit encoding (default: recorded hypothesis)')
    _common(p)
    p = subparsers.add_parser('health-import', help='import a health/fitness export (opt-in; special-category data)')
    p.add_argument('path', type=Path)
    p.add_argument('--enable-health', action='store_true', help='required explicit per-run opt-in')
    p.add_argument('--format', choices=HEALTH_FORMATS, default='auto')
    p.add_argument('--sleep-session-gap-s', type=int, default=3600, help='Apple sleep grouping gap (rule ledger.sleep_session.v1)')
    p.add_argument('--samsung-mapping', type=Path, help='private Samsung table mapping (default: shipped example)')
    _common(p)
    p = subparsers.add_parser('ledger-profiles', help='list tabular profiles (EXAMPLES unless --profiles names a private file)')
    p.add_argument('--profiles', type=Path)
    p = subparsers.add_parser('ledger-entries', help='list typed ledger entries (exact amounts)')
    p.add_argument('--artifact-id', type=int)
    p.add_argument('--limit', type=_positive, default=100)
    p.add_argument('--after-id', type=int, default=0)
    p.add_argument('--mask-accounts', action='store_true', help='display masking of account numbers (stored values stay raw)')
    p = subparsers.add_parser('ledger-statements', help='list statements with their code-computed balance checks')
    p.add_argument('--artifact-id', type=int)
    p = subparsers.add_parser('health-sessions', help='list health sessions (sleep, workouts) with spans')
    p.add_argument('--artifact-id', type=int)
    p.add_argument('--limit', type=_positive, default=100)


def _limits(args):
    from ..importers.comm_common import DEFAULT_LIMITS
    return DEFAULT_LIMITS.replace(max_records=getattr(args, 'max_records', None))


def _kwargs(args) -> dict:
    return {'source_locator': args.source_locator, 'collection_id': args.collection_id, 'label': args.label,
            'limits': _limits(args)}


def detect_health_format(path: Path) -> str:
    name = path.name.lower()
    with path.open('rb') as handle:
        head = handle.read(65536)
    if head.startswith(b'PK\x03\x04') or b'<HealthData' in head:
        return 'apple'
    if name.startswith('com.samsung.') or head.lstrip().startswith(b'com.samsung.'):
        return 'samsung'
    if (name.startswith(('sleep-', 'exercise-')) and name.endswith('.json')) or b'"logId"' in head:
        return 'fitbit'
    if b'fitnessActivity' in head or b'Move Minutes' in head or name.endswith('.csv'):
        return 'google-fit'
    raise ValueError('health export format not recognized; pass --format')


def run_health(path: Path, fmt: str, *, enable_health: bool, sleep_session_gap_s: int = 3600,
               samsung_mapping: Path | None = None, **kwargs) -> dict:
    from ..importers import health_apple, health_fitbit, health_google_fit, health_samsung
    from ..ledger.health import require_opt_in
    require_opt_in(enable_health)  # refuse before even sniffing the format
    fmt = detect_health_format(path) if fmt == 'auto' else fmt
    if fmt == 'apple':
        return {'format': fmt, **health_apple.import_apple_health(path, enable_health=enable_health,
                                                                  sleep_session_gap_s=sleep_session_gap_s, **kwargs)}
    if fmt == 'samsung':
        return {'format': fmt, **health_samsung.import_samsung_health(path, enable_health=enable_health,
                                                                      mapping_path=samsung_mapping, **kwargs)}
    if fmt == 'fitbit':
        return {'format': fmt, **health_fitbit.import_fitbit(path, enable_health=enable_health, **kwargs)}
    return {'format': fmt, **health_google_fit.import_google_fit(path, enable_health=enable_health, **kwargs)}


def run(args) -> dict:
    from ..ledger import queries
    command = args.command
    if command == 'ledger-import':
        from ..importers.tabular_profiles import import_tabular
        delimiter = '\t' if args.delimiter == 'tab' else args.delimiter
        return import_tabular(args.path, profile_id=args.profile, profiles_path=args.profiles, encoding=args.encoding,
                              delimiter=delimiter, **_kwargs(args))
    if command == 'camt-import':
        from ..importers.camt053 import import_camt053
        return import_camt053(args.path, **_kwargs(args))
    if command == 'mt940-import':
        from ..importers.mt940 import import_mt940
        return import_mt940(args.path, two_digit_year_base=args.two_digit_year_base, encoding=args.encoding,
                            **_kwargs(args))
    if command == 'health-import':
        return run_health(args.path, args.format, enable_health=args.enable_health,
                          sleep_session_gap_s=args.sleep_session_gap_s, samsung_mapping=args.samsung_mapping,
                          **_kwargs(args))
    if command == 'ledger-profiles':
        return queries.profiles(args.profiles)
    if command == 'ledger-entries':
        return queries.entries(artifact_id=args.artifact_id, limit=args.limit, after_id=args.after_id,
                               mask_accounts=args.mask_accounts)
    if command == 'ledger-statements':
        return queries.statements(artifact_id=args.artifact_id)
    if command == 'health-sessions':
        return queries.health_sessions(artifact_id=args.artifact_id, limit=args.limit)
    raise ValueError(f'unknown command {command}')


def handle(args) -> int | None:
    if getattr(args, 'command', None) not in COMMANDS:
        return None
    value = getattr(args, 'path', None)
    if value is not None and not value.exists():
        raise FileNotFoundError(f'input not found: {value}')
    from ..db import init_db
    init_db()
    result = run(args)
    print(json.dumps({'command': args.command, 'source_bytes_written': False, **result}, ensure_ascii=False,
                     allow_nan=False, default=str), file=sys.stdout)
    return 0

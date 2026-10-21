"""CLI subcommands owned by the timeline area.

Module import stays free of settings/database imports (``cli`` import contract);
each handler imports the timeline package lazily. Outputs are new files only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

COMMANDS = ('timeline-window', 'timeline-rebuild', 'correlate', 'correlation-review')
_FILTERS = (('event_type', 'event-type'), ('kind', 'kind'), ('source', 'source'), ('collection', 'collection'),
            ('counterparty', 'counterparty'), ('chatbot', 'chatbot'), ('thread', 'thread'), ('tag', 'tag'))


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def _projection_args(parser):
    parser.add_argument('--rule', help='display rule, e.g. display_time@1 (default: latest released)')
    parser.add_argument('--assume-zone', help='IANA zone applied to wall-clock times without zone (requires --assumption-rule)')
    parser.add_argument('--assumption-rule', help='identifier of the operator decision behind --assume-zone')


def register(subparsers) -> None:
    window = subparsers.add_parser('timeline-window', help='export a projected timeline window to a NEW csv/json file')
    window.add_argument('output', type=Path)
    window.add_argument('--from', dest='from_', help='UTC ISO bound (date-only allowed), inclusive')
    window.add_argument('--to', help='UTC ISO bound, exclusive (date-only = end of that unit)')
    window.add_argument('--format', choices=('csv', 'json'), help='default: from the output suffix')
    for name, flag in _FILTERS:
        window.add_argument(f'--{flag}', dest=name, action='append', help='repeatable or comma-separated')
    window.add_argument('--q', help='FTS5 text query (literal syntax as /api/search)')
    window.add_argument('--transcript-versions', default='latest', help='latest | all | comma-separated version ids')
    window.add_argument('--include-hidden', action='store_true', help='include items the display rule hides by default')
    window.add_argument('--match', choices=('display', 'overlap'), default='display')
    window.add_argument('--order', choices=('asc', 'desc'), default='asc')
    window.add_argument('--max-rows', type=_positive, default=200_000)
    window.add_argument('--max-output-bytes', type=_positive, default=512 * 1024 * 1024)
    _projection_args(window)
    rebuild = subparsers.add_parser('timeline-rebuild', help='refresh (or fully rebuild) a timeline projection cache')
    rebuild.add_argument('--full', action='store_true', help='recompute every item instead of an incremental refresh')
    _projection_args(rebuild)
    correlate = subparsers.add_parser('correlate', help='run a versioned correlation rule; appends a new run and hypotheses')
    correlate.add_argument('--rule', required=True, help='recording_call_log | email_chat | same_conversation [@version]')
    correlate.add_argument('--params', default='{}', help='JSON object overriding named rule parameters')
    correlate.add_argument('--actor', default='cli-operator')
    review = subparsers.add_parser('correlation-review', help='append accept/reject/revoke to a hypothesis status history')
    review.add_argument('hypothesis_id', type=_positive)
    review.add_argument('action', choices=('accept', 'reject', 'revoke'))
    review.add_argument('--reason', required=True)
    review.add_argument('--actor', default='cli-operator')
    review.add_argument('--no-link', action='store_true', help='accept without creating a links row')


def _open_db():
    from ..config import settings
    from ..db import connect
    if not Path(settings.db_path).exists():
        raise FileNotFoundError(f'database not found: {settings.db_path}; start the service once to initialize it')
    db = connect()
    applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations WHERE version IN (30, 31)")} \
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone() else set()
    if applied != {30, 31}:
        db.close()
        raise RuntimeError('timeline schema versions 30-31 are not applied; back up SQLite and the store, then start '
                           'the service once (init_db applies additive migrations)')
    return db


def _projection(db, args, *, snapshot: bool = False):
    from ..timeline.analysis_items import open_snapshot
    from ..timeline.projection import ensure_projection
    from ..timeline.ruleset import display_params, load_rule
    rule = load_rule(args.rule, kind='display_rule')
    params = display_params(args.assume_zone, args.assumption_rule)
    if snapshot:  # reads follow: keep them in one snapshot (review 2 P4)
        return rule, open_snapshot(db, rule, params)
    return rule, ensure_projection(db, rule, params, force_full=getattr(args, 'full', False))


def handle(args) -> int | None:
    if getattr(args, 'command', None) not in COMMANDS:
        return None
    from ..cli import _emit, _write_new_bytes
    try:
        db = _open_db()
    except (OSError, RuntimeError) as exc:
        _emit({'command': args.command, 'error': type(exc).__name__, 'message': str(exc)}, error=True)
        return 2
    try:
        if args.command == 'timeline-window':
            from ..timeline.query import export_window, make_filters
            from ..timeline.timeutil import parse_bound
            fmt = args.format or ('json' if args.output.suffix.lower() == '.json' else 'csv' if args.output.suffix.lower() == '.csv' else None)
            if fmt is None:
                raise ValueError('choose --format csv|json or an output ending in .csv/.json')
            filters = make_filters(event_types=args.event_type, kinds=args.kind, sources=args.source,
                                   collections=args.collection, counterparties=args.counterparty, chatbots=args.chatbot,
                                   threads=args.thread, tags=args.tag, q=args.q, transcript_versions=args.transcript_versions,
                                   include_hidden=args.include_hidden)
            rule, projection = _projection(db, args, snapshot=True)
            payload, meta = export_window(db, projection, rule, fmt=fmt, max_rows=args.max_rows,
                                          start_us=parse_bound(args.from_), end_us=parse_bound(args.to, end=True),
                                          filters=filters, match=args.match, order=args.order, group=None)
            _write_new_bytes(payload, args.output, max_bytes=args.max_output_bytes)
            _emit({'command': args.command, 'output': str(args.output.resolve()), 'format': fmt,
                   'rows': meta['row_count'], 'complete': meta['complete'], 'rule': meta['rule'], 'params': meta['params'],
                   'projection': meta['projection'], 'source_bytes_written': False})
            return 0
        if args.command == 'timeline-rebuild':
            rule, projection = _projection(db, args)
            _emit({'command': args.command, **projection})
            return 0
        if args.command == 'correlate':
            from ..timeline.correlation import run_rule
            try:
                params = json.loads(args.params)
            except ValueError as exc:
                raise ValueError(f'--params must be a JSON object: {exc}') from exc
            if not isinstance(params, dict):
                raise ValueError('--params must be a JSON object')
            run = run_rule(db, args.rule, params, actor=args.actor)
            _emit({'command': args.command, 'run_id': run['id'], 'rule': f"{run['rule_id']}@{run['rule_version']}",
                   'rule_sha256': run['rule_sha256'], 'status': run['status'], 'hypotheses': run['hypothesis_count'],
                   'counts': run['result'].get('counts'), 'truncated': run['result'].get('truncated')})
            return 0
        from ..timeline.correlation import review
        detail = review(db, args.hypothesis_id, args.action, reason=args.reason, actor=args.actor,
                        create_link=not args.no_link)
        _emit({'command': args.command, 'hypothesis_id': detail['id'], 'status': detail['status'],
               'history': [{k: h[k] for k in ('id', 'status', 'actor', 'reason', 'link_id', 'created_at')} for h in detail['history']]})
        return 0
    except LookupError as exc:
        _emit({'command': args.command, 'error': 'NotFound', 'message': str(exc)}, error=True)
        return 2
    finally:
        db.close()

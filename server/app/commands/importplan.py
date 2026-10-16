"""CLI subcommands of the import planner (AGEDS-TASK-20261016-I).

Each command prints one JSON object on stdout (errors on stderr, exit 2).
Planning reads catalogued sources with bounded probes; runs import through the
real importers. Temporary directories are refused inside any source root.
"""
from __future__ import annotations

import argparse
from pathlib import Path

COMMANDS: tuple[str, ...] = (
    'import-plan-create', 'import-plan-list', 'import-plan-show', 'import-plan-summary', 'import-plan-decide',
    'import-plan-run', 'import-plan-runs', 'import-plan-work', 'import-plan-cross', 'catalog-scan-abandon',
)


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def _pair(value: str) -> tuple[int, str]:
    item, sep, rest = value.partition('=')
    if not sep or not item.isdigit() or not rest:
        raise argparse.ArgumentTypeError('expected ITEM_ID=VALUE')
    return int(item), rest


def register(subparsers) -> None:
    from ..importplan.limits import LIMIT_NAMES as PLAN_LIMITS, RUN_LIMIT_NAMES as RUN_LIMITS
    create = subparsers.add_parser('import-plan-create', help='detect importable units in catalog scans and store a plan')
    create.add_argument('--collection', type=_positive, action='append', default=[], help='current scans of a collection (repeatable)')
    create.add_argument('--scan-id', type=_positive, action='append', default=[], help='a specific finished scan (repeatable)')
    create.add_argument('--billing-profile', default=None, help='explicit billing profile id for matching tables')
    create.add_argument('--billing-profiles', type=Path, default=None, help='private billing profile file')
    create.add_argument('--recording-patterns', type=Path, default=None, help='private recording-name pattern file')
    create.add_argument('--detectors', type=Path, default=None, help='detector table (default: shipped v1)')
    create.add_argument('--temp-dir', type=Path, default=None, help='parent of temporary spool directories (outside sources)')
    create.add_argument('--actor', default='cli')
    for name in PLAN_LIMITS:
        create.add_argument('--' + name.replace('_', '-'), type=_positive, default=None)

    listing = subparsers.add_parser('import-plan-list', help='recent import plans')
    listing.add_argument('--limit', type=_positive, default=50)

    show = subparsers.add_parser('import-plan-show', help='one page of plan items with their state')
    show.add_argument('plan_id', type=_positive)
    show.add_argument('--version', type=_positive, default=None)
    show.add_argument('--importer', default=None)
    show.add_argument('--status', default=None)
    show.add_argument('--ambiguous', action='store_true')
    show.add_argument('--blocked', action='store_true')
    show.add_argument('--collection', type=_positive, default=None)
    show.add_argument('--q', default=None)
    show.add_argument('--after-ordinal', type=int, default=None)
    show.add_argument('--limit', type=_positive, default=200)
    show.add_argument('--item', type=_positive, default=None, help='show one item with members and history')

    summary = subparsers.add_parser('import-plan-summary', help='dry-run summary (reads the stored plan only)')
    summary.add_argument('plan_id', type=_positive)
    summary.add_argument('--version', type=_positive, default=None)

    decide = subparsers.add_parser('import-plan-decide', help='append a plan version with decisions')
    decide.add_argument('plan_id', type=_positive)
    decide.add_argument('--base-version', type=_positive, required=True)
    decide.add_argument('--actor', required=True)
    decide.add_argument('--note', default=None)
    decide.add_argument('--approve', type=_positive, nargs='*', default=[])
    decide.add_argument('--skip', type=_positive, nargs='*', default=[])
    decide.add_argument('--propose', type=_positive, nargs='*', default=[])
    decide.add_argument('--approve-all-ready', action='store_true', help='bulk-approve every proposed item without blockers')
    decide.add_argument('--importer', type=_pair, action='append', default=[], help='ITEM_ID=IMPORTER')
    decide.add_argument('--opt-in', type=_pair, action='append', default=[], help='ITEM_ID=OPT_IN_NAME (enables it)')
    decide.add_argument('--param', type=_pair, action='append', default=[], help='ITEM_ID=NAME=VALUE')
    decide.add_argument('--reason', default=None)

    run = subparsers.add_parser('import-plan-run', help='import the approved items of a plan version')
    run.add_argument('plan_id', type=_positive)
    run.add_argument('--version', type=_positive, default=None)
    run.add_argument('--mode', choices=('sync', 'queue'), default='sync')
    run.add_argument('--recover-running', action='store_true', help='assert the previous process is dead and resume')
    run.add_argument('--rerun-done', action='store_true', help='re-run done items (importers re-observe, no duplicates)')
    run.add_argument('--item', type=_positive, action='append', default=None)
    run.add_argument('--temp-dir', type=Path, default=None)
    run.add_argument('--actor', default='cli')
    for name in RUN_LIMITS:
        run.add_argument('--' + name.replace('_', '-'), type=_positive, default=None)

    runs = subparsers.add_parser('import-plan-runs', help='runs of a plan, or one run with --run')
    runs.add_argument('plan_id', type=_positive)
    runs.add_argument('--run', type=_positive, default=None)

    work = subparsers.add_parser('import-plan-work', help='drain queued import_plan_item jobs with leases')
    work.add_argument('--max-jobs', type=_positive, default=None)
    work.add_argument('--lease-seconds', type=_positive, default=None)

    crossing = subparsers.add_parser('import-plan-cross', help='the same conversation in several exports')
    crossing.add_argument('plan_id', type=_positive)

    abandon = subparsers.add_parser('catalog-scan-abandon', help='close a catalog scan left running by a dead process')
    abandon.add_argument('scan_id', type=_positive)
    abandon.add_argument('--actor', required=True)
    abandon.add_argument('--reason', required=True)
    abandon.add_argument('--min-idle-seconds', type=int, default=3600)


def _decisions(args) -> list[dict]:
    decisions: dict[int, dict] = {}

    def slot(item_id: int) -> dict:
        return decisions.setdefault(item_id, {'item_id': item_id})
    for status, ids in (('approved', args.approve), ('skipped', args.skip), ('proposed', args.propose)):
        for item_id in ids:
            slot(item_id)['status'] = status
    for item_id, importer in args.importer:
        slot(item_id)['importer'] = importer
    for item_id, name in args.opt_in:
        slot(item_id).setdefault('opt_ins', {})[name] = True
    for item_id, rest in args.param:
        name, sep, value = rest.partition('=')
        if not sep or not name:
            raise ValueError(f'--param must be ITEM_ID=NAME=VALUE: {item_id}={rest}')
        slot(item_id).setdefault('parameters', {})[name] = int(value) if name == 'coarse_decimals' else value
    for decision in decisions.values():
        if args.reason:
            decision['reason'] = args.reason
    # Approval goes last so opt-ins/importers given in the same command apply first.
    return [decisions[k] for k in sorted(decisions)]


def handle(args) -> int | None:
    if args.command not in COMMANDS:
        return None
    from ..cli import _emit
    from ..db import init_db
    from ..evidence import IntegrityError
    from ..importplan import abandon, cross, execute, plan
    from ..importplan.limits import LIMIT_NAMES as PLAN_LIMITS, RUN_LIMIT_NAMES as RUN_LIMITS, PlanLimits, RunLimits
    try:
        init_db()
        command = args.command
        if command == 'import-plan-create':
            limits = PlanLimits(**{n: getattr(args, n) for n in PLAN_LIMITS if getattr(args, n) is not None})
            options = {'billing_profile_id': args.billing_profile,
                       'billing_profiles_path': str(args.billing_profiles) if args.billing_profiles else None,
                       'recording_patterns_path': str(args.recording_patterns) if args.recording_patterns else None}
            created = plan.create_plan(collection_ids=args.collection, scan_ids=args.scan_id, options=options,
                                       limits=limits, detectors_path=args.detectors, temp_dir=args.temp_dir,
                                       actor=args.actor)
            result = {'plan': created['plan'], 'summary': created['summary']}
        elif command == 'import-plan-list':
            result = {'plans': plan.list_plans(args.limit)}
        elif command == 'import-plan-show':
            if args.item:
                result = plan.get_item(args.plan_id, args.item, version=args.version)
            else:
                filters = {'importer': args.importer, 'status': args.status, 'ambiguous': args.ambiguous,
                           'blocked': args.blocked, 'collection_id': args.collection, 'q': args.q}
                result = plan.get_plan(args.plan_id, version=args.version, filters=filters,
                                       after_ordinal=args.after_ordinal, limit=args.limit)
        elif command == 'import-plan-summary':
            result = plan.summary(args.plan_id, args.version)
        elif command == 'import-plan-decide':
            bulk = {'action': 'approve'} if args.approve_all_ready else None
            result = plan.decide(args.plan_id, base_version=args.base_version, decisions=_decisions(args), bulk=bulk,
                                 actor=args.actor, note=args.note)
        elif command == 'import-plan-run':
            limits = RunLimits(**{n: getattr(args, n) for n in RUN_LIMITS if getattr(args, n) is not None})
            result = execute.execute_plan(args.plan_id, version=args.version, mode=args.mode, actor=args.actor,
                                          temp_dir=args.temp_dir, recover_running=args.recover_running,
                                          rerun_done=args.rerun_done, item_ids=args.item, limits=limits)
        elif command == 'import-plan-runs':
            result = execute.get_run(args.run) if args.run else {'runs': execute.list_runs(args.plan_id)}
        elif command == 'import-plan-work':
            result = execute.work(max_jobs=args.max_jobs, lease_seconds=args.lease_seconds)
        elif command == 'import-plan-cross':
            header = plan.get_plan(args.plan_id, limit=1)['plan']
            result = {'before_import': header['cross_export'], 'after_import': cross.post_import(args.plan_id)}
        else:
            result = abandon.abandon_scan(args.scan_id, actor=args.actor, reason=args.reason,
                                          min_idle_seconds=args.min_idle_seconds)
        _emit({'command': command, **result})
        return 0
    except (plan.NotFound, plan.Conflict, IntegrityError, PermissionError, FileExistsError, ValueError, OSError) as exc:
        _emit({'command': args.command, 'error': type(exc).__name__, 'message': str(exc)}, error=True)
        return 2

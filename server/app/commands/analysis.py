"""CLI subcommands owned by the analysis area (time budget, feature series, labels).

One JSON object on stdout; errors go through ``cli.main`` (exit 2). The category
configuration comes from ``--config PATH`` or ``AGEDS_ACTIVITY_CATEGORIES``;
it is private user data and is never copied into the repository. Optional
OUTPUT files are new files only (never overwritten).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

COMMANDS: tuple[str, ...] = ('budget-run', 'budget-show', 'budget-list', 'features-run', 'features-show',
                             'label-correct', 'labels-show', 'model-label-run')
MAX_OUTPUT_BYTES = 512 * 1024 * 1024


def _json_object(value: str) -> dict:
    try:
        parsed = json.loads(value, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f'invalid JSON: {error}') from error
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError('must be a JSON object')
    return parsed


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def _window(parser) -> None:
    parser.add_argument('--config', type=Path, default=None,
                        help='private category configuration (default: $AGEDS_ACTIVITY_CATEGORIES)')
    parser.add_argument('--from', dest='window_from', required=True, help='YYYY-MM-DD (local midnight) or ISO with offset')
    parser.add_argument('--to', dest='window_to', required=True, help='exclusive end, same forms as --from')
    parser.add_argument('--zone', required=True, help='IANA zone for calendar buckets, e.g. Europe/Warsaw')
    parser.add_argument('--assume-zone', default=None,
                        help='place wall-clock times with unknown zone in this zone (recorded on every item)')
    parser.add_argument('--params', type=_json_object, default=None, help='JSON object of parameter overrides')


def register(subparsers) -> None:
    run = subparsers.add_parser('budget-run', help='time budget with lower/probable/upper bounds (interpretive)')
    _window(run)
    run.add_argument('output', nargs='?', type=Path, default=None, help='optional NEW file for the full result JSON')
    show = subparsers.add_parser('budget-show', help='show a stored time-budget run')
    show.add_argument('run_id', type=_positive)
    show.add_argument('output', nargs='?', type=Path, default=None, help='optional NEW file for the full result JSON')
    listing = subparsers.add_parser('budget-list', help='list stored analysis runs (newest first)')
    listing.add_argument('--kind', choices=('time_budget', 'feature_series'), default=None)
    listing.add_argument('--limit', type=_positive, default=50)
    feat = subparsers.add_parser('features-run', help='feature series over time (deterministic + labelled model series)')
    _window(feat)
    feat.add_argument('output', nargs='?', type=Path, default=None, help='optional NEW file for the full result JSON')
    fshow = subparsers.add_parser('features-show', help='show a stored feature-series run')
    fshow.add_argument('run_id', type=_positive)
    fshow.add_argument('output', nargs='?', type=Path, default=None)
    correct = subparsers.add_parser('label-correct', help='append a human label correction (takes precedence)')
    correct.add_argument('event_id', type=_positive)
    correct.add_argument('--categories', type=_json_object, required=True,
                         help='{"category": weight, ...}; {} withdraws the previous correction')
    correct.add_argument('--note', default='')
    correct.add_argument('--author', default='unknown')
    lshow = subparsers.add_parser('labels-show', help='current label of one event with path and history')
    lshow.add_argument('event_id', type=_positive)
    lshow.add_argument('--config', type=Path, default=None)
    model = subparsers.add_parser('model-label-run', help='record a model-label hypothesis run (provider none/fake only)')
    _window(model)
    model.add_argument('--provider', choices=('none', 'fake'), default='none')
    model.add_argument('--opt-in', action='store_true', help='explicit per-run opt-in for a provider other than none')


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _config(path):
    from ..analysis import categories
    resolved = categories.resolve_config_path(path)
    if resolved is None:
        raise ValueError(f'no category configuration: pass --config PATH or set {categories.ENV_VAR}')
    return categories.load_config(resolved)


def _require_database():
    from ..config import settings
    if not Path(settings.db_path).is_file():
        raise ValueError(f'database not found at {settings.db_path}; set EW_DB_PATH or start the service first')
    from ..db import init_db
    init_db()


def _write(result: dict, output: Path | None) -> str | None:
    if output is None:
        return None
    from ..cli import _write_new_json
    _write_new_json(result, output, max_bytes=MAX_OUTPUT_BYTES)
    return str(output.resolve())


def budget_summary(run: dict | None, result: dict) -> dict:
    return {
        'run': run, 'schema': result['schema'], 'artifact_class': result['artifact_class'],
        'window': result['window'], 'config': {k: result['config'][k] for k in ('sha256', 'version')},
        'labels': result['labels'], 'exclusions': result['exclusions'], 'grid': result['grid'],
        'totals': result['totals'], 'unstable': result['sensitivity']['unstable'],
        'flip_counts': result['sensitivity']['flip_counts'], 'disclaimer': result['disclaimer'],
    }


def handle(args) -> int | None:
    if args.command not in COMMANDS:
        return None
    _require_database()
    from ..analysis import budget, categories, features, model_runs, runs
    from ..db import session
    try:
        if args.command in ('budget-run', 'features-run'):
            config = _config(args.config)
            fn = budget.run_budget if args.command == 'budget-run' else features.run_features
            with session() as db:
                out = fn(db, config, window_from=args.window_from, window_to=args.window_to, zone=args.zone,
                         params=args.params, assume_zone=args.assume_zone)
            written = _write(out['result'], args.output)
            if args.command == 'budget-run':
                summary = budget_summary(out['run'], out['result'])
            else:
                summary = {'run': out['run'], 'schema': out['result']['schema'], 'window': out['result']['window'],
                           'exclusions': out['result']['exclusions'],
                           'series': [{'id': s['id'], 'kind': s['kind'], 'version': s['version']} for s in out['result']['series']]}
            _emit({'command': args.command, **summary, 'output': written, 'source_bytes_written': False})
            return 0
        if args.command in ('budget-show', 'features-show'):
            kind = 'time_budget' if args.command == 'budget-show' else 'feature_series'
            with session() as db:
                run = runs.get_run(db, args.run_id, kind=kind)
            if run is None:
                raise ValueError(f'{kind} run {args.run_id} not found')
            result = run.pop('result')
            written = _write(result, args.output)
            body = budget_summary(None, result) if kind == 'time_budget' else {'series': [s['id'] for s in result['series']]}
            _emit({'command': args.command, 'run': run, **{k: v for k, v in body.items() if k != 'run'}, 'output': written})
            return 0
        if args.command == 'budget-list':
            with session() as db:
                _emit({'command': args.command, 'runs': runs.list_runs(db, kind=args.kind, limit=args.limit)})
            return 0
        if args.command == 'label-correct':
            with session() as db:
                annotation_id = categories.add_annotation(db, args.event_id, args.categories, note=args.note,
                                                          author=args.author)
            _emit({'command': args.command, 'annotation_id': annotation_id, 'event_id': args.event_id,
                   'categories': args.categories, 'path': 'annotation', 'append_only': True})
            return 0
        if args.command == 'labels-show':
            from ..analysis.labels_view import event_labels
            config = _config(args.config)
            with session() as db:
                _emit({'command': args.command, **event_labels(db, args.event_id, config)})
            return 0
        if args.command == 'model-label-run':
            config = _config(args.config)
            with session() as db:
                prepared = budget.prepare_inputs(db, config, window_from=args.window_from, window_to=args.window_to,
                                                 zone=args.zone, params=args.params, assume_zone=args.assume_zone,
                                                 persist=False)
                out = model_runs.run_model(db, prepared['items'], purpose='labels', provider=args.provider,
                                           opt_in=args.opt_in, config=config,
                                           parameters={'window': prepared['window']})
            _emit({'command': args.command, **out, 'note': 'model labels are hypotheses; use them in a budget run '
                   'only through params.model_label_run_id (they never override rules or corrections)'})
            return 0
    except LookupError as exc:
        raise ValueError(str(exc)) from exc
    return None

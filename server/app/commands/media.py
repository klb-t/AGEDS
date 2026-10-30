"""CLI subcommands owned by the media area (one JSON object on stdout, errors on stderr)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

COMMANDS: tuple[str, ...] = ('asr-benchmark', 'asr-recipes', 'media-enqueue', 'media-queue',
                             'transcript-compare', 'media-capabilities')


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def _json_object(value: str) -> dict:
    try:
        parsed = json.loads(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f'invalid JSON: {error}') from error
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError('must be a JSON object')
    return parsed


def register(subparsers) -> None:
    bench = subparsers.add_parser('asr-benchmark', help='WER/CER of ASR recipes on a user-supplied local corpus (no quality claims)')
    bench.add_argument('--manifest', required=True, type=Path, help='ageds.asr_benchmark_manifest JSON')
    bench.add_argument('--output', required=True, type=Path, help='private results directory outside the repository')
    bench.add_argument('--recipe', action='append', dest='recipes', help='recipe id (repeatable); default from manifest or legacy')
    bench.add_argument('--hypotheses', type=Path, help='score precomputed hypotheses instead of running inference')
    bench.add_argument('--max-cells', type=_positive, default=4_000_000, help='alignment matrix budget per comparison')
    recipes = subparsers.add_parser('asr-recipes', help='list recipe presets with effective values and hashes')
    recipes.add_argument('--kind', choices=('asr', 'ocr', 'diarization'), default=None)
    enqueue = subparsers.add_parser('media-enqueue', help='queue a transcription/OCR/diarization job with a pinned recipe')
    enqueue.add_argument('artifact_id', type=_positive)
    enqueue.add_argument('--kind', choices=('transcribe', 'ocr', 'diarize'), default='transcribe')
    enqueue.add_argument('--recipe', default=None)
    enqueue.add_argument('--override', type=_json_object, default=None, help='JSON object merged into the preset')
    enqueue.add_argument('--priority', type=int, default=None)
    enqueue.add_argument('--dedup', choices=('reuse', 'reuse_completed', 'duplicate'), default=None)
    queue = subparsers.add_parser('media-queue', help='list jobs with measured processing-time estimates')
    queue.add_argument('--status', action='append', choices=('queued', 'running', 'done', 'failed', 'cancelled'), default=None)
    queue.add_argument('--limit', type=_positive, default=100)
    compare = subparsers.add_parser('transcript-compare', help='deterministic comparison of two text versions of one artifact')
    compare.add_argument('a', type=_positive)
    compare.add_argument('b', type=_positive)
    compare.add_argument('--normalize', action='store_true', help='apply the benchmark normalization to tokens')
    compare.add_argument('--output', type=Path, default=None, help='new JSON file; existing files are never overwritten')
    subparsers.add_parser('media-capabilities', help='which optional local media engines are usable')


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _require_database():
    from ..config import settings
    if not Path(settings.db_path).is_file():
        raise ValueError(f'database not found at {settings.db_path}; set EW_DB_PATH or start the service first')
    from ..db import init_db
    init_db()


def handle(args) -> int | None:
    if args.command not in COMMANDS:
        return None
    if args.command == 'asr-benchmark':
        from ..config import settings
        from ..media.benchmark import run_benchmark
        result = run_benchmark(args.manifest, args.output, recipes=args.recipes, hypotheses_path=args.hypotheses,
                               max_cells=args.max_cells, store_dir=settings.store_dir)
        summary = {key: value for key, value in result.items() if key != 'result'}
        summary['aggregates'] = {run['label']: run['aggregates']['overall_micro'] for run in result['result']['runs']}
        _emit({'command': args.command, **summary, 'source_bytes_written': False})
        return 0
    if args.command == 'asr-recipes':
        from ..media.recipe import effective_recipe, load_preset, preset_ids
        items = []
        for recipe_id in preset_ids(args.kind):
            kind = load_preset(recipe_id)[0]['kind']
            record = effective_recipe(recipe_id, kind=kind)
            items.append({'id': record['id'], 'kind': kind, 'sha256': record['sha256'], 'effective': record['effective'],
                          'provenance': record['provenance']})
        _emit({'command': args.command, 'recipes': items})
        return 0
    if args.command == 'media-capabilities':
        from ..media.capabilities import media_capabilities
        _emit({'command': args.command, **media_capabilities()})
        return 0
    _require_database()
    if args.command == 'media-enqueue':
        from ..media.queue import enqueue_media_job
        result = enqueue_media_job(args.kind, args.artifact_id, recipe_id=args.recipe, overrides=args.override,
                                   priority=args.priority, dedup=args.dedup)
        _emit({'command': args.command, **result})
        return 0
    if args.command == 'media-queue':
        from ..media.estimates import queue_listing
        statuses = tuple(args.status) if args.status else ('running', 'queued')
        _emit({'command': args.command, **queue_listing(statuses=statuses, limit=min(args.limit, 1000))})
        return 0
    from ..media.comparison import compare_versions
    try:
        result = compare_versions(args.a, args.b, normalize=args.normalize)
    except LookupError as error:
        raise ValueError(str(error)) from error
    if args.output is None:
        _emit({'command': args.command, **result})
        return 0
    from ..cli import _write_new_json
    _write_new_json(result, args.output, max_bytes=64 * 1024 * 1024)
    _emit({'command': args.command, 'output': str(args.output.resolve()), 'content_sha256': result['content_sha256']})
    return 0

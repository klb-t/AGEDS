"""CLI subcommands owned by the workbench area.

Module import stays free of ``config``/``db`` so ``python -m server.app.cli``
keeps its isolated scan path; every handler imports lazily.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

COMMANDS: tuple[str, ...] = (
    'workbench-search', 'workbench-saved-list', 'workbench-saved-run', 'workbench-tag', 'workbench-tag-rename',
    'workbench-tag-merge', 'workbench-folder-list', 'workbench-folder-export', 'workbench-index-check',
    'workbench-theme-report', 'workbench-i18n-check',
)


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def register(subparsers) -> None:
    search = subparsers.add_parser('workbench-search', help='search all evidence layers with an optional layer filter (read-only)')
    search.add_argument('query')
    search.add_argument('--layer', action='append', default=None, help='artifact_metadata|event|transcript|ocr|parsed_text|derived_other|annotation; repeatable')
    search.add_argument('--limit', type=_positive_integer, default=100)
    subparsers.add_parser('workbench-saved-list', help='list saved searches (queries and last-run counts, never results)')
    run = subparsers.add_parser('workbench-saved-run', help='run a saved search now; stores counts only')
    run.add_argument('saved_id', type=_positive_integer)
    tag = subparsers.add_parser('workbench-tag', help='add (or --remove) one tag on many artifacts/events, all-or-nothing, audited')
    tag.add_argument('--tag', required=True)
    tag.add_argument('--remove', action='store_true')
    tag.add_argument('targets', nargs='+', help='artifact:ID or event:ID (a:ID / e:ID)')
    rename = subparsers.add_parser('workbench-tag-rename', help='rename a tag (audited); an existing name requires an explicit merge')
    rename.add_argument('tag_id', type=_positive_integer)
    rename.add_argument('name')
    merge = subparsers.add_parser('workbench-tag-merge', help='merge source tags into a target tag; source tags are deleted, ids audited')
    merge.add_argument('--into', type=_positive_integer, required=True)
    merge.add_argument('sources', nargs='+', type=_positive_integer)
    subparsers.add_parser('workbench-folder-list', help='list working folders as a tree')
    export = subparsers.add_parser('workbench-folder-export', help='write a folder reference list to a NEW file outside source roots and the store')
    export.add_argument('folder_id', type=_positive_integer)
    export.add_argument('output', type=Path)
    export.add_argument('--format', choices=('json', 'csv'), default=None, help='default: from the output suffix, else json')
    export.add_argument('--recursive', action='store_true')
    export.add_argument('--max-output-bytes', type=_positive_integer, default=64 * 1024 * 1024)
    check = subparsers.add_parser('workbench-index-check', help='compare the workbench search index with its sources; --rebuild recomputes it')
    check.add_argument('--rebuild', action='store_true')
    subparsers.add_parser('workbench-theme-report', help='WCAG contrast of the theme text tokens in app.css')
    subparsers.add_parser('workbench-i18n-check', help='completeness of the NL/EN catalogs against Polish')


def _emit(value: dict) -> None:
    from ..cli import _emit as emit
    emit(value)


def _forbidden_output(output: Path) -> str | None:
    from ..config import settings
    target = output.expanduser().resolve()
    for root in settings.scan_roots:
        if target.is_relative_to(Path(root).resolve()):
            return 'output must not be inside a configured source root (originals are read-only)'
    for name, folder in (('store', settings.store_dir),):
        if target.is_relative_to(Path(folder).resolve()):
            return f'output must not be inside the evidence {name}'
    if target == Path(settings.db_path).resolve():
        return 'output must not be the evidence database'
    return None


def handle(args) -> int | None:
    command = getattr(args, 'command', None)
    if command not in COMMANDS:
        return None
    try:
        return _handle(command, args)
    except LookupError as exc:
        # cli.main reports ValueError/RuntimeError as JSON on stderr with exit code 2.
        raise ValueError(str(exc)) from exc


def _handle(command: str, args) -> int:
    if command == 'workbench-theme-report':
        from ..workbench.theme import contrast_report
        _emit({'command': command, **contrast_report()})
        return 0
    if command == 'workbench-i18n-check':
        from ..workbench.i18n import catalog_report
        report = catalog_report()
        _emit({'command': command, **report})
        return 0 if report['complete'] else 1
    from ..db import init_db
    init_db()
    if command == 'workbench-search':
        from ..workbench.layered_search import layered_search_page
        page = layered_search_page(args.query, args.layer, args.limit)
        page.pop('epistemic_legend', None)
        _emit({'command': command, **page})
        return 0
    if command == 'workbench-saved-list':
        from ..workbench.saved import list_saved_searches
        _emit({'command': command, 'items': list_saved_searches(), 'results_stored': False})
        return 0
    if command == 'workbench-saved-run':
        from ..workbench.saved import run_saved_search
        result = run_saved_search(args.saved_id)
        _emit({'command': command, 'saved_search': result['saved_search']['id'], 'counts': result['counts']})
        return 0
    if command == 'workbench-tag':
        from ..workbench.tags import apply_tag
        _emit({'command': command, **apply_tag(args.tag, args.targets, remove=args.remove, channel='cli')})
        return 0
    if command == 'workbench-tag-rename':
        from ..workbench.tags import rename_tag
        _emit({'command': command, **rename_tag(args.tag_id, args.name, channel='cli')})
        return 0
    if command == 'workbench-tag-merge':
        from ..workbench.tags import merge_tags
        _emit({'command': command, **merge_tags(args.sources, args.into, channel='cli')})
        return 0
    if command == 'workbench-folder-list':
        from ..workbench.folders import list_folders
        _emit({'command': command, 'items': list_folders()})
        return 0
    if command == 'workbench-folder-export':
        from ..cli import _write_new_bytes
        from ..workbench.folders import export_folder
        problem = _forbidden_output(args.output)
        if problem:
            raise ValueError(problem)
        fmt = args.format or ('csv' if args.output.suffix.lower() == '.csv' else 'json')
        payload, media_type = export_folder(args.folder_id, fmt, recursive=args.recursive)
        _write_new_bytes(payload, args.output, max_bytes=args.max_output_bytes)
        _emit({'command': command, 'output': str(args.output.resolve()), 'format': fmt, 'media_type': media_type,
               'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(), 'references_only': True,
               'source_bytes_written': False})
        return 0
    from ..workbench.index import index_status, rebuild_index
    if args.rebuild:
        _emit({'command': command, **rebuild_index(channel='cli')})
        return 0
    status = index_status()
    _emit({'command': command, **status})
    return 0 if status['consistent'] else 1

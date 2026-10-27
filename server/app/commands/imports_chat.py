"""CLI for AI chat export importers and the logical dedup report.

``import-chatgpt PATH...`` and ``import-claude PATH...`` take export files or
unpacked export directories (read-only; nothing is written next to them).
``chat-dedup-report`` prints the cross-export report as one JSON object.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

COMMANDS = ('import-chatgpt', 'import-claude', 'chat-dedup-report')
_LIMITS = ('max_file_bytes', 'max_conversation_bytes', 'max_conversations_per_file', 'max_nodes_per_conversation',
           'max_input_files', 'max_walk_depth', 'max_attachment_bytes', 'max_issues_listed')


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def register(subparsers) -> None:
    for name, label in (('import-chatgpt', 'ChatGPT conversations.json / conversations-NNN.json shards'),
                        ('import-claude', 'Claude.ai conversations.json')):
        parser = subparsers.add_parser(name, help=f'import a {label} export (tree, times, logical records)')
        parser.add_argument('paths', nargs='+', type=Path, help='export files and/or unpacked export directories')
        parser.add_argument('--export-label', default=None,
                            help='name of this logical export; default derives from the conversation files\' SHA-256')
        parser.add_argument('--collection', type=_positive, default=None, help='existing collection id')
        parser.add_argument('--mapping', type=Path, default=None, help='alternative key-mapping JSON (new interpretation)')
        for limit in _LIMITS:
            parser.add_argument('--' + limit.replace('_', '-'), type=_positive, default=None)
    report = subparsers.add_parser('chat-dedup-report', help='logical records across exports (presence, variants, branches)')
    report.add_argument('--namespace', default=None, help='chatgpt | claude (default: all)')
    report.add_argument('--conversation', default=None, help='one conversation id')
    report.add_argument('--interpretation', default=None)
    report.add_argument('--changes-only', action='store_true')
    report.add_argument('--limit', type=_positive, default=100)
    report.add_argument('--offset', type=int, default=0)
    report.add_argument('--output', type=Path, default=None, help='write to a NEW file instead of stdout')
    report.add_argument('--max-output-bytes', type=_positive, default=64 * 1024 * 1024)


def handle(args) -> int | None:
    if getattr(args, 'command', None) not in COMMANDS:
        return None
    from ..db import init_db
    init_db()
    if args.command == 'chat-dedup-report':
        from ..records.report import dedup_report
        result = dedup_report(namespace=args.namespace, conversation_id=args.conversation,
                              interpretation=args.interpretation, changes_only=args.changes_only,
                              limit=args.limit, offset=args.offset)
        if args.output is not None:
            from ..cli import _write_new_json
            _write_new_json(result, args.output, max_bytes=args.max_output_bytes)
            print(json.dumps({'command': args.command, 'output': str(args.output.resolve()),
                              'namespaces': [n['namespace'] for n in result['namespaces']]}, ensure_ascii=False))
        else:
            print(json.dumps({'command': args.command, **result}, ensure_ascii=False, allow_nan=False))
        return 0
    from ..importers.chat_common import ChatImportLimits
    limits = ChatImportLimits(**{name: getattr(args, name) for name in _LIMITS if getattr(args, name) is not None})
    if args.command == 'import-chatgpt':
        from ..importers.chatgpt import import_chatgpt_export as run
    else:
        from ..importers.claude_ai import import_claude_export as run
    result = run(args.paths, export_label=args.export_label, collection_id=args.collection,
                 mapping_path=args.mapping, limits=limits)
    print(json.dumps({'command': args.command, 'source_bytes_written': False, **result}, ensure_ascii=False, allow_nan=False))
    if result['coverage'] != 'complete':
        print(json.dumps({'command': args.command, 'warning': 'partial_coverage',
                          'files': [{'file_name': f['file_name'], 'coverage': f['coverage']} for f in result['files']]}),
              file=sys.stderr)
    return 0

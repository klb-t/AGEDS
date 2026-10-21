"""CLI subcommands owned by the integrations area.

Module import stays free of ``config``/``db`` so ``python -m server.app.cli``
keeps its isolated scan path; every handler imports lazily.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

COMMANDS: tuple[str, ...] = (
    'token-create', 'token-list', 'token-revoke', 'read-contract',
    'proposal-list', 'proposal-accept', 'proposal-reject',
    'loom-export', 'loom-import-hypotheses', 'hypothesis-list', 'hypothesis-promote', 'hypothesis-reject',
)


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def register(subparsers) -> None:
    create = subparsers.add_parser('token-create', help='create a local read-API token; the value is printed once and stored only as a salted hash')
    create.add_argument('--label', required=True)
    create.add_argument('--scope', action='append', default=None, help='read (default) and/or propose; repeatable')
    create.add_argument('--client-id', default=None, help='client identity recorded on proposals (e.g. chatadhd)')
    create.add_argument('--expires-in-days', type=_positive_integer, default=None)
    subparsers.add_parser('token-list', help='list tokens without secrets')
    revoke = subparsers.add_parser('token-revoke', help='revoke a token (row is kept)')
    revoke.add_argument('token_id', type=_positive_integer)
    revoke.add_argument('--note', default=None)
    subparsers.add_parser('read-contract', help='print the read API contract descriptor')
    plist = subparsers.add_parser('proposal-list', help='list annotation proposals from clients')
    plist.add_argument('--status', choices=('proposed', 'accepted', 'rejected'), default=None)
    for name, help_text in (('proposal-accept', 'accept a proposal: creates the real annotation with provenance'),
                            ('proposal-reject', 'reject a proposal; the row is kept')):
        parser = subparsers.add_parser(name, help=help_text)
        parser.add_argument('proposal_id', type=_positive_integer)
        parser.add_argument('--by', default='cli', help='who decided (recorded in audit)')
        parser.add_argument('--note', default=None)
    export = subparsers.add_parser('loom-export', help='write a typed graph export (new file only, outside the repository/source tree)')
    export.add_argument('output', type=Path)
    export.add_argument('--from', dest='from_', default=None, help='lower bound over stored ts_start text')
    export.add_argument('--to', default=None, help='upper bound over stored ts_start text')
    export.add_argument('--no-text', action='store_true', help='export hashes of bodies instead of text')
    export.add_argument('--max-nodes', type=_positive_integer, default=None)
    export.add_argument('--max-edges', type=_positive_integer, default=None)
    export.add_argument('--max-output-bytes', type=_positive_integer, default=256 * 1024 * 1024)
    load = subparsers.add_parser('loom-import-hypotheses', help='quarantine relations from a graph tool; never writes links')
    load.add_argument('input', type=Path)
    load.add_argument('--origin', default='loom')
    load.add_argument('--max-input-bytes', type=_positive_integer, default=32 * 1024 * 1024)
    hlist = subparsers.add_parser('hypothesis-list', help='list quarantined/promoted/rejected relation hypotheses')
    hlist.add_argument('--status', choices=('quarantined', 'promoted', 'rejected'), default=None)
    for name, help_text in (('hypothesis-promote', 'promote one hypothesis into links with audit'),
                            ('hypothesis-reject', 'reject one hypothesis; the row is kept')):
        parser = subparsers.add_parser(name, help=help_text)
        parser.add_argument('hypothesis_id', type=_positive_integer)
        parser.add_argument('--by', default='cli')
        parser.add_argument('--note', default=None)


def _emit(value: dict) -> None:
    from ..cli import _emit as emit
    emit(value)


def handle(args) -> int | None:
    command = getattr(args, 'command', None)
    if command not in COMMANDS:
        return None
    from ..db import init_db
    init_db()
    if command == 'token-create':
        from ..integrations.tokens import create_token
        result = create_token(args.label, args.scope or ['read'], client_id=args.client_id, expires_in_days=args.expires_in_days)
        _emit({'command': command, **result, 'stored': 'salted_sha256_only'})
        return 0
    if command == 'token-list':
        from ..integrations.tokens import list_tokens
        _emit({'command': command, 'tokens': list_tokens()})
        return 0
    if command == 'token-revoke':
        from ..integrations.tokens import revoke_token
        _emit({'command': command, **revoke_token(args.token_id, note=args.note)})
        return 0
    if command == 'read-contract':
        from ..integrations.read_api import contract
        _emit({'command': command, **contract()})
        return 0
    if command == 'proposal-list':
        from ..integrations.proposals import list_proposals
        _emit({'command': command, 'proposals': list_proposals(status=args.status)})
        return 0
    if command in ('proposal-accept', 'proposal-reject'):
        from ..integrations.proposals import decide_proposal
        decision = 'accepted' if command == 'proposal-accept' else 'rejected'
        _emit({'command': command, **decide_proposal(args.proposal_id, decision, decided_by=args.by, note=args.note)})
        return 0
    if command == 'loom-export':
        from ..cli import _write_new_json
        from ..integrations.loom_graph import MAX_EDGES, MAX_NODES, export_graph
        graph = export_graph(from_=args.from_, to=args.to, include_text=not args.no_text,
                             max_nodes=args.max_nodes or MAX_NODES, max_edges=args.max_edges or MAX_EDGES)
        _write_new_json(graph, args.output, max_bytes=args.max_output_bytes)
        _emit({'command': command, 'output': str(args.output.resolve()), 'schema': graph['schema'], 'counts': graph['counts'],
               'truncation': graph['truncation'], 'claims_available': graph['claims']['available'], 'source_bytes_written': False})
        return 0
    if command == 'loom-import-hypotheses':
        from ..integrations.loom_graph import import_hypotheses
        size = args.input.stat().st_size
        if size > args.max_input_bytes:
            raise ValueError(f'input exceeds size limit ({size} > {args.max_input_bytes})')
        raw = args.input.read_bytes()
        document = json.loads(raw.decode('utf-8'))
        _emit({'command': command, **import_hypotheses(document, origin=args.origin, file_name=args.input.name, raw_bytes=raw)})
        return 0
    if command == 'hypothesis-list':
        from ..integrations.loom_graph import list_hypotheses
        _emit({'command': command, 'hypotheses': list_hypotheses(status=args.status)})
        return 0
    from ..integrations.loom_graph import decide_hypothesis
    decision = 'promoted' if command == 'hypothesis-promote' else 'rejected'
    _emit({'command': command, **decide_hypothesis(args.hypothesis_id, decision, decided_by=args.by, note=args.note)})
    return 0

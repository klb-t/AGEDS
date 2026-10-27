"""CLI subcommands of the read-only account connectors (docs/GMAIL_API.md).

``gmail-auth-url`` and ``gmail-auth-complete`` run the owner's OAuth consent for
gmail.readonly; ``gmail-import`` lists a query or thread (dry run) and with ``--apply``
acquires each message's raw RFC 822 through the EML importer. Errors go to stderr, exit 2.
"""
from __future__ import annotations

import json
import sys

COMMANDS = ('gmail-auth-url', 'gmail-auth-complete', 'gmail-import')


def register(subparsers) -> None:
    p = subparsers.add_parser('gmail-auth-url', help='owner consent URL for read-only Gmail (step 1 of 2)')
    p.add_argument('--port', type=int, default=8765, help='loopback port named in the redirect (nothing listens)')
    p = subparsers.add_parser('gmail-auth-complete', help='store the read-only token from the redirect address (step 2)')
    p.add_argument('--redirect-url', required=True, help='the full address the browser was sent to after consent')
    p = subparsers.add_parser('gmail-import', help='list (dry run) or acquire messages of a Gmail query or thread')
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--query', help='Gmail search, e.g. "from:uwv.nl after:2026/09/01"')
    group.add_argument('--thread-id')
    p.add_argument('--max-messages', type=int, default=500)
    p.add_argument('--collection-id', type=int)
    p.add_argument('--apply', action='store_true', help='fetch the raw messages and import them')


def handle(args) -> int | None:
    if getattr(args, 'command', None) not in COMMANDS:
        return None
    from ..connectors import gmail
    from ..db import init_db
    try:
        if args.command == 'gmail-auth-url':
            result = gmail.auth_url(args.port)
        elif args.command == 'gmail-auth-complete':
            result = gmail.auth_complete(args.redirect_url)
        else:
            if not 1 <= args.max_messages <= 5000:
                raise gmail.GmailError('--max-messages must be 1..5000')
            init_db()
            result = gmail.import_selection(query=args.query, thread_id=args.thread_id, apply=args.apply,
                                            max_messages=args.max_messages, collection_id=args.collection_id)
    except gmail.GmailError as exc:
        print(json.dumps({'command': args.command, 'error': 'GmailError', 'message': str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({'command': args.command, **result}, ensure_ascii=False, default=str), file=sys.stdout)
    return 0

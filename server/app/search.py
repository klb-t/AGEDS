"""Bounded FTS5 queries with literal syntax, explicit coverage and plain snippets."""
from __future__ import annotations

import sqlite3
from .db import connect

MAX_QUERY_BYTES = 4096
MAX_QUERY_TOKENS = 128
MAX_PAREN_DEPTH = 16
MAX_RESULTS = 200
MAX_VM_STEPS = 2_000_000
PROGRESS_INTERVAL = 1000


class SearchQueryError(ValueError):
    """Query syntax or an explicit input budget is invalid."""


class SearchWorkLimitError(RuntimeError):
    """Query exceeded the live SQLite work budget; no partial result returned."""


def _query_shape(q):
    """Bound a quote-aware lexical shape without rewriting FTS5 syntax.

    Whitespace-separated barewords and quoted phrases each count as a token;
    punctuation operators/column delimiters also count. Doubled quotation marks
    are escapes inside a phrase. Parentheses inside phrases have no nesting role.
    """
    tokens = depth = i = 0
    while i < len(q):
        char = q[i]
        if char.isspace():
            i += 1
            continue
        tokens += 1
        if tokens > MAX_QUERY_TOKENS:
            raise SearchQueryError('query exceeds lexical token limit')
        if char == '"':
            i += 1
            while i < len(q):
                if q[i] == '"':
                    if i + 1 < len(q) and q[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise SearchQueryError('unterminated quoted phrase')
        elif char in '(){}:+*^,-':
            if char == '(':
                depth += 1
                if depth > MAX_PAREN_DEPTH:
                    raise SearchQueryError('query exceeds parenthesis depth limit')
            elif char == ')':
                depth -= 1
                if depth < 0:
                    raise SearchQueryError('unbalanced query parentheses')
            i += 1
        else:
            i += 1
            while i < len(q) and not q[i].isspace() and q[i] not in '"(){}:+*^,-':
                i += 1
    if depth:
        raise SearchQueryError('unbalanced query parentheses')


def validate_query(q, limit=100):
    """Validate original query and limit before any live database access."""
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise SearchQueryError(f'limit must be an integer from 1 to {MAX_RESULTS}')
    if not isinstance(q, str):
        raise SearchQueryError('query must be text')
    try:
        byte_length = len(q.encode('utf-8', errors='strict'))
    except UnicodeError as error:
        raise SearchQueryError('query must be valid UTF-8 text') from error
    if byte_length > MAX_QUERY_BYTES:
        raise SearchQueryError('query exceeds UTF-8 byte limit')
    if '\x00' in q:
        raise SearchQueryError('query must not contain NUL')
    _query_shape(q)
    if not q.strip():
        return
    # The same SQLite FTS5 parser validates operators/column filters against an
    # empty isolated index. No user syntax is interpolated into SQL. Failures to
    # create the parser database or FTS module remain operational failures.
    validation = sqlite3.connect(':memory:')
    try:
        validation.execute('''CREATE VIRTUAL TABLE search_fts USING fts5(
            object_kind UNINDEXED, object_id UNINDEXED, title, content,
            tokenize='unicode61 remove_diacritics 2')''')
        try:
            validation.execute('SELECT rowid FROM search_fts WHERE search_fts MATCH ?', (q,)).fetchall()
        except sqlite3.OperationalError as error:
            message = str(error)
            if (message.startswith(('fts5: syntax error', 'no such column:', 'expected integer, got'))
                    or message in ('unterminated string', 'fts5: parser stack overflow')):
                raise SearchQueryError('invalid FTS5 query syntax: ' + message) from error
            raise
    finally:
        validation.close()


def search_page(q: str, limit: int = 100):
    """Return bounded ranked results and explicit result coverage metadata.

    ``complete`` means this MATCH result fits the requested limit, not that the
    index covers every source. Work-budget interruption never returns a partial
    result. The supplied query is retained unchanged.
    """
    validate_query(q, limit)
    if not q.strip():
        return {'query': q, 'results': [], 'limit': limit, 'has_more': False, 'complete': True}
    steps = 0
    exhausted = False
    def progress():
        nonlocal steps, exhausted
        steps += PROGRESS_INTERVAL
        exhausted = steps >= MAX_VM_STEPS
        return int(exhausted)
    db = connect(read_only=True)
    try:
        db.set_progress_handler(progress, PROGRESS_INTERVAL)
        try:
            # Snippets contain untrusted source text. Keep them plain so HTML
            # clients can autoescape the entire fragment, including raw markup.
            rows = db.execute("""SELECT object_kind,object_id,title,
                snippet(search_fts,3,'','','…',24) AS snippet,bm25(search_fts) AS rank
                FROM search_fts WHERE search_fts MATCH ? ORDER BY rank,rowid LIMIT ?""", (q, limit + 1)).fetchall()
        except sqlite3.OperationalError as error:
            if exhausted and getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
                raise SearchWorkLimitError('search exceeded SQLite work budget; simplify the query or retry after narrowing the index') from error
            raise
    finally:
        db.set_progress_handler(None, 0)
        db.close()
    has_more = len(rows) > limit
    return {'query': q, 'results': [dict(row) for row in rows[:limit]], 'limit': limit,
            'has_more': has_more, 'complete': not has_more}


def search(q: str, limit: int = 100):
    """Compatibility list API; use search_page when reporting result coverage."""
    return search_page(q, limit)['results']

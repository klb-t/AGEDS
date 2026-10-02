# Bounded literal FTS search

`server/app/search.py` keeps SQLite FTS5 query syntax and the original query text.
It does not quote, escape, normalize, strip, repair or silently rewrite the user's
query. SQLite parameters keep query text separate from SQL. Existing snippets
remain plain source text with no generated HTML highlighting; HTML consumers must
continue escaping both snippets and titles.

The Python APIs are:

- `search(q, limit=100)` returns the compatible list of result objects.
- `search_page(q, limit=100)` returns `query`, `results`, `limit`, `has_more` and
  `complete`. It fetches one extra match to determine whether results were cut
  off. `complete` means that the current MATCH result fits this limit; it does
  not mean every source is indexed or that the indexed material is complete.
- `validate_query(q, limit=100)` checks input without accessing the live database.
- `SearchQueryError(ValueError)` identifies invalid input syntax/type/limits.
- `SearchWorkLimitError(RuntimeError)` identifies live query work exhaustion.
  No partial result is returned as successful search.

The original query is preserved in page metadata. Whitespace-only queries produce
an empty, complete page without opening the live database, but type, byte and
limit validation still apply. Limits are:

| Input/work | Bound |
|---|---:|
| UTF-8 query bytes | 4,096 |
| Lexical tokens | 128 |
| Parenthesis nesting outside quoted phrases | 16 |
| Requested result count | 1–200, integer excluding booleans |
| Live SQLite VM instruction budget | 2,000,000 |
| SQLite progress callback interval | 1,000 instructions |

The lightweight lexer counts barewords, quoted phrases and punctuation operators;
it does not pretend to implement the FTS grammar. Parentheses inside quoted text
do not affect nesting. Doubled quotes within phrases are preserved as FTS escapes.
Phrase contents remain bounded by the UTF-8 byte budget. Invalid UTF-8 surrogate
text and NUL are rejected. Over-budget input is rejected, never truncated.

Actual syntax validation uses the installed SQLite FTS5 parser in an empty isolated
in-memory index with the same columns and tokenizer as `search_fts`. The MATCH
statement is stepped through `fetchall()`, so deferred syntax failures are caught.
Malformed quotation, unknown column filters and malformed operators become typed
input errors. Failures creating that validation database or loading the FTS module
remain operational errors. The original query is then sent unchanged to the live
index through a read-only connection.

Live database failures—including missing database/schema, unavailable index and
unrelated interruption—are not reclassified as bad query syntax. Only interruption
caused by this query's progress handler becomes `SearchWorkLimitError`. Its
connection is closed and later queries use a fresh connection. The deterministic
VM budget limits practical work but is not a wall-clock deadline or a bound on
every native tokenizer operation. It cannot guarantee identical runtime across
index sizes, SQLite builds or machines. Retrying a query on unchanged data may
exhaust the same budget; narrowing the query or index is the useful next action.
Ordering is by rank and row ID for ties, with no mutation of source/index text.

HTTP/HTML integration belongs to the caller: invalid input maps to 422; exhausted
work maps to 503; operational failures retain server-error semantics. A client
using the compatibility list must obtain coverage separately or must not imply
that the list is complete. The current integration keeps the `/api/search` list
body and reports coverage in `X-AGEDS-Search-Limit`, `X-AGEDS-Search-Has-More` and
`X-AGEDS-Search-Complete` headers; the HTML page displays limited-result coverage.

## Synthetic verification

`test_search_validation.py` adds 12 tests; together with the two existing search
rendering tests, 14 tests and 43 subtests pass. They exercise malformed quotes,
unknown columns, deep groups/OR/NEAR, exact UTF-8 byte boundary, surrogate rejection,
quoted parentheses/doubled quotes, supported operators, unchanged Unicode/raw
markup, result-limit boundary and truncation, empty-query validation, missing
read-only database/schema, live operational-error separation, deterministic budget
interruption and recovery, and unrelated interruption. No private corpus was read
and no performance benchmark on a real corpus is claimed.

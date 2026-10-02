# Bounded read pages

`server.app.read_pages.read_page(artifact_id, kind, *, limit=50,
before_id=None, snapshot_max_id=None)` returns descending-ID pages for
`transcripts`, `citations` or `annotations`. Existing unlimited list APIs retain
their separate compatibility contract. This module never writes, migrates,
creates indexes, opens evidence paths or imports tasks.

The envelope has exactly these fields:

```json
{
  "artifactId": 12,
  "items": [],
  "nextBeforeId": null,
  "snapshotMaxId": 0,
  "hasMore": false,
  "limit": 50
}
```

| Kind | Item shape |
|---|---|
| `transcripts` | Existing version-summary fields: `id`, `artifact_id`, `run_id`, `model`, `language`, `created_at`. Full text, segments and raw metadata are not fetched. |
| `citations` | Existing anchor fields, replacing `selector_json` with decoded `selector`, plus existing `validation` and `audio_verification` markers. |
| `annotations` | Existing `id`, `artifactId`, `kind`, `label`, `body`, `startMs`, `endMs`, `createdAt`, `derivedTextId`. |

These are existing read projections, not new evidence semantics. Raw stored
rows remain unchanged. Citation paging strictly checks JSON representation
(duplicate keys, malformed/nonfinite values and invalid Unicode reject it), but
does not re-read entire transcripts or independently certify acoustic alignment.
The inherited validation marker describes the saved anchor contract; use the
independent citation evidence packet verifier for full version/selector checking.
Literal HTML, paths and other text remain data for consumers to render safely.

## Cursor semantics

The first request omits both cursor values. In a single read-only SQLite
transaction it finds the selected artifact/kind's greatest ID, or zero for an
empty collection. It returns only matching rows at or below this
`snapshotMaxId`, descending by ID. `hasMore` is true when an older eligible row
remains; it can be true with fewer than `limit` items because the byte budget
stopped the page. A nonterminal page is nonempty and `nextBeforeId` equals its
last returned item ID. A terminal page has `nextBeforeId: null`.

Continuation supplies **both** `before_id` and the unchanged `snapshot_max_id`.
Rows must be below `before_id` and at or below the snapshot upper bound. IDs are
positive signed 63-bit SQLite integers; booleans and coerced strings are not
accepted by the Python API. Snapshot zero is allowed and remains an empty
selection even if rows were subsequently appended. A nonzero snapshot requires
`before_id <= snapshot_max_id`. `limit` is an integer from 1 through 100.

Cursor integers are bounds, not signed tokens or ownership credentials. A caller
can provide a bound obtained elsewhere, but every query independently filters
by the requested artifact and kind. Authorization remains the route's concern.
An appended row with an ID above the upper bound is excluded until a fresh first
request. This is **not MVCC across requests**: a backfilled lower ID, updates or
deletions may appear/disappear between pages. The server does not fabricate a
stable historical snapshot, total count or completion proof beyond the current
bounded query. Clients should reset the cursor when switching artifact/server or
kind and reject stale asynchronous results using their own request identity.

## Resource and failure contracts

Each selected raw row and each projected JSON item is limited to **1,048,576
bytes**; the complete compact UTF-8 JSON envelope is limited to **2,097,152
bytes**. Only exposed columns count toward the row budget. For example, huge
transcript text does not prevent listing its small version summary. JSON byte
measurement uses `ensure_ascii=False`, `allow_nan=False` and compact separators.
The module checks raw selected-column byte lengths in SQLite before fetching
large values into Python. Candidate metadata is capped at `limit + 1` ID/length
rows. It uses neither OFFSET nor total collection counts.

An oversized next row after some returned items stops the page and retains a
cursor before that row. When the same row is first on continuation, it fails
explicitly; rows are never silently skipped. Projected escaping overhead also
counts. Selector nesting is capped at 32 object/array containers and 100,000
nodes (including dictionary keys). Malformed rows fail instead of being repaired.

SQLite work is capped by a progress handler at approximately 1,000,000 VM
instructions, checked every 1,000 instructions. This makes old databases without
suitable indexes fail bounded instead of permitting an unbounded scan. It is an
instruction budget, not a wall-clock or storage-I/O deadline. Normal schema
initialization owns indexes on artifact/kind/ID; the read module never creates
them. All resource ceilings are deterministic defaults, not corpus estimates.

| Exception | HTTP mapping | Meaning |
|---|---|---|
| `PageInputError` | 422 | Invalid kind, IDs, limit or cursor pairing. |
| `PageNotFound` | 404 | Requested artifact absent, distinct from an empty collection. |
| `PageStoredError` | 409 | Stored data cannot form the declared finite JSON projection. |
| `PageLimitError` | 413 | First row, page envelope, selector or SQLite work exceeds a bound. |
| SQLite access error | Route's unavailable response | Database unavailable; no migration or fallback to unlimited reads. |

The read transaction is rolled back and closed after every outcome; the progress
handler is removed before cleanup. No audit event or source content is modified.

## Verification

`server/tests/test_read_pages.py` covers sparse IDs, concurrent appends, empty
snapshots, exact item shapes, strict input validation, missing artifacts, large
omitted transcript data, deferred oversized rows, page-byte stopping, read-only
SQL, bounded SQLite work and malformed selector preservation. Independent
acceptance tests cover HTTP compatibility, adversarial stored rows and indexed
query plans. Browser/Android pagination and stale-response behavior have separate
owners and receipts; backend pages alone do not establish those client behaviors.

# N31 — independent bounded history-page acceptance

Base/claim: `342520613c2ee0ceff5eb0f527a7c0cf298c00f9`.
Owned additions: `server/tests/test_read_pages_acceptance.py` and this receipt.
The producer and HTTP/index integration were changed by their assigned owners.

## Executed result

```
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:$PWD \
python -m pytest server/tests/test_read_pages*.py -q
26 passed, 73 subtests passed, 3 warnings
```

The independent acceptance suite contributes **18 tests and 60 subtests**.
Producer-owner tests contribute eight tests and 13 subtests. Warnings are the
existing Starlette/AnyIO and FastAPI startup deprecations. No open acceptance
blocker was found. This is a local working-tree result, not a deployment claim.

## Contract and evidence

All three routes — transcripts/page, citations/page and annotations/page —
return the envelope `artifactId`, `items`, `nextBeforeId`, `snapshotMaxId`,
`hasMore`, `limit`. Tests use synthetic sparse IDs `7`, `9007199254740993` and
`9223372036854775803`, including values beyond JavaScript's exact integer range.
Python API and HTTP JSON round trips preserve these integers exactly.

- Descending pages neither duplicate nor omit IDs, including sparse IDs and
  byte-limited pages. The continuation is the last returned ID only when more
  rows exist. Every query remains scoped to the requested artifact and kind,
  including forged cursor bounds copied from another artifact.
- A newer append above the captured maximum stays outside an existing page
  walk but appears in a fresh walk. An explicitly empty snapshot remains empty
  after a later append. Missing artifact is 404; an existing empty artifact
  has snapshot zero and an empty complete result.
- Invalid limits, cursor pairing/bounds, SQL integer overflow and noninteger
  values yield 422. Direct producer inputs reject booleans and floats rather
  than treating them as IDs. Legacy list bodies and each corresponding page's
  valid item schema/content remain compatible.
- Exact Unicode, combining marks, newlines and source-looking HTML are retained.
  The tests compare citation and annotation payloads without normalization.
- Each page uses a read-only connection. Database bytes remain unchanged and
  patched file-opening APIs prohibit source-locator reads. A missing database
  gives 503 without creating it or exposing its filesystem path.
- An oversized next row shortens the current page with an explicit cursor; the
  next request receives 413 when that row is first. It is never skipped. SQL
  tracing guards prove that an oversized first row's TEXT projection is not
  fetched into Python after scalar byte preflight.
- Huge transcript text, segment JSON and metadata do not enter transcript
  summary pages; only exposed summary columns are selected. Projected JSON
  escape expansion cannot bypass the per-row cap. Compact UTF-8 envelope bytes
  stay within the configured page cap across successive pages.
- Invalid, duplicate-key, nonfinite and deeply nested citation selector JSON
  fails explicitly. Malformed later rows fail the whole page instead of returning
  a misleading partial success. Work-budget exhaustion raises the explicit page
  limit error without writes.

## Query-plan review

Independent `EXPLAIN QUERY PLAN` on the synthetic database observed:

| Read model | Continuation query plan |
|---|---|
| Transcripts | `SEARCH derived_text USING COVERING INDEX idx_derived_artifact_kind_id (artifact_id=? AND kind=? AND id<?)` |
| Citations | `SEARCH evidence_anchors USING COVERING INDEX idx_anchors_artifact_id (artifact_id=? AND id<?)` |
| Annotations | `SEARCH annotations USING COVERING INDEX idx_annotations_artifact_id (artifact_id=? AND id<?)` |

These indexes are created by initialization, not the read-page operation. Source
review confirms the producer fetches at most `limit + 1` scalar candidate IDs and
byte counts before bounded projection fetches, in one read transaction. HTTP
mapping distinguishes 422 input, 404 missing artifact, 409 malformed stored
metadata, 413 page/work limits and 503 unavailable database.

## Scope and limits

The profile defaults to 50 items, caps requested count at 100, caps raw selected
row bytes and projected item bytes at 1 MiB, and caps the compact UTF-8 envelope
at 2 MiB. Selector structure has depth/node caps; SQLite work has its own bounded
progress-handler budget. SQL byte-count preflight prevents huge selected TEXT
materialization in Python; it does not promise constant-time SQLite scanning of
stored values, a process-wide memory cap or a wall-clock deadline.

`snapshotMaxId` is an ID upper bound, not a persisted snapshot or authorization
token. Later backfills below it, annotation edits and deletions between requests
are not frozen. Cursors must be carried with their original artifact/kind by
clients; artifact filtering alone does not establish cursor provenance. This
receipt tests Python/HTTP delivery, not browser number precision or Android page
chain behavior, which have separate owners and acceptance.

Legacy list endpoints remain available and were not made bounded by this
compatibility extension. No private corpus, source media, browser runtime,
external adapter or model was accessed by these tests. No production files or
commits were changed by this independent reviewer.

## Final HTML and browser review

Independently reviewed the coordinator's final artifact HTML contexts and the
new real-browser fixture/runner without repeating the API suite. The artifact
route obtains transcript/citation/annotation histories through bounded pages.
It no longer fetches a full list of transcript text. Other derived-text previews
select at most 51 rows with `substr(text,1,4096)` before displaying 50; event
previews select 101 rows with body/subject substrings of 4096 characters before
displaying 100. Template notices identify these as limited previews, disclose
additional rows, and label truncated derived text. These are count/content
bounds, not a claimed global byte cap over all artifact metadata fields.

The browser's end marker is explicitly scoped to the current snapshot, and its
per-view cap has a separate visible message. Full transcript text is fetched for
the specifically selected version, rather than embedded for every listed version.
No misleading assertion of complete underlying history was found in this review.

The coordinator's expanded synthetic fixture creates 125 versions, citations and
annotations. New Chromium cases load 50 → 100 → 125 rows, retain a deliberately
changed selected version while an older-version page is delayed, expose the
oldest full selected transcript, and reject a modified snapshot response without
appending rows. Citation IDs are checked for duplicates and literal source HTML
remains inert. The reviewer read `docs/WAVE5_BROWSER_RECEIPT.json`: it records
20 passing cases and the expected 125/125/125 page counts. At that review instant,
`citations.mjs` had changed since the run while other recorded hashes matched;
the coordinator was notified to refresh the final browser receipt after freeze.
HTML/fixture source review itself is accepted with no functional blocker.

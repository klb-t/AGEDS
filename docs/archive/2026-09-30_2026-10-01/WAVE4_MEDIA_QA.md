# N24 — independent verified-media acceptance

Base/claim: `d7f03661ca7177ac2bfef549b71b32fdae77d2d0`.
Independent scope: `server/tests/test_verified_media_acceptance.py` and this receipt.
Production module and HTTP integration were changed by their assigned owners.

## Observed defect and result

Before the change, an independent deterministic experiment wrapped the endpoint's
`sha256_file(path)`: after hashing the original synthetic file, it atomically
replaced that pathname with different bytes before returning the original digest.
The HTTP result was **200 with `UNVERIFIED CHANGE`**, proving that hashing a path
and subsequently handing the path to FileResponse could serve unverified bytes.

After integration, the corresponding HTTP regression prepares the verified
response, replaces the pathname before streaming, and receives only the original
verified bytes from the retained descriptor. Direct stream races likewise pass.

Executed:

```
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:$PWD \
python -m pytest server/tests/test_verified_media_acceptance.py server/tests/test_verified_media.py -q
28 passed, 32 subtests passed, 3 warnings
```

The independent suite contributes **20 tests and 17 subtests**; owner tests
contribute eight tests and 15 subtests. Warnings are existing Starlette/AnyIO and
FastAPI startup deprecations. This is a working-tree receipt, not a publication
or remote deployment claim.

## Independently verified behavior

- Full HTTP GET returns exact original bytes, content length, byte-range support
  and correctly encoded Unicode attachment filename. HEAD returns no body and
  ignores Range while preserving full content length.
- Closed, open-ended, suffix and clamped ranges return the exact requested bytes
  and 206 headers. Empty media has a 200 empty body; its range is unsatisfiable.
  Invalid/multiple/negative-zero/overflowing-start ranges receive 416 and
  `Content-Range: bytes */size`. Matching strong If-Range uses 206; mismatched and
  weak validators fall back to the complete representation. Responses are no-store.
- Replacing a pathname after preparation cannot redirect the stream. Changing
  the same inode before serving raises an integrity error before any changed
  bytes are yielded. Mutating a later chunk aborts after an already verified
  prefix. Truncation and append also abort. A tiny range checks the entire
  covering chunk; mutation outside the requested slice is still detected.
- Short descriptor reads are reassembled; requested allocations never exceed
  the configured 256 KiB chunk size. A boundary-spanning range remains exact.
- A parent-directory swap after its descriptor opens cannot redirect the final
  file open. A symlink substituted before parent open is rejected before any
  `pread`, even if the outside file has the expected content hash. Final symlinks,
  FIFOs and directories are rejected without hanging.
- The initial review identified that final-component O_NOFOLLOW alone would not
  close a parent-directory race. The owner implemented no-follow descriptor
  traversal for the configured root and relative components before these tests.
- Failed sends close the owned descriptor. A real ASGI disconnect while an
  intentionally held worker read is active waits for that read before closing
  the descriptor; no closed-descriptor race occurs. Repeated failed verification
  and HEAD preparation do not leak `/proc/self/fd` entries.
- Corrupt source bytes give HTTP 409 without modifying the source or database.
  The configured serving cap gives 413; outside-store paths give 409 before
  source reads. Chunk-count and expected-size limits fail before streaming.

## Scope and remaining limits

All files and database rows are synthetic and temporary. No private corpus,
network source, model, media conversion, production media copy or commit was
performed. Implementation review confirms read-only descriptor operations and
an in-memory chunk-digest table, not temporary copies of media.

The profile permits at most 4 GiB, with 256 KiB chunks and at most 16,384 digests
(512 KiB of digest bytes). It initially hashes the full file on each request,
including HEAD and ranges, then checks each covering chunk before yielding its
buffered bytes. This costs full-file read latency and is not a shared cache,
immutable filesystem, signature or attribution/truth guarantee.

A mutation discovered after response headers or verified earlier chunks have
already been sent aborts the connection; it cannot retroactively replace HTTP
status with 409 or revoke already delivered original bytes. These tests prove
that altered chunks are not yielded, not that callers always receive a complete
response. The no-follow path profile requires the supported POSIX descriptor
APIs and deliberately rejects symlink components. Cancellation testing exercises
ASGI disconnect and failed sends, not abrupt process termination.

Browser playback under this new endpoint is a separate acceptance step; this
receipt covers deterministic response/descriptor races and actual local HTTP
through TestClient. Earlier browser receipts apply to their recorded source hashes.

## Final integration review

Independently reviewed the final HTTP media and search wiring plus
`server/tests/test_search_http_contract.py`. Media passes the configured store
root, Range/If-Range and HEAD intent directly to descriptor preparation, maps
serving limits to 413 and typed unavailable/integrity failures to 409. Search
keeps the legacy list response while exposing limit/has-more/complete headers;
invalid queries return 422, exhausted search work returns 503 without partial
success, and HTML displays escaped errors and explicit truncation.

The coordinator subsequently ran the actual Chromium suite with **17 passing
cases**. This reviewer independently read `docs/WAVE4_BROWSER_RECEIPT.json` and
confirmed every recorded source hash still matched the working tree. This adds
browser coverage for the new media endpoint beyond the earlier receipt.

One final malformed-metadata edge was referred to the media owner: an embedded
NUL in `stored_path` could raise an untyped ValueError from `os.open`, escaping the
route's intended unavailable-content 409 mapping. The owner now rejects NUL in
both stored path and configured root with `MediaUnavailable` before any file
open, and reports a passing two-case regression. This reviewer inspected that
normalization; no integration blocker remains. Its changed module hash requires
a refreshed browser receipt for final publication.

A full-suite run also exposed a pre-existing initialization connection-lifecycle
issue: SQLite's connection context manager commits or rolls back but does not
close its connection. Delayed garbage collection could checkpoint WAL into the
main file during a later read-only test. The coordinator owns the explicit-close
fix in `init_db`. The HTTP/CLI exchange regression now takes its physical-byte
baseline after application startup and checks all exports while that client
lifecycle remains active. Startup migrations/checkpoints are thereby excluded
from the export-only measurement; the independent producer read-only assertion
and exact HTTP/CLI/direct-export comparisons remain unchanged.

After the explicit-close initialization fix and lifecycle correction, the focused
exchange-acceptance plus search-HTTP suites passed **22 tests and 30 subtests**
with the same three pre-existing warnings. Search status/coverage behavior and
media error mapping are accepted in their documented scope.

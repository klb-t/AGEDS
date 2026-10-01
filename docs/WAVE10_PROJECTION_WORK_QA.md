# Wave 10 — independent archive projection work acceptance

N66 adds `server/tests/test_archive_projection_work.py` (11 tests) against the actual package validator, citation projection functions, and inert archive import/read/export paths. Fixtures contain four synthetic single-character words and one to three pinned anchors; no source corpus, dependencies, or production files were changed.

## Observed results

Command from repository root:

```sh
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:$PWD python -m unittest server.tests.test_archive_projection_work server.tests.test_metadata_archive server.tests.test_packages -q
```

Observed: **48 tests passed**, zero failures/errors, in 0.888 seconds. This includes all 11 new independent tests and the existing archive/package suites. Elapsed time is recorded only as a receipt; assertions measure calls and deterministic work charges.

Three anchors against one decoded transcript perform three actual `validated_segment_words` calls but only one stored transcript decode. The decoded cache therefore does not itself eliminate repeated word projection work. For the fixture, each projection costs 16 visits: one anchor, one reference, one segment, four words, four raw segment characters, four raw word characters, and one selected character. A cap of 31 admits one expensive validation and refuses the next before its call; a cap of 15 admits none. The counter includes the refused precharge (32 in the former case); it is not a count of completed expensive operations.

Exact-cap calls succeed. Fresh validation invocations reset accounting; different pinned versions share the current invocation's work budget. A noncanonical selector still consumes its attempted projection allowance, so invalid anchors cannot bypass accounting. Segment selectors preserve legacy access when unused word timestamps are malformed; word selectors reject those timestamps.

Archive import rejects exhausted validation before creating an output directory. Import's initial validation and inert roundtrip validation each receive a fresh budget. Reading and exporting an existing archive enforce the same projection cap; rejected export creates no output and leaves archive bytes unchanged.

## Scope

These tests exercise production Python functions and SQLite archive roundtrips, using wrappers around real decoding/word-validation functions rather than replacement parsers. They establish deterministic projection-work admission and observable output behavior, not a process-memory, wall-clock, operating-system I/O, or source-authenticity guarantee. Root owns broader integration and publication receipts.

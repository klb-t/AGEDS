# N60 — real scan-engine result to bounded cache

**Five host JVM tests passed** in
`androidApp/src/test/java/dev/klbt/ageds/SourceScanEngineCacheFlowTest.kt`.
They execute the new production `SourceScanEngine`, its actual CSV and WAV
parsers/readers, `SourceScanResult` serialization, the production
`BoundedMetadataCache` writer/reader, and typed cache decoding. Unlike the earlier
serialization-only fixtures, the cached snapshots in these tests are returned by
real engine runs rather than manually assembled scan-result objects.

## Executed paths

| Test | End-to-end evidence |
|---|---|
| Mixed scan → cache → typed readback | Two CSV files with the same name in distinct synthetic directories retain separate provider URIs, paths, collision locators, hashes and exact raw quoted Unicode cells. WAV declarations, raw inventory identity, schema version, scan timestamp, byte accounting and limits survive typed roundtrip. |
| Larger real scan exceeds configured cache cap | A real one-file scan is published first. A subsequent real four-file scan serializes to more bytes than that cache's configured cap. Its write throws, the publication-success flag remains false, and the first snapshot's exact stored bytes and typed timestamp/result remain available. |
| Canceled publication of second real snapshot | Both scans finish, but cancellation at the second write's final publication check preserves the first cached snapshot and removes the temporary metadata file. Finishing a scan is not treated as successful cache publication. |
| Malformed source → partial result → cache | A real invalid-UTF-8 CSV produces the parser's `unsupported_text_encoding` diagnostic, empty rows, partial coverage and a hash of the bytes actually read. Cache readback retains the failure and does not fabricate a successful table. |
| Header-only source read → cache | A real WAV scan with a 64-byte file/total budget retains declared duration and header observations, but has no complete-file hash, no EOF claim, no body validation and explicit byte-limit/partial coverage after cache restoration. |

Every fixture uses real temporary source files and read-only `FileInputStream`s
behind a deterministic `SourceScanProvider`. The provider has fixed lazy directory
cursors and literal `content://cache-flow/...` identities; those URIs are not
resolved through Android. Opened streams are checked against closed streams.
Source files are compared byte-for-byte after scanning and cache publication;
the set of source-directory files is also unchanged.

The cache destination is a separate `metadata` temporary directory, outside the
source directory. Only `source-scan.json` remains there after success or failure.
The mixed-flow check rejects both a raw audio sentinel and a base64 copy of the
synthetic WAV in the stored cache. CSV lexical fields intentionally remain as
metadata; audio and opaque inventory bodies are not copied into cache.

## Bounds and meaning

The overflow path uses the task's permitted **smaller configured cap**, calibrated
to the encoded size of the first actual engine result. It is not presented as a
new test involving a greater-than-4-MiB real scan result. The production cache's
4 MiB UTF-8 overflow behavior was checked separately in wave 7. This test shows
that a newly completed engine result cannot bypass the same bounded writer or
silently replace a previously published snapshot when it does not fit.

Typed equality preserves scan fields and raw cell values, not cache JSON byte
formatting across serialization. A saved header-derived duration remains a header
declaration; cache restoration does not add acoustic validation, authenticity or
a complete-file hash. `scannedAt` values are caller-supplied fixture strings and
survive unchanged. Saving a historical result does not imply that its provider
URIs still exist or that sources remain unchanged in a later real session.

## Execution and limits

The cached Kotlin 2.4.20 compiler/serialization plugin, JDK 21,
kotlinx.serialization 1.11.0 and JUnit 4.13.2 compiled the actual engine, source
models, CSV/workbook/XLS dispatch dependencies, WAV reader/probe, bounded cache
and this test class into an isolated temporary output directory. JUnitCore returned
`OK (5 tests)`. No Gradle, downloads or new dependencies were used by this task.
The coordinator's integrated build is separate evidence.

These tests do **not** instantiate the Android `Context` wrapper or exercise
`DocumentsContract`, SAF grants, a real provider process, provider permission
revocation, device lifecycle, a remote filesystem or physical-device runtime.
The actual orchestration runs against an independent deterministic provider whose
byte streams are local synthetic files. No private corpus was read, no production
source was edited, and no commit or push was made by this task.

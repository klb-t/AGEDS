# Host-testable source scan orchestration

`SourceScanEngine` contains the production traversal, byte-accounting and parser
orchestration formerly embedded in the Android `SourceScanner`. The SAF adapter
now supplies Android document access to this same engine. Host-JVM tests execute
the engine and actual CSV/TSV/XLSX/XLS/WAV parsing paths; they do not duplicate a
simplified scan algorithm. This narrow execution seam enables useful acceptance
while no SAF provider/device runtime is available. It is not a universal storage
platform or a claim that host tests exercise Android permissions and providers.

## Read-only seam

All types are in `dev.klbt.ageds`, with no `android.*` import in the engine:

```kotlin
data class SourceScanDocument(
    val id: String, val name: String, val mime: String?, val sizeBytes: Long?
)
interface SourceScanCursor : java.io.Closeable {
    fun next(): SourceScanDocument? // null means EOF
}
interface SourceScanProvider {
    val rootId: String
    val rootUri: String
    fun uri(id: String): String
    fun children(id: String): SourceScanCursor
    fun openRead(id: String): java.io.InputStream
}
SourceScanEngine(provider).scan(limits, scannedAt, checkCancelled)
```

`children` is lazy and closeable, never an unbounded returned list. Implementations
must avoid eagerly materializing a provider tree. `openRead` returns an owned
read-only stream; a missing stream is an exception. The engine owns cursor and
stream `use` scopes. The seam has no write, copy, delete, rename or grant method.
IDs are opaque identity; names and relative paths remain provider observations.
`SOURCE_DIRECTORY_MIME` retains the Android document-directory MIME string.

The engine is synchronous. The Android adapter executes it on `Dispatchers.IO`
and passes coroutine cancellation checks. The caller supplies `scannedAt` as
literal metadata; the engine does not invent an execution timestamp. Root ID,
root URI and document URI construction errors propagate, as they did before;
ordinary enumeration errors become `directory_unreadable`, and ordinary file
read/parse errors become `read_failed`. Cancellation exceptions escape rather
than produce a false completed result. Cursor/input close failures retain the
same exception policy, with `use` ensuring cleanup attempts.

## Preserved policy and one explicit boundary tightening

The engine retains breadth-first traversal, visited-ID deduplication/cycle
avoidance, distinct colliding names, directory/depth/file caps and existing
`SourceScanLimits`. It retains the one-million-character accumulated locator
budget and global retained-result limits of 10,000 rows, 50,000 cells and four
million text characters. It calls the real `SourceDelimitedParser`,
`SourceWorkbookParser`, `SourceXlsParser` and `SourceWavReader` without parser
changes. Output remains the existing `SourceScanResult`/`ScannedSourceFile`
schema, including raw cells, format hypotheses, WAV header provenance and issues.

One deliberate entry-boundary tightening is necessary because `next()` now
materializes a document observation. Once the global discovered-entry budget
(`maxFiles + maxDirectories`) is reached, the engine checks it **before** calling
`next()` again. It reports `entry_limit` and closes the cursor without reading
extra metadata. At an exact boundary, EOF is therefore unknown and coverage is
conservatively partial, even if the next cursor operation would have found EOF.
This avoids reading oversized or invalid metadata beyond an accepted budget.

The existing non-WAV content loop still uses one extra byte to distinguish an
exact cap from a larger stream. That byte is counted in `bytesRead` and never
used to claim a complete-file hash. Consequently a non-WAV truncated read may
account for `cap + 1` bytes, including one beyond a remaining total budget. This
is preserved legacy policy, not a new assertion of a strict zero-overread cap.
WAV uses its separately tested strict `SourceWavReader` policy and does not adopt
this extra-byte convention. A complete hash requires observed EOF; parser
coverage and full-byte hash scope remain separate.

Mutable or remote providers do not offer a stable tree snapshot through this
seam. Repeated IDs are not traversed twice, but this is not a filesystem replay
or proof that all provider entries were enumerated. Invalid provider metadata
and resource exhaustion remain explicit issues; source observations are not
corrected or conflated with stored evidence acquisitions.

## Verification scope

`SourceScanEngineTest` runs the production engine with generated lazy providers
and actual parsers: literal CSV fields and SHA256, supplied scan metadata,
breadth-first order/cycles/name collisions, entry-boundary non-materialization,
known oversized-file refusal, counted non-WAV lookahead and cursor cancellation
cleanup. Independent suites add traversal budgets, lifecycle failures/cancellation,
mixed formats and typed cache roundtrips. No test grants private provider access,
uses source corpus data or writes through the provider seam.

Host-JVM success establishes this orchestration against controlled streams and
cursors. Android compilation checks adapter signatures. SAF provider runtime,
picker/grant lifecycle, mutable provider behavior and physical-device UI remain
separate acceptance boundaries; extracting this seam does not resolve that
runtime limitation by renaming host tests.

# Android source-scan adapter (N56)

`SourceScanner.scan(treeUri, limits)` is the Android binding for the production
`SourceScanEngine`. It runs on `Dispatchers.IO` and passes a coroutine
`ensureActive` check into the engine. Traversal, all processing/retention budgets,
format parsers, hashing, WAV header probing, issue classification and scan-result
assembly live in the engine rather than a second Android-specific policy path.

The provider port exposes only document URI construction, a closeable child
cursor, and read-only stream acquisition. The adapter has no create, write,
delete, rename or move operation.

## Android binding

- The root ID comes from `DocumentsContract.getTreeDocumentId(treeUri)`.
- A document URI is always produced with
  `DocumentsContract.buildDocumentUriUsingTree(treeUri, id)`; names and inferred
  relative paths never substitute for provider document identity.
- Child queries use `buildChildDocumentsUriUsingTree` and the existing four-column
  projection: document ID, display name, MIME type and size. No sort/filter or
  selection is added at the Android boundary.
- `SourceScanCursor.next()` advances the underlying Android cursor one row at a
  time. It does not load a directory into an unbounded list. `close()` delegates
  to the Android cursor; the engine owns closure on completion, budget stops,
  cancellation and row-read failures.
- A null cursor or null content stream fails explicitly. A null document ID
  fails instead of manufacturing identity. IDs are opaque provider strings;
  they are not trimmed, rewritten or derived from names. A null display name
  falls back to that exact ID, matching the previous adapter.
- Null or negative provider size becomes unknown. Other size reads retain the
  platform cursor's conversion behavior; the adapter does not claim to verify
  provider size against the content stream. MIME remains nullable.
- `openRead` calls only `ContentResolver.openInputStream` on the exact document
  URI. The engine owns the returned stream. Permission and provider exceptions
  are handled by the engine's production scan policy.

## Verification boundary

Host tests can exercise the same engine through closeable synthetic provider
cursors and streams, including cancellation and malformed-provider behavior.
They do not execute `ContentResolver`, a real Android cursor, SAF permission
lifecycle, or a mutable remote provider. Android compilation is a separate check;
real device/emulator SAF acceptance remains distinct. The existing six
instrumentation tests were not changed by this extraction.

Document IDs, sizes and names remain provider observations. The interface does
not promise a stable source snapshot or establish identity from a filename/hash.

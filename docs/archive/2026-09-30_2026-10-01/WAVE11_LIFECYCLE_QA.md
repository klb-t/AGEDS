# Wave 11 — independent gate/cache/view-model lifecycle review

N72 reviews the actual `SourceScanPublicationGate`, `BoundedMetadataCache`, `SourceScanCache`, and `CorpusVm` integration. It adds one focused `SourceScanPublicationFailureTest` after coordination with the race-test owner, who covers successful publication/invalidation orderings separately.

## Reviewed production integration

- Tokens use object identity. Another gate's token, an old token, or a separately constructed token cannot equal the current token. Invalidation and new-token issuance use the same monitor as final publication. This is a per-owner gate, not a global or cross-process lock.
- `writeGuarded` serializes, checks UTF-8 size, stages to a unique temporary file, writes/fsyncs/closes, and runs cancellation checkpoints outside the gate. Only `atomicReplace` runs under the gate monitor. The guarded typed wrapper has no `@Synchronized` cache lock, so this path introduces no cache-monitor/gate lock inversion. `finally` attempts removal of staging on stale return, cancellation, or move failure.
- The old `write(text, beforeCommit)` API remains `Unit`-returning with the same trailing-lambda checkpoints and atomic replacement behavior. The typed legacy wrapper remains available and synchronized. That legacy API is not generation-fenced; production `CorpusVm` now calls the guarded API.
- `scanSources` issues its new token before cancelling the old job; explicit cancel and `onCleared` invalidate before job cancellation. A move already inside the gate can finish before invalidation returns; there is no rollback claim.
- UI installation checks the token after scanner IO. Cache IO uses the same token and rethrows `CancellationException` captured by `runCatching`. A second token check and guarded-write Boolean suppress stale completion; error/finally updates also check currency. The subsequent main-thread UI blocks and recording-tree save contain no suspension between their check and update.
- Initial historical-cache restoration occurs while `importing` prevents `scanSources` from starting. Cache-save failure leaves the newly scanned in-memory result visible and labels its persistence failure. Cancellation does not become an ordinary error notice.

No production defect was identified in these reviewed paths. Stale work may still encode or stage before rejection; the gate guarantees publication ordering, not interruption of uncooperative work.

## Targeted regression

`SourceScanPublicationFailureTest` injects an `IOException` immediately before the atomic replacement. It asserts exact exception propagation, unchanged prior cache bytes, no leftover temporary file, and continued token currency. A separate thread then obtains a new token and performs the real atomic `Files.move`, establishing both monitor release after the exception and subsequent cache usability. A bounded latch wait detects failure; elapsed execution time is not the correctness oracle.

N70 executed the combined direct JVM runner `/tmp/ageds-compile-wave11-race-qa.py`: **17 tests passed** in 0.192 seconds, including this regression, four independent race cases, seven existing cache cases, and five gate-owner cases. Classes were emitted to `/tmp/ageds-wave11-race-qa-classes`. N72 did not launch a duplicate compiler; this execution result is attributed to N70.

## Evidence limits

Gate/cache tests execute production JVM classes with real temporary files. `CorpusVm`, Compose state, Android lifecycle dispatch, and real SAF/provider behavior are source-reviewed here, not executed on an Android device. The review assumes the UI entrypoints run on the main thread, as their production Compose call sites do. Root owns serial Android build and overall integration receipts. No production edits, dependencies, Gradle invocation, commits, or Actions were performed by N72.

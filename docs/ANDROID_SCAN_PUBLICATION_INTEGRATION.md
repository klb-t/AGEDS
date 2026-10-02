# N69 — ViewModel integration of guarded scan-cache publication

`CorpusVm` now owns one `SourceScanPublicationGate` for its scan lifecycle. It uses
opaque tokens in place of the earlier independent integer generation check. The
same gate passed to `SourceScanCache.writeGuarded` both invalidates stale tokens
and guards the final atomic file move.

## Lifecycle wiring

- Starting a scan calls `begin()` before cancelling the previous job. That replaces
  its token before an older coroutine can publish a staged cache.
- Explicit cancellation calls `invalidate()` before cancelling the scan job, then
  updates the visible scanning state.
- `onCleared()` invalidates before cancelling the scan job and delegating to the
  superclass. No subsequent move can publish under that invalidated token.
- Existing stale UI guards now query `isCurrent(token)` before installing scan
  results, handling cache outcomes, setting errors or clearing progress.

The VM invokes guarded writing from `Dispatchers.IO`, with the scan coroutine's
`ensureActive()` callback. Encoding, temporary-file writing and fsync are staged
outside the gate. The cache owner places only the final atomic publication move
inside `publishIfCurrent`; the VM does not wrap general I/O or UI updates in that
critical section.

The completed scan remains visible in memory before persistence is attempted.
An ordinary cache failure retains the existing memory-only notice and prior-cache
message. `CancellationException` is explicitly rethrown from the local
`runCatching` result and from the outer scan handler. Cancellation is not presented
as a cache failure. A false guarded-write result means stale publication and does
not update cache notices or persist that obsolete tree selection.

## Exact guarantee and boundary

The gate linearizes invalidation against publication. If invalidation obtains the
shared lock first, the old final move is refused. If a final move already holds
that lock and completes first, cancellation waits and the resulting historical
cache may legitimately remain. Cancellation does **not** retroactively undo an
already completed move. The guarantee is per ViewModel/gate instance; it is not
cross-process cache coordination.

The previous check-then-move gap is closed at the cache publication boundary.
Coroutine cancellation and stale UI checks remain separate protections; neither
is claimed to provide the atomic publication guarantee by itself.

## Acceptance boundary

This task changes `CorpusVm.kt` and this integration report only. Gate and guarded
cache implementation/tests belong to N67/N68; independent lifecycle/source review
belongs to N72. Their JVM race tests exercise the gate/cache primitives, not an
Android ViewModel lifecycle runtime. The coordinator's integrated compilation is
also distinct from executing this ViewModel on a phone or emulator. No device
runtime is available or claimed here, and no new dependencies or CI Actions were used.

Independent N72 source review found no concrete wiring defect. The independent
N70/N72 race/failure suite passed **17 host-JVM tests** for the gate/cache
primitives; it does not establish actual Android ViewModel runtime behavior.
`git diff --check` passed after the VM integration. Production compilation and
final test receipts are maintained by the coordinator and independent owners.

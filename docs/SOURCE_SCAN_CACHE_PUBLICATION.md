# Generation-guarded source-scan cache publication (N68)

`BoundedMetadataCache.writeGuarded(text, gate, token, beforeCommit)` returns true
only after a current scan token publishes its staged metadata. It returns false
when that token was superseded before publication, without replacing the prior
cache. `SourceScanCache.writeGuarded(result, gate, token, beforeCommit)` supplies
the typed JSON serialization and the existing 4 MiB private-cache limit.

Encoding, size checks, temporary-file creation, writing, fsync and cancellation
checkpoints happen outside the publication gate. Each writer has its own temporary
file. Only the final atomic `Files.move(..., ATOMIC_MOVE, REPLACE_EXISTING)` runs
inside `gate.publishIfCurrent(token)`. There is no non-atomic fallback. The
source-scan guarded wrapper holds no cache monitor while acquiring the gate.

Starting a new scan or invalidating a scan token uses the same gate lock as final
publication. Thus invalidation first prevents replacement; publication first is a
completed write and is not rolled back by later cancellation. This closes the
previous gap between a successful `ensureActive` check and an unguarded rename.
The controller must invalidate before canceling its old coroutine; cooperative
cancellation checks alone do not provide this ordering.

The legacy `write(text, beforeCommit)` and typed wrapper remain compatible,
including trailing-lambda calls and both existing cancellation checkpoints.
Legacy writes do not gain generation fencing automatically. The source-scan
controller uses the new guarded method.

I/O, encoding-limit and cancellation exceptions propagate. Temporary files are
removed in `finally` after success, stale-token rejection or failure. An atomic
replacement failure keeps the prior cache; a successful replacement remains
published. An internal constructor callback for atomic replacement allows host
tests to pause or fail the actual commit boundary. Its production default is the
same atomic filesystem operation as before.

This is app-private metadata publication, not a source write or a stable SAF
snapshot. Writers must share the appropriate gate for their controller scope;
the gate is not a cross-process or global filesystem lock. Historical cached
results retain their existing provenance and availability caveats.

## Validation

`GuardedMetadataCacheTest` adds six owner cases for current/stale tokens,
cancellation cleanup, replacement errors, UTF-8 byte limits and legacy-call
compatibility. Independent tests exercise real atomic replacement with barriers,
both race orderings, gate recovery, typed source-scan cache flows and cleanup.
The independent gate/cache race checkpoint passed 17 tests. The integrated
wave 11 build passed all six owner cases and the full JVM suite: 246 Android
unit tests plus 111 desktop tests, 357 total. It completed 57 Gradle tasks;
88 source hashes remained unchanged and APK signature verification exited 0.
The coordinator records the build in `archive/2026-09-30_2026-10-01/ANDROID_WAVE11_BUILD_RECEIPT.json`.
No Android device/SAF runtime acceptance is implied.

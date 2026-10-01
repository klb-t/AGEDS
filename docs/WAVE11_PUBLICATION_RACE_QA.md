# Wave 11 — independent publication race acceptance

N70, 2026-10-01. Four new deterministic JVM race tests in `SourceScanPublicationRaceAdversarialTest.kt` exercise the actual `BoundedMetadataCache` and `SourceScanPublicationGate` with small synthetic metadata and real atomic filesystem replacement. No production edits, dependencies, downloads, Gradle commands, corpus, or commits were used.

## Result

**4 independent race tests passed.** A focused combined direct Kotlin/JUnit run including N72's `SourceScanPublicationFailureTest`, the unchanged `BoundedMetadataCacheTest`, and the gate owner's `SourceScanPublicationGateTest` passed **17 tests** in 0.192 seconds. The cached Kotlin 2.4.20 compiler and JDK 21 compiled the production gate/cache plus these tests; the test classpath uses existing JUnit 4.13.2 and Hamcrest, without Kotlin test dependencies. The compiler reported only unused `Unit` expression warnings.

The local reproducible command was `python /tmp/ageds-compile-wave11-race-qa.py`, using the existing cached compiler environment. Root owns the full project/Android build verification. No blocker was found in this race scope.

## Controlled orderings

1. **Old check-before-move gap reproduced.** The existing unguarded writer passes its second cancellation check and pauses before returning from that callback. Another thread marks cancellation, then releases the writer. Actual cache replacement still publishes obsolete metadata. This preserves the former failure mode as an executable contrast; it does not assert the legacy unguarded API is race-safe.
2. **Invalidation wins.** A guarded write pauses after staging/fsync and before gate acquisition. Invalidation completes first. The writer returns false, prior cache bytes remain byte-for-byte identical, and staging is removed.
3. **Publication wins.** The injected atomic-replace wrapper pauses while the production publication gate holds its monitor. The invalidator is observed in JVM `BLOCKED` state, with prior cache bytes still present. Releasing the wrapper executes real `Files.move(ATOMIC_MOVE, REPLACE_EXISTING)`, after which invalidation completes. The newly published cache is legitimate historical output even though its token is no longer current.
4. **New token supersedes staged old work.** An old writer pauses outside the gate. A new token successfully publishes newer metadata. Releasing the old writer returns false and cannot overwrite the newer cache.

CountDownLatch barriers select the interleavings. Five-second await/join deadlines bound failures and cleanup; there are no sleep-based scheduling assertions. A bounded yield loop observes the invalidator's monitor-blocked state in the publication-first test. Every worker failure is collected and asserted, every started worker is joined, release latches run in finally blocks, and completed tests assert no staging-file residue.

N72 separately contributes the combined move-exception test: exception identity, unchanged prior bytes, staging cleanup, and successful publication from a new thread after failure demonstrate monitor release. Typed result serialization and ViewModel lifecycle ordering have separate owners; this receipt makes no device/SAF or crash-durability claim.

## Tested hashes

- `androidApp/src/main/java/dev/klbt/ageds/SourceScanPublicationGate.kt`: `63df8e06bc439bd004035597b7ef91b324003ddb12d3249d16bc5d8c3e3ccaa6`
- `androidApp/src/main/java/dev/klbt/ageds/BoundedMetadataCache.kt`: `42166153d4eccf433a953435dd64460c6cb3487d6753a136694a9e186f438603`
- `androidApp/src/test/java/dev/klbt/ageds/SourceScanPublicationRaceAdversarialTest.kt`: `3c255e31f06560799781dd90c4aaeb2b35bb8a2b85d6bc78ae1ee7f286e59a10`

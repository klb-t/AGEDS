# N71 — typed scan results through guarded cache publication

`androidApp/src/test/java/dev/klbt/ageds/GuardedScanCacheFlowTest.kt` adds four
host JVM integration cases. Each uses the actual `SourceScanEngine` and real CSV/
WAV processing to produce `SourceScanResult`, the production generation gate and
`BoundedMetadataCache`, and typed JSON cache readback. These are deterministic
publication-boundary tests; N70 owns the separate concurrent latch/race tests.

## Acceptance cases

| Case | Required result |
|---|---|
| Token superseded after staging | The second precommit callback observes a nonempty staged file while the old cache still has its original bytes, then starts a fresh generation. The stale write returns false, does not replace the old snapshot, and removes its temporary file. The fresh token subsequently publishes another actual scan result. |
| Oversized actual scan result | A larger real CSV/WAV scan exceeds a cache cap calibrated to a prior real result. The guarded write throws, cannot report success and preserves the old file exactly. A later fresh small result remains publishable and readable. |
| Cancellation after staging | A cancellation exception at the final precommit callback preserves the prior typed snapshot and cleans the staged file. A fresh generation then writes successfully. |
| Atomic replacement failure | An injected replacement function observes the staged file and original destination, then throws an I/O exception before replacement. The error propagates, old bytes and typed result remain unchanged, and temporary metadata is removed. Recovery uses the production atomic move with a fresh token. |

Every success or rejected/failed write is followed by typed cache inspection and
verification that only the published `scan.json` remains in the metadata
directory. Success is checked from `writeGuarded`'s actual boolean result; scan
completion alone is not treated as cache publication. The stale-after-staging
case specifically exercises the final guarded decision rather than a convenient
early stale check.

The synthetic source provider returns real read-only file streams for a CSV and a
small PCM WAV. Source bytes and file membership are compared after each case;
opened/closed stream counts must agree. The stored cache must not contain the raw
audio-body sentinel. Source and metadata directories are separate. Typed readback
must exactly preserve the actual result and schema version, including CSV fields,
header declarations, URI identities, scan time and coverage. The larger fixture
contains only a few kilobytes of Unicode CSV data; the overflow case intentionally
uses a smaller configured cap rather than another large 4 MiB fixture.

## Execution boundary

All four `GuardedScanCacheFlowTest` cases passed in the coordinator's actual
isolated wave 11 build, on its first run. The integrated result was **357 JVM
tests passed: 246 Android host tests and 111 desktop tests**, with 57 Gradle tasks,
88 source hashes unchanged, and signature verification exit code 0. The six owner
`GuardedMetadataCacheTest` cases also passed in that run.

The execution receipt is `docs/ANDROID_WAVE11_BUILD_RECEIPT.json`. This task kept
test source frozen and did not launch another direct compiler or Gradle process
in parallel. These counts describe JVM/build acceptance, not physical-device
execution.

The cases do not instantiate Android `Context`, `SourceScanCache`'s Context wrapper,
SAF `DocumentsContract`, a remote provider, the VM lifecycle, or a physical device.
They use the production read/scan/cache/gate components with a deterministic host
provider and the same JSON codec configuration as the Context wrapper. Their
success does not establish cross-process writer locking or acoustic/media
validation. The injected atomic failure is an exception before move, not a claim
about arbitrary filesystem damage or a move that succeeds and subsequently
reports failure.

Only this new test file and report belong to N71. No production changes, new
dependencies, external Actions, downloads, commits or pushes were performed by
this task.

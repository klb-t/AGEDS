# Wave 3 Android quotation QA — N22

Task: `AGEDS-20261001-N22`. Independent QA began from claim
`e208310073933ac8d29576de7d17fe98b75de4b3`; production implementation belongs
to N16/N20. This receipt describes synthetic JVM checks and source review,
not Android device acceptance or acoustic alignment.

## Independent tests

| Suite | Cases | Scope |
|---|---:|---|
| `core/.../CitationAdversarialTest.kt` | 13 | Exact whitespace/Unicode quote, pinned IDs, selector continuity/order/duplicates/bounds, gaps vs overlaps, unavailable timing fallback, corrupted unselected word, nonfinite/negative/overflow times, boolean/null/nonfinite wire values, ties-to-even, defensive selection snapshot |
| `androidApp/.../RangePlaybackAdversarialTest.kt` | 13 | Fake decoder prepare→seek→start ordering, end boundary, stale prepare/seek/error after replacement, disposal/pause release, invalid ranges, decoder failure, generation fencing, finite preparation/playback watchdog, actual media duration bounds and seek landing |
| `androidApp/.../CitationWorkspaceAdversarialTest.kt` | 7 | Actual workspace with injected fake service; late fetch/save after server/artifact/version change, disposal, wrong transcript version and wrong save response rejection |

Workspace transport deliberately uses `suspendCoroutine`: cancellation does
not itself suppress a late synthetic response. The tests therefore check the
workspace generation guard rather than merely a cooperative fake cancellation.
The test scope runs with `Dispatchers.Unconfined` for deterministic delivery;
it does not model Android dispatch latency.

## Executed evidence

The cached Kotlin 2.4.20 compiler, serialization compiler plugin and JUnit 4.13.2
run the real core models/selection and real range controller without Gradle.
No Android runtime classes are replaced with stubs in this run.
The direct suite includes the independent 23 core/player cases and the N16
owner's six baseline cases as they stood at that run; those six are not independent QA additions.

Final direct result: **29 tests passed, zero failures** (JUnit duration 0.143 s).
The later cases were subsequently verified in the coordinator's isolated final
Gradle run, whose receipt was independently read by N22:
`review/night-build/wave3-pass1/receipt.json` (workspace review directory).
The run completed at `2026-09-30T23:48:03.182502+00:00` with build exit code 0:
`:androidApp:assembleDebug`, `:androidApp:testDebugUnitTest`, `:core:desktopTest`.

**116 JVM tests passed: 86 Android-module and 30 desktop-core tests; zero
failures, errors or skips.** These include all 33 independent N22 cases above,
including all seven actual-workspace tests with Compose dependencies and all
13 range-controller tests. The N16 suite has seven cases, including the added
10,000-segment cap; the two display-projection cases also passed. These counts
are subsets of 116, not additional tests.

The receipt records `source_unchanged=true`, exact source SHA-256 values,
and `apksigner_exit_code=0`. Debug APK: 14,124,053 bytes; SHA-256
`b6f085dc5579e96f1a44e18281379aba9649e081e6db0fd226f7de549fbce572`.
Build inputs are identified by the receipt's hashes; its checkout commit alone
does not identify the then-uncommitted wave-3 source changes. APK compilation
and signature verification do not establish device or MediaPlayer execution.

## Review findings and production fixes

Read-only independent review found that initial workspace code accepted a
returned transcript/citation without checking that its artifact, version and
quote matched the captured request. N20 added explicit transcript/list/citation
response identity checks; the two rejection tests exercise that correction.
N20 also added analogous annotation checks following the review. A fakeable
service factory was added by N20 so stale requests can be tested against the
actual workspace. QA did not change production files.

Source review confirms server/artifact change clears the workspace; version
change clears the current preview and stops audio; Compose disposal invalidates
outstanding operations; pause/stop/destroy invokes audio stop. The MediaPlayer
adapter clears callbacks and releases the decoder. These lifecycle wiring facts
were read in source, not exercised on a phone. The pure fake tests verify release
and stale callback rejection in the range controller, not the OS lifecycle.

## Boundaries

No device/emulator, real MediaPlayer, SAF provider, HTTP transport integration,
private corpus, paid service, model download, new real ASR inference or acoustic
measurement was used. The quoted text remains raw ASR; successful validation
shows agreement with a stored version, not transcription truth or alignment.
A canceled save may already have committed remotely; the workspace suppresses
its stale UI response but cannot undo a server-side write.

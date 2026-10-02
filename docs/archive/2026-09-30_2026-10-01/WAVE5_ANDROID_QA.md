# Wave 5 independent Android paging QA — N34

Task `AGEDS-20261001-N34`, claim
`342520613c2ee0ceff5eb0f527a7c0cf298c00f9`. Production core/API changes belong to
N32 and workspace/UI changes to N33. QA owns the two new adversarial suites,
this report, and the authorized minimal adaptation of the existing seven-case
workspace fake to the new page service methods.

## Independent coverage

| Suite | Cases | Scope |
|---|---:|---|
| `ArtifactPageAdversarialTest.kt` | 22 | Envelope/item ownership; null ownership; positive descending unique IDs; snapshot/cursor continuity; malformed or oversized pages; empty initial/terminal pages; legal short byte-budget pages; immutable accepted list; terminal-chain refusal; 1000-item cap; validation of malicious rows beyond cap; explicit partial local truncation |
| `CitationPagingAdversarialTest.kt` | 10 | Separate collection cursors/snapshots; late version/citation/annotation pages after server/artifact/disposal changes; clear fencing; version switch and pinned selection; preservation of accepted pages; malformed page atomicity/error; duplicate in-flight suppression; explicit 1000-item cap/no extra requests |
| Existing `CitationWorkspaceAdversarialTest.kt` | 7 retained | Previous stale fetch/save and returned-version identity checks; service fake now returns valid bounded pages and descending version IDs |

The paging fake uses `suspendCoroutine`, which deliberately does not cooperate
with coroutine cancellation. Late callbacks therefore exercise actual workspace
and collection generation guards. Version/selection change cancels outstanding
pages but preserves accepted chains; the test requires the new pinned preview
to survive a late page and a subsequent valid retry. Tests use a deterministic
unconfined coroutine scope and do not simulate Android scheduling latency.

The original fake listed versions 41,42 in ascending order. It now returns 41,40
with initial snapshot 41 and a terminal page; the second-version assertions were
changed from 42 to 40. All seven previous test scenarios remain present.

## Observed execution

Independent core suite: **22 passed**, zero failures, direct cached Kotlin
2.4.20/JUnit 4.13.2 execution (0.103 s). Final combined direct core run: **29 passed**, zero failures (0.384 s),
including those same 22 and the N32 owner's seven baseline cases. These runs
are overlapping, not additive. No Gradle was launched by QA.

N34 independently inspected `docs/ANDROID_WAVE5_BUILD_RECEIPT.json` and confirmed
it exactly matches the isolated `review/night-build/wave5-pass1/receipt.json`.
The final integrated build completed at `2026-10-01T00:20:21.682096+00:00`:
**184 JVM tests passed (125 Android-module, 59 desktop-core), zero failures,
errors or skips.** This includes all ten paging workspace cases, all seven
retained workspace cases, and all 29 paging core cases above. These are subsets
of 184, not additional test totals.

The receipt records build exit 0 for `:androidApp:assembleDebug`,
`:androidApp:testDebugUnitTest` and `:core:desktopTest`, `source_unchanged=true`
for 62 hashed inputs, and `apksigner_exit_code=0`. The debug APK is 14,140,437
bytes with SHA-256
`2a56f2f49c3761c3f78b07cbed2c7ce465d7f18eb2293845a3e71721cc8c34ba`.
Workspace tests executed with the actual Compose dependencies, but remain JVM
fake-service tests; APK compilation/signing does not establish device execution.

## Source review and limits

Source review confirms separate version/citation/annotation page states and
explicit coverage messages, with no unbounded fallback. Changing a transcript
version retains accepted pages. Returning a save result refreshes only the
corresponding collection snapshot rather than appending outside its bound.
These observations are source review, not UI/runtime acceptance.

No Android device/emulator, live HTTP transport, private corpus, paid service or
new download was used. Core/collection bounds and synthetic stale callbacks do
not establish actual phone lifecycle behavior or server deployment. Existing
playback and quote semantics remain separate from paging coverage.

Wave 5 bounds retained item counts, not aggregate retained bytes or total process
memory. Large accepted rows across many pages can still accumulate substantial
payloads. A client retention-byte budget remains follow-up work; no global
memory-cap claim is made by this receipt.

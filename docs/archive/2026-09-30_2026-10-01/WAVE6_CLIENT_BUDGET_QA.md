# Wave 6 independent client payload-budget QA — N41

Task `AGEDS-20261001-N41`, claim
`c8cfdbdfe89652e4a43d9f32773ad4445be37648`. The production owner is N40.
QA owns only the new independent Kotlin/core, Android workspace and Node suites
listed here plus this report. No production files or prior tests were changed.

## Contract checked

Each production history collection retains at most 1000 rows and at most
4,194,304 bytes of encoded row payload. Bytes are summed across accepted rows
and pages. Kotlin measures serialized retained models with defaults and nulls
included; JavaScript measures UTF-8 of `JSON.stringify(row)`. The two client
representations need not have identical encoded sizes.

A complete terminal page that exactly fills the budget stays complete. An
exactly full budget with more history stops further requests. If a row does not
fit, only the earlier fitting prefix is admitted; the client does not skip that
row to admit a smaller later row or advance below it. Entire pages are validated
before admission, including rows after the cutoff. Omission remains explicit.

## Independent suites

| Suite | New cases | Scope |
|---|---:|---|
| `core/.../ArtifactPayloadBudgetAdversarialTest.kt` | 13 | Real serializer UTF-8/escaping/defaults/nulls, exact boundary, first-row oversize, prefix cursor, aggregation across pages, malformed hidden tail, closed chain, real 4 MiB models, refresh, overflow-safe arithmetic, invalid zero/negative costs |
| `androidApp/.../CitationPayloadBudgetAdversarialTest.kt` | 7 | All three production serializer callbacks, real multi-page 4 MiB quote retention, oversized first row, explicit coverage/request stop, refresh reset, late page after refresh/disposal, save-refresh racing a late older page |
| `server/tests/js/history-budget-adversarial.test.mjs` | 12 | Real UTF-8/escaping, initial/continuation prefix and exact boundaries, malformed tail atomicity, pending save/page race, oversized/duplicate saves, close/refresh accounting, actual default 4 MiB rows |

Android late-response fixtures use non-cooperative `suspendCoroutine` callbacks.
They test that an old callback cannot charge bytes or publish rows after the
collection's snapshot is reset, not merely that cancellation was requested.
The budget tests do not use model `toString()` or fixed fake costs to establish
production wiring. Synthetic costs are used only for invalid-cost/overflow
boundary tests; large-row and production-collection tests use real serializers.

## Observed execution

- Direct cached Kotlin 2.4.20/JUnit 4.13.2: **42 passed**, zero failures
  (0.302 s): 13 new independent budget cases plus the prior 29 paging cases.
- Node full JS test directory: **49 passed**, zero failures/cancellations/skips
  (310.94 ms), including all 12 new independent budget cases and the owner's
  three budget cases. Running the independent 12 separately also passed;
  these counts overlap and must not be added.
- Coordinator integration subsequently passed all seven Android workspace budget
  cases with actual Compose dependencies, within 207 JVM tests (132 Android,
  75 desktop). They are not part of the direct 42-case result. Receipt:
  `ANDROID_WAVE6_BUILD_RECEIPT.json`.

Source review confirmed production serialization costs are wired for version,
citation and annotation collections; browser retained-row checks cover initial
bootstrap, later pages and local saved citations. The browser removes omitted
initial rendered rows/options and removes the bootstrap JSON element. Those DOM
wiring observations are source review; Node execution is DOM-independent and
does not establish real-browser acceptance. Any coordinator browser run is
separate evidence.

## Limits

The bound is the sum of serialized retained-row payload sizes per collection,
not process heap, DOM memory, all collections combined, transport allocation or
peak serialization memory. A fetched page and temporary encoded strings can
exist before retention admission. No claim of a global memory cap is made.
The three collections each have their own 4 MiB budget. Android defaults/nulls
and browser JSON representations are measured independently.

No private corpus, device/emulator, real MediaPlayer, new model download or paid
service was used by this QA task. Counts and serialization do not establish
phone lifecycle behavior, server deployment, ASR correctness or alignment.

Coordinator Chromium acceptance separately passed 21 scenarios, including the
large-row cap and explicit omission: `WAVE6_BROWSER_RECEIPT.json`.

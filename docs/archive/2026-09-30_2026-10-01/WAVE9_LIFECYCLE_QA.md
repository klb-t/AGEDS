# Wave 9 independent native scan-engine lifecycle QA — N59

Claim `6317c5c`; task `AGEDS-20261001-N59`. QA owns only the new
`SourceScanEngineLifecycleTest.kt` and this receipt. N55 owns the production
engine, N56 the SAF binding, N57 traversal/limit acceptance, and N58 mixed-format
acceptance. No production files were edited by N59.

## Exercised boundary

Tests instantiate the actual `SourceScanEngine` with a narrow fake read-only
provider, lazy closeable cursors and counted/failing input streams. The engine,
source models, WAV reader and real CSV/TSV/XLS/XLSX parsers are compiled directly;
there is no second scan-policy implementation in the tests.

| Lifecycle area | Independent acceptance |
|---|---|
| Normal operation | Every acquired cursor/input is closed once; completed input can publish its hash |
| Early budget stop | Entry and byte-budget stops close resources; partial content has no full hash |
| Directory failure | Query/iteration/close errors remain explicit partial coverage; already admitted sibling work is retained |
| Cancellation | Query, lazy cursor, stream open/read/close, cursor close and cancellation checks propagate cancellation instead of returning successful or partial results |
| Missing/invalid progress | Missing input is unreadable; zero progress fails promptly; neither invents a hash |
| Failure accounting | Generic and WAV partial reads remain charged; close failure preserves byte count and withholds hash |

The 20 lifecycle cases are separate from N57's traversal assertions. Resource
checks sometimes reach the same caps because closure on early exit is the
behavior under test. N58's five format cases were also included in the final
direct execution at that owner's request; they are not N59-authored cases.

## Observed execution

Initial cached Kotlin 2.4.20/JUnit 4.13.2 direct run: **18 passed**, zero failures
(0.130 s). Two final cap/open-cancellation cases were added afterward. The final
combined run passed **25 tests**, zero failures (0.435 s): all 20 lifecycle
cases and the five N58 format cases, including the final exact raw-XLS assertion.
These runs overlap and must not be added together. Compiler warnings are
non-fatal redundant string conversion/non-null assertions in production code.

No Gradle was launched by N59. The coordinator owns serial Android compilation
and the final integrated JVM receipt.

## Production-binding review

Source review confirms `SourceScanner` constructs the read-only provider and
calls `SourceScanEngine.scan` with the coroutine's `ensureActive` check. The SAF
binding converts one cursor row at a time, closes the underlying cursor through
the engine-owned wrapper, and maps a null content stream/query result to an
explicit failure. It does not preload the tree or implement another traversal
policy. Root/document identity construction failures remain fatal as before;
ordinary directory enumeration failures become partial diagnostics.

The engine's `use` blocks own closure; cancellation is caught separately and
rethrown. Generic non-WAV reads retain their pre-existing accounted sentinel
policy. This receipt makes no new strict no-extra-byte claim for every format.
WAV's strict byte cap remains separately covered by N47.

## Limits

This is host-JVM execution of production orchestration, not real SAF provider,
permissions, Android lifecycle, MediaPlayer or device/emulator acceptance.
Synthetic providers cannot prove platform cursor behavior or remote snapshot
stability. No source corpus, paid service, new dependency, download or commit
was used. Hash coverage does not prove content truth, source authorship or
acoustic correctness.

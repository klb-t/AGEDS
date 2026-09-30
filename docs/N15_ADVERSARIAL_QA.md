# N15 independent parser acceptance — 2026-10-01 wave 2

Scope: independent review and new adversarial JVM tests for the N9 bounded native
XLS adapter and N13 native encoding/delimiter changes. Production fixes belong to
the respective implementation owners. This review did not change backend parsing,
use private source material, run ASR, or establish SAF/device acceptance.

## Findings and owner fixes

| Finding | Risk | Owner fix | Independent regression |
|---|---|---|---|
| Transport BOM followed by a genuine U+FEFF was removed twice | Silent source-value loss | Separate decoded-text entry point, consuming only one transport BOM | `onlyOneByteOrderMarkIsConsumed` |
| Zero-length BIFF records repeatedly scanned the remaining zero tail | Quadratic work on malformed input | Validate zero padding once; reject nonzero tail; cancellation checks | `zeroRecordWithNonzeroTailIsRejected` |
| MULRK duplicated/reformatted entire payload hex for each projected cell | Large retained-output amplification | Memoize record hex and cap cumulative projection characters | `repeatedMulrkPayloadCannotAmplifyRetainedTextBeyondBudget` |
| Formula Boolean cache byte 2 was coerced to true | Invented meaning from malformed source | Validate Boolean cache 0/1 | `invalidFormulaBooleanIsNotInventedAsTrue` |
| Nested XLSX rows passed all start-time row budget checks before emission | Retention budget bypass on malformed XML | Reject nested rows/cells and recheck row budget at emission | `nestedRowsCannotBypassRetentionBudget` |

The 20 independent tests also cover an independently generated valid CFB/BIFF
control; FAT and directory cycles; directory/workbook sector overlap; oversized
unsigned stream size; oversized BIFF record; row/cell limits; cancellation;
ambiguous separator votes; quoted separators; malformed UTF-8/UTF-16/UTF-32 input;
UTF-16 DTD rejection; ZIP entry bounds; and expansion budgets on ignored members.
The XLS fixture builder is independent of the adapter owner's fixture builder.
Input byte arrays are checked for mutation in XLS cases and selected format cases.
Synthetic fixture SHA-256 values are printed by the tests and captured in the
machine-readable receipt. No hash is presented as proof of authorship or truth.

## Execution evidence

Passed: all 20 independent N15 tests, together with 14 XLS implementation-owner tests (34 total, zero failures). Current sources were compiled with cached Kotlin 2.4.20 K2JVMCompiler and executed with JUnitCore on JDK 21. The run took 0.367 seconds after compilation. See `N15_ADVERSARIAL_QA_RECEIPT.json` for exact source/input hashes and `N15_ADVERSARIAL_TEST_LOG.txt` for captured output. Direct compilation was coordinated with the XLS owner after Gradle cache contention and exhausted disk space; it is separate from the integrated Android build.
An initial Gradle attempt failed before compilation because concurrent builds
contended for the Gradle journal cache. This is a toolchain scheduling failure,
not a parser test result. Follow-up builds are sequential.

## Limits

These are bounded synthetic parser checks, not broad compatibility certification,
fuzzing, a formal security proof, a phone/provider test, or corpus acceptance.
Native text decoding intentionally differs from the server's legacy fallback
policy; this review makes no cross-platform equivalence claim. XLS remains an
explicit partial BIFF8 projection, including unsupported constructs; the adapter
does not execute formulas or macros. Complete source bytes remain authoritative.

# Wave 9 — independent scan engine policy acceptance

N57, 2026-10-01. Independent acceptance of the actual JVM `SourceScanEngine`, using a counted synthetic read-only provider and the real delimited/WAV parsers. No production source changes, Gradle invocation, downloads, corpus access, or commits were made for this acceptance.

## Result

**14 tests passed** in `SourceScanEnginePolicyAdversarialTest`; JUnit 4.13.2 reported `OK (14 tests)` in 0.619 seconds after direct compilation with the cached Kotlin 2.4.20 compiler, serialization compiler plugin, and JDK 21. Compilation emitted redundant-conversion/non-null-assertion warnings but no errors. No open blocker was found.

The direct compilation included `SourceScanModels`, `SourceTextParser`, `WavHeaderObservation`, `WavHeaderProbe`, `SourceScanEngine`, `SourceDelimitedParser`, `SourceWorkbookParser`, `SourceXlsParser`, and `SourceWavReader`. The test class is in `androidApp/src/test/java/dev/klbt/ageds/SourceScanEnginePolicyAdversarialTest.kt` and can also run with the project's JVM unit-test suite. The local direct-compiler invocation was `python /tmp/ageds-compile-engine-policy-qa.py`; this temporary command depends on the already cached build environment.

Tested source SHA-256:

- `SourceScanEngine.kt`: `435c38eaaaad13cb8af74611362f2aa2456add0a322000b14aafafe8b885e6a8`
- `SourceScanEnginePolicyAdversarialTest.kt`: `1c27294f9a635bf60478eaec2ad8cd03a0a4f20f029eb0b69e67fa321ee3bab9`

## Evidence covered

- Cycles and repeated file IDs produce explicit issues, without a second traversal/read. Distinct IDs with colliding raw names preserve both URI identities and independently parsed raw CSV values.
- Depth zero and a one-directory budget prevent nested provider enumeration while retaining visible root files.
- A virtual million-row directory stops after three metadata materializations when the entry budget is three. File budget two separately prevents opening a third file.
- The next metadata row can be deliberately throwing: it is never requested after entry exhaustion. An exactly full cursor is conservatively partial with `entry_limit`; no speculative EOF row is requested.
- Oversized individual locators are omitted before content access. The cumulative million-character locator budget stops lazy enumeration explicitly (13 materializations, 12 retained files for this fixture).
- Known oversized non-WAV content remains inventoried without opening it or publishing a hash.
- Unknown-length non-WAV content preserves the existing sentinel policy: a ten-byte budget can consume eleven bytes to establish overflow. The next file is skipped and no complete hash is published. This is **not** a strict ten-byte physical-read guarantee.
- The WAV branch consumes exactly ten bytes under the same ten-byte cap, reports partial header evidence, and has no complete hash.
- Real CSV parsing shares the global retained-result budgets across files: 10,000 rows, 50,000 cells, and at most 4,000,000 retained characters. Exhaustion emits `result_limit` and partial coverage; the tests do not replace parsers with canned results.

## Boundary decision and scope

The extracted cursor API returns a fully materialized document from `next()`. Root identified that applying the former Android cursor's entry check after this call could access one extra metadata row. The production owner moved the check before `next()`. The independent tests pin that deliberate tightening and its honest exact-boundary partial result. This metadata rule is distinct from the unchanged non-WAV content sentinel.

These are JVM policy tests with synthetic provider streams, not device SAF integration or proof of provider behavior. Resource closure, cancellation and error lifecycle are separately owned by N59; mixed-format interpretation and cache behavior have separate acceptance suites. This receipt does not claim full media decoding or strict non-WAV content byte limits.

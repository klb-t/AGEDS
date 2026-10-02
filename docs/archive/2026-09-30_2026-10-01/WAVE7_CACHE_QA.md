# Wave 7 — WAV metadata cache compatibility QA

N48 independently tests the new `ScannedSourceFile.wavHeader` metadata against
frozen old cache JSON, typed serialization, the real header probe and the actual
`BoundedMetadataCache` storage implementation. No production file was modified by
this task.

**Direct JVM result: 20 tests passed** — seven new common serialization tests,
six new metadata/cache compatibility tests, and the seven existing bounded-cache
regressions. The cached Kotlin 2.4.20 compiler and serialization plugin, JDK 21,
kotlinx.serialization 1.11.0 and JUnit 4.13.2 were used directly. Gradle was not
invoked by this task. The coordinator's integrated Android build remains a
separate acceptance step.

## Evidence and boundaries

| Check | Observed result |
|---|---|
| Frozen schema-1 JSON without `wavHeader` | Decodes with a null header; old `audioDurationSec`, URI and coverage remain unchanged. Encoding does not invent a header or claim a new scan. |
| Explicit null / unknown future sibling field | Missing observation stays unknown with the existing `ignoreUnknownKeys` codec. |
| Full new observation and sibling CSV metadata | Every declared header value, duration basis, incomplete-body flag and literal issue text survives typed roundtrip; CSV cells/text-format metadata remain unchanged. |
| Failed/partial/unsupported/size-mismatch statuses | Status and missing duration survive roundtrip without substituting a successful observation. |
| Unusable metadata | Wrong containers, nonnumeric text, nonfinite duration and overflowing long values fail decoding. No claim is made that this codec rejects every noncanonical representation. |
| Real synthetic PCM probe | Header-derived duration survives serialization; supplied audio bytes remain unchanged; the literal audio sentinel is absent from encoded metadata and the encoded observation is under 4 KiB. `bodyValidated` remains false. |
| Private cache publication | Only metadata is created in a separate private directory; the synthetic original source bytes and source-directory contents remain unchanged. No raw/base64 audio copy is retained in cache. |
| Exact UTF-8 boundary | Encoded metadata fits at its measured byte budget. A one-byte-smaller replacement budget rejects it and preserves the published cache. |
| Actual 4 MiB write bound | A Unicode payload whose character count fits but encoded size exceeds 4 MiB is rejected before publication; the old metadata survives. |
| Canceled publication | Cancellation at the final commit check preserves the previous typed snapshot and removes the temporary metadata file. |
| Oversized or invalid UTF-8 existing cache | Reading fails without repairing or replacing the stored bytes. |

The storage tests run `BoundedMetadataCache` with the same JSON configuration used
by `SourceScanCache` (`Json { ignoreUnknownKeys = true }`). They do **not** instantiate
the Android `Context` wrapper, test SAF permissions/provider behavior, or execute
on a physical device. `SourceScanCache`'s schema-version check was read in the
source, not replaced by a duplicated assertion pretending to test that wrapper.
The synthetic original is an ordinary temporary file. These tests demonstrate
that the chosen cache destination remains separate, not that an arbitrary caller
cannot intentionally point a cache writer at a source file.

Roundtrip means typed metadata equality, not exact preservation of cache JSON
whitespace, field order, omitted defaults or unknown extension fields. The existing
kotlinx JSON codec accepts some quoted numeric values; it is not a strict external
wire validator. New scans serialize actual numeric fields. Historical durations
are not upgraded into header provenance during decode. Header declarations remain
observations, not evidence that the audio body was read, decoded or authenticated.

## Files and execution

- `core/src/commonTest/kotlin/dev/klbt/ageds/core/WavHeaderCacheSerializationTest.kt`
- `androidApp/src/test/java/dev/klbt/ageds/WavMetadataCacheCompatibilityTest.kt`
- Existing `androidApp/src/test/java/dev/klbt/ageds/BoundedMetadataCacheTest.kt`

The isolated direct runner compiled the actual source models, header observation,
header probe, bounded cache and these test classes with the serialization compiler
plugin, then invoked JUnitCore on the three classes above. The result was
`OK (20 tests)`. Temporary build outputs were outside the repository. No original
corpus, downloaded media, paid service, source repair, commit or push was involved.

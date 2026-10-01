# Wave 7 independent WAV stream QA — N47

Claim `06588a9`; task `AGEDS-20261001-N47`. QA owns the new
`SourceWavReaderAdversarialTest.kt` and this receipt. Production stream/scanner
changes belong to N44; header-model/parser and presentation have separate owners.

## Independent acceptance

The 21 stream tests invoke the actual `SourceWavReader` with counted JVM input
streams. They measure returned bytes, requested buffer lengths, read calls and
close calls independently of the provider's reported size.

| Boundary | Evidence |
|---|---|
| Per-file/total cap | Smaller budget wins; no sentinel byte; zero remaining budget reads nothing |
| Exact cap | Known or unknown reported size does not substitute for observed EOF; no complete hash at an unproven exact boundary |
| Oversized/lying provider | Known oversize uses at most 64 KiB; understated length cannot exceed actual budget; actual short EOF can establish a full hash despite overstatement, with mismatch diagnostic |
| EOF/hash | Small and large complete streams hash exactly all read bytes; empty input does not invent duration |
| Failure/progress | Zero-progress, partial IOException and close failure return explicit failure, preserve actual byte accounting and withhold a complete hash |
| Cross-file accounting | A 14-byte failed read leaves only 36 bytes of a 50-byte synthetic total for the next stream |
| Cancellation | Cancellation before first read or after partial progress propagates and closes the acquired input |
| Prefix retention | Large fully hashed file exposes at most 65,536 inspected header bytes and no body-validation claim |
| Actual EOF vs header claims | Under/overdeclared RIFF/data size beyond retained prefix suppresses duration with explicit mismatch, while retaining the full hash of actual bytes |

Independent header-format adversarial coverage belongs to N45; these tests
focus on actual stream budget and resource behavior. They use generated WAV bytes,
not a private corpus or external spreadsheet/audio writer.

## Observed execution

Initial direct cached Kotlin 2.4.20/JUnit 4.13.2 run passed **35 tests**, zero
failures (0.253 s): independent stream 20 + stream owner 8 + UI owner 7.
The later actual-EOF mismatch fix and neutral missing-observation UI fix added
three cases across those suites. Final direct run passed **38 tests**, zero
failures (0.205 s): independent stream 21 + stream owner 9 + UI owner 8.
Overlapping runs are not additive. The compiler reported non-fatal redundant
non-null assertion warnings in the header parser.

No Gradle was launched by QA. The coordinator owns the final isolated Android
build and integrated JVM receipt.

## Read-only production review

Source review confirms `SourceScanner` opens WAVs through `openInputStream` only
when remaining budget is positive. A missing stream raises a read failure and
produces an unreadable entry. For a returned helper result, `read.bytesRead` is
added to the global scanner counter before header/coverage interpretation,
including `read_failed` results. The helper closes acquired streams and returns
failed-read byte counts; cancellation aborts the scan rather than processing
another file.

The new WAV path retains a prefix capped at 64 KiB, streams the digest and
returns metadata/hash without an audio payload. It creates no audio copy or
source write. Full hashing and header observation have separate coverage.
The display labels duration as a header declaration, not playback measurement,
and hash presence does not assert valid audio samples. Missing header metadata
is described neutrally because it can also result from a fresh skipped read.

The old non-WAV branch still uses its pre-existing accounted sentinel byte.
The strict no-extra-byte claim in this receipt applies to the new WAV reader,
not a claim that every scanner format's read policy changed.

## Limits

JVM tests do not execute Android SAF, a provider process, permissions, lifecycle,
MediaPlayer or acoustic playback. Missing-stream and scanner wiring statements
above are source review, not runtime provider acceptance. A complete hash means
all returned bytes were hashed, not that the provider supplied a stable snapshot,
that RIFF declarations are truthful, or that audio is playable. No device,
private source corpus, paid service, download, commit or production edit was
performed by this QA task.

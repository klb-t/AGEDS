# AGEDS

**Auditable evidence system, Android-first but not Android-only.**

The first practical module is the Evidence Workbench: ingest communication evidence, preserve originals, batch-transcribe recordings, search transcripts and annotate exact evidence without collapsing source facts and interpretation into one field.

## Current foundation increment

- Android app: multi-select audio/video with the system picker
- streaming upload to the self-hosted Evidence Workbench server
- SHA-256/content-addressed evidence storage on ingestion
- transactional transcription queue with renewable leases, recovery and fenced publication
- one processing run per attempt and a new transcript version per successful run
- faster-whisper adapter with word timestamps; language probability is not transcript confidence
- transcript list/detail on Android
- annotations and exact segment/word citations pinned to the displayed transcript version, with a browser selection and range-playback workspace
- every acquisition retains source context, including repeated ingestion of the same bytes
- idempotent SMS Backup and WhatsApp imports retaining raw fields and time uncertainty
- bounded read-only CSV/TSV, XLS, XLSX and WAV scanner with conflicts and coverage reporting
- metadata-only package export, structural/digest verification and exact canonical roundtrip through an inert SQLite archive
- Android corpus filename collisions expose all candidates; selected URIs reach the evidence client
- KMP `core` with shared evidence/transcript models and deterministic priority scoring
- JSON API intended for Android now and desktop/iOS/web clients later
- local backend test suite and GitHub Actions build definition

The Android client is deliberately thin. Evidence bytes, provenance, derived text and the audit log live in the evidence service rather than in Android-specific state.

Android now opens a **Sources** workspace without a prepared seed. Choose a folder through SAF to inspect bounded read-only CSV/TSV, XLSX and WAV observations, inventory other files, and select exact audio URIs for the evidence client. The previous seed-based catalog remains optional. Native XLS provides a bounded, explicitly partial BIFF8 projection; UTF-16 BOM text and explicit CSV dialect hypotheses are supported. The server/local CLI has its separate parser policy. See [native format boundaries](docs/NATIVE_SOURCE_FORMATS.md). No device/provider interaction test has been performed. See [HANDOFF.md](HANDOFF.md) for verified behavior and remaining work.

## Project coordination

Start with [HANDOFF.md](HANDOFF.md), [coordination/state.json](coordination/state.json), [coordination protocol](coordination/README.md) and [architecture rules](docs/ARCHITECTURE_RULES.md). A separate management chat exchanges task/result IDs and repository revisions with execution threads. Chat history supports recall; it does not acknowledge delivery or completion. This session has one coordinator and six concurrent worker slots, with management responsibilities rotating within those slots.

The current overnight checkpoint is [coordination/night-20261001.json](coordination/night-20261001.json); resumption rules are in [NIGHT_WORK.md](coordination/NIGHT_WORK.md). They identify claims, completed results and the remaining queue.

## Repository layout

```text
ageds/
  core/          Kotlin Multiplatform domain model (Android/JVM/JS/Wasm)
  androidApp/    Android Compose client
  server/        Evidence Workbench / transcription service
  docs/          architecture and roadmap
  .github/       reproducible CI build
```

## Run server

```bash
cd server
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-whisper.txt
set -a
. ./.env
set +a
uvicorn app.main:app --host 0.0.0.0 --port 8080
# another shell:
# activate the same environment and load .env with the three lines above
python -m app.worker
```

For a physical Android device set the app's **Evidence server** field to the reachable host, e.g. `http://192.168.x.x:8080` (or HTTPS when exposed remotely). Android Emulator uses `http://10.0.2.2:8080` for the host machine.

## Android build

Install a full JDK 21 (including `javac`) and Android SDK platform 37.0, then run `./gradlew :androidApp:assembleDebug :androidApp:testDebugUnitTest :core:desktopTest`, or open the repository in Android Studio. The official Gradle 9.7.0 wrapper is included with a distribution checksum. Dependency versions remain pinned. [The wave-2 build report](docs/ANDROID_WAVE2_BUILD.md) records the current JVM tests, APK integrity and source-input hashes; the earlier [wave-1 receipt](docs/ANDROID_NIGHT_BUILD.md) is preserved. Compilation and a phone test remain distinct checks.

## Read-only source scan

From the repository root:

```bash
pip install -r server/requirements-scanner.txt
python -m server.app.cli scan /path/to/sources --output /path/outside/sources/scan.json
python -m server.app.cli metadata-export /path/outside/sources/package.json
python -m server.app.cli metadata-verify /path/outside/sources/package.json
```

The scan command never initializes the database, copies originals or executes formulas. Optional spreadsheet dependencies enable XLS/XLSX parsing; unavailable adapters produce explicit issues. Limits and partial coverage are recorded. On the HTTP service, scan access is disabled until the operator sets `EW_SCAN_ROOTS` (OS path separator). `GET /api/source-roots` lists configured root IDs; `POST /api/source-scans` accepts a root ID and relative subdirectory. Client-uploaded locators are literal metadata and are never opened on the server.

`GET /export/manifest.json` now emits `ageds.metadata-package/v1` with 16 metadata tables, relationships, versions, acquisition observations and digests. It contains no source bytes, signature, replay or restore operation. Verification checks the recorded graph and pinned quote text; it does not verify the truth of speech or the content of externally referenced files.

To transfer metadata into an isolated archive and back without opening live tables or resuming jobs:

```bash
python -m server.app.cli metadata-archive-import package.json archive.sqlite
python -m server.app.cli metadata-archive-export archive.sqlite roundtrip.json
```

Outputs must be new files. The exact canonical package, including historical errors and original export time, is preserved. See [METADATA_ARCHIVE.md](docs/METADATA_ARCHIVE.md) for limits and exclusions.

## Backend verification and upgrade

```bash
pip install -r server/requirements-test.txt
python -m pytest server/tests -q
node --test server/tests/js/range-player.test.mjs
```

The default tests use synthetic source data and an ASR adapter without model downloads. A separate [real local ASR smoke](docs/REAL_ASR_SMOKE.md) executed tiny.en on generated speech through the worker, word citation and inert archive roundtrip; recognition errors remain raw. [Actual Chromium acceptance](docs/BROWSER_NIGHT_QA.md) covers selection and playback of saved version-pinned citations. Neither establishes human-corpus quality or physical-device behavior. Before upgrading an existing service, stop old workers, back up the SQLite database and content store, then start the new service/workers. Startup applies additive migrations while preserving legacy records; unknown historical provenance stays unknown. Old loaded workers do not use the new lease fencing.

## Non-negotiable evidence invariants

1. Raw source bytes and source metadata are preserved.
2. Parsing, filename correction, identity resolution, transcription, OCR, LLM analysis and human annotations are **derived assertions**.
3. A derived assertion never silently overwrites source evidence.
4. Cross-source matching is stored as a relation with rationale/confidence.
5. Unknown/ambiguous/conflicting state is representable; it is never forced into a single truth value.
6. Deterministic code owns dates, numbers, hashes, enumerations and citations; language models may reason over them but do not manufacture them.

This is the foundation for the broader AGEDS legal-analysis system rather than a disposable transcription app.

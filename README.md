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
- annotations pinned to the displayed transcript version; deterministic segment citations via API
- every acquisition retains source context, including repeated ingestion of the same bytes
- idempotent SMS Backup and WhatsApp imports retaining raw fields and time uncertainty
- bounded read-only CSV/TSV, XLS, XLSX and WAV scanner with conflicts and coverage reporting
- metadata-only package export and structural/digest verification
- Android corpus filename collisions expose all candidates; selected URIs reach the evidence client
- KMP `core` with shared evidence/transcript models and deterministic priority scoring
- JSON API intended for Android now and desktop/iOS/web clients later
- local backend test suite and GitHub Actions build definition

The Android client is deliberately thin. Evidence bytes, provenance, derived text and the audit log live in the evidence service rather than in Android-specific state.

Android's corpus screen still reads a prepared JSON seed. The independent spreadsheet scanner runs on the server/local CLI; a native SAF scanner without a seed is the next increment. This increment does not yet have a compiled Android APK or a device test. See [HANDOFF.md](HANDOFF.md) for verified behavior and remaining work.

## Project coordination

Start with [HANDOFF.md](HANDOFF.md), [coordination/state.json](coordination/state.json), [coordination protocol](coordination/README.md) and [architecture rules](docs/ARCHITECTURE_RULES.md). A separate management chat exchanges task/result IDs and repository revisions with execution threads. Chat history supports recall; it does not acknowledge delivery or completion. This session has one coordinator and six concurrent worker slots, with management responsibilities rotating within those slots.

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
uvicorn app.main:app --host 0.0.0.0 --port 8080
# another shell:
python -m app.worker
```

For a physical Android device set the app's **Evidence server** field to the reachable host, e.g. `http://192.168.x.x:8080` (or HTTPS when exposed remotely). Android Emulator uses `http://10.0.2.2:8080` for the host machine.

## Android build

Open the repository in Android Studio, or install Gradle 9.7.0, Java 21 and Android SDK 37, then run `gradle :androidApp:assembleDebug :core:desktopTest`. The workflow defines the same command. The repository currently has wrapper properties but lacks `gradlew` and the wrapper JAR, so `./gradlew` is not available. Dependency versions are pinned in the build files; their presence alone does not establish build success.

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

## Backend verification and upgrade

```bash
pip install -r server/requirements-test.txt
python -m pytest server/tests -q
```

Tests use synthetic source data and an ASR adapter. They do not download a model or establish actual recognition quality. Before upgrading an existing service, stop old workers, back up the SQLite database and content store, then start the new service/workers. Startup applies additive migrations while preserving legacy records; unknown historical provenance stays unknown. Old loaded workers do not use the new lease fencing.

## Non-negotiable evidence invariants

1. Raw source bytes and source metadata are preserved.
2. Parsing, filename correction, identity resolution, transcription, OCR, LLM analysis and human annotations are **derived assertions**.
3. A derived assertion never silently overwrites source evidence.
4. Cross-source matching is stored as a relation with rationale/confidence.
5. Unknown/ambiguous/conflicting state is representable; it is never forced into a single truth value.
6. Deterministic code owns dates, numbers, hashes, enumerations and citations; language models may reason over them but do not manufacture them.

This is the foundation for the broader AGEDS legal-analysis system rather than a disposable transcription app.

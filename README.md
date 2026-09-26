# AGEDS

**Auditable evidence system, Android-first but not Android-only.**

The first practical module is the Evidence Workbench: ingest communication evidence, preserve originals, batch-transcribe recordings, search transcripts and annotate exact evidence without collapsing source facts and interpretation into one field.

## Current vertical slice

- Android app: multi-select audio/video with the system picker
- streaming upload to the self-hosted Evidence Workbench server
- SHA-256/content-addressed evidence storage on ingestion
- automatic transcription queue; selection order becomes explicit queue priority
- faster-whisper worker with word timestamps
- transcript list/detail on Android
- annotations stored as a separate layer
- KMP `core` with shared evidence/transcript models and deterministic priority scoring
- JSON API intended for Android now and desktop/iOS/web clients later
- GitHub Actions Android build workflow

The Android client is deliberately thin. Evidence bytes, provenance, derived text and the audit log live in the evidence service rather than in Android-specific state.

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

Open the repository in a current Android Studio and run `androidApp`, or use the included CI workflow. The project pins Kotlin 2.4.20, AGP 9.4.0, Compose/Jetpack current stable lines from September 2026, and Ktor 3.6.0.

## Non-negotiable evidence invariants

1. Raw source bytes and source metadata are preserved.
2. Parsing, filename correction, identity resolution, transcription, OCR, LLM analysis and human annotations are **derived assertions**.
3. A derived assertion never silently overwrites source evidence.
4. Cross-source matching is stored as a relation with rationale/confidence.
5. Unknown/ambiguous/conflicting state is representable; it is never forced into a single truth value.
6. Deterministic code owns dates, numbers, hashes, enumerations and citations; language models may reason over them but do not manufacture them.

This is the foundation for the broader AGEDS legal-analysis system rather than a disposable transcription app.

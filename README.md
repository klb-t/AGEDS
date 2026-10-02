# AGEDS

**Auditable evidence workbench with Android and browser clients.**

AGEDS preserves communication records and recordings, tracks how they were acquired, and keeps transcripts, citations and annotations separate from their sources. Its first working module supports reviewing evidence: importing files, searching messages and transcripts, running local transcription, and selecting exact passages from a recorded transcript version.

## Capabilities

- Content-addressed file ingestion with SHA-256 and a separate acquisition record for each import.
- SMS Backup & Restore XML and WhatsApp text imports with raw fields, repeated occurrences and time ambiguity preserved.
- A transcription queue with renewable leases, recovery and publication checks; each successful attempt produces a new transcript version.
- Browser and Android transcript review, annotations and citations pinned to the selected version and segment or word occurrence.
- Bounded, read-only source scanning for CSV/TSV, XLS, XLSX and WAV, with explicit format, resource and coverage limits.
- Metadata package verification, exact canonical roundtrip through an inert SQLite archive, and independently inspectable citation packets.

See [features and boundaries](docs/FEATURES.md) for the supported scope.

## Start locally

From the repository root, with Python 3.12:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r server/requirements.txt
cd server
cp .env.example .env
set -a
. ./.env
set +a
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080>. Local transcription is an optional worker with separate dependencies; Android builds need a full JDK 21 and Android SDK. Follow [Getting started](docs/GETTING_STARTED.md) for both, source scanning and metadata export.

## Repository

| Path | Purpose |
|---|---|
| [androidApp/](androidApp/) | Android Compose client, source-provider adapter and JVM/device tests. |
| [core/](core/) | Kotlin Multiplatform domain types and deterministic rules. |
| [server/](server/) | FastAPI service, SQLite catalog, importers, workers, browser client and tests. |
| [docs/](docs/README.md) | Setup, features and active technical contracts. |
| [scripts/](scripts/) | Reproducible builds and synthetic acceptance runners. |
| [coordination/](coordination/README.md) | Task/result records and development history. |

## Current limits

The service has no built-in authentication or tenant isolation; local use assumes a trusted operator. Source preservation is enforced by the application and filesystem checks, rather than physical WORM storage or signed provenance. A content hash establishes byte identity, not authorship, truth or acquisition time.

Source scanners report partial coverage and unsupported formats explicitly. Word citations depend on stored ASR timing; they do not establish acoustic alignment or recognition quality. Metadata archives exclude source media and cannot restore a live database, resume jobs or replay inference. Physical-device and SAF acceptance are separate from successful compilation and host tests. Ecosystem adapters and broader legal-analysis workflows remain future work.

[Documentation](docs/README.md) · [Local checks](docs/DEVELOPMENT.md) · [Change history](CHANGELOG.md) · [Development handoff](HANDOFF.md)

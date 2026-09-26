# Evidence Workbench

Self-hosted evidence/communications workbench for ingesting, preserving, searching, transcribing and annotating communication records.

## What this starter already does

- immutable-ish evidence store keyed by SHA-256
- provenance for every imported artifact
- SQLite catalog + FTS5 full-text search
- upload/import through web UI
- Android `SMS Backup & Restore` XML importer (calls, SMS, MMS text)
- WhatsApp `.txt` export importer
- generic file ingestion for audio/video/images/docs
- annotations, tags and evidence links
- timeline/search UI
- batch transcription queue
- optional local `faster-whisper` worker
- audit log of imports, annotations and processing actions
- JSON/CSV export endpoints

The design deliberately separates **raw source evidence** from **derived data** (normalized events, transcripts, annotations, embeddings later). A derived result never overwrites source metadata.

## Run

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Open http://localhost:8080.

For transcription:

```bash
pip install -r requirements-whisper.txt
python -m app.worker
```

Or use Docker:

```bash
docker compose up --build
```

## Core evidence model

`Artifact` is a source file or source-native object. It has a content hash, source locator, original name, timestamps and raw metadata. `Event` is a normalized communication event such as a call, SMS, WhatsApp message or e-mail. `DerivedText` stores transcript/OCR/parser output and points back to its artifact. `Annotation` is user-authored and never mutates the evidence itself.

The critical invariant is:

> original bytes + source metadata + import metadata stay addressable forever; every interpretation is a separate layer with its own provenance and confidence.

## Connectors planned next

- Gmail API incremental sync (`historyId`) with raw RFC 822 preservation
- Google Drive incremental sync (`changes` token) with revisions where available
- WhatsApp media + chat association
- Google Takeout parsers
- Android call/SMS backup cross-correlation and recorder filename reconciliation
- OCR, PDF text extraction and image metadata
- speaker diarization + word timestamps
- vector search / semantic clustering
- entity/contact resolution with confidence and alias history
- case bundles / exhibit numbering / report generation
- cryptographic manifests / signed exports / WORM destination

## Existing catalog integration

The application can ingest the existing communication catalog as a generic spreadsheet now. A dedicated importer should map its rows into normalized events without treating filename-derived numbers as authoritative; the current catalog already demonstrates why provenance/confidence needs to remain explicit.

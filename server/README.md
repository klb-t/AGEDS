# Evidence Workbench service

The AGEDS service owns preserved source bytes, acquisition provenance, normalized communication records, transcription runs, versioned results, citations and annotations. Android and browser clients consume the same HTTP contracts.

## Run

From this directory:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
set -a
. ./.env
set +a
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080>; API documentation is at <http://127.0.0.1:8080/docs>.
The service has no built-in authentication. Keep access within a trusted environment.

For transcription, activate the same virtual environment in a second shell, change to this directory and load the same `.env`:

```sh
set -a
. ./.env
set +a
python -m pip install -r requirements-whisper.txt
python -m app.worker
```

The first use of a Whisper model may download it. Model, device and compute type are configured with `EW_WHISPER_*`. Each processing attempt retains its own run; successful attempts create new transcript versions.

## Service boundaries

- `app/evidence.py`, `app/importers/`: content-addressed ingestion, acquisition records and SMS/WhatsApp imports.
- `app/jobs.py`, `app/worker.py`: transactional claims, renewable leases and fenced publication.
- `app/citations.py`, `app/exchange.py`: exact version-pinned citations and bounded evidence packets.
- `app/search.py`, `app/read_pages.py`: literal FTS queries and bounded history reads.
- `app/verified_media.py`, `app/verified_reader.py`: verified descriptor reads for playback and decoding.
- `app/scanner.py`: bounded source observations without modifying or importing originals.
- `app/packages.py`, `app/archive.py`: metadata verification and inert archive roundtrips.
- `app/templates/`, `app/static/`: browser review, citation selection and range playback.

## Commands and checks

Run local CLI commands from the repository root:

```sh
python -m server.app.cli --help
python -m server.app.cli scan /path/to/sources --output /path/outside/sources/scan.json
python -m server.app.cli citation-inspect /path/to/citation.json
```

Spreadsheet scanning adds optional dependencies from `requirements-scanner.txt`.
HTTP scanning is disabled until `EW_SCAN_ROOTS` is configured. Recorded locators in imported packets are inert metadata and are never followed.

[Getting started](../docs/GETTING_STARTED.md) covers configuration and export; [Development](../docs/DEVELOPMENT.md) gives reproducible local checks and upgrade procedure. [Technical contracts](../docs/README.md) define preservation, budgets and failure behavior.

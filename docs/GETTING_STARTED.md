# Getting started

AGEDS runs as a local Evidence Workbench service, an optional transcription worker, and an Android client. The browser workspace is served by the service itself.

## Python service

Python 3.12 is the historical local validation baseline. From the repository root:

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

Open <http://127.0.0.1:8080>. The home page supports file uploads, SMS Backup & Restore XML and WhatsApp text imports. The artifact page shows stored provenance, transcript versions, annotations and citation controls. FastAPI exposes the route schema at `/docs`.

`.env` is a shell-loaded configuration file: the Python process does not load it automatically. Paths such as `./data` are relative to the process working directory. Start the service and worker from the same directory with the same configuration.

| Setting | Meaning |
|---|---|
| `EW_DATA_DIR` | Local service data directory. |
| `EW_DB_PATH` | Live SQLite catalog path. |
| `EW_STORE_DIR` | Content-addressed preserved file store. |
| `EW_CASE_NAME` | Default case name. |
| `EW_WHISPER_MODEL` | Local faster-whisper model, default `small`. |
| `EW_WHISPER_DEVICE` | Inference device, default `cpu`. |
| `EW_WHISPER_COMPUTE_TYPE` | Compute profile, default `int8`. |
| `EW_SCAN_ROOTS` | Optional allowed HTTP scan roots, separated by the OS path separator; empty disables HTTP scanning. |

There is no built-in authentication or tenant isolation. The loopback address is the local default here. Access from a phone requires a reachable host and an operator-controlled network.

## Optional transcription worker

In another terminal, activate the same virtual environment, change to `server/` and load its `.env`:

```sh
. ../.venv/bin/activate
set -a
. ./.env
set +a
python -m pip install -r requirements-whisper.txt
python -m app.worker
```

Queue an audio artifact from its browser detail page or the Android client. The model may be downloaded on first use. A result is published only by the current lease owner; another successful run creates another transcript version. Raw ASR output is retained, including recognition errors. Language probability is not a transcription-accuracy score.

## Android client

Build prerequisites are a full JDK 21 with `javac`, Android SDK platform `android-37.0`, and Build Tools `36.0.0`. Set `JAVA_HOME` and `ANDROID_HOME`, then run from the repository root:

```sh
./gradlew :androidApp:assembleDebug :androidApp:testDebugUnitTest :core:desktopTest
```

The APK is written to `androidApp/build/outputs/apk/debug/androidApp-debug.apk`. Android Studio can open the repository. The app targets API 36 and requires API 26 or later; the synthetic instrumentation harness requires API 29 or later.

Set the app's **Evidence server** field to the server address. Android Emulator uses `http://10.0.2.2:8080` for the host machine; a physical device needs the host's reachable LAN address. To accept LAN connections, the operator can start Uvicorn with `--host 0.0.0.0` in a trusted network.

The **Sources** workspace can inspect a selected SAF directory without a prepared seed. It scans supported files read-only, keeps distinct URI identities and reports unsupported formats or incomplete coverage. Select exact audio URIs for explicit upload to the configured service. An optional private corpus seed remains supported; see [Corpus import](CORPUS_IMPORT.md).

Compilation and host tests do not establish picker, permission, provider or phone behavior. Follow [SAF acceptance](ANDROID_SAF_PROVIDER_ACCEPTANCE.md) for the separate synthetic device harness.

## Read-only source scan

Return to the repository root with the virtual environment active:

```sh
python -m pip install -r server/requirements-scanner.txt
python -m server.app.cli scan /path/to/sources --output /path/outside/sources/scan.json
```

Choose an output outside the source tree. The CLI neither initializes the live database nor copies originals. CSV/TSV and inventory use the standard library; optional adapters enable XLS/XLSX. The manifest records raw observations, source locators, parser coverage, conflicts and limits. Native Android and server parsers have separate profiles; see [Native source formats](NATIVE_SOURCE_FORMATS.md).

For HTTP scanning, configure `EW_SCAN_ROOTS` before starting the service. `/api/source-roots` exposes configured root IDs; `/api/source-scans` accepts a root ID and a relative subdirectory. Client-supplied locators are never treated as server filesystem permissions.

## Metadata and citation export

Use the same `EW_*` configuration as the service, while running from the repository root. Prefer absolute configured paths when changing working directories:

```sh
python -m server.app.cli metadata-export /path/to/new-package.json
python -m server.app.cli metadata-verify /path/to/new-package.json
python -m server.app.cli metadata-archive-import /path/to/new-package.json /path/to/new-archive.sqlite
python -m server.app.cli metadata-archive-export /path/to/new-archive.sqlite /path/to/new-roundtrip.json
```

Outputs must be new files. An inert metadata archive is never a live `EW_DB_PATH`. It preserves canonical metadata, versions and historical errors, with no source media, live restore, job resumption or inference replay. See [Metadata archive](METADATA_ARCHIVE.md).

Export a saved citation by its artifact and anchor IDs, then inspect it independently:

```sh
python -m server.app.cli citation-export 1 1 /path/to/new-citation.json
python -m server.app.exchange_consumer /path/to/new-citation.json
```

The consumer checks the exact pinned transcript and selector without opening source locators. A packet contains no media bytes and is not anonymized: source metadata may contain personal information. See [Evidence exchange](EVIDENCE_EXCHANGE.md) and [Independent consumer](EVIDENCE_CONSUMER.md).

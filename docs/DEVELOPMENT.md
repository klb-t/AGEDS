# Development and local validation

Read [AGENTS.md](../AGENTS.md), [HANDOFF.md](../HANDOFF.md) and the current coordination index before changing shared contracts. Use synthetic data for tests; private source material belongs outside the repository.

## Python and JavaScript checks

From the repository root, with Python 3.12 and Node available:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r server/requirements-test.txt
python -m pytest server/tests -q
node --test server/tests/js/*.test.mjs
```

`requirements-test.txt` includes the service and scanner requirements. Tests use temporary synthetic fixtures. The default suite needs no ASR model download; real inference is a separate acceptance run. These commands run locally and do not require GitHub Actions. The optional [Manual verification workflow](../.github/workflows/android.yml) starts only through `workflow_dispatch`; pushes and pull requests do not automatically trigger it.

## Android and shared Kotlin checks

Use the full JDK 21 and Android SDK described in [Getting started](GETTING_STARTED.md):

```sh
./gradlew :androidApp:assembleDebug :androidApp:testDebugUnitTest :core:desktopTest
```

For an isolated source snapshot, recorded input hashes, test XML and APK signature verification:

```sh
./scripts/build_verify.sh /path/to/empty-build-results
```

Set `JAVA_HOME` and `ANDROID_HOME`; the result directory must be empty. The script compares source hashes before and after the run and checks the generated debug APK. A successful build proves compilation and the executed JVM suite, without implying a device test.

Synthetic SAF instrumentation is separate:

```sh
./scripts/verify_saf_provider.sh
./scripts/verify_saf_provider.sh --run
```

The first command probes runtime capability. The second installs the app and test APK on an available API 29+ Android runtime and runs six synthetic provider scenarios. See [SAF provider acceptance](ANDROID_SAF_PROVIDER_ACCEPTANCE.md) for the exact acceptance gate and picker/grant limitations.

## Browser acceptance

`server/tests/browser/acceptance.cjs` starts an isolated Python fixture with generated WAV, transcript versions and SQLite data. It checks real Chromium citation selection, save-response consistency, stale responses, packet download and audio-range playback.

Install Playwright and its Chromium in a local tool environment, then make `require('playwright')` available to Node. For example, with a newly chosen external directory:

```sh
npm install --prefix /path/to/browser-tools playwright@1.62.1
/path/to/browser-tools/node_modules/.bin/playwright install chromium
NODE_PATH=/path/to/browser-tools/node_modules PYTHON=/path/to/AGEDS/.venv/bin/python node server/tests/browser/acceptance.cjs
```

`AGEDS_BROWSER_EXECUTABLE` can select an already installed Chromium. Set `AGEDS_BROWSER_RECEIPT` to a new output path to retain the run receipt. The runner uses a temporary synthetic database, never the operator's live catalog. Node unit tests and source review are different evidence from this browser runtime check.

## Optional experiments and smoke runners

The reproducible runners remain in [scripts/](../scripts/), with synthetic fixtures beside the tests:

| Runner | Scope |
|---|---|
| `real_asr_smoke.py` | Real faster-whisper inference, worker publication, word citation and inert archive roundtrip on generated speech. |
| `verified_asr_smoke.py` | Real inference through the verified descriptor reader. |
| `server_wav_scan_smoke.py` | Source preservation, WAV scan and metadata export through actual CLI commands. |
| `archive_budget_cli_smoke.py` | Inert archive roundtrip and bounded rejection through CLI commands. |
| `csv_cli_smoke.py` | Exact CSV decoded raw records and source preservation through the CLI. |
| `compare_wav_headers.py` | Shared synthetic WAV fixtures compared between Kotlin and Python header parsers. |

Read a runner's arguments and dependency prerequisites before invoking it; ASR needs a local model and FFmpeg speech synthesis, while cross-runtime comparison requires Kotlin/JVM tooling. Keep new outputs outside the repository's source data and preserve failed attempts alongside a later success. Existing experiments and receipts are indexed in [Historical development records](archive/2026-09-30_2026-10-01/README.md); their source hashes and environment remain specific to each run.

## Upgrade a live installation

Stop all old workers and the service before migration. Back up the SQLite database together with its content store, then verify the backup before starting the new code. Startup performs additive migrations while retaining legacy data; absent historical provenance stays unknown. An already running old worker does not gain new lease-publication checks by changing files on disk.

Do not configure an inert metadata archive as the live `EW_DB_PATH`. It is a separate export format with historical jobs represented as inert data.

## Documentation layout

Active contracts stay in `docs/`. New acceptance evidence belongs in `docs/validation/`; historical snapshots stay in `docs/archive/` with their original contents. The archive manifest maps prior paths and records hashes, including initial failures and blocked execution. [CHANGELOG.md](../CHANGELOG.md) describes functional increments; [HANDOFF.md](../HANDOFF.md) describes the current actionable state.

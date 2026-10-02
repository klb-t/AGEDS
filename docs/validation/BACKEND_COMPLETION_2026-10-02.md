# Backend and CLI acceptance — 2026-10-02

The unchanged backend at source baseline `b6abe86` passed **429 tests and
588 subtests**, with no failures or skips. A fresh Python 3.12 virtual environment
installed `server/requirements-test.txt`. Three deprecation warnings are recorded;
they concern the existing FastAPI startup hook and do not represent test failures.
[Receipt](BACKEND_COMPLETION_2026-10-02.json) includes dependency versions and
source fingerprints.

Actual CLI subprocesses also passed on generated synthetic data:

| Check | Receipt |
|---|---|
| Inert archive roundtrip, semantic budgets, no partial outputs | [Archive](ARCHIVE_CLI_2026-10-02.json) |
| CSV physical records, Unicode controls and explicit truncation | [CSV](CSV_CLI_2026-10-02.json) |
| Bounded WAV header scan, declarations and unchanged sources | [WAV](WAV_CLI_2026-10-02.json) |

```bash
.venv/bin/python -m pytest server/tests -q
.venv/bin/python scripts/archive_budget_cli_smoke.py --work-dir /absolute/new/archive-check
.venv/bin/python scripts/csv_cli_smoke.py --receipt /absolute/new/csv-receipt.json
.venv/bin/python scripts/server_wav_scan_smoke.py --work-dir /absolute/new/wav-check
```

Each work directory must be new. These checks use no private corpus, restore no
live database and exercise no Android provider. WAV declarations and ASR timings
are not measured acoustic truth.

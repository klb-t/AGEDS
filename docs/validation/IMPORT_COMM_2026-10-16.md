# Communication, Takeout and media importers — acceptance 2026-10-16

Task `AGEDS-TASK-20261016-B` part 2 (track B2), branch `wt-B2`, tested head
`49eec5b` (code and tests); the following commit updates documentation and this receipt.
Machine-readable receipt: [IMPORT_COMM_2026-10-16.json](IMPORT_COMM_2026-10-16.json).
Feature description: [IMPORTERS_COMM.md](../IMPORTERS_COMM.md).

| Check | Command | Result |
|---|---|---|
| Backend suite | `/home/user/wt/venv/bin/python -m pytest server/tests -q` | 487 passed (baseline 434), 588 subtests, 0 failed |
| New tests | `pytest server/tests/test_import_comm.py server/tests/test_import_comm_api.py -q` | 53 passed |
| Node | `node --test server/tests/js/*.test.mjs` | 85 passed (no JS changed) |
| Repository | `python3 scripts/verify_repository.py` | passed after adding this receipt |

## Runtime-executed

* All 12 new CLI commands on a freshly generated synthetic corpus: imports exit 0; a
  `calls.xml` with a DTD/entity and location/browser history without opt-in exit 2 with a
  JSON error; SHA-256 of all 34 corpus files unchanged afterwards.
* Live `uvicorn` (run at `de48eb4`) with scratch `EW_*` directories: e-mail upload (6 messages, PDF + inline
  image children), ICS upload (8 events, DST gap/fold flagged), location upload 403 without
  and 200 with the opt-in flag, WhatsApp folder import through a configured
  `EW_SCAN_ROOTS` path, and the recurrence projection returning DST-shifted UTC instants.

## Only unit-tested / not run

Remaining endpoints were exercised through FastAPI `TestClient`. No real exports were
available: Gmail, Takeout, WhatsApp, operator bills and recorder file names are synthetic;
shipped profiles and patterns are examples. XLS (BIFF8) billing input, UI and Android were
not run. No model, provider or network calls are made by these importers.

## Failure found and fixed during acceptance

A manual CLI run passed a location `Timeline.json` to `import-takeout-activity`; the first
version unwrapped the JSON object and imported location segments as activity records,
bypassing the location opt-in. The importer now accepts only a list of My Activity
records and skips objects without My Activity keys; a regression test covers both.

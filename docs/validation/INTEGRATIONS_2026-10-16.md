# Integrations acceptance — 2026-10-16 (task G)

Receipt: [INTEGRATIONS_2026-10-16.json](INTEGRATIONS_2026-10-16.json). Code under
test: branch `wt-G` at `5974b5e` on top of `9c1d513` + F0 `f09e8f7`.
Feature docs: [Read API](../READ_API.md), [Ecosystem adapters](../ECOSYSTEM_ADAPTERS.md).

| Check | Result |
|---|---|
| `/home/user/wt/venv/bin/python -m pytest server/tests -q` | 469 passed, 658 subtests passed (434 pre-existing + 35 new) |
| New integration suites only | 35 passed, 70 subtests |
| `python3 scripts/verify_repository.py` | OK: 108 snapshots, 51 pages, 245 links |
| Node tests | not run (no JavaScript changed) |

**Runtime-executed:** every read/integration route and the `/integrations`
page through FastAPI TestClient on a fresh SQLite database; `EW_REQUIRE_TOKEN=1`
on legacy routes (including streamed content and multipart upload); real CLI
subprocesses for tokens, `loom-export`, `loom-import-hypotheses`, hypothesis
promotion and proposal acceptance; read-only receipts (database bytes unchanged,
`open()` patched to fail during reads).

**Unit-tested only:** the published intent JSON Schema is enforced by
hand-written validation (no `jsonschema` in the venv); the Loom vocabulary
mapping is documentation, not codec validation.

**Not run:** a live ChatADHD/Loom process against a live service; the Android
client under token enforcement (no token setting in the app); any model call;
browser acceptance of the new page.

**Boundaries:** tokens cover HTTP routes only (no rate limiting, HTML UI open);
timeline windows are raw-string windows with `display_time: null`; counterparties
are hypotheses; new tables are outside the metadata package; `annotations` keeps
no provenance column (proposal row + audit are the record).

**Shared edit:** `server/app/api/__init__.py` gained a three-line
`install_app` hook. **Files outside the listed scope by name:**
`server/app/templates/integrations.html`, `server/tests/test_integrations_support.py`.

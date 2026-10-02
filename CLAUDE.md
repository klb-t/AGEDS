# AGEDS — start here

Read [HANDOFF.md](HANDOFF.md), [coordination/state.json](coordination/state.json),
[AGENTS.md](AGENTS.md) and [architecture rules](docs/ARCHITECTURE_RULES.md) at the
same checkout revision. Current status is authoritative; chat recollection and
archived plans are context. Do not resume expired overnight claims.

AGEDS currently provides an Android-first Evidence Workbench backed by FastAPI,
SQLite and a local transcription worker. It preserves source acquisitions,
versions derived text, pins citations and exports inert metadata. The five-layer
legal-analysis standard and ecosystem adapters remain future work.

## Development boundaries

- Scan originals read-only. Write metadata outside the selected source tree.
- Content identity, acquisition provenance, parser observations, ASR output and
  human interpretation are separate. Preserve unknowns and conflicts.
- A word citation requires stored word timings and exact version/occurrence;
  text/hash agreement does not verify spoken truth or acoustic alignment.
- A worker publishes only with its valid lease. Stop old workers and back up
  both SQLite and the content store before upgrading an existing installation.
- Exported locators are inert. Metadata archives do not restore live jobs,
  include source media or provide a cryptographic custody signature.
- Case groups and priority defaults belong in a private seed; no private names,
  addresses, recordings, credentials or database records in tracked files.
- JNI sources are an unconnected prototype. Do not advertise on-device ASR.
- GitHub Actions is manual only. Run checks locally; do not dispatch paid jobs
  or use paid models merely to obtain a receipt.
- Preserve experiments, failures and receipts. Add a new result rather than
  editing a historical snapshot. Keep old branches and incremental history.

## Local acceptance

```bash
python -m venv .venv
.venv/bin/pip install -r server/requirements-test.txt
.venv/bin/python -m pytest server/tests -q
node --test server/tests/js/*.test.mjs
python3 scripts/verify_repository.py
./scripts/build_verify.sh /absolute/new/output-directory --include-instrumentation
```

The Android command needs full JDK 21, SDK platform 37 and the pinned wrapper.
Browser acceptance additionally needs Playwright and real Chromium; see
[development](docs/DEVELOPMENT.md) and current receipts. APK compilation,
JVM tests, instrumentation compilation and SAF execution are separate claims.

Claim a stable task ID and explicit file scope in the current coordination
ledger before parallel edits. Commit small checked increments with `[skip ci]`;
report exact executed checks and blockers. Avoid repeating already accepted
work. Routine reversible decisions remain autonomous within the user's mandate.

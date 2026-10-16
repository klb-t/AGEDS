# End-to-end acceptance — 2026-10-16 (task Z)

Branch `wt-Z`, base `8e491f7`, tested source `48994d2`. [Receipt](E2E_ACCEPTANCE_2026-10-16.json) ·
[criterion → evidence matrix](../ACCEPTANCE.md).

The run is one reproducible pass through the real CLI (subprocesses, fresh `EW_DATA_DIR`/`EW_DB_PATH`/
`EW_STORE_DIR`, `EW_SCAN_ROOTS` naming three generated source roots, `EW_REPORTS_DIR` outside the
repository) and the real service (`uvicorn`, `EW_REQUIRE_TOKEN=1`). The input is a synthetic multi-device
layout composed from the corpus parts.

## Result

| Run | Checks | Criteria | Commands | Wall time |
|---|---|---|---:|---:|
| default + PDF (`scripts/e2e_acceptance.py … --pdf-chromium …`) | 52/52 passed | 19 met, 0 not met, 3 not verifiable in a run | 98 | 36.9 s |
| heavy + PDF (`--heavy`: adversarial fourth collection, full sensitivity grid) | 53/53 passed | same | 105 | 43.2 s |

Not verifiable in a run: K5, which these local suites cover, N4 (a change-set property) and N5 (a
documentation property). All have explicit notes in the receipt.

## Measured stage timings (default + PDF, seconds)

| layout | collections | catalog | import_plan | reimport | catalog_ingest | asr | service_and_citation | timeline |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.34 | 0.67 | 1.40 | 4.36 | 7.52 | 0.27 | 1.21 | 1.20 | 0.82 |

| time_budget | hypothesis | reports | citations | metadata | read_api | originals | repository |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1.41 | 1.37 | 13.76 | 0.55 | 0.65 | 0.25 | 0.17 | 0.98 |

Reports dominate: five Chromium PDF prints. Each figure is a single measurement on this container.

## Local suites

| Command | Revision | Result |
|---|---|---|
| `python -m pytest server/tests -q` | `c7a3182` | 1074 passed, 1 skipped, 764 subtests, 0 failed (base: 1047) |
| `python -m pytest server/tests/test_e2e_acceptance.py server/tests/test_e2e_regressions.py -q` | `48994d2` | 27 passed |
| same e2e module with `AGEDS_E2E_HEAVY=1 AGEDS_E2E_PDF_CHROMIUM=…` | `be61c3b` | 23 passed |
| regression tests without the fixes (`git stash push server/app`) | before fixes | 4 failed (each test fails without its fix) |
| `node --test server/tests/js/*.test.mjs` | `c7a3182` | 127 passed |
| 8 browser scripts with real Chromium 141 | `be61c3b` | all exit 0; 86 PASS (acceptance 24, analysis_budget 9, hypotheses 10, importplan 9, media 7, reports 10, timeline 8, workbench 9) |
| `python3 scripts/verify_repository.py` | receipt commit | exit 0 |

Between `c7a3182` and `48994d2` only two strings in `server/tests/e2e/pipeline.py` changed. Android was not
changed and was not run.

## Defects found and fixed (shared edits, each with a regression test)

1. **`metadata-verify` failed on any database with a file-name recording import.** Audit rows said
   `object_kind='artifact'` with a NULL id. New rows name no target. The verifier reads historical rows of
   that shape as the warning `audit_target_id_missing`, so `audit_log` is never rewritten. Files:
   `importers/comm_common.py` and `packages.py`.
2. **`import-plan-decide --approve A … --approve B` kept only the last list**, so typed decisions were
   silently lost. Fix: `commands/importplan.py` uses `action='extend'`.
3. **The time-budget method record named `analysis_source.v1`** while `display_time@1` placed the items.
   Fix: `analysis/budget.py` uses rule `time_budget.v2`, and the method takes seam, rule and SHA-256 from the
   run input. v1 runs stay stored with their hash and still render.

## Runtime-executed vs. not

Runtime-executed in the run: catalog, import plan with human decisions, re-import idempotency (plain,
`--rerun-done`, re-scan with a new plan), ASR job wiring with a fake model, timeline and correlations,
time budget, hypothesis analyzer, five report types with PDF, verification and tamper detection,
citation bundles, metadata package, read API with token and revocation, originals' tree state, and the
repository privacy check.

Not run or not claimed:

- real speech recognition: the PL/NL/EN fixture is tones and the model is a fake;
- event-study calibration: only determinism and warnings are asserted, and no p-value is claimed while
  the null models are being fixed;
- OCR and diarization: adapters unavailable here;
- queue-mode import with separate workers and crash recovery: covered by unit tests;
- Android/SAF and the Loom/ChatADHD adapters beyond the read API.

The verifier's historical-row branch is unit-tested only.

# Hypothesis analyzer receipt — 2026-10-16 (AGEDS-TASK-20261016-E, track E2)

Branch `wt-E2` from `4fc0627`, with `ccr-d25c3cb3-qgbhql` (b181c21) and `wt-E` (15aeb6b, E1 categories
API) merged in. Tested source head `0b70e72`. Full data: [HYPOTHESES_2026-10-16.json](HYPOTHESES_2026-10-16.json).
Feature document: [Hypothesis analyzer](../HYPOTHESES.md).

| Check | Result |
|---|---|
| `pytest server/tests -q` (shared venv, Python 3.13.16) | 835 passed, 1 skipped, 680 subtests, 0 failed (788 after merges without E2 tests; 557 on the base) |
| `pytest server/tests/test_hypotheses_*.py -q` | 47 passed |
| `node --test server/tests/js/*.test.mjs` | 104 passed (9 new chart/page-helper tests) |
| `node server/tests/browser/hypotheses.cjs` (real Chromium) | 10 PASS |
| `node server/tests/browser/acceptance.cjs` (existing suite) | 24 PASS |
| `scripts/verify_repository.py` | passed |

**Runtime-executed:** CLI subprocesses for every command; a uvicorn server on a temporary synthetic
database driven by real Chromium (charts, WebCrypto check of the disclaimer hash in all six sections,
always-visible warnings, forms for registration, versioning, runs, classification, theses and disclosure,
phone width); a 20 000-event probe (~3.5 s per 999-permutation run).

**Unit-tested only:** the statistical claims on generated scenarios (planted effect detected; density-burst
null significant only before normalization, with the detection-bias warning; weekday confound removed by
the stratified null; clustered stimuli counted and flagged; unknown-zone and coarse-time exclusions),
immutability and chain triggers, tamper refusal, determinism, the p-value formula and the category adapter.

**Not run:** any real LLM provider (only `none` exists), private data, the timeline-delegated analysis seam
(E1 has not switched `analysis/source.py` yet; runs record `analysis_source.v1` with rule hash `unknown`),
Android.

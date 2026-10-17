# Review 2 fixes — receipt (2026-10-16)

Task `AGEDS-TASK-20261016-R2`, base `8e491f7`, head `ebdf5af` (merged with the integration head that includes the end-to-end acceptance). Findings: second adversarial review of `b181c21..8e491f7`. Machine-readable: [REVIEW2_FIXES_2026-10-16.json](REVIEW2_FIXES_2026-10-16.json).

The R2 agent committed every fix, regression test and owning-doc update, then stopped at a usage limit before writing this receipt; the coordinator ran the checks and wrote it.

| # | Severity | Finding | Status | Commits | Decision |
|---|---|---|---|---|---|
| 1 | high | Permutation/stratified nulls placed stimuli on outcome times (miscalibrated, p=0.001 under H0) | fixed | `794a652` | Event-study method v2: outcome (and borrowed density) events are not eligible stimulus positions; H0 calibration tests for permutation, stratified and block nulls. Stored v1 runs stay as historical records. |
| 2 | medium-high | Budget/feature runs held the SQLite write lock during grid computation | fixed | `2b82384` | Compute first, then store label run + run in one short transaction. |
| 3 | medium | Full grid product materialized before the cap (memory exhaustion via HTTP) | fixed | `57c6654` | Lazy deduplicated grid with early stop; totals from distinct axis sizes. |
| 4 | medium | Projection eviction between refresh and read silently emptied analysis inputs | fixed | `0e03694, 2b7fdbc` | Reads in one snapshot re-checked for eviction; retry, then 503 / ProjectionNotFresh; never an empty stored run. |
| 5 | medium | DNS rebinding passed the CSRF guard (Origin compared with Host) | fixed | `fe60a82` | Host allowlist: localhost, IP literals, EW_TRUSTED_HOSTS / EW_TRUSTED_ORIGINS; Origin checked against the allowlist. Named LAN host names need explicit configuration (documented in GETTING_STARTED). |
| 6 | medium | EW_REQUIRE_TOKEN left data-bearing pages and report files open | fixed | `fe60a82` | All pages and report files need the token (session cookie for browsers); login form and static assets stay open. |
| 7 | medium | Post-hoc re-tagging changed results without a preregistration deviation flag | fixed | `794a652` | Selection-drift and post-registration edit checks; surfaced in result and disclosure. |
| 8 | low-medium | count_both remainder could invert lower/probable/upper | fixed | `57c6654, 99de5e1` | Rule time_budget.v3 with RULE_HISTORY entry; e2e expectations follow. |
| 9 | low | Exact Fraction arithmetic made two-sided normalized summaries very slow and uncounted | fixed | `794a652` | Integer cross-multiplication for extremes, fsum for displayed means, cost in the work estimate. |
| 10 | low | Import-plan heartbeat stopped after one failed renewal | fixed | `4d2e8bf` | Retry with backoff; stop only when the lease was taken over. |
| 11 | low | Unbounded numeric decoding options over HTTP | fixed | `4d2e8bf` | Bounded decoding/VAD/PDF/speaker override values. |
| 12 | low | Block permutation wrapped a full block onto slot 0 with a partial last block | fixed | `794a652` | Full-block permutation; at least two full blocks required. |
| 13 | low | Synthetic fake model provider reachable over HTTP | fixed | `57c6654` | Gated behind EW_ANALYSIS_TEST_PROVIDERS. |
| P1 | plausible | Long recordings starting before the load margin lost confirmed time | real, fixed | `2dd74fd` | Items loaded up to duration_lookback_s (default 24 h) when their declared duration reaches the window. |
| P2 | plausible | Each rejected token took the write lock | real, fixed | `fe60a82` | Repeated rejections counted lazily. |
| P3 | plausible | Disclosure undercounted theses without accepted evidence | real, fixed | `4e30db3` | Disclosure counts theses whose pointers reach only candidate/rejected items, next to theses without any pointer. |
| P4 | plausible | /api/timeline with assume_zone had the same eviction race | real, fixed | `0e03694` | Same snapshot re-check as #4. |

## Checks

- Full backend suite: 1204 passed, 1 opt-in skip, 764 subtests; the two e2e failures before this receipt existed were the repository link check pointing at this file.
- `server/tests/test_review2_*.py`: 132 passed. Node: 127 passed.
- Real Chromium: acceptance 24, timeline 8, workbench 9, reports 10, time budget 9, hypotheses 10, import plan 9, media 7 PASS; catalog page check passed.

## Method versions

- Event study **v2**: outcome and borrowed density events are not eligible stimulus positions; nulls are calibrated under independence in tests. Stored v1 runs are kept as historical records, not rewritten.
- Time budget **`time_budget.v3`**: layered `count_both` remainder keeps lower ≤ probable ≤ upper.

## Not verified

- No real DNS-rebinding page was served; the Host allowlist is tested with crafted headers.
- The Android client against a named LAN host needs `EW_TRUSTED_HOSTS`; IP-literal hosts work by default.
- Calibration is shown on synthetic H0 data only.

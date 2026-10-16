# Review fixes @ b181c21 — receipt (task R, 2026-10-16)

Tested head `943b32e` (branch `wt-R`, base `b181c21`); the receipt commit changes
documentation only. Machine-readable: [REVIEW_FIXES_2026-10-16.json](REVIEW_FIXES_2026-10-16.json).

| Check | Result |
|---|---|
| `pytest server/tests -q` | 846 passed, 1 skipped, 680 subtests (base: 765 passed) |
| `pytest server/tests/test_review_fixes_*.py` | 81 passed |
| `node --test server/tests/js/*.test.mjs` | 95 passed |
| `scripts/verify_repository.py` | OK |
| `server/tests/browser/acceptance.cjs` (Chromium 141) | 24 PASS, exit 0 |
| `server/tests/browser/timeline.cjs` | 7 PASS, exit 0 |
| `test_catalog_browser.py` (real Chromium) | passed |

Each confirmed finding first got a failing regression test (run against the unfixed
code), then the fix. The reviewer's reproducers were re-run against this worktree from
a scratch copy only; tests never depend on them.

| # | Status | Regression tests (`server/tests/test_review_fixes_*.py`) | Change (docs) |
|---|---|---|---|
| 1 | fixed | `test_finding1_*` (7), `test_finding7_builds_from_older_code_are_rebuilt` | `ingest_file(source_stat=…)`, `temporary_copy_acquisitions()`; catalog ingest declares the catalogued mtime; projection file_mtime only from `source_path`/`declared`; marked legacy rows ignored ([TIMELINE](../TIMELINE.md), [IMPORTERS_COMM](../IMPORTERS_COMM.md), [CATALOG](../CATALOG.md)) |
| 2 | fixed | `test_finding2_*` (2) | numeric offsets ≥ 24 h / minutes ≥ 60 → local candidate `numeric_offset_out_of_range` |
| 3 | fixed | `test_finding3_*` (3, 21 cases) | HTTP override allowlist `CLIENT_OVERRIDE_KEYS` ([MULTILINGUAL_ASR](../MULTILINGUAL_ASR.md)) |
| 4 | fixed | `test_finding4_*` (2) | one short transaction per catalog write; worker claim backoff ([CATALOG](../CATALOG.md)) |
| 5 | fixed | `test_finding5_*` (4) | child locators `<container>@sha256:<hash>!/…` (parser v2; v1 containers stay v1); additional parent observed ([IMPORTERS_COMM](../IMPORTERS_COMM.md)) |
| 6 | fixed | `test_finding6_*` (4, 12 cases) | CSRF guard; `EW_REQUIRE_TOKEN` covers `/export/*` + state changes; session cookie ([READ_API](../READ_API.md)) |
| 7 | fixed | `test_finding7_*` (4) | projection keyed by zone params, label per request, cap + eviction log ([TIMELINE](../TIMELINE.md)) |
| 8 | fixed | `test_finding8_*` (2) | read-only verification, lazy use bookkeeping, rejection counts ([READ_API](../READ_API.md)) |
| 9 | fixed | `test_finding9_timeline_csv_guards_every_text_column` | every CSV text cell guarded |
| 10 | fixed | `test_finding10_*` (4) | expat declaration handlers refuse DOCTYPE/ENTITY in any encoding |
| 11 | fixed | `test_finding11_exdate_and_until_are_compared_in_the_dtstart_zone` | `ics_recurrence_projection/v2` |
| 12 | fixed | `test_finding12_bound_offsets_out_of_range_are_input_errors` | 422 instead of 500 |
| 13 | fixed | `test_finding13_client_hypothesis_list_filters_origin_before_limit` | origin filtered in SQL |
| U1 | confirmed, fixed | `test_u1_single_file_scan_keeps_temp_db_and_store_out_of_its_directory` | single-file root protects its directory |
| U2 | confirmed, fixed | `test_u2_benchmark_output_never_inside_the_audio_directories` | benchmark output refused inside audio dirs |
| U3 | not touched | — | `server/app/analysis/` belongs to track E1 |
| U4 | confirmed (low), fixed | `test_u4_test_double_provider_is_not_offered_in_the_ui_by_default` | `fake` listed only with `EW_CATALOG_TEST_PROVIDERS=1` |
| U5 | claim not reproduced; related edge fixed | `test_u5_7z_duplicate_names_count_every_member_read_into_memory` | py7zr bounds a member by its declared size (source read, not executed); same-named members now counted |
| U6 | confirmed, fixed | `test_u6_*` (6 cases) | `*_paths` parameters validated → 422 |

Schema additions: timeline 32 (`timeline_projection_evictions`), integrations 73
(`api_token_rejections`). Not verified: 7z/RAR execution, behaviour on a real
pre-existing database, a real cross-site attack page, scan commit cost beyond 5,000
small synthetic files.

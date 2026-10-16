# Time budget and feature series — validation 2026-10-16 (task E, part 1)

Machine-readable receipt: [TIME_BUDGET_2026-10-16.json](TIME_BUDGET_2026-10-16.json). Feature
contract: [TIME_BUDGET.md](../TIME_BUDGET.md).

Tested on branch `wt-E` at `4e035bd` (after merging integration head `b181c21`, which
includes tracks A, B1, B2, C, D and G). The receipt commit adds documentation only. No
model, provider or network call was made.

| Check | Result |
|---|---|
| `pytest server/tests -q` | 797 passed, 1 skipped, 680 subtests |
| E1 tests (budget 14, API/CLI 4, source seam 4, categories 7, intervals 4) | 33 passed |
| `node --test server/tests/js/*.test.mjs` | 102 passed (7 budget chart helpers) |
| `server/tests/browser/analysis_budget.cjs` (real Chromium 141, generated corpus) | 9/9 cases, no page errors; only the deliberate invalid-parameter 422 response |
| Existing `server/tests/browser/acceptance.cjs` / `timeline.cjs` | 24 PASS lines / 7 PASS lines, exit 0 |
| `scripts/verify_repository.py` | OK |

## Planted hard cases (server/tests/corpus/activity.py)

Parallel Claude/ChatGPT conversations of one category (union, not sum); a three-day gap
(nieobserwowane in all variants, never idle); DST days of 23 h and 25 h; an official call
overlapping a chat (overlap rules, layering); calls with durations and a 2 s WAV recording
(lower bound); a calendar entry split 0.5/0.5 by one rule; a human correction overriding a
keyword rule; reasoning parts and hidden chat messages (excluded, counted); unknown-zone
wall clocks, a nonexistent DST time and an ambiguous DST fold (excluded and reported, fold
carried only on request); a day where typing/reading speeds flip the top category (flagged
unstable); determinism and new-parameter runs; append-only triggers.

## Measured (synthetic, one container, default 11-point grid, 90 days)

| Events | First run | Repeat (reused result) |
|---|---|---|
| 20 000 | 9.3 s | 7.4 s |
| 100 000 | 43.4 s | 38.4 s |

## Boundaries

Runtime-executed: everything above on synthetic data through the timeline projection.
Unit-tested only: the labelled interim fallback seam and the chart geometry helpers. Not
run: real model providers (none exist; `none`/`fake` only), private data or a private
category file, Android. Results are interpretive estimates; upper bounds of different
categories may overlap; candle event lists are capped (exact counts kept).

# Integration of all tracks (task X, 2026-10-16)

Tested head `e2aaa9c` on branch `wt-X`. The base is `cab22bc`, with the
integration branch at `1fcfe53` merged (track R review fixes). Machine record:
[INTEGRATION_2026-10-16.json](INTEGRATION_2026-10-16.json).

| Check | Result |
|---|---|
| `pytest server/tests -q` | 1047 passed, 1 skipped (opt-in chat scale test), 764 subtests |
| `node --test server/tests/js/*.test.mjs` | 127 passed |
| `scripts/verify_repository.py` | passed |
| Browser `acceptance.cjs` | 24 PASS |
| Browser `timeline.cjs` | 8 PASS (new bookmarks case) |
| Browser `workbench.cjs` | 9 PASS |
| Browser `reports.cjs` | 10 PASS (new analysis-report case) |
| Browser `analysis_budget.cjs` | 9 PASS |
| Browser `hypotheses.cjs` | 10 PASS |
| Browser `importplan.cjs` | 9 PASS |
| Browser `media.cjs` | 7 PASS |
| Browser catalog (`test_catalog_browser.py`) | 1 passed |

At the start, `cab22bc` had 11 failures and 21 errors in pytest. Merging R
added three: unclassified tables, xfails that now pass, and the CSRF error code.
All of them are resolved.

## What changed

- **Reports stay read-only.** The timeline projection is a derived cache. It is
  refreshed in its own write transaction before the read-only snapshot. Inside
  the snapshot it must be fresh (`read_projection` under `PRAGMA query_only`);
  otherwise the snapshot is retried up to three times and then refused.
  `method.derived_caches` records the rule id, version and SHA-256, the
  parameters, the projection and build id, and whether this report triggered
  the build. A test digests every evidence and analysis table before and after
  a report.
- **Wired sections.**
  - **Timeline** (area C, `timeline/report.py`): projection identity, placement
    by basis and precision, flags, transcript segments, and correlation
    hypotheses with their review status (no probability, labelled as not facts).
  - **Time budget** (area E1, `analysis/budget_report.py`): a stored run with
    its result hash rechecked, totals, candles, the `nieobserwowane` band,
    sensitivity, category definitions and exclusions.
  - **Hypothesis dossier** (area E2, `analysis/dossier_report.py`): the version
    hash chain, dossier, theses with computed `unsupported` flags, event-study
    windows and warnings, and disclosure. It reuses E2's disclaimer and bundles
    the anchors pinned in the dossier.
  - Every number in the HTML resolves to `report.json`. This is tested for all
    report types with real data.
- **Analysis contract.**
  - A DST gap under an operator zone is `nonexistent_local_time` on the timeline
    seam. Changed expectation: E1 corpus. This is more correct, because 02:30
    does not exist on that day.
  - `no_candidates` maps to `no_time_candidate`. Changed expectation: the
    timeline contract test.
- **Bookmarks on the swimlanes.** Workbench bookmarks are drawn in a strip as a
  human annotation layer, rendered through `textContent` only. Zone-less
  bookmarks are counted and not placed. `timeline.css` uses the `app.css`
  tokens; the page does not opt in to the light theme yet.
- **Metadata package.** New tables are declared or excluded with a reason, as
  listed in the JSON receipt.
- **Import planner.** Materialized members declare the catalogued mtime. The
  xfails for review findings #1 and #5 were removed, because both tests pass.

Not run: the PDF format, the light theme of `/timeline`, and Android builds.
The stale-snapshot path is only unit-tested, with a monkeypatched refresh.

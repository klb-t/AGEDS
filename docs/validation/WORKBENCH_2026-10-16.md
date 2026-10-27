# Workbench acceptance — 2026-10-16 (task H)

Receipt: [WORKBENCH_2026-10-16.json](WORKBENCH_2026-10-16.json). Code under test:
branch `wt-H` at `4114284` on top of `887cc55` (F0 + track G). Feature doc:
[Workbench](../WORKBENCH.md).

| Check | Result |
|---|---|
| `/home/user/wt/venv/bin/python -m pytest server/tests -q` | 526 passed, 742 subtests (471 pre-existing + 55 new) |
| New workbench suites only (5 files) | 55 passed, 84 subtests |
| `node --test server/tests/js/*.test.mjs` | 98 passed (85 pre-existing + 13 new) |
| `node server/tests/browser/acceptance.cjs` (real Chromium 141) | 24 PASS, no page errors |
| `node server/tests/browser/workbench.cjs` (real Chromium 141) | 9 PASS, no page errors; only console error is the automatic `/favicon.ico` 404 |
| `python -m server.app.cli workbench-theme-report` | all 22 text pairs ≥ 4.5:1 in dark and light (minimum 4.57 light `--warning`/`--badge-model`, 4.63 dark button hover) |
| `python -m server.app.cli workbench-i18n-check` | complete: 373 Polish keys; NL/EN 0 missing, 0 extra, 0 placeholder mismatches |
| `python3 scripts/verify_repository.py` | OK: 108 snapshots, 54 pages, 256 links, 347 privacy-scanned files |

**Runtime-executed:** all `/api/workbench` routes and `/workbench` pages in
PL/NL/EN on fresh SQLite per test; index triggers, migration-80 backfill,
drift detection and rebuild; adversarial FTS input and VM-budget exhaustion
(503 without partial result); Polish diacritics including ł folding in the
separate index; real CLI subprocesses (folder export refuses existing files,
`EW_SCAN_ROOTS` and the store); evidence-table digest unchanged by tags,
folders and index rebuild; real Chromium for theme (cookie, server-rendered
choice, `prefers-color-scheme`, blocked storage), language (Accept-Language,
selector cookie, `?lang=`), shortcut overlay and typing protection, palette
jump and layered search, bulk tag add/remove with the audit summary, folder
add/copy-citation/remove, the artifact knowledge panel beside the citation
workspace, inert source HTML.

**Unit-tested only:** conflict detection for page-scoped bindings other areas
may add; sanitising of the per-browser recent list.

**Not run:** large-corpus performance of the index triggers and search; a
screen-reader audit; bookmarks on the timeline page (track C) and catalogue
deep links (track A); Android; browser pages under `EW_REQUIRE_TOKEN=1`.

**Deliberate change to the dark look:** button text `#e6edf3` on `#238636`
measured 3.92:1 (below AA); now `#ffffff` on `#1a7f37` (5.08:1), hover
`#238636` (4.63:1).

**Boundaries:** no authentication beyond the trusted network (a same-origin
guard protects workbench writes only); facet counts are lower bounds above
5,000 matches; workbench tables are not in metadata packages yet; the
citation-workspace items rendered by `citations.mjs` stay Polish in every
language; pages with hard-coded dark CSS keep the dark theme.

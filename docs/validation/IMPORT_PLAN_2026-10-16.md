# Import plan validation (task I, 2026-10-16)

Feature: [Import plan](../IMPORT_PLAN.md). Machine-readable receipt: [IMPORT_PLAN_2026-10-16.json](IMPORT_PLAN_2026-10-16.json).
Code tested at `d7fdf70` on branch `wt-I` (base `e1ce8d2`).

| Check | Result |
|---|---|
| `pytest server/tests -q` | 748 passed, 1 skipped, 2 xfailed, 680 subtests (baseline 719 passed) |
| Import plan tests (3 files) | 29 passed, 2 xfailed (review findings #5 strict, #1 non-strict) |
| `node --test server/tests/js/*.test.mjs` | 88 passed |
| Real Chromium `/import` (`server/tests/browser/importplan.cjs`) | 9 PASS |
| Real Chromium `/media` (`server/tests/browser/media.cjs`, track D pages) | 7 PASS; finding: compare view overflows 390 px by 38 px |
| Existing browser acceptance | 24 PASS |
| `scripts/verify_repository.py` | passed |

Runtime-executed on a generated multi-device layout: exact plan contents (24 items), ambiguous item
not auto-chosen nor bulk-approved, opt-in items disabled by default, billing only with an explicit
profile, full execution through the real importers with collection and virtual-locator provenance,
idempotent re-run (14 table counts unchanged), tampered archive member refused, killed process resumed
without duplicates (counts equal a clean run), lost job lease fenced, cross-export view before/after
import, abandoned catalog scan closed with audit, originals unchanged (tree hashes), temporary
directories removed. Not verified: real exports, large-root timing, 7z/RAR members, multi-process
concurrency. Open review findings #1 and #5 belong to other tracks and are documented by xfail tests.

# AGEDS — current handoff

State: **2026-10-16**, branch `ccr-d25c3cb3-qgbhql` (draft PR #6), based on `main`
@ `9c1d513`. Read [CLAUDE.md](CLAUDE.md), [current coordination](coordination/state.json),
the [increment plan](docs/plans/EVIDENCE_TIMELINE_2026-10-16.md) and the
[validation index](docs/validation/README.md) before continuing. The previous
handoff (2026-10-02 recovery) is preserved in Git history at `9c1d513`.

## What this increment added

Work ran as parallel tracks with disjoint file scopes, integrated sequentially.
Each area owns its migrations (reserved version ranges), router, CLI module,
templates and corpus part through the registries in
`server/app/{schema,api,commands}/__init__.py` and `server/tests/corpus/__init__.py`.

| Stage | Behavior | Contract |
|---|---|---|
| Catalog | Collections per device/account/export; read-only scans through nested ZIP/TAR with virtual locators (`a.zip!/b.zip!/c`); byte signatures over extensions; content groups by SHA-256 with every observation; collisions; coverage with per-basis ranges and gaps; profile → explain → promote descriptions (model output only as hypotheses). | [CATALOG](docs/CATALOG.md) |
| Import plan | Detectors propose importers for catalogued files and archive members; ambiguous items need a human choice, opt-ins stay off; idempotent, resumable runs with verified materialization outside source roots. | [IMPORT_PLAN](docs/IMPORT_PLAN.md) |
| Importers | ChatGPT (tree, shards) and Claude.ai with logical identity across exports; MBOX/EML, ICS, VCF, call logs, billing profiles, recording-name hypotheses, Takeout activity, opt-in location/browser history, WhatsApp media, EXIF. | [chat](docs/IMPORTERS_CHAT.md), [communications](docs/IMPORTERS_COMM.md) |
| Time | `event_times` keeps every observed time with kind, zone status and precision; unknown zones stay unknown; DST folds/gaps are reported. | [plan §5](docs/plans/EVIDENCE_TIMELINE_2026-10-16.md), [timeline](docs/TIMELINE.md) |
| Timeline | Versioned display rule with uncertainty intervals, undated list, transcript segments, swimlanes, density, bookmarks; correlation hypotheses with reviewed status. | [TIMELINE](docs/TIMELINE.md) |
| Media | ASR recipes pinned to jobs; per-segment language strategy (allowed languages, e.g. pl/nl/en); run comparison; WER/CER tool; OCR and diarization adapters. | [MULTILINGUAL_ASR](docs/MULTILINGUAL_ASR.md) |
| Interpretation | Time budgets (lower/probable/upper, unobserved band, sensitivity), feature series; preregistered hypotheses, dossier, thesis typology, permutation event study with warnings and a fixed disclaimer. | [TIME_BUDGET](docs/TIME_BUDGET.md), [HYPOTHESES](docs/HYPOTHESES.md) |
| Reports | Five report types; HTML primary, JSON/CSV/XLSX, optional PDF; method section; every number traceable to `report.json`; verifiable manifests; citation bundles; private output directory only. | [REPORTS](docs/REPORTS.md) |
| Ecosystem | Read API v1 with local hashed tokens, optional enforcement, CSRF and host checks; ChatADHD intents (anchored quotes) and proposals; Loom graph export with quarantine. ChatADHD client stub: klb-t/chatadhd#46. | [READ_API](docs/READ_API.md), [ECOSYSTEM_ADAPTERS](docs/ECOSYSTEM_ADAPTERS.md) |
| Workbench | Layered search with epistemic labels, saved searches, bulk tags, folders, bookmarks, shortcuts, command palette, light/dark themes, PL/NL/EN. | [WORKBENCH](docs/WORKBENCH.md) |

## Acceptance (this session, synthetic data only)

| Check | Evidence |
|---|---|
| Backend | Full `pytest server/tests`: see the latest receipt in [validation](docs/validation/README.md) (1074 passed, 1 opt-in skip at `3ae1b24`, before the second review's fixes). |
| JavaScript | `node --test server/tests/js/*.test.mjs`: 127 passed. |
| Browser | Real Chromium: acceptance, timeline, workbench, reports, time budget, hypotheses, import plan, media scripts and the catalog page check (86 PASS lines at `be61c3b`). |
| End to end | [ACCEPTANCE](docs/ACCEPTANCE.md): one run of the real CLI and service on a generated multi-device layout; 19 of 22 criteria met, 3 not verifiable by a run (covered by suites or by change-set/documentation properties). |
| Reviews | Two adversarial reviews; findings fixed with regression tests ([first](docs/validation/REVIEW_FIXES_2026-10-16.md); second in `REVIEW2_FIXES_2026-10-16`). |
| Privacy | `scripts/verify_repository.py` flags e-mails outside example domains, phone-like numbers outside fictional ranges and home-directory paths in tracked files. |

## Concrete remaining boundaries

- **No real inference ran here.** Hugging Face was blocked and Tesseract/pyannote
  are absent: multilingual ASR, OCR and diarization were exercised with fake
  engines only. Run `asr-benchmark` on a consented corpus before relying on any
  strategy for Polish/Dutch speech.
- **No real exports were available.** Chat, Takeout, MBOX and billing layouts
  follow public observations and data-file mappings; check them against a real
  export (mapping changes need no code change).
- 7z/RAR archive adapters were written but not executed (libraries absent).
- Time budgets and hypothesis results are interpretive: bounds depend on recorded
  parameters, `nieobserwowane` is missing data, and a temporal association is not
  causation. Every result records its method and parameters.
- No user accounts; tokens protect the read API and, with `EW_REQUIRE_TOKEN=1`,
  data-bearing and state-changing routes. Android has no token setting yet.
- Manifests and packages are hashed, not signed. Hash agreement is byte identity,
  not authorship, truth or custody.
- Android and SAF device execution remain as described in the 2026-10-02 handoff;
  Android code was not changed in this increment.
- The five-layer legal-analysis standard, Live Explainer and signed custody remain
  future work.
- Public Git history retains the earlier case-specific identifiers noted on
  2026-10-02.

## Continue without restarting completed work

Use the local checks in [Development](docs/DEVELOPMENT.md) plus
`pytest server/tests/test_e2e_acceptance.py` and the browser scripts listed in
the receipts. GitHub Actions remains manual-only; no paid model calls were made.
Before upgrading a live installation: stop workers, back up SQLite and the
content store; migrations are additive (versions 1, 10–99) and the timeline cache
rebuilds once (`PROJECTION_CODE`). New tables must be classified in
`server/app/packages.py` (exported or excluded with a reason) — a test enforces it.
Record new work as a task in `coordination/state.json` with scope, base revision
and acceptance; add new receipts instead of editing old ones.

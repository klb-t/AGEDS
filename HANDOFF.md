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

## Digital traces wave (2026-10-17 – 2026-10-21)

Plan: [DIGITAL_TRACES](docs/plans/DIGITAL_TRACES_2026-10-17.md). Location history and places
([LOCATION](docs/LOCATION.md)), personal-data exports, photo and document metadata
([PERSONAL_EXPORTS](docs/PERSONAL_EXPORTS.md)), bank/transport/health records
([LEDGER](docs/LEDGER.md)). Time budget is now `time_budget.v5`: provider inferences, provider-side
user edits and derived spans are estimates unless a run says otherwise. At integration: 1376 backend
tests passed, 137 Node tests, 11 browser scripts PASS (location 7, traces 5, ledger 6 among them).
Passive sources are excluded from event-study density controls by default (method v3). No real
provider export, bank statement or health export was available; all formats were tested on generated
data.

## Acceptance (this session, synthetic data only)

| Check | Evidence |
|---|---|
| Backend | Full `pytest server/tests` at `6023d0f`: 1206 passed, 1 opt-in skip (chat scale), 764 subtests. |
| JavaScript | `node --test server/tests/js/*.test.mjs`: 127 passed. |
| Browser | Real Chromium on the R2 head: acceptance 24, timeline 8, workbench 9, reports 10, time budget 9, hypotheses 10, import plan 9, media 7 PASS; catalog page check passed. |
| End to end | [ACCEPTANCE](docs/ACCEPTANCE.md): one run of the real CLI and service on a generated multi-device layout; 19 of 22 criteria met, 3 not verifiable by a run (covered by suites or by change-set/documentation properties). |
| Reviews | Two adversarial reviews (13 + 13 confirmed findings, plus plausible ones verified); all fixed with regression tests: [first](docs/validation/REVIEW_FIXES_2026-10-16.md), [second](docs/validation/REVIEW2_FIXES_2026-10-16.md). Event study is method v2 and time budget `time_budget.v3`; earlier stored runs remain as historical records. |
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
- Two token mechanisms coexist after the 2026-10-27 integration: the read-API
  tokens of `integrations` (with `EW_REQUIRE_TOKEN=1` also data-bearing routes) and
  the accounts/grants of the advocate branch (`auth.mode=tokens`). In
  `auth.mode=tokens` every catalogue/import/timeline/report router requires
  installation scope; recipients reach only the advocate and gated artifact routes.
  The default is still `legacy_open`, which serves only local clients
  (`docs/ADVOCATE.md`). Android sends the advocate token (`X-AGEDS-Token`).
- Manifests and packages are hashed, not signed. Hash agreement is byte identity,
  not authorship, truth or custody.
- Android and SAF device execution remain as described in the 2026-10-02 handoff;
  Android code was not changed in this increment.
- The five-layer legal-analysis standard, Live Explainer and signed custody remain
  future work.
- Public Git history retains the earlier case-specific identifiers noted on
  2026-10-02.
- Advocate (2026-10-25..27, `docs/design/RZECZNIK_MODEL_2026-10-25.md`): Vertex text
  and TTS were called once on synthetic data; two independent authorization review
  rounds are regression tests (`server/tests/test_review*_regressions.py`).

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

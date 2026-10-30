# AGEDS — current handoff

State: **2026-10-30**, branch `claude/rzecznik-design`. It contains the 2026-10-16 increment and
the digital traces wave of draft PR #6 (`ccr-d25c3cb3-qgbhql`, merged 2026-10-27 at `65c7823`),
plus the advocate work of 2026-10-25 and everything since. Read [CLAUDE.md](CLAUDE.md),
[current coordination](coordination/state.json), [AGENTS.md](AGENTS.md) and the
[architecture rules](docs/ARCHITECTURE_RULES.md) before continuing. Earlier handoffs are preserved
in Git history: 2026-10-16 at `65c7823^2`, and the 2026-10-02 recovery at `9c1d513`.

Deployment: production on the VM (`127.0.0.1:8090`, `legacy_open` behind the proxy's basic auth)
was last deployed by the coordinator at `ec9fb6a` on 2026-10-27. Deploying the current head with
`scripts/deploy_vm.sh`, then the pilot transfer (`scripts/transfer_pilot_to_production.sh`),
waits for the owner. Nobody deploys to production without them. Closing draft PR #6 on GitHub is
also left to the owner.

## What exists

| Stage | Behavior | Contract |
|---|---|---|
| Catalog, import plan, importers | Read-only scans through nested archives, content groups by SHA-256, import plans with human approval. Importers: chat exports, MBOX/EML (forwarded `message/rfc822` kept as an attachment since parser v3), ICS, VCF, call logs, billing, WhatsApp with media, Takeout, EXIF. | [CATALOG](docs/CATALOG.md), [IMPORT_PLAN](docs/IMPORT_PLAN.md), [chat](docs/IMPORTERS_CHAT.md), [communications](docs/IMPORTERS_COMM.md) |
| Time, timeline | Every observed time with kind, zone status and precision; unknown zones stay unknown; versioned display rule. | [TIMELINE](docs/TIMELINE.md) |
| Digital traces (2026-10-17..21) | Location history and places, personal-data exports, photo/document metadata, bank/transport/health records. All tested on generated data only. | [LOCATION](docs/LOCATION.md), [PERSONAL_EXPORTS](docs/PERSONAL_EXPORTS.md), [LEDGER](docs/LEDGER.md), [open gaps](docs/validation/TRACES_GAPS_2026-10-30.md) |
| Media | Pinned ASR/OCR recipes. Vertex Gemini engines (text, speakers, languages), with word times from local faster-whisper aligned to the Gemini words (default `asr-vertex-gemini-aligned`). Optional Chirp, forced-alignment and hybrid presets. PDF text layer first. Superseded text versions are marked, append-only. | [MULTILINGUAL_ASR](docs/MULTILINGUAL_ASR.md) §6a |
| Interpretation | Time budgets; preregistered hypotheses with a dossier, including preregistered disconfirming queries and a disclosure of the search for contrary material; thesis typology; event study. | [TIME_BUDGET](docs/TIME_BUDGET.md), [HYPOTHESES](docs/HYPOTHESES.md) |
| Advocate (rzecznik, 2026-10-25..) | Accounts, grants, packages and tokens (`auth.mode=tokens`). The advocate answers only from gated passages; `proven` must quote verbatim; the status `contradicts` exists. Voice uses a separate Text-to-Speech consent. Private conversations stay out of whole-database exports. | [ADVOCATE](docs/ADVOCATE.md), [design](docs/design/RZECZNIK_MODEL_2026-10-25.md) |
| Owner in the browser (2026-10-28..30) | `/start`: upload, catalogue, plan, originals, ASR/OCR queue, report. `/report`: pilot and coverage report. `/settings`: consents for Vertex and Text-to-Speech, provider, budget, default presets, upload limit, recipients and invitations. | [OWNER_WEB](docs/OWNER_WEB.md) |
| Connectors | Read-only Gmail API (owner OAuth, `gmail.readonly`, raw RFC 822 through the EML importer). Spam and Trash are opt-in. Never run against an account. | [GMAIL_API](docs/GMAIL_API.md) |
| Pilot | A separate pilot installation (`scripts/pilot_local.sh`, `~/ageds-pilot`). Its report comes from the database; `import-pilot-results` copies pilot results into another installation without a model call. | `scripts/pilot_local.sh`, `server/app/pilot_transfer.py` |
| Reports, workbench, ecosystem | As in the 2026-10-16 increment. | [REPORTS](docs/REPORTS.md), [WORKBENCH](docs/WORKBENCH.md), [READ_API](docs/READ_API.md) |

## Where results came from

* **Real data, with the owner's consent (R100, 2026-10-27):** 7 pilot files (3 phone recordings
  in AMR, 4 PNG screenshots), in the pilot installation only. Vertex Gemini ASR/OCR ran on them,
  local faster-whisper (small, then large-v3-turbo) and MMS forced alignment ran offline on the
  stored Gemini text. Provider spend in the pilot is 0.40 €. The same files went through the web
  path once, on a throwaway copy of the pilot database, without a model call.
* **Everything else is synthetic or generated:**
  * mail and chat export structures;
  * all digital-traces formats;
  * the browser suites (with a test-only fake Vertex transport);
  * the review reproductions.

  No real mail, chat, location, bank or health export has been imported yet.

## Acceptance (latest runs)

| Check | Evidence |
|---|---|
| Backend | Full `pytest server/tests`: 1627 passed, 3 skipped on Python 3.11 (upload space check, 2026-10-30); 1626 passed at `90d318b` also on 3.12 with the image's package set ([DEPLOY_DEPS](docs/validation/DEPLOY_DEPS_2026-10-30.md)). |
| JavaScript | `node --test server/tests/js/*.test.mjs`: 137 passed. |
| Browser | Real Chromium, 14 suites, 135 steps at `b17e2e7`: acceptance 24, advocate 8, advocate_model 6, analysis_budget 9, hypotheses 10, importplan 9, ledger 6, location 7, media 7, owner 7, reports 10, timeline 8, traces 5, workbench 9. Owner and advocate suites re-run at `dc4f326`. |
| Reviews | Three independent security reviews (2026-10-26, 2026-10-26, 2026-10-27). All confirmed findings are fixed, with regression tests in `server/tests/test_review*_regressions.py`. The third reviewer's repro suite no longer reproduces any finding. |
| Repository | `scripts/verify_repository.py` passes (links and privacy scan). |
| CI | GitHub Actions "Manual verification" is dispatched only by the coordinator. It was green on `65c7823`; later dispatches are reported by the coordinator. |

## Concrete remaining boundaries

- **Inference quality is not measured against a human reference.** ASR/OCR quality,
  speaker hypotheses and the word-time alignment are model output. Pilot alignment made 77 of
  147 segments word-citable. Tesseract and pyannote have never run here, and 7z/RAR adapters were
  not executed.
- **No real exports yet.** Gmail and WhatsApp formats were rehearsed on synthetic structures
  modelled on documented formats ([REAL_EXPORT_FORMATS](docs/validation/REAL_EXPORT_FORMATS_2026-10-30.md)). For digital traces,
  every gap is listed with its source and what closes it in
  [TRACES_GAPS](docs/validation/TRACES_GAPS_2026-10-30.md).
- **The production image lacks some optional engines:** torch/torchaudio
  (`server/requirements-align.txt`), Tesseract and pyannote are not in it. Spreadsheet import and
  PDF/XLSX report output are in it since `6ec4f81`. The faster-whisper model downloads to
  `HF_HOME=/models` on the first job. `/settings` disables presets the installation cannot run.
  Details and the pre-deploy checklist: [DEPLOY_DEPS](docs/validation/DEPLOY_DEPS_2026-10-30.md).
- Production runs `legacy_open`, so recipients' grants are not enforced. Invitations are
  disabled there with an explanation; switching to `auth.mode=tokens` is an operator decision
  (docs/ADVOCATE.md).
- Manifests and packages are hashed, not signed. Hash agreement is byte identity, not
  authorship, truth or custody.
- Android and SAF device execution are as described in the 2026-10-02 handoff.
- The five-layer legal-analysis standard, Live Explainer and signed custody remain future work.
- Public Git history retains the earlier case-specific identifiers noted on 2026-10-02.

## Continue without restarting completed work

Use the local checks in [Development](docs/DEVELOPMENT.md). Browser suites: `scripts/browser_tests.sh`
(Playwright pinned with a lockfile in `server/tests/browser/`, browser from `/opt/pw-browsers`). GitHub Actions
remains manual-only; do not dispatch it or call paid models to obtain a receipt.

Before upgrading a live installation:
* stop the workers and back up SQLite and the content store (`deploy_vm.sh` does both);
* migrations are additive: versions 1–6 and the reserved ranges 10–139 (`server/app/schema/__init__.py`);
* classify new tables in `server/app/packages.py` (exported or excluded with a reason); a test
  enforces it.

Record new work as a task in `coordination/state.json` with scope, base revision and acceptance,
and add new receipts instead of editing old ones. Open tasks are in the ledger; the owner's next
steps are deployment and real exports through `/start`.

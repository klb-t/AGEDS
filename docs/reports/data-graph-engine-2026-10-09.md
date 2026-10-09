# Task B — AGEDS — 2026-10-09

Base after fresh fetch: `9c1d513bc19d177bd324d7506a21fbab98c2e268`.
Branch: `gpt/data-graph-engine-2026-10-09`; main and historical experiments
unchanged. Owner/scope: AGEDS-B-20261009-ASR-OPTIONS in coordination/state.json.
Audit A pinned at ChatADHD `baa9e30c12a29ab7f14fc060a13676b1bd35b036`.

## ASR recipe vertical increment

Source server/profiles/asr/default.json → strict immutable recipe validation →
existing Settings environment overlay → real FasterWhisperAdapter and verified
worker → existing run/result metadata → inert metadata-package graph. No second
settings resolver, graph engine, database migration or live deployment.
Default model/device/compute/options are unchanged. Model/device/compute values
remain independent existing settings; VAD and timings are independently
composable options. Full effective recipe/hash is recorded before decoding and
in the successful result. Library defaults and model revision are not invented.

Implementation decision, reversible: snapshot per adapter operation rather than
reload while producing lazy segments. Alternative lifetime-worker freeze would
prevent option edits; per-segment reload could mix profiles. Startup model
overlays remain pinned until restart. Invalid source raises an explicit error
before model construction and is preserved for correction. Failed jobs/runs
remain in history; repair and requeue creates a separate run.

R42 exceptions: schema/key names are serialization contract (1/4); SHA-256 is
standard (2); lifecycle/error mechanism and source locator are mechanism/bootstrap
(3/5); explicit validation messages are diagnostics (6). No broad allowlist.

## Executed local acceptance

Existing word/verified-worker suites PASS (16 tests). New recipe suite 3/3 PASS:
exact default decoder parameters, three data variants, descriptor lifetime,
unchanged source bytes, hash/snapshot retained by actual worker/SQLite/export
and reopen, missing/duplicate/unsupported source rejection, separate failure
and repaired run; disabled timings report unavailable. Decoder is injected,
not a real model: this proves runtime wiring, not ASR quality or inference.

Full Python 432/432 + 588 subtests PASS (12.30 s); Node 85/85, no skips/failures.
Repository verifier PASS. Initial command used the general onboarding venv
without pytest and failed; retry used the existing ageds-venv, no dependencies
installed and no historical gate counted as current. Logs retained externally:
/workspace/.onboarding/logs/data-graph-ageds-{initial,initial-retry,recipe,full,final,js,verify}.log.
No paid model calls, CI, new services, source media or private data. Android
unchanged and not built here; existing proxy/dependency blocker remains.

## Audit and next concrete step

EA-AGEDS-001 closed for versioned existing local ASR adapter/model defaults and
VAD/timing consumer path. Other decoding parameters/providers explicitly
unsupported, not mock switches. EA-AGEDS-002 stays open: pending jobs are not
pinned to recipes and still deduplicate by artifact, so changing worker
configuration can affect a future job. Next: recipe-aware enqueue/payload and
deduplication, preserving empty legacy payloads as unknown rather than inventing
historical intent. No claim that all app configuration is already graph data
(EA-AGEDS-006 open), or that this produces a full replay/restore.

Final source-path check: 19/19 scoped tests PASS; full final Python 432/432 + 588 subtests PASS (9.81 s), repository verifier PASS. Default source is under profiles/, outside the private runtime data ignore rule. Appended Settings field preserves existing positional fields. Ready for product commit/push.

Published product commit: `157d88f`; push PASS. Source main remains at baseline, no deployment/migration of live storage. Local synthetic runtime acceptance is accepted in the task ledger; this is not main integration or real ASR-quality acceptance.

Additional fresh baseline at original SHA in an isolated detached worktree: 429/429 + 588 subtests PASS (9.46 s), compared with 432/432 final tree. Log data-graph-ageds-fresh-baseline.log. This execution occurred after implementation on preserved baseline source; it is not a historical receipt relabeled as a new run.

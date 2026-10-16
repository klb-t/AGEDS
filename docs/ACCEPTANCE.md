# End-to-end acceptance: criterion → evidence

Task `AGEDS-TASK-20261016-Z` checks the whole 2026-10-16 increment (catalog, import plan, chat and
communication importers, timeline, multilingual ASR, time budget, hypotheses, reports, workbench, read
API) against the acceptance criteria of the stage plan ("Kryteria odbioru (dla całości)" and "Czego
nie robić"). It runs as **one reproducible run of the real product interfaces**: `python -m
server.app.cli` subprocesses with a fresh data directory and the real service (`uvicorn
server.app.main:app`, `EW_REQUIRE_TOKEN=1`). The input is a generated multi-device layout. Validation:
[receipt](validation/E2E_ACCEPTANCE_2026-10-16.json) and [report](validation/E2E_ACCEPTANCE_2026-10-16.md).

## Run it

```bash
# default (bounded, about 35 s here), part of the normal suite
.venv/bin/python -m pytest server/tests/test_e2e_acceptance.py -q
# opt-in: adversarial fourth collection + full sensitivity grid; PDF through a local Chromium
AGEDS_E2E_HEAVY=1 AGEDS_E2E_PDF_CHROMIUM=/path/to/chrome .venv/bin/python -m pytest server/tests/test_e2e_acceptance.py -q
# receipt into a NEW directory outside the repository (work data, receipt.json, receipt.md)
.venv/bin/python scripts/e2e_acceptance.py /absolute/new/dir [--heavy] [--pdf-chromium /path/to/chrome]
```

`AGEDS_E2E_SKIP=1` skips the module (e.g. a machine without a free local port). The runner refuses an
output inside the checkout and a non-empty directory. The work directory holds the synthetic sources,
the SQLite database, the content store, reports and the service log; delete it after reading.

## What runs

| Part | File | Role |
|---|---|---|
| Layout | [`server/tests/e2e/layout.py`](../server/tests/e2e/layout.py) | Composes the corpus parts `import_layout`, `chat_exports`, `communications`, `audio` and the `catalog_files` time clusters into three collections (`phone_a`, `drive_b`, `drive_c`). Nothing is duplicated; only placements are added. |
| Pipeline | [`server/tests/e2e/pipeline.py`](../server/tests/e2e/pipeline.py) | Stages, commands, measured seconds, checks with evidence, criterion mapping. The orchestrator never imports `server.app` and reads SQLite read-only. Inherited `EW_*`/`AGEDS_*` variables are dropped. |
| ASR worker | [`server/tests/e2e/fake_asr_worker.py`](../server/tests/e2e/fake_asr_worker.py) | The real `worker.main` loop (claim with lease, pinned recipe, fenced publication). Only the model is the audio part's `FakeFasterWhisper`: tones, **not speech**. |
| Report check | [`server/tests/e2e/report_check.py`](../server/tests/e2e/report_check.py) | Runs the reports suite's `crosscheck` on every generated report. Compares the time-budget report with the stored run. |
| Test | [`server/tests/test_e2e_acceptance.py`](../server/tests/test_e2e_acceptance.py) | One module-scoped run. Tests group the checks by criterion, so a failure names the criterion. |
| Runner | [`scripts/e2e_acceptance.py`](../scripts/e2e_acceptance.py) | Same run, receipt written outside the repository. |

Stages in order: `layout` → `collections` (3 × `catalog-collection-add`) → `catalog` (`catalog-scan` ×3,
`catalog-coverage`, `-duplicates`, `-collisions`, `-entries`, `-describe`, `-export`) → `import_plan`
(`import-plan-create`; `import-plan-decide --approve-all-ready`; an explicit importer for the ambiguous
unit and an explicit billing profile; `import-plan-run`; `import-plan-cross`) → `reimport` (plain
re-run; `--rerun-done`; re-scan and a second plan) → [`heavy_adversarial_plan`] → `catalog_ingest` (the
PDF on disk) → `asr` (`media-enqueue` with `legacy` and `per-segment-pl-nl-en`, the worker, `transcript-compare` ×2,
`media-queue`) → `service_and_citation` (`token-create`, service start, word citation via `POST
/api/artifacts/{id}/citations`) → `timeline` (`timeline-rebuild`, `timeline-window` json/csv, `correlate`
with the three rules, run detail and hypotheses over HTTP) → `time_budget` (private categories file;
`budget-run` ×6, `budget-show`, `budget-list`) → `hypothesis` (`hypothesis-register`, `eventstudy-run` ×2,
`dossier-candidates`, `dossier-add`, `dossier-classify`, `thesis-add` ×2, `disclosure-add`) → `reports`
(five types, `report-verify` ×5, `report_check`, tamper a copy, `report-list`) → `citations`
(`citation-bundle-export`/`-inspect`, `citation-export`/`citation-inspect`) → `metadata`
(`metadata-export`, `metadata-verify`) → `read_api` (search, timeline window, artifact, packet; no
token; revoked token) → `originals` (tree state) → `repository` (`git status`, `scripts/verify_repository.py`).

## Planted cases (where they live)

| Case | Location in the layout | Source part |
|---|---|---|
| Conversation C in three exports, an edit (`u2`), a branch (`a1x`), a regenerated answer (`a3b`) | `drive_b/exports/chatgpt-2024-12/` (e1), `drive_b/chatgpt-export-2025-03.zip` (e2), `drive_c/later-export/` (e3) | `chat_exports`, `import_layout` |
| Copy of an export on a second drive | `drive_c/copy/chatgpt-export-2025-03.zip` (byte-identical to e2) | `import_layout` |
| E-mail attachment = PDF on disk | `phone_a/Documents/raport.pdf`; MBOX in `drive_b/takeout-20241001.zip!/Takeout/takeout-inner.zip!/Mail/…`; also `phone_a/WhatsApp/export.zip!/DOC-…pdf` | `communications` |
| Interleaved PL/NL/EN recording (labelled synthetic tones) | `phone_a/Recordings/Call recording Example D_+31600000005_20240105_101600.wav` | `audio` |
| Recording without a call / call without a recording | `phone_a/Recordings/out-+48000000102-….amr`, the code-switch WAV; call `+48000000101` in `phone_a/backup/calls-20240107.xml` | `communications`, `import_layout` |
| File-name collision | `phone_a/notes/todo.txt` vs `drive_b/todo.txt` (different bytes) | new placement |
| AM/PM; unknown zone; DST | `phone_a/billing/bill_infer.csv` (`11:00 PM`, day/month order unknown), My Activity HTML (`AM CET`); WhatsApp wall clocks, recording names, ICS floating/unknown TZID; ICS weekly meeting across 2024-03-31, 02:30 in the spring gap and the autumn fold | `communications` |
| Two chatbots overlapping | ChatGPT `Parallel GPT` (in e1) and Claude `Parallel research`, 2025-03-01 10:05–10:25 UTC | `chat_exports` |
| Long gap | file times 2024-01-09 → 2024-09-01 in `phone_a/timeline/`; no event 2024-10-28 → 2024-12-01; empty budget days | `catalog_files` clusters |
| Zip in zip; `.dat` that is a PDF | `drive_b/takeout-20241001.zip!/Takeout/takeout-inner.zip!/…`; `…chatgpt-export-2025-03.zip!/file-DAT1.dat` | `import_layout` |

## Criterion → evidence

Status of the 2026-10-16 receipt run (default variant with PDF, and the heavy variant): **19 met, 0 not
met, 3 not verifiable in a run**. Each row lists the pipeline checks (ids in the receipt) and the decisive
assertion. All tests are in `server/tests/test_e2e_acceptance.py`. The `test_every_check_passed` test
additionally requires every check of the run.

| Criterion | Status | Checks → assertion | Test |
|---|---|---|---|
| K1.a Same ChatGPT conversation in three exports, one edit, one branch | met | `timeline.same_conversation_three_exports`: `same_conversation` run lists C in 4 export labels (3 exports + copy); e1↔e3 `changed_examples == ['u2']`, `a1x` only in e1, `a3b` only in e3; e2↔copy content equal | `test_corpus_planted_cases[K1.a]` |
| K1.b E-mail attachment is the same PDF as a disk file | met | `catalog.pdf_on_disk_and_in_whatsapp` (one content group); `ingest.same_pdf_three_provenances`: one SHA-256, three artifact rows (disk, MBOX `mime-part`, WhatsApp member) | `[K1.b]` |
| K1.c Interleaved PL/NL/EN recording | met (fixture: tones, fake model) | `asr.two_recipes_two_runs_two_versions`: 2 jobs/runs/versions, strategies `auto_per_file`/`per_segment_language`, segment languages `{pl,nl,en}`, `real_inference: false`; `asr.comparison_deterministic`; `service.word_citation_across_language_switch` (word anchor across pl→nl, `matches_stored_transcript_version`) | `[K1.c]` |
| K1.d Recording without a call; call without a recording | met | `timeline.recording_call_cases_visible`: both recordings in `unmatched.recordings` (`only_uncorroborated_unknown_zone_matches`), the `+48000000101` calls in `unmatched.calls`, one ambiguity group with two proposed hypotheses | `[K1.d]` |
| K1.e File-name collision | met | `catalog.name_collision`: `todo.txt`, ≥2 variants with distinct SHA-256 in ≥2 collections, both planted locations | `[K1.e]` |
| K1.f AM/PM, unknown zone, DST | met | `timeline.ampm_unknown_zone_dst`: `11:00 PM` → local `T23:00:00` in both date orders; DST flags `nonexistent_local_time` and `ambiguous_fold`; ≥3 event types with `tz_status = unknown` | `[K1.f]` |
| K1.g Two chatbots overlapping | met | `timeline.two_chatbots_overlap` (spans overlap in the projected window); `budget.parallel_chatbots_not_double_counted`: one-category total = Σ separate unions − measured overlap = `split_equal` total < Σ | `[K1.g]` |
| K1.h Long gap without data | met | `catalog.gap_found` (235 empty days, phone_a txt); `timeline.long_gap_without_events`; `budget.unobserved_band` | `[K1.h]` |
| K1.i Zip in zip | met | `catalog.zip_in_zip`: depth-2 members `…zip!/Takeout/takeout-inner.zip!/…` | `[K1.i]` |
| K1.j `.dat` is a PDF | met | `catalog.dat_is_pdf` (`sniffed_type pdf`, `opaque_extension`, conflict kept); `catalog.dat_in_both_drives` | `[K1.j]` |
| K1.k ≥2 collections, export copy on a second drive | met | `collections.created`, `catalog.scans_complete`, `catalog.export_copy_on_second_drive` (zip group in 2 collections), `catalog.dat_in_both_drives`, `timeline.same_conversation_three_exports` | `[K1.k]` |
| K2.a Re-import duplicates nothing | met | `plan.run_completed`; `reimport.plain_rerun_selects_nothing` (only `import_plan_runs`, `audit_log` change); `reimport.rerun_done_creates_no_evidence_rows` (growth ⊆ `source_observations`, `audit_log`, `import_plan_attempts`, `import_plan_runs`; `events_new = artifacts_new = 0`); `reimport.rescan_and_new_plan_creates_no_evidence_rows`; `reimport.no_duplicate_events`; heavy: `heavy.adversarial_plan_bounded` | `test_reimport_duplicates_nothing_and_originals_are_untouched[K2.a]` |
| K2.b Originals unchanged before/after | met | `originals.unchanged`: `tree_state` (SHA-256, size, mode, mtime of every node, symlinks unread) identical for every root | `[K2.b]` |
| K3.a Numbers reproducible / traceable to code | met | `reports.numbers_traceable` (every `data-k` span = its `report.json` value, no stray numeral, all 5 reports); `reports.budget_numbers_equal_stored_run` (totals and candle rows = stored run, hash recomputed); `reports.manifests_verify`; `reports.tamper_detected`; `reports.five_types_written`; `budget.method_names_real_rule`; `hypothesis.eventstudy_reproducible` | `test_numbers_are_reproducible_and_parameter_changes_keep_old_results[K3.a]` |
| K3.b Parameter change → new result, old kept | met | `budget.parameter_change_new_run_old_kept`: new run id and hash with different totals; `budget-show` of the old run re-hashes to the original; same parameters reuse the old row; `hypothesis.eventstudy_reproducible` (same version → same `result_sha256`, `identical_result_runs`) | `[K3.b]` |
| K4 No private data in tracked files | met | `repository.verify_passes` (`scripts/verify_repository.py`: links, snapshots, e-mail/phone/user-path heuristics); `repository.untouched_by_run` (the run writes nothing into the checkout) | `test_privacy_opt_ins_and_interpretive_labels[K4]` |
| K5 Backend/JS/browser tests locally; Android if changed | not verifiable in a run | Recorded in the [validation report](validation/E2E_ACCEPTANCE_2026-10-16.md): full pytest, node, all browser scripts with real Chromium; Android not changed | `test_criteria_not_verifiable_in_a_run_are_explicit` |
| N1 Nothing private to external APIs without per-run opt-in | met | `layout.private_outside_repo`; `catalog.describe_without_provider` (`provider none`, `not_run`); `plan.bulk_approval_leaves_choices_to_a_person`; `plan.opt_ins_stay_off` (location/browsing items never run, 0 artifacts); `hypothesis.no_provider_call` | `[N1]` |
| N2 Unobserved ≠ inactivity; correlation ≠ cause | met | `budget.unobserved_band` (`nieobserwowane` full days, disclaimer "a nie bezczynność"); `timeline.nothing_auto_accepted` (all hypotheses `proposed`, no probability column); `hypothesis.causation_warning`; `hypothesis.thesis_without_evidence_flagged` | `[N2]` |
| N3 Originals, names, numbers, dates not modified | met | `originals.unchanged`, `catalog.export_outside_sources`, `repository.untouched_by_run` | `[N3]` |
| N4 Accepted functions not re-implemented | not verifiable in a run | Change-set property: this task adds tests, a runner and three defect fixes only | explicit note |
| N5 Five layers not declared as implemented | not verifiable in a run | Documentation property: CLAUDE.md, HANDOFF.md and FEATURES.md keep the five layers as future work | explicit note |

Pipeline requirements of the task brief without a criterion of their own are checked too (and must pass):
`plan.explicit_choices_applied`, `service.token_required`, `citations.bundle_and_packet_verify`,
`metadata.export_verifies`, `read_api.token_search_window_packet`, `read_api.revoked_token_refused`.

## Defects found by the run (fixed, each with a regression test)

Regression tests: [`server/tests/test_e2e_regressions.py`](../server/tests/test_e2e_regressions.py).
Each one fails on the base commit.

| # | Defect | Fix (shared edit) | Reversal |
|---|---|---|---|
| 1 | `metadata-verify` of any database with a file-name recording import failed: `record_import_run(artifact_id=None)` wrote `object_kind='artifact'` with a NULL id (`broken_object_reference`). EXIF directory imports use the same path. | `importers/comm_common.py`: a run without an artifact names no target (`object_kind` and `object_id` NULL). `packages.py`: a non-strict (audit) reference with a known kind and a NULL id is the warning `audit_target_id_missing`, so historical rows stay verifiable without rewriting `audit_log`. A real dangling id is still an error, and links stay strict. | Make the warning an error again once no installation holds pre-fix rows. |
| 2 | `import-plan-decide --approve A … --approve B` silently kept only the last list (argparse `store`), so a decision a person typed was lost. | `commands/importplan.py`: `--approve/--skip/--propose` use `action='extend'`. | None needed (strictly additive). |
| 3 | The time-budget method record said `source_rule: analysis_source.v1` although the items came through the timeline seam (`display_time@1`, stated correctly only in `result.input`). | `analysis/budget.py`: rule `time_budget.v2`; `method.seam`, `method.source_rule` and `method.source_rule_sha256` come from the run's input; `method.interim_fallback_rule` and `method.rule_history` are added. v1 runs stay stored with their hash and render in reports. | Not applicable: stored runs are append-only. |

## Boundaries

- **ASR quality is not tested.** The PL/NL/EN fixture is sine tones and the model is a fake. The run
  proves jobs, pinned recipes, runs, versions, per-segment language records, comparison, word timings
  and citations. It proves nothing about recognition of real Polish, Dutch or English speech: no model
  weights are installed and Hugging Face is blocked here.
- **Statistics are not asserted.** The event study is checked for determinism (same version and seed →
  same `result_sha256`), the causation warning and the disclaimer only. A parallel review found the
  permutation and stratified null models miscalibrated; they are being fixed separately. No p-value is
  claimed.
- **PDF** is optional. It ran with the local Chromium (`EW_PDF_CHROMIUM`) in the receipt run. Without it,
  the format is recorded as not requested.
- The time-budget overlap identity uses the two rule categories only. The `count_both` remainder
  (`bez_kategorii`) is under review.
- The orchestrator reads SQLite directly (read-only) for some evidence (counts, time candidates). Every
  product action goes through the CLI or HTTP.
- Not covered end to end: OCR and diarization (adapters unavailable here), queue-mode import with
  separate workers, the browser UI (covered by the browser scripts), Android/SAF, Loom/ChatADHD adapters
  beyond the read API.

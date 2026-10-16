# Import plan: catalog → reviewable plan → confirmed import

Task `AGEDS-TASK-20261016-I` closes the gap between stage 1 ([source catalog](CATALOG.md)) and
stage 2 ([chat importers](IMPORTERS_CHAT.md), [communication importers](IMPORTERS_COMM.md)) of the
[Evidence Timeline plan](plans/EVIDENCE_TIMELINE_2026-10-16.md). Validation:
[receipt](validation/IMPORT_PLAN_2026-10-16.json) and [report](validation/IMPORT_PLAN_2026-10-16.md).

The user points AGEDS at many directories and archives (several phones, accounts, Google
Takeout zips, ChatGPT exports with `.dat` files, WhatsApp export zips). The catalog records what is
there. The planner proposes, for every catalogued file or archive member, which importer could read
it and why. A person reviews and approves. One confirmed run then imports everything approved
through the real importers, with the catalog collection and the virtual locator as provenance.
Re-running creates nothing new.

## Concepts and boundaries

| Concept | Meaning | Not |
|---|---|---|
| Plan | Immutable record of one planning pass: pinned catalog scans, detector table (schema, version, SHA-256), planner limits and options, items, coverage, cross-export view. | An import; nothing is acquired by planning. |
| Item | One importer invocation unit: a file, an archive (WhatsApp zip, ChatGPT export zip) or a directory (chat export folder, WhatsApp folder). It lists its member catalog entries, candidates with reasons, defaults. | A decision. Items never change. |
| Candidate | An importer a detector rule proposes, `primary` or `alternative`, with human-readable reasons. Two different primary candidates make the item **ambiguous**: no importer is chosen. | A probability; the planner does not score. |
| Plan version | Append-only review step by a named actor: per-item decisions (`proposed`/`approved`/`skipped`, importer, parameters, opt-ins). The state at version V is the latest decision ≤ V, else the item defaults. | An edit of earlier versions. |
| Run / attempt | One execution of the approved items of a plan version; one attempt per item, written `running` first and changed once to `done`, `failed`, `refused` or `interrupted`. | Proof of truth; an attempt records what the importer did. |

Displayed item status: `proposed`, `approved`, `skipped`, then for approved items `done`, `failed`,
`refused` (and transient `running`). `done` means an attempt finished with the **same** importer,
parameters and opt-ins (`config_sha256`); changing any of them makes the item runnable again.

## Detection (versioned, data-driven)

Rules: [`server/app/importplan/data/detectors.v1.json`](../server/app/importplan/data/detectors.v1.json)
(`ageds.importplan-detectors`, version `1`). Its SHA-256 is stored with each plan; another table can
be passed explicitly (`--detectors`), tests use one to create overlapping rules. Code implements only
rule evaluation and named, bounded probes:

| Probe | Reads | Used for |
|---|---|---|
| `xml_root` | prefix; first element after prolog, comments, DOCTYPE | `<calls>` (call log, `sms_backup` as alternative projection), `<smses>` |
| `json_keys` | prefix; top-level kind and `"key":` markers | Chrome `Browser History`, Location `locations`/`timelineObjects`/`semanticSegments`, My Activity lists |
| `text_markers` | prefix | My Activity HTML `outer-cell` |
| `exif_marker` | prefix | JPEG APP1 `Exif`, TIFF header, PNG `eXIf` |
| `billing_header` | CSV/TSV prefix (first 20 lines); XLSX first rows via openpyxl within `max_spreadsheet_probe_bytes` | billing profile header aliases |
| `recording_name` | file name only | recording-name patterns of the recording importer |

Order of passes:

1. **Chat exports.** A directory (on disk or inside an archive) holding `conversations*.json` is one
   unit with every entry below it (attachments, `file-*.dat`, `user.json`). The chat importers'
   mapping files decide which names fit which vendor; element key markers in the prefix
   (`mapping`/`current_node` → ChatGPT, `chat_messages` → Claude) decide the vendor. No marker and two
   fitting vendors (e.g. an empty `[]`) → **ambiguous**. Shards (`conversations-NNN.json`) fit only
   ChatGPT. An export zip whose root holds the files is the unit "as a whole" (`archive`).
2. **WhatsApp.** A zip whose root `.txt` (named like a WhatsApp chat, or probed) has WhatsApp-like
   lines → `archive` unit (the zip itself is imported). A directory with such a chat `.txt` →
   `directory` unit with its sibling files. Several chats → the `chat_member` parameter is required.
3. **Single files.** Every remaining entry is matched against the file rules by effective type (or
   the extension's claim when bytes were only classified heuristically as text, e.g. a CSV with a
   preamble), locator regexes and probes. Entries inside units are never offered again.

Context: entries below `Takeout/` or in a container holding `archive_browser.html` are tagged
`google_takeout`. Opt-in importers are proposed with their opt-in **off**: `location_history`
(`enable_location`) and `browser_history` (`enable_browser_history`; Chrome `BrowserHistory.json`,
My Activity Chrome/YouTube folders). `takeout_activity` keeps browsing-like products skipped unless its
optional opt-in is set. `billing` needs an explicit `profile_id` (plan option `--billing-profile` when
the header matches it, or a decision); profiles are the importer's examples or a private file.

Not planned entries are counted per reason and type with up to 20 samples: `no_detector_matched`,
`structure_probe_did_not_match`, `probe_not_possible`, `entry_not_verifiable:<status>` (no verified
hash: encrypted, damaged, over a limit), `covered_by_unit`, `archive_container_members_considered`.

## Limits (presets; every hit is visible)

| Planner limit | Default | Effect |
|---|---|---|
| `max_entries` | 1,000,000 | entries beyond are not considered; coverage incomplete |
| `max_probe_bytes` | 4 GiB | further probes skipped (`limit:max_probe_bytes`), entries listed as not planned |
| `max_unit_members` | 200,000 | members beyond are left out of the unit |
| `max_conversation_id_scan_bytes` | 64 MiB | larger conversation files are not scanned for ids (cross-export view says so) |
| `max_conversation_ids_listed` | 500 | shared conversations listed; total exact |
| `max_spreadsheet_probe_bytes` | 32 MiB | larger XLSX headers not probed |
| `max_whatsapp_txt_probes_per_zip` | 8 | txt members probed per zip |

| Run limit | Default | Effect |
|---|---|---|
| `max_item_bytes`, `max_item_members` | 64 GiB, 200,000 | item `refused` (`limit_exceeded`) before anything is read |
| `min_free_temp_bytes` | 256 MiB | item `refused` (`insufficient_temp_space`) |
| `lease_seconds` | 6 h | attempt lease, renewed by a heartbeat |

## Execution

For each approved, unblocked item not yet done:

1. attempt row `running` with a lease token;
2. pre-checks: required members (`file`, `archive`, `conversations`, `chat_text`) must have status
   `ok` and a catalog SHA-256, optional ones (attachment/media candidates) are skipped and listed;
3. members are materialized into a new directory `ageds-importplan-a<attempt>-…` under the work
   parent (default `<EW_DATA_DIR>/importplan-work`, refused inside any catalogued or configured source
   root) by re-opening disk files without following symlinks and archive chains by member index and
   raw name; every copy is verified against the catalog SHA-256 and size — a mismatch is `refused`
   (`content_changed_or_unreadable`) and nothing of that item is imported;
4. the importer runs (adapters in `server/app/importplan/adapters.py`) with `collection_id` and
   `source_locator = catalog:collection/<id>/<virtual locator>` (the catalog-ingest form, so the same
   entry maps to one artifact row). Chat exports get `locator_for` per file and the export label
   `catalog:collection/<id>/<unit locator>` (one export per collection and place, so a byte-identical
   copy on another drive is its own export, not a label conflict). `sms_backup` (no collection
   argument), `exif` and `recording_names` (no locator argument) acquire the verified copy first,
   then call the importer's documented entry point;
5. footprint (artifacts under the item's locators and below `!/`, events of those artifacts, new
   vs re-observed rows, new acquisition observations and time candidates), `catalog_entry_artifacts`
   links, then a fenced publication of `done`/`failed`/`refused` (only the lease owner can publish);
6. the temporary directory is removed (the result records `temp_removed`).

**Idempotency.** A done item is not selected again. `rerun_done` re-invokes the importers: tested to
leave artifacts, events, time candidates, logical records/occurrences, exports, attachment refs,
contact observations, recording observations, media references and catalog links unchanged; only
acquisition observations (and audit rows) grow, by design.

**Crash and resume.** A killed process leaves its attempt `running` and its temporary directory. The
next run refuses while the lease is valid; after the lease expires — or immediately with
`recover_running` (the operator asserts the process is dead) — the attempt becomes `interrupted`, its
temporary directory is removed, the old run becomes `interrupted` and the item runs again. Tested with a
crash after the first ChatGPT shard was written: final row counts equal a clean run, no duplicate
`(artifact, external_id)` events. An exception inside an importer is a `failed` attempt (never
`done`) and is retried by the next run. A sync run between two items has no running attempt; another
run is refused until the operator passes `recover_running`.

**Queue mode.** `mode='queue'` enqueues one `import_plan_item` job per item in the existing job queue.
`import-plan-work` (or the general worker, which now dispatches this kind) claims a job with its lease
and heartbeat; the attempt's terminal state is written inside `jobs.publish_result`, i.e. in the same
fenced transaction as the job completion. Job `done` means "attempt finished"; the item outcome is the
attempt status. A worker whose job lease expired cannot publish (tested); the requeued job supersedes
its stale attempt. `claim_job` serializes `import_plan_item` jobs (same kind, no artifact), which also
keeps SQLite writes sequential. Importer writes themselves are not fenced; they are idempotent.

## Cross-export awareness

Before import (stored with the plan): chat-export units with identical conversation-file bytes, and
conversation ids read with the chat importers' own element scanner and mapping pointers (files up to
`max_conversation_id_scan_bytes`), listing every conversation that appears in more than one item.
After import: logical records of the chat importers grouped per conversation across the exports this
plan produced, with a link to the dedup report. Every occurrence is imported; nothing is deduplicated
or deleted (tested: the same conversation in three exports gives three occurrences and three events).

## Catalog follow-up: abandoned scans

`catalog-scan-abandon SCAN --actor A --reason R [--min-idle-seconds 3600]` /
`POST /api/catalog/scans/{id}/abandon` closes a scan left `running` by a dead process. The scan must
have been idle (start, latest entry, latest issue) for `min_idle_seconds`. The catalog's status CHECK
has no `abandoned` value and rebuilding that table would be destructive, so the scan row becomes
`failed` with `error = 'abandoned: <reason>'`; `catalog_scan_abandonments` (append-only) and
`audit_log` record the decision. No entry is changed or deleted. Plans accept only `completed` or
`partial` scans.

## Data model (schema 90, `server/app/schema/importplan.py`)

`import_plans`, `import_plan_items`, `import_plan_item_members`, `import_plan_versions`,
`import_plan_decisions` (no update, no delete); `import_plan_runs`, `import_plan_attempts` (no delete;
update only while `running`); `catalog_scan_abandonments` (append-only); index
`idx_artifacts_source_locator` for footprint queries.

## CLI

```bash
python -m server.app.cli import-plan-create --collection 1 --collection 2 [--scan-id N] [--billing-profile ID] [--temp-dir DIR] [--max-probe-bytes N]
python -m server.app.cli import-plan-show PLAN [--importer ics] [--status proposed] [--ambiguous] [--blocked] [--item ITEM]
python -m server.app.cli import-plan-summary PLAN [--version V]          # dry run: reads the stored plan only
python -m server.app.cli import-plan-decide PLAN --base-version V --actor NAME [--approve-all-ready] [--approve ID ...] [--skip ID ...] \
        [--importer ID=chatgpt_export] [--opt-in ID=enable_location] [--param ID=profile_id=VALUE] [--note TEXT]
python -m server.app.cli import-plan-run PLAN [--mode sync|queue] [--recover-running] [--rerun-done] [--temp-dir DIR] [--max-item-bytes N]
python -m server.app.cli import-plan-work [--max-jobs N]                 # drain queued import_plan_item jobs
python -m server.app.cli import-plan-runs PLAN [--run RUN] ; import-plan-cross PLAN ; import-plan-list
python -m server.app.cli catalog-scan-abandon SCAN --actor NAME --reason TEXT
```

## HTTP and page

`GET/POST /api/import-plans`, `GET /api/import-plans/{id}` (filters `importer`, `status`, `ambiguous`,
`blocked`, `collectionId`, `q`, keyset `afterOrdinal`, `limit` ≤ 500, `version`), `GET …/summary`,
`GET …/items/{item}`, `POST …/decisions` (`baseVersion` must be the latest, else 409; bulk
`approve|skip|propose` by filter or item ids, blocked items are reported in `not_applied`),
`POST …/runs`, `GET …/runs`, `GET /api/import-plan-runs/{run}`, `GET …/cross-exports`,
`POST /api/catalog/scans/{id}/abandon`. No API authentication (see HANDOFF).

`/import` (navigation **Import**): choose collections → generate → summary (counts per status and
importer, estimated bytes, blockers, not planned) → cross-export table → filters → bulk approve/skip →
per-item importer choice, opt-in toggles and parameters → run (sync or queue, re-run, recover) →
results with artifact links and run history. Source text (locators, names, reasons, errors) is data:
Jinja autoescaping, no `innerHTML`; a file named `<img src=x onerror=…>.ics` is a tested plan item.

## Transformation contract

| Input → result | Kept | Lost | Added | Reversibility / provenance |
|---|---|---|---|---|
| catalog entries → plan items | entry ids, locators, sizes, hashes, statuses | — | candidates, reasons, probe observations, context tags | plan is recomputable from the scans with the same detector table |
| plan item → materialized copy | exact catalogued bytes (verified) | filesystem metadata of the source (temp copy has its own mtime, see below) | — | copy deleted after import |
| copy → importer rows | per importer contracts | per importer contracts | collection and virtual locator on every acquisition | attempts record importer, parameters, opt-ins, footprint |

## Decisions (default; reversal condition)

| Decision | Reverse when |
|---|---|
| Plan items immutable; review is append-only versions with optimistic `baseVersion`. | Concurrent multi-user review needs merging instead of 409. |
| Ambiguous = two different primary candidates; nothing is auto-chosen, not even in bulk. | A validated scoring with a stated meaning exists. |
| Chat export label = catalog locator of the unit per collection. | A stable account/export identity becomes available in the export itself. |
| Sync runs by default (CLI/HTTP); queue mode via the job queue. | Real imports routinely exceed an HTTP request: default the page to queue mode. |
| Members of units are not offered to other rules (no EXIF item for a WhatsApp photo). | Chained post-steps (EXIF/ASR on acquired children) are designed. |

## Prompt vs repository invariants

* "Mark the scan as `abandoned`": stored as `failed` + `abandoned:` prefix + an abandonment record,
  because the catalog status CHECK cannot change without a destructive table rebuild.
* "Same conversation in several exports": shown, never deduplicated by deletion.

## Not done / not verified

* Review finding #1: importers acquire the temporary copy, so `source_modified_at` is the copy's mtime;
  `xfail` test `test_acquisition_keeps_the_catalogued_source_time` and a TODO in
  `execute._materialize` wait for the explicit stat scope in `ingest_file` (track R).
* Review finding #5: a later export at the **same** virtual locator with the same first email/WhatsApp
  attachment fails with "different parent"; `xfail(strict=True)` test
  `test_later_export_at_the_same_locator_imports_with_shared_attachments` flips when track R's fix lands.
  Copies in different collections/places are unaffected (different locators).
* `whatsapp_txt` and `sms_backup` (older importers without a collection argument) add a second
  acquisition observation without collection; the first one carries it.
* No "acquire only" choice for files without an importer (PDF, DOCX, audio without a recorder-like
  name); they are listed as not planned. XLS billing headers are not probed.
* No timing on large real exports; plan views load all items of a plan per request.
* Detector rules, billing profiles and recording patterns are examples, not verified against every
  app or export version. Real exports were not available.
* Real Chromium checked `/import` (9 cases) and track D's `/media` pages (7 cases); the media compare
  view overflows a 390 px viewport by 38 px (reported to the owners, not fixed here).

# Common timeline: projection, correlation hypotheses and swimlane view

Task `AGEDS-TASK-20261016-C` (Etap 2.2–2.5). Code: `server/app/timeline/`, schema
versions 30–31 (`server/app/schema/timeline.py`), routes `server/app/api/timeline.py`,
CLI `server/app/commands/timeline.py`, page `/timeline`. Receipt:
[TIMELINE_2026-10-16](validation/TIMELINE_2026-10-16.md).

The timeline is a **view, not new truth**. Importers record what sources say
(`events`, append-only `event_times`); the timeline chooses one display/sort time per
item with a versioned rule, keeps every candidate visible and reports the basis and an
uncertainty interval of the choice. Nothing is written back into events or candidates.

## 1. Time candidates

| Origin | Where it comes from |
|---|---|
| `stored` | rows of `event_times` written by an importer (kind, raw, utc/local, zone status, precision) |
| `derived_legacy` | computed on the fly for events without usable stored candidates: SMS/MMS/calls from the epoch-ms parser metadata (`timestamp.raw`, `raw_attributes.date_sent`), otherwise `events.ts_start`/`ts_end`; WhatsApp minute precision from the raw time |
| `derived_artifact` | candidates of events attached to a recorded artifact (`recording`, `image_capture`, …): file-name times, EXIF |
| `derived_observation` | `source_observations.source_modified_at` (file mtime), only from observations whose `filesystem_stat_scope` is `source_path` (stat of the original path) or `declared_by_caller` (e.g. the catalogued mtime; the candidate carries `source_modified_at_basis`). Temporary copies (uploads, extracted children, catalog/import-plan copies) record no source time since 2026-10-16 (review finding 1). **Unusable** for older rows with a temporary marker: upload metadata or an `upload:`/`client-upload:` locator → `file_mtime_of_temporary_upload`; a container-member locator `…!/…`, extraction or catalog-ingest metadata → `file_mtime_of_temporary_copy`. Older rows without any marker stay usable, labelled `legacy_unmarked_assumed_source_path` (residual ambiguity: a direct `ingest_file` of a temporary file by other code cannot be recognised) |
| `derived_correlation` | start of a call-log entry linked by an **accepted** correlation hypothesis |

Every derived candidate names `derived_from` (column or JSON path). `events.created_at`
(import time) is never a candidate.

## 2. Display rule `display_time@1`

Data file: [`server/app/timeline/rules/display_time.v1.json`](../server/app/timeline/rules/display_time.v1.json).
Its SHA-256 is pinned in `ruleset.RELEASED_SHA256`; an edited released file is refused at
load time (add `display_time.v2.json` instead). Experimental rules can be passed as data
(`ruleset.experimental_rule`, CLI `--rule-file` is not provided) and never reuse a released
`(rule_id, version)`.

Selection order: usable → kind priority per event type (`kind_priority`, `default` for
unknown types, so new importers' types work without code) → origin priority (`stored`
first; legacy derivations are used only when no stored candidate is usable) → zone basis
(`explicit_offset`, `utc_epoch`, assumed, unknown) → precision → candidate ref.

| Situation | Sort time | Interval | `basis` |
|---|---|---|---|
| explicit offset / epoch | the instant | the instant (widened by precision) | `explicit_offset` / `utc_epoch` |
| zone assumed by an importer | stored UTC; DST fold alternatives widen the interval | both fold instants | `assumed_zone` |
| wall clock, zone unknown | local time **read as if UTC** | `[local − 14 h, local + 12 h]` (every real UTC offset) | `local_as_utc_unknown_zone` |
| operator zone (`assume_zone` + mandatory `assumption_rule`) | earlier fold instant | covers both fold instants; flags `dst=ambiguous_fold` | `assumed_zone_operator` |
| operator zone, DST gap | stays unknown-zone | unknown-zone interval; flags `dst=nonexistent_local_time` | `local_as_utc_unknown_zone` |
| no usable candidate | — | — | `none` (listed separately as **undated**) |

Precision coarser than a second widens the interval to the whole unit (a date-only value
spans the day; sort time is the start of the unit). `end_kinds` give duration bars (`end`;
`updated` for `chat_conversation`). `visibility` hides `chat_message_part` events and
events with `metadata.chat.hidden` by default; they stay materialized and appear with
`include_hidden=true`. Lane keys: `source`, `collection` (earliest observation of the
artifact; filters match any collection), `counterparty` (per-type field list; separators
removed from phone-like values, raw values unchanged), `chatbot`
(`metadata.chat.chatbot`, then `metadata.chatbot`), `thread`, `type`, `kind`.

**Transcript segments** (`kind=transcript_segment`): the recording start is selected from
the artifact's candidates with the same rule (`kind_priority.transcript_segment`) and each
segment adds its offset; interval and basis are inherited. All transcript versions are
materialized; windows show the latest per artifact by default, `transcript_versions=all`
or explicit version ids pin others (the version id is in every item). Per-segment
`language` (track D) is shown with `language_basis`.

## 3. Materialized projection

`timeline_projections` identifies `(rule_id, version, rule_sha256, params_sha256)`;
`timeline_items` is a **rebuildable cache** per projection (one row per event/segment:
sort/lo/hi/end µs, basis, precision, chosen candidate, lane keys, hidden, flags);
`timeline_projection_builds` is an append-only build log with data watermarks (max ids and
counts of events, event_times, derived_text, observations, hypothesis statuses). Every read
compares watermarks: unchanged → `fresh`; new events/candidates/transcripts/observations/
decisions → **incremental** recomputation of affected events and recordings; deleted rows →
full rebuild; a build written by older projection code (`watermarks.projection_code` ≠ the
current `PROJECTION_CODE`, now 2) is rebuilt in full on first use after an upgrade.
**Projection identity and cache bound (review finding 7).** A projection is keyed by
the rule and the *effective zone parameters* (`assume_zone`, `zone_by_source`) only; the
free-text `assumption_rule` label is not stored in the cache: items keep the placeholder
`$request.assumption_rule` and every reader (window, item, export, `analysis_items`)
reports the label of its own request, so a typo or a new label never copies the items
again. At most `EW_TIMELINE_MAX_PROJECTIONS` (default 8) non-default projections keep
materialized items; after a full build the least recently built others lose their items
(rebuildable cache) and an append-only `timeline_projection_evictions` row (schema 32)
records projection, build, count and reason; their next use rebuilds them in full. The
default projection (`{}`) is never evicted. Projection and build-log rows stay
(append-only); reads still write the cache when data or parameters changed.
Rule versions and operator parameters get separate projections, so a new
version never overwrites an older one. Measured on 200 000 synthetic events: first build
≈ 11 s, incremental refresh after 1 000 new events ≈ 0.15 s, fresh check ≈ 1 ms
(receipt). Reads that refresh write only the cache tables.

## 4. HTTP API

| Route | Purpose |
|---|---|
| `GET /api/timeline` | **compatibility**: same response shape (list of `events` rows), same `limit`/`phone`/`event_type`. **Behaviour change:** ordering is the projected display time (newest first, ties by id) instead of `COALESCE(ts_start, created_at)` compared as text; events without a usable time come **last, by event id** (never by import time). Headers `X-AGEDS-Timeline-Rule`, `-Rule-SHA256`, `-Order`. Hidden items are included (unchanged set of rows). |
| `GET /api/timeline/window` | `from`/`to` (UTC ISO; date-only `to` ends the unit), `match=display|overlap` (overlap uses the uncertainty interval), `order`, `cursor` (keyset on `(sort_us, kind, id, segment)`, stable under ties; a cursor is bound to projection and query; a refresh between pages is reported in `notes.cursor`), `limit` ≤ 1000, filters `event_type`, `kind`, `source`, `collection`, `counterparty`, `chatbot`, `thread`, `tag`, `q` (FTS5, transcript match per version, ≤ 50 000 matches else `complete=false`), `transcript_versions`, `include_hidden`, `group`, `candidates`, operator `assume_zone`+`assumption_rule`, `rule`. Returns items (time, candidates with per-candidate placement and eligibility), lanes (first page), rule/projection identity, `has_more`, `complete`, `undated_count`. |
| `GET /api/timeline/density` | bucketed counts per lane computed in SQL: `bucket=auto|year|month|week|day|hour|minute` (UTC calendar; weeks start Monday), `max_buckets` ≤ 5000 (exceeding → 413), `max_lanes` (rest in an "other" lane); each bucket also counts unknown-zone items. Items are placed at their sort time. |
| `GET /api/timeline/undated` | items without a usable time, ordered by identity, with `undated_reason`. |
| `GET /api/timeline/items/{event:ID | segment:VERSION:INDEX}` | detail: all candidates, evaluation, tags, hypotheses, links to the artifact page / transcript version. |
| `GET /api/timeline/export?format=csv|json` | the current window (same filters), ≤ 200 000 rows, `X-AGEDS-Timeline-Complete`; CSV prefixes **every** text cell starting with `= + - @`, tab or CR with `'` (review finding 9: raw parser values such as a billing direction cell are not free text but still guarded), JSON is verbatim. |
| `GET /api/timeline/facets`, `/rules`, `/projection` | filter values, rule data, projection/build state. |
| `POST /api/timeline/correlations/runs`, `GET …/runs[/{id}]`, `GET /api/timeline/correlations[/{id}]`, `POST …/{id}/review` | correlation runs and review (below). |

Source text is returned as data; the page never inserts it as HTML.

## 5. Correlation hypotheses

Rules are data files with parameters (pinned like the display rule): 
[`recording_call_log.v1`](../server/app/timeline/rules/recording_call_log.v1.json),
[`email_chat.v1`](../server/app/timeline/rules/email_chat.v1.json),
[`same_conversation.v1`](../server/app/timeline/rules/same_conversation.v1.json).
A run stores rule id/version/hash, the merged parameters (only declared names, same
types), the display projection used, counts, unmatched lists and truncation flags
(`timeline_correlation_runs`, append-only). Each hypothesis (`timeline_hypotheses`,
append-only) stores the pair, `ambiguity_group`, `candidate_count` and **per-criterion
evidence**; there is **no probability** field. Recomputing is a new run; old runs,
hypotheses and decisions stay.

* **recording ↔ call log.** Left: audio/video artifacts and file-name `recording` events
  without an acquired artifact. Time is the gate: explicit/epoch/assumed instants within
  `time_tolerance_seconds`; an unknown-zone wall clock (file name) must be explained by a
  real UTC offset (quarter hours in [−12 h, +14 h]) within tolerance, and then duration or
  number agreement is required (`unknown_zone_requires_corroboration`). Evidence: tested
  point, residual, implied offset, DST fold/gap under an assumed zone, AM/PM alternative
  (`ampm_ambiguous` metadata), every other matching reading; duration (metadata, WAV
  header declaration, or call `end`) and number (file-name hypothesis from
  `recording_name_observations` or metadata vs call number: exact / last-N-digits /
  mismatch). Several calls within tolerance → several hypotheses in one ambiguity group,
  none picked. Recordings without a call and calls without a recording are listed in the run.
* **email ↔ chat message.** Minimum possible gap of both uncertainty intervals ≤
  `max_lag_seconds` (optionally e-mail first) and, by default, shared word n-grams after
  NFKC/casefold (count and one example).
* **same conversation across exports.** Primary: the chat importer's logical records
  (`logical_records`/`logical_occurrences`): a conversation id observed in two exports →
  hypothesis between the export files with shared/changed/only-left/only-right record
  counts and examples ("not observed", never deletion). Fallback for events without
  logical records: same `thread_id` and chatbot in two artifacts.

Review (`timeline_hypothesis_status`, append-only): `proposed → accepted | rejected |
revoked`, `accepted/rejected → revoked`, `revoked → accepted | rejected`; actor
(self-declared, `actor_verified=false`) and non-empty reason are required. Accepting may
create a `links` row (`confidence` NULL, rationale JSON naming the hypothesis); revoking
deletes that link and records the deleted row in the status details and `audit_log`. An
accepted recording ↔ call hypothesis becomes a `derived_correlation` start candidate for
the recording's transcript segments; revoking removes it at the next refresh.

## 6. Browser view `/timeline` (nav “Oś czasu”)

Swimlanes grouped by source, collection, counterparty, chatbot, conversation, type or
item kind; zoom ladder from one minute to fifty years (`+`/`−`, arrow keys pan); zoomed
out it draws density bars (with the unknown-zone share), at ≤ 1 500 items in view it
draws items with their uncertainty interval and duration. Only lanes inside the viewport
are in the DOM and only the fetched window is drawn. Filters: event type, kind, chatbot,
source, collection, tag, counterparty, text, transcript versions, hidden items, operator
zone with assumption id; the category filter is a disabled placeholder for track E.
Click → detail with every candidate, its placement and eligibility, the rule hash, links
to the artifact page/citation workspace. Drag on the ruler or empty lane area → range panel
with zoom, CSV/JSON export of the range and a "raport z zakresu" link to
`/reports/new?from=&to=` that is rendered disabled with a tooltip while that route does
not exist. "Całość" fits the extent of the current filters.

## 7. CLI

```
python -m server.app.cli timeline-window OUT.csv|OUT.json [--from --to --event-type --kind --source --collection
    --counterparty --chatbot --thread --tag --q --transcript-versions --include-hidden --match --order
    --rule --assume-zone --assumption-rule --max-rows]
python -m server.app.cli timeline-rebuild [--full] [--rule --assume-zone --assumption-rule]
python -m server.app.cli correlate --rule recording_call_log|email_chat|same_conversation [--params JSON] [--actor]
python -m server.app.cli correlation-review ID accept|reject|revoke --reason TEXT [--actor] [--no-link]
```

Outputs are new files only (existing files are refused). The commands need an initialized
database with schema versions 30–31 (start the service once after a backup).

## 8. Functions for other areas

* `server.app.timeline.analysis_items.load_items(db, start_utc=, end_utc=, event_types=,
  assume_zone=, assumption_rule=, unknown_zone='exclude'|'interval', include_hidden=False)`
  keeps the item contract of the interim analysis seam (`analysis/source.py`) and adds
  `t_lo`/`t_hi`, `basis`, `candidate_ref`, `hidden`. Unknown-zone items go to `untimed`
  (`reason=unknown_zone`) unless `unknown_zone='interval'` or an operator zone with its
  assumption id is given.
* `server.app.timeline.project_window(start, end, filters=...)` and
  `iter_projected_items(...)` return/stream window items (events and transcript segments)
  with `time.sort_us`, `time.interval`, `time.end_us`, `time.basis`, `time.precision`.

## 9. Transformations (A03)

| Input → output | Kept | Lost | Added | Reversibility / provenance |
|---|---|---|---|---|
| events + candidates → projected item | every candidate (shown), chosen ref, precision | nothing in the sources; the cache holds one sort time | sort time, interval, basis, lane keys, hidden flag | rebuildable from sources; rule id/version/hash and params in every response |
| recording start + segment offsets → segment items | transcript version id, offsets, language | — | inherited interval | recomputed when transcripts, observations or decisions change |
| rule run → hypotheses | parameters, per-criterion evidence, unmatched lists | candidates beyond explicit caps (reported) | ambiguity groups | append-only; recompute = new run |
| decision → link | hypothesis id, actor, reason | — | `links` row (accepted only) | revocation deletes the link with an audit record |

## 10. Boundaries and open points

* The dashboard list on `/` (`main.py`, "Ostatnie zdarzenia") and `/export/events.csv`
  still use their old ordering; they are outside this task's file scope.
* Density places items at their sort time; an unknown-zone item can belong to a
  neighbouring day. The unknown-zone share is shown per bucket; `match=overlap` exists for
  windows, not for density.
* A transcript match for `q` is per transcript version, not per segment.
* No probability or score is computed for hypotheses; acceptance is a human decision.
* Collections lane uses the earliest observation's collection; other memberships are
  filterable.
* Chat parts/hidden messages, billing alternative readings (day/month order) and AM/PM
  alternatives remain separate candidates/items; the rule does not merge them.
* The first build of a large catalog is slow (≈ 11 s per 200 000 events); later reads are
  incremental. Run `timeline-rebuild` after large imports to pay it once.
* Prompt vs invariants: "hypothesis with a probability" is not implemented (A11); the
  report-from-range route belongs to track F and is linked, not implemented.

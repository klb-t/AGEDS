# Hypothesis analyzer (interpretive layer)

Task `AGEDS-TASK-20261016-E`, part 2 (Etap 5). Code: `server/app/analysis/{hypotheses,preregistration,dossier,eventstudy,theses}.py`,
router `server/app/api/hypotheses.py` (page `/hypotheses`, nav „Hipotezy”), CLI `server/app/commands/hypotheses.py`,
schema versions 55–57 in `server/app/schema/analysis.py` (58–59 reserved). Receipt:
[HYPOTHESES_2026-10-16](validation/HYPOTHESES_2026-10-16.md).

Every section this layer produces — API objects, CLI output, each panel of the page, every stored result —
carries a fixed label:

> Wynik analizatora jest materiałem pomocniczym dla człowieka/biegłego, nie wnioskiem przyczynowym.
> *(EN: The analyzer's result is auxiliary material for a human/expert, not a causal conclusion.)*

The label object `ageds.analysis-disclaimer/v1` contains both sentences and their SHA-256 (UTF-8):
`sha256_pl = 4b0c08a0ce3c680fd97550372383859ef2458a7b877133bf04ff8aab58d48a5c`,
`sha256_en = 25724ee6b57866f2dfdaa3015f0e9607505193e68d8b5803aa080e3a912f1cf1`. They are computed by code
(`hypotheses.DISCLAIMER`), stored inside every event-study
result (and therefore covered by the result hash), rendered with `data-disclaimer-sha256` on the page and
re-hashed in real Chromium by the browser acceptance script.

## What is stored (append-only)

| Table | Meaning | Guard |
|---|---|---|
| `hypotheses` | identity of a hypothesis lineage | UPDATE/DELETE raise |
| `hypothesis_versions` | normalized definition JSON, `definition_sha256`, `registered_at`, `previous_version_id`, `previous_record_sha256`, `record_sha256` | append-only; INSERT trigger requires version *n* to extend version *n−1* of the same hypothesis and to quote its record hash (a chain like a commit log) |
| `hypothesis_runs` | event-study and candidate-search runs: version, definition hash, parameters, seed, code version, input hash, provider + opt-in, result JSON + hash | append-only; INSERT trigger requires the registered definition hash and `registered_at <= created_at`; `provider <> 'none'` requires `provider_opt_in = 1` |
| `hypothesis_dossier_items` | pointer to one record with a pinned locator (+ its hash) and a DB excerpt | append-only; one item per (hypothesis, locator hash) |
| `hypothesis_dossier_decisions` | classification history | append-only; `decided_by='system'` may only write `candidate` |
| `hypothesis_theses`, `hypothesis_thesis_evidence` | thesis text, typology status, revision link, evidence items | append-only; evidence must belong to the same hypothesis |
| `hypothesis_disclosures` | human text per disclosure quadrant (history kept) | append-only |

`audit_log` additionally receives `hypothesis_version_registered`, `event_study_run` (with the timeline
projection id when the seam reports one), `dossier_decision` and `thesis_added`.

## Preregistration

A definition is JSON (`ageds.hypothesis-definition/v1`):

| Key | Content (defaults in brackets) |
|---|---|
| `title`, `text` | the hypothesis in words |
| `stimulus`, `outcome` | event filters (at least one criterion each) |
| `density`, `eligible` | filters for the documentation-density control and for permutation positions (`{}` = every timed event) |
| `windows` | 1–20 half-open windows `[start_s, end_s)` relative to the stimulus, each with a unique label |
| `lags` | lag histogram `{min_s, max_s, bin_s}` (span of the windows, 24 bins, at most 500) |
| `direction` | `increase` \| `decrease` \| `two_sided` (`increase`) |
| `analysis` | `density_excludes_signal_classes` (`["passive"]` for definitions registered since 2026-10-17, method v3; earlier definitions lack the key and exclude nothing — see [LOCATION](LOCATION.md) §7), `permutations` (999, max 100 000), `seed` (1), `null_model` (`circular_shift`), `block_s` (7 days), `stratify_zone` (`UTC`), `assume_zone` + `assumption_rule` (both null; a zone needs a named assumption), `span` (observed span), `exclude_self_pairs` (true), `density_excludes_stimuli` / `density_excludes_outcomes` (true), `control_period_s` (1 day), `imprecise_times` (`exclude`), `max_time_uncertainty_s` (null = `lags.bin_s`), `flag_threshold` (0.05, used only to compare raw and normalized results in a warning), `density_ratio_warning` (1.5) |
| `dossier.queries` | default FTS5 queries for candidate search |
| `category_config_sha256` | required when a filter uses `categories`: hash of the private category configuration |

Filter criteria (`event_types`, `exclude_event_types`, `tags_any`, `tags_all`, `counterparties`, `chatbots`,
`directions`, `collection_ids`, `categories`, `text_query`) combine with AND; values of one list with OR.
Lists are sets: they are sorted and de-duplicated before hashing, so reordering never changes the hash;
window order does. Unknown keys, booleans in number fields, NaN, invalid zones and invalid FTS5 syntax are
refused — nothing is silently ignored. The hash is SHA-256 of the canonical JSON
(`python-json-sorted-keys-compact-utf8-no-nan/v1`, the same profile as citation packets) of the
*normalized* definition including defaults. The page shows the server hash, recomputes it with WebCrypto
from the canonical text, and shows the canonical JSON before anything is stored.

An unchanged definition returns the latest version (`created: false`); any change appends a version
linked to the previous one. An analysis accepts only a version id, re-hashes the stored definition and
refuses on mismatch (e.g. if someone dropped a trigger and edited the row). The page and list show
**registered at** and **first analysed at** (earliest event-study run of that version).

What preregistration proves: the order of records in this database. It does not prove what the author
had seen before registering; the disclosure therefore reports how many events had already been imported
at the first registration.

## Event study

Inputs come only through the analysis seam `analysis.source.load_items` (UTC epoch seconds; events without
a placeable time go to `untimed` with a reason). The run records the seam's rule id and rule SHA-256
(`unknown` for the interim seam rule `analysis_source.v1`; the timeline's `display_time@N` once the seam
delegates to it) and passes `assumption_rule` when the seam accepts it. Times are converted to integer
milliseconds; every statistic below is integer or exact-fraction arithmetic.

* **Window statistic:** number of (stimulus, outcome) pairs with `outcome − stimulus ∈ [start_s, end_s)`.
  Effect sizes: observed and null-mean pairs per stimulus, their difference and ratio, stimuli with ≥ 1
  outcome, and unique outcomes inside the union of windows (shows double counting).
* **Null distribution:** `n` moves of the stimulus times by the registered model, using the versioned
  SplitMix64 generator (`splitmix64.v1`, reference value checked in tests) — not Python's `random`.
  * `circular_shift`: one offset per permutation, wrapped within the observed span (keeps stimulus spacing);
  * `permutation`: each stimulus takes the time of a distinct eligible *event* (destroys clustering);
  * `stratified_weekday_hour`: permutation inside weekday × hour strata in `stratify_zone`; strata with fewer
    eligible events than stimuli keep their stimuli in place and are listed;
  * `block_permutation`: the full blocks of `block_s` are shuffled (keeps clustering inside blocks; needs ≥ 2
    full blocks); when the span is not a multiple of `block_s` the partial last block stays in place with its
    stimuli (`partial_block_s`, `stimuli_kept_in_partial_block`).
* **Eligible pool (method `ageds.event-study-method/v2`).** A permuted stimulus borrows an eligible event's
  time and stands for that event, exactly as an observed stimulus is treated: while `exclude_self_pairs` is on
  (default) outcome events are **not** eligible — the default `eligible: {}` and a user-defined set that
  intersects the outcomes alike (`eligible_excluded_outcomes` is recorded) — and, while
  `density_excludes_stimuli` is on, a borrowed density event is not counted as its own documentation at lag 0.
  A pool that holds only the stimuli themselves is refused (every permutation would reproduce the data; use
  `circular_shift`/`block_permutation` or let other documented events be eligible). Density events cannot
  also be removed from the pool: with the default selectors the pool would then be the stimuli alone.
* **p-value:** `(k + 1) / (n + 1)`, `k` = null statistics at least as extreme as observed in the registered
  direction (two-sided: distance from the null mean; for the normalized share the mean is rounded to a double
  with `math.fsum` and every distance is compared exactly — fractions are never accumulated, so the cost is
  linear and counted in `max_work`). Never 0, never above 1. No parametric p-values.
* **Calibration (tested).** On independent uniform stimuli, outcomes and other documented events (40 seeded
  datasets × both one-sided directions), every null model gives p ≤ 0.05 in at most 15 % of datasets and a
  mean p in [0.35, 0.65], for the raw and the density-normalized statistic. Method v1 failed this for
  `permutation` and `stratified_weekday_hour` (review 2 finding 1: placements on outcome times created
  lag-0 pairs the data cannot have; on the reviewer's data the null mean was ≈ 48 pairs against ≈ 3 observed).
* **Detection bias control:** documentation density `D` (default: all timed events that are neither stimuli
  nor outcomes). The control series shows counts per period (`control_period_s`, coarsened to at most 5000
  bins) of all events, documentation, outcomes and stimuli, plus empty periods and the longest gaps. The
  **density-normalized** statistic is the outcome share of documentation inside the windows
  `ΣO_w / Σ(O_w + D_w)`, compared as exact fractions with the same permutations (draws with an empty
  denominator are reported as undefined, not counted). Design note: an excess statistic
  `O_w − (|O|/|D|)·D_w` was tried first and rejected — under bursts the null windows fall in sparse
  periods with smaller variance, which made the excess test anti-conservative in the planted null.
* **Lag histogram:** observed counts per bin and the null mean and 2.5/50/97.5 % nearest-rank quantiles.

### Warnings — always present

Each result lists all of these with computed values; `flagged` marks the ones the data trigger.

| id | Computed from |
|---|---|
| `causation` | always flagged (temporal association ≠ causation) |
| `detection_bias` | windows where the raw p ≤ `flag_threshold` < normalized p (severity high), and documentation density in windows ≥ `density_ratio_warning` × null |
| `seasonality` | total-variation distance of stimulus and outcome weekday/hour distributions from all events (≥ 0.2 each) with a shared deviation; mitigated under the stratified null |
| `clustering` | stimulus pairs closer than each window width; severity high when the null model destroys clustering |
| `dependence` | coefficient of variation of outcome gaps > 1.5 (bursty outcomes) |
| `unknown_zone_excluded` | untimed events by reason (`unknown_zone`, `nonexistent_local_time`, `no_time_candidate`, …) |
| `time_uncertainty` | events whose time interval (timeline `t_lo`/`t_hi`, else the candidate precision) is wider than `max_time_uncertainty_s` (default: lag bin); excluded or kept per `imprecise_times` |
| `time_basis` | stimulus/outcome times based on weak kinds (file mtime, export time, …) |
| `preregistration_deviation` | per-run overrides of `permutations`, `seed`, `null_model`, `block_s`, `stratify_zone`; **selected sets that changed among events that already existed at the version's first recorded run** (`changed_sets`, e.g. post-hoc re-tagging, category corrections, time edits); tag edits on tags named in the filters and category corrections of loaded events made **after `registered_at`** (`edits_after_registration`). New events since that run are listed (`new_events_since_baseline`) without flagging. |

### Preregistration check of the inputs (review 2 finding 7)

Filters select by mutable attributes (tags, human category corrections, text, projected times). Every run
of method v2 stores `selection`: per set (`stimulus`, `outcome`, `density`, `eligible` pool) the count and
the SHA-256 of the sorted event ids, plus the largest event id the run could see. A later run of the same
version compares, for events with an id up to that maximum, its sets with the version's first run that has
such a record (runs of method v1 have none and are skipped) and flags any difference; audit rows
`tag_bulk_add/remove`, `tag_rename`, `tag_merge` touching a tag named in the filters and
`analysis_label_annotations` of loaded events after `registered_at` (second resolution, ties counted) are
flagged as edits after registration. A re-run on unchanged data writes an identical record, so the result
hash stays reproducible. Text queries are not audited (the FTS index has no edit log); a changed text match
still shows up as a changed set.

### Reproducibility

The result JSON contains the definition hash, effective parameters, seed, RNG id, an input hash over the
exact (event id, time) sets, untimed/imprecise exclusions and the source rule; `result_sha256` is its
canonical hash. Re-running the same version on the same data yields the same `result_sha256`
(`identical_result_runs` lists earlier runs). `code_version` (`git describe --always --dirty`, else
`unknown`) and the run timestamp are stored on the run row, outside the result hash. Limits are explicit
(`max_events`, `max_fts_ids`, `max_work`, `max_null_cells`); exceeding one refuses the run instead of
analysing partial input. Results of method v2 carry `schema: ageds.event-study-result/v2` and
`method: {id: ageds.event-study-method/v2, history}`; stored v1 runs (no `method` key) stay unchanged and
valid as historical records with their own hashes — a new run of the same version records the new method.

## Evidence dossier

Items point to exactly one record and pin its version in a locator whose hash identifies the item:
`event` (body hash), `artifact` (content hash; optional acquisition observation), `derived_text` (version
id, run id, text hash), `anchor` (citation anchor with derived-text version, range, quote hash and
selector; the page links the existing citation packet), `annotation` (body hash and `updated_at`).
Reading an item re-locates the record and reports `matches`, `changed` or `missing`.

Classification (`supporting`, `contradicting`, `context`, `rejected`, back to `candidate`) is a human
decision stored as a new row naming the hypothesis version considered. Candidate search stores a run and
only creates `candidate` items:

* FTS5 over the existing index (events, transcripts, artifacts) with the definition's queries;
* deterministic rules: stimulus events and outcome events falling into a registered window (hint with
  window and lag), under the same time-uncertainty policy as the event study;
* an optional provider interface (`CandidateProvider.propose`). Only `none` ships; it makes no call. Any
  other provider must be registered in code and requires `opt_in=true` for that run; the run records
  provider, model, version and opt-in. Provider output is validated against the database (unresolvable
  references are rejected and counted); its rationale is stored as labelled model text, never as evidence.

### Material against the hypothesis (2026-10-27)

A definition can preregister what would count against it:
`dossier.disconfirming_queries` (FTS5, the same limits as `queries`) and
`dossier.disconfirmation_criteria` (text). Both enter the normalized definition only when
given, so existing definitions keep their hash.

`candidate_search(direction=…)` runs the supporting queries (`definition`), the
disconfirming ones (`disconfirming`) or `both`; `both` is the default when the definition has
disconfirming queries. CLI: `dossier-candidates --direction`; API: `direction`. Each FTS
report and hint names its `search_direction`, and a hit of both says so in
`search_directions`. A contrary hit is still a `candidate`: only a human classifies it as
`contradicting`.

`dossier.disconfirmation_status` reports:
* the preregistered disconfirming queries and criteria;
* disconfirming searches for the latest version;
* contrary hits by classification.

The disclosure shows this as the known unknown `disconfirmation_search`. It is flagged when
nothing was preregistered, nothing was searched for the current version, or contrary hits
await review. Every current thesis carries `disconfirmation_flags`
(`no_disconfirmation_search`, `contrary_candidates_unreviewed`). These are kept apart from
`flags`, which stay about the thesis's own evidence. Finding no contrary hit is not proof that
none exists; see the documentation gaps of the event study.

Not covered: ledger entries (bank, health) are not in the search index or dossier targets.
Cite them through their statement artifact.

## Theses and epistemic disclosure

Review 2 P3: next to `current_theses_without_evidence` (no pointer) the disclosure lists
`current_theses_without_accepted_evidence` — current theses flagged `unsupported` or `evidence_not_accepted`
(pointers only to candidates or rejected items); `list_theses` returns it as
`without_accepted_evidence_current`.

Each thesis has a status from the typology (rank 1–7): `quote` (cytat) → `paraphrase` (parafraza) →
`inference_chain` (łańcuch inferencji) → `analogy` (analogia) → `speculation` (spekulacja) →
`counterfactual` (kontrfaktyk) → `normative` (teza normatywna). Flags are computed on every read:
`unsupported` (no evidence item), `evidence_not_accepted` (only candidates/rejected), `quote_without_exact_source`,
`source_changed`, `superseded`. Revisions are linked rows; forks are refused.

The disclosure shows, per quadrant, the latest human text (history kept) and computed facts: registered
versions and hashes, dossier counts and the latest run (known knowns); candidates awaiting review, theses
without evidence, changed sources, untimed and out-of-span exclusions, flagged warnings, weak time bases,
insufficient strata (known unknowns); events already imported at first registration, accepted items no
thesis uses, contradicting items (unknown knowns); documentation gaps and the fixed statement that
undocumented events cannot be counted (unknown unknowns).

## Categories (time-budget track)

`categories` filters use `analysis.categories` (track E1): effective label = latest human correction >
deterministic rule > default; model labels are never used here. The definition stores only
`category_config_sha256`; the private configuration is supplied per run (CLI `--categories PATH`,
`AGEDS_ACTIVITY_CATEGORIES`, or an inline object over HTTP — never a server path from a client) and must
hash to the registered value. The result records the configuration description and a hash of the labels.

## Interfaces

HTTP: `GET /api/hypotheses`, `POST /api/hypotheses/preview`, `POST /api/hypotheses`,
`GET /api/hypotheses/{id}`, `POST /api/hypotheses/{id}/versions`, `GET /api/hypotheses/versions/{vid}`,
`POST /api/hypotheses/versions/{vid}/event-study` (`overrides`, `category_config`),
`GET /api/hypotheses/runs/{rid}`, `POST /api/hypotheses/{id}/dossier`,
`POST /api/hypotheses/dossier/{item}/classify`, `POST /api/hypotheses/{id}/candidates`,
`POST /api/hypotheses/{id}/theses`, `POST /api/hypotheses/{id}/disclosure`. Errors: 404 unknown record,
422 invalid input or refused analysis.

CLI (`python -m server.app.cli …`): `hypothesis-register FILE.json`, `hypothesis-version ID FILE.json`,
`hypothesis-preview FILE.json`, `hypotheses-list`, `hypothesis-show ID`, `dossier-add ID KIND TARGET_ID`,
`dossier-classify ITEM CLASS`, `dossier-candidates ID`, `dossier-list ID`, `thesis-add ID --type T --text …`,
`disclosure-add ID QUADRANT TEXT`, `eventstudy-run VERSION_ID [--permutations N --seed N --null-model M --block-s S --summary]`,
`eventstudy-show RUN_ID [--output NEW.json]`. The analyzer list command is `hypotheses-list` because
`hypothesis-list` already lists the Loom relation-hypothesis quarantine (track G).

The page `/hypotheses` has: hypothesis list and registration form with hash preview; versions; event
study (run form, runs, warnings, windows table, lag histogram vs null band with hover tooltips and a
table view, density control series as small multiples, weekday table); dossier with candidate search,
manual pointers and decision history; theses; the four disclosure quadrants.

## Decisions and reversal conditions

| Decision | Default / alternative | Revisit when |
|---|---|---|
| Null model | `circular_shift` (keeps stimulus spacing); others selectable and recorded | a domain needs a different exchangeability assumption |
| Normalization | outcome share of documentation | another density model is validated on planted nulls |
| Coarse times | excluded when wider than the lag bin | the timeline offers calibrated sub-interval placement |
| `flag_threshold` | 0.05, only for the raw-vs-normalized warning | the user prefers a different review threshold (it is a parameter) |
| LLM candidates | interface only, provider `none` | a provider is approved with per-run opt-in and a cost bound |

## Boundaries

* Results are descriptive and interpretive. No causal inference, no parametric tests, no confidence that
  a lag "explains" anything.
* Preregistration proves record order in this database, not the author's prior knowledge.
* `actor` is self-declared (the service has no authentication).
* Edge effects: windows near the span ends are truncated in both observed and null statistics.
* Pairs in overlapping windows are dependent; the warning reports it, the circular/block nulls preserve
  stimulus clustering, but outcome dependence still widens the true spread.
* No real LLM provider is implemented or called.
* Tables of this layer are declared extension tables of the metadata package (integration 2026-10-16).
  The `hypothesis_dossier` report section uses `analysis.dossier_report.report_payload` and reuses
  `DISCLAIMER` from this module, re-hashed by the reports layer.

# Time budget and feature series (interpretive layer)

Task `AGEDS-TASK-20261016-E` part 1 (Etap 4). Code: `server/app/analysis/` (except the
hypothesis modules of part 2), schema versions 50–53 (`server/app/schema/analysis.py`),
routes `server/app/api/analysis.py`, CLI `server/app/commands/analysis.py`, page
`/analysis/budget` (nav “Budżet czasu”). Receipt:
[TIME_BUDGET_2026-10-16](validation/TIME_BUDGET_2026-10-16.md).

Every result here is an **interpretive artifact**: an estimate with an explicit model,
parameters, bounds and exclusions. It never replaces observations, and
`nieobserwowane` (no observation) is never reported as idleness. Every number comes
from code; no model writes a number into a result.

## 1. Use

```bash
python -m server.app.cli budget-run --config PRIVATE.json --from 2025-03-01 --to 2025-04-01 \
    --zone Europe/Warsaw [--params '{"session_gap_s": 600}'] \
    [--assume-zone Europe/Warsaw --assumption-rule my-phone-2025] [NEW_OUTPUT.json]
python -m server.app.cli budget-show RUN_ID [NEW_OUTPUT.json]
python -m server.app.cli budget-list [--kind time_budget|feature_series]
python -m server.app.cli features-run --config PRIVATE.json --from … --to … --zone … [--params JSON] [NEW_OUTPUT.json]
python -m server.app.cli features-show RUN_ID [NEW_OUTPUT.json]
python -m server.app.cli label-correct EVENT_ID --categories '{"zdrowie": 1}' [--note …] [--author …]
python -m server.app.cli labels-show EVENT_ID [--config PRIVATE.json]
python -m server.app.cli model-label-run --config … --from … --to … --zone … --provider none|fake [--opt-in]
```

`--config` falls back to the environment variable `AGEDS_ACTIVITY_CATEGORIES`. Output files
are new files only (never overwritten). Each command prints one JSON object.

| Route | Purpose |
|---|---|
| `GET /analysis/budget` | page: run picker, candle chart (SVG, no CDN), unobserved strip, table, sensitivity, exclusions, method; each candle and table row lists its events |
| `POST /api/analysis/budget/runs` | body `{from, to, zone, params?, assume_zone?, assumption_rule?, config?}`; returns the run summary |
| `GET /api/analysis/budget/runs/{id}` | stored run with the full result |
| `GET /api/analysis/budget/runs/{id}/candle?granularity=&bucket=&category=` | events of one candle with the label stored for that run (weight, path, rule ids, correction id, model run) and links (artifact page, `/api/analysis/events/{id}`, `/api/timeline/items/event:{id}`) |
| `GET /api/analysis/runs?kind=` | run list |
| `POST /api/analysis/features/runs`, `GET /api/analysis/features/runs/{id}` | feature series |
| `GET /api/analysis/events/{id}` | one event: current label with path and shadowed candidates, correction history, stored labels |
| `POST /api/analysis/labels/corrections` | `{event_id, categories, note, author}`; `{}` withdraws the previous correction |
| `POST /api/analysis/model-label-runs` | model-label hypothesis run (`provider` `none`/`fake`, `opt_in`) |
| `GET /api/analysis/config`, `/defaults` | server configuration identity (hash, version, categories) and parameter presets |

Over HTTP the configuration is either inline (`config`) or the operator's
`AGEDS_ACTIVITY_CATEGORIES` file. **A filesystem path is never accepted from a request**
(the API is unauthenticated).

## 2. Categories (private configuration)

Schema `ageds.activity_categories.v1`; a generic example only:
[`categories.example.json`](../server/app/analysis/data/categories.example.json). Keep the
real file outside the repository.

| Field | Meaning |
|---|---|
| `version` | declared version; recorded with the canonical SHA-256 of the parsed JSON (and the file's byte hash) in every result |
| `categories[]` | `id` (`[a-z0-9_]`), `label`, optional `color` (`#rrggbb`; otherwise a validated categorical order) |
| `default_category` | category for events no rule matches, or `null` → reserved `bez_kategorii` |
| `combine` | `max` (per category the largest weight of all matching rules) or `first` (first matching rule only) |
| `official_categories` | categories counted by the feature `official_counterparties_distinct` |
| `rules[]` | `id`, `version`, `kind`, `values`, `weights`, optional `event_types`; keyword rules also `fields` and `whole_word` |

Rule kinds: `counterparty` (dictionary; phone-like values compared by digits, others
case-folded; also addresses in sender/recipients), `email_domain` (domain or subdomain of
any address in the event), `keyword` (NFKC + case-fold substring or whole word in the
listed fields; bodies are scanned up to their first 65 536 characters), `chatbot`, `tag`,
`thread` (conversation id), `event_type`.

**Weights** are shares of the event's time in `(0, 1]` and are not normalized:
`{"a": 0.5, "b": 0.5}` splits an event, `{"a": 1, "b": 1}` claims it fully for both and the
overlap rule decides (§4). Reserved categories `nieobserwowane` and `bez_kategorii` always
exist; no rule or correction may assign an observed event to `nieobserwowane`.

## 3. Labels and their provenance

Precedence, each label naming its path:

1. `annotation` — latest human correction (append-only `analysis_label_annotations`);
2. `rule` — deterministic rules; every label keeps `rule_id@version` of all rules used;
3. `model` — a model-label hypothesis, only if the run names one stored model-label run
   (`params.model_label_run_id`) and only for events no rule matched;
4. `default` — the default category.

Lower-precedence candidates stay visible (`shadowed`). Each budget run stores its labels
append-only under a labelling run (`analysis_label_runs`, `analysis_event_labels`: event,
category, weight, path, rule ids, correction id, model run id); an identical labelling is
reused. Model runs (`analysis_model_runs`, `analysis_model_outputs`) record provider, model,
version, parameters and the per-run opt-in. Providers: `none` (default; records that nothing
ran) and `fake` (deterministic hash of the event id, for tests; explicitly not a model).
Any other provider is refused as unavailable — no network adapter exists.

## 4. The budget model (rule `time_budget.v1`)

**Time source.** Items come from the timeline projection (`display_time@N`, its SHA-256 and
the projection parameters are recorded in `result.input`), see [TIMELINE.md](TIMELINE.md).
Unknown-zone wall clocks are excluded and reported (also when only some real UTC offset
could place them in the window); an operator zone needs `assume_zone` **and** an
`assumption_rule` id. Items hidden by the projection (chat parts, hidden chat messages) are
excluded unless `include_hidden`; chat messages whose `content_kind` is not in
`chat_content_kinds` (default `text`, `multimodal`) are excluded; `chat_conversation`
containers are excluded by default. DST folds are never resolved silently: by default they
are excluded with reason `ambiguous_fold`; with `ambiguous_fold: carry` the display
(earlier) instant is used for lower/probable and only the upper variant covers both
instants. Items whose uncertainty interval `[t_lo, t_hi]` is wider than
`max_time_uncertainty_s` are excluded; with `uncertainty_mode: widen_upper` the upper
variant covers `[t_lo, t_hi + confirmed duration]` of every remaining uncertain item.
The interim projection `analysis_source.v1` is only a labelled fallback
(`seam: interim_fallback`) for a checkout without the timeline package.

**Variants** per category, built from intervals on the UTC axis:

| Variant | Contents |
|---|---|
| lower | confirmed time only: source start–end ranges (call `end`, calendar entries, timed blocks) and declared durations (call `duration`, WAV header of a recording's stored copy) |
| probable | lower + typing before outgoing text (`chars / typing_chars_per_min`, ≤ `max_typing_s`) + reading after incoming chatbot answers (`chars / reading_chars_per_min`, ≤ `max_reading_s`; event types in `reading_event_types`) + session spans (events of one category joined while the gap to the session's end is **below** `session_gap_s`) |
| upper | probable + gap attribution: every gap between observation blocks (intervals and point events) is attributed to the category on each side, `min(gap, max_attribution_s)` seconds adjacent to that side |

Inside one category time is the **union** of its intervals weighted by label weight
(maximum, never a sum): two parallel chatbot conversations of one category count once.
Across categories `overlap_rule` decides:

| Rule | Behaviour |
|---|---|
| `split_equal` | simultaneous shares scaled to sum 1 when they exceed it |
| `priority` | shares assigned in `priority` order (then configuration order) up to 1 |
| `count_both` | each category keeps its full union; `overlap_s` and `double_counted_probable_s` show the double counting |

For `split_equal` and `priority` the variants are **layered**: confirmed time takes
precedence over estimates, estimates over gap attribution. This keeps
`lower ≤ probable ≤ upper` per category (an estimate never reduces confirmed time).
`overlap_s` is time where shares of active categories sum above 1. Observed time whose
weights sum below 1 goes to `bez_kategorii`, so for non-double-counting rules
`Σ categories + nieobserwowane = bucket length` (tested).

**nieobserwowane** is the time no observation covers in a variant: lower =
`length − observed(upper)`, probable = `length − observed(probable)`, upper =
`length − observed(lower)`.

Items are loaded for the window ± a margin (largest session gap, attribution, typing and
reading cap in the grid + 1 s) so sessions and gaps crossing the window edge are exact
inside the window; only time inside the window is allocated.

## 5. Candles

Buckets are local days in `zone` (DST days last 23 h or 25 h), ISO weeks and months
(unions of days); buckets cut by the window are `partial` (marked `*` on the page). For each
bucket × category: `lower`, `probable`, `upper` at the run's parameters, **wick** =
[min over the grid of lower, max over the grid of upper], **body** = [min, max] over the grid
of probable. The unobserved band has the same fields and is drawn as a separate strip
(share of the bucket) so it does not compress the category scale. Each candle lists its
events (`event_ids`, capped by `max_candle_events` with `event_ids_truncated`; exact
`event_count`), role counts (`explicit`, `typing`, `reading`, `session`, `gap_before`,
`gap_after`, `time_uncertainty`, `point`) and label-path counts.

## 6. Sensitivity

The grid (`grid_mode` `one_at_a_time` around the run's parameters — default 11 points — or
`full` Cartesian product, cut at `max_grid_points` with `truncated: true`) varies
`session_gap_s`, `typing_chars_per_min`, `reading_chars_per_min`, `max_attribution_s` and
`overlap_rule`. Reported: totals per point, per-category range and relative spread, and
**flips** beyond `flip_tolerance_s`: window-total ordering of two categories, the top
category of a bucket (only when it was a clear winner), and the direction of change between
consecutive buckets. Any flip sets `sensitivity.unstable`; affected candles list the reason.
“Reasonable change” means the declared grid; change the grid to change that judgement.

## 7. Feature series (rule `feature_series.v1`)

Deterministic, bucketed by the local date of the start time: `message_count`,
`messages_outgoing`, `messages_incoming`, `official_counterparties_distinct` (normalized
counterparties of events labelled with an official category by a counterparty/e-mail-domain
rule or a human correction), `night_activity_share` (count share in
`[night_start, night_end)`; `null` for a bucket without events), `assistant_response_chars`
(mean, median, n), `category_time_share` (probable seconds of the budget at `budget_params`
divided by bucket length and by observed probable time). The model-estimated series `tone`
is separate, labelled `model_estimated`, bound to its model run, and `not_run` with the
default provider `none`.

## 8. Storage and reproducibility

`analysis_runs` (append-only): kind, parameters JSON + hash, configuration hash + version,
code version (git head or `unknown`) + hash of the analysis sources and presets, source rule,
input event-id-set hash, labelling run, result JSON + hash, created time. A request whose row
would be identical (same parameters, configuration, inputs, code and result) returns the
stored run with `reused: true`; anything else is a new row and earlier rows stay readable.
Results also carry `items_sha256` (times, durations, lengths) and the label digest.

## 9. Parameter presets

[`budget_defaults.json`](../server/app/analysis/data/budget_defaults.json) and
[`features_defaults.json`](../server/app/analysis/data/features_defaults.json) are presets,
not rules; every value is a per-run parameter stored in the result. Unknown parameter names
are refused.

## 10. Decisions, conflicts and limits

* The prompt lists “session span” under the lower bound. Inferred sessions depend on a gap
  threshold and are not confirmed, so they belong to the probable variant; only an explicitly
  recorded start–end range counts as confirmed.
* “Whole gap to the adjacent category” is applied on both sides of a gap (each neighbour may
  claim up to `max_attribution_s`); the upper bounds of different categories may therefore
  overlap — they are per-category bounds, not a partition.
* Reading time defaults to chatbot answers only; reading SMS/e-mail at arrival is an opt-in
  (`reading_event_types`).
* Prompt Etap 4 asks for “configuration path via API”; over the unauthenticated HTTP API a
  path is refused (inline JSON or the operator's environment file instead).
* Analysis runs call the timeline projection, which refreshes in its own transaction: start a
  run before writing in the same connection.
* Measured on synthetic data (single container): 20 000 events over 90 days with 11 grid
  points ≈ 7 s; 100 000 events ≈ 38 s. `max_items` (default 250 000) bounds a run; a cut
  input is reported as `source_truncated`.
* Not done: real model providers (none exist by design), per-event roles in the candle event
  list (role counts are per candle), a light theme of the page, report export (track F).

## 11. Python API shared with the hypothesis track

`analysis.categories`: `load_config`, `parse_config`, `resolve_config_path`, `label_items`,
`labels_sha256`, `add_annotation`, `load_effective_annotations`, `annotation_history`,
`record_label_run`, `category_palette`, constants `UNOBSERVED`, `UNLABELLED`.
`analysis.intervals`: `normalize`, `union`, `total`, `clip`, `intersect`, `complement`,
`subtract`, `sessionize` (rule `sessionize.v1`), `weighted_segments`.
`analysis.source`: `load_items` (timeline seam), `event_item`.
`analysis.budget`: `prepare_inputs`, `compute_budget`, `run_budget`, `effective_params`.
`analysis.runs`: `store_run`, `get_run`, `list_runs`.

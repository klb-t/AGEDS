# Workbench conveniences (task H, 2026-10-16)

Task `AGEDS-TASK-20261016-H` (Etap 7). Everything here is a working aid: a
projection computed by code or a human annotation. Nothing changes originals,
evidence rows or their provenance. Receipt:
[WORKBENCH_2026-10-16](validation/WORKBENCH_2026-10-16.md).

| Area | Code | HTTP | Page |
|---|---|---|---|
| Layered search | `server/app/workbench/layered_search.py`, `layers.py`, schema v80 | `GET /api/workbench/search` | `/workbench` |
| Saved searches | `saved.py`, schema v81 | `/api/workbench/saved-searches[/{id}[/run]]` | `/workbench` |
| Bulk tags, rename, merge | `tags.py` (existing `tags`, `artifact_tags`, `event_tags`) | `/api/workbench/tags[/apply\|/merge\|/{id}/rename]` | `/workbench#tags` |
| Working folders ("teczki") | `folders.py`, schema v82 | `/api/workbench/folders/...` | `/workbench/folders` |
| Timeline bookmarks | `bookmarks.py`, schema v83 | `/api/workbench/bookmarks[/{id}]` | `/workbench/bookmarks` |
| Knowledge summary, quick jump, citation text | `knowledge.py` | `/api/workbench/artifacts/{id}/knowledge`, `/api/workbench/jump`, `/api/workbench/citations/{id}/text` | `/workbench/artifacts/{id}`, panel on `/artifact/{id}` |
| Index check / rebuild | `index.py` | — | CLI `workbench-index-check [--rebuild]` |
| i18n PL/NL/EN | `i18n.py`, `static/i18n/*.json`, `static/workbench-i18n.mjs` | `GET /api/workbench/i18n` | all pages through `_nav.html` |
| Theme | `static/app.css` tokens, `theme.py`, `static/workbench-theme.mjs` | `GET /api/workbench/theme/contrast` | toggle in the nav |
| Shortcuts, palette, recent | `static/workbench-keys.mjs`, `workbench-keys.json`, `workbench-shell.mjs` | — | `?`, `Ctrl/Cmd+K` on every page |

JSON errors use `{"detail": {"code", "message"}}`: 422 `invalid_input`,
404 `not_found`, 409 `conflict`, 503 `work_limit` or `database_unavailable`,
403 `cross_site_write`. Every non-GET route under `/workbench` and
`/api/workbench` refuses browser requests marked `Sec-Fetch-Site: cross-site`
or with a foreign/`null` `Origin` (cheap CSRF guard; local scripts send
neither). With `EW_REQUIRE_TOKEN=1` the integrations middleware also gates
`/api/workbench/*` and, since review 2 finding 6, the HTML pages (search result
snippets included); browsers log in once through `/integrations/session` (see
[read API](READ_API.md)). The Host allowlist applies always.

## 1. Search across all layers

`GET /api/workbench/search?q=&layer=&layer=&limit=` (Python:
`layered_search_page(q, layers=None, limit=100)`). The query is validated by
`server.app.search.validate_query` exactly as the legacy search: UTF-8 bytes,
lexical tokens, nesting, the FTS5 parser in an isolated index, 1–200 results,
`has_more`/`complete`, and a 2,000,000-step SQLite VM budget per statement whose
exhaustion returns no partial result (503). The query text is never rewritten.
`/api/search`, its headers and the legacy `search_fts` are unchanged.

The index is a separate external-content FTS5 table `workbench_fts` over
`workbench_search_docs`, filled from four views (`workbench_search_source_*`)
by triggers on `artifacts`, `events`, `derived_text` and `annotations`
(insert, update, delete) and backfilled from existing rows by migration 80.
Annotations were not indexed before; they are now, in this index only.
Index writes are upserts without foreign keys, so they cannot block an
evidence write. `workbench-index-check` compares the index with the views
(missing / stale / orphaned per kind, FTS integrity-check); `--rebuild`
recomputes it (audited, evidence untouched).

| Layer | Source | Epistemic label | Meaning |
|---|---|---|---|
| `artifact_metadata` | `original_name`, `source_locator`, `mime_type`, `metadata_json` | `raw_source_field` | value as recorded at acquisition; not verified |
| `event` | type, contact, address, sender, recipients, subject, body | `parser_output` | parser projection of a raw record; time/direction are parser interpretation |
| `transcript` | `derived_text` kinds `transcript`, `asr` | `model_output` | ASR output; `provenance.provenance_status` is `recorded_processing_run` or `legacy_unknown` |
| `ocr` | kind `ocr` | `model_output` | OCR output |
| `parsed_text` | kinds `parsed_text`, `parser_text`, `extracted_text`, `text_extraction` | `parser_output` | deterministic text extraction |
| `derived_other` | any other kind | `unclassified` | never guessed into another layer |
| `annotation` | `label`, `body` | `human_annotation` | a person's note or interpretation |

Each hit: `layer`, `epistemic_layer`, `object_kind`, `object_id`, `subkind`,
`artifact_id`, `title`, `snippet` (plain source text: escape it), `rank`,
`url`, `object_status`, `provenance` (version/run/model for derived text).
`facets` counts hits per layer over all layers (independent of the filter)
for at most `facet_scan_limit` = 5,000 matches; beyond that
`facets_complete=false` and counts are lower bounds.

Polish: `unicode61 remove_diacritics 2` folds ą ć ę ń ó ś ź ż but not ł/Ł
(no Unicode decomposition), so `lodz` would not find `Łódź`. A third indexed
column `folded` holds `ł→l`, `Ł→L` copies of title+content when they contain
ł; stored text is never changed. Unqualified queries match it; a column
filter (`content:lodz`) deliberately does not, and `folded:` is rejected by
the shared validator.

## 2. Saved searches

A saved search stores name (unique), query, `{"layers": [...]}`, limit and a
note — never results. `POST .../{id}/run` executes the query now, stores the
counts computed by code (`returned`, `has_more`, `complete`, `facets`,
`facets_complete`, per-epistemic-layer counts, `results_stored: false`) on the
row and appends them to `workbench_saved_search_runs` (append-only; the log
survives deletion of the saved search). Queries are validated at save time.

## 3. Bulk tagging, rename and merge

`POST /api/workbench/tags/apply {"tag", "targets": ["artifact:12", "e:7", {"kind": "event", "id": 9}], "action": "add"|"remove"}`.
At most 10,000 distinct targets; unknown targets fail the whole operation
(404, nothing written). One `audit_log` row (`tag_bulk_add`/`tag_bulk_remove`)
lists `changed` and `unchanged` ids per kind. Tag names are kept exactly as
typed (single line, no leading/trailing whitespace, ≤ 200 characters).
`rename` refuses an existing name (409: merge explicitly). `merge` moves all
memberships onto the target, deletes the source tags and audits the full
member lists of each source. Similar names (NFKC + diacritics + ł + case
folding) are listed as `possible_duplicates` — a suggestion, never merged
automatically. Tags are human annotations; track C's timeline tag filter uses
the same tables.

## 4. Working folders ("teczki")

Folders nest (≤ 32 levels, sibling names unique, cycles refused) and hold up
to 10,000 ordered items. An item is `kind + id` with `pinned_version_id`:

| Kind | Target | Pinned version | Identity recorded at add time |
|---|---|---|---|
| `artifact` | `artifacts.id` | — | sha256, size, original name |
| `event` | `events.id` | — | artifact, source, type, ts_start, external_id, body/subject SHA-256 |
| `citation` | `evidence_anchors.id` | its `derived_text_id` | range, quote SHA-256 |
| `text_version` | `derived_text.id` (transcript, OCR, …) | itself | kind, run, model, language, text SHA-256 |
| `annotation` | `annotations.id` | its `derived_text_id` | label/body SHA-256, range, `updated_at` |

Every read recomputes the identity and reports `unchanged`, `changed`
(with `changed_keys`, e.g. an edited annotation) or `missing`. Adding,
removing, reordering, item notes and folder deletion (refused when not empty
unless `recursive=true`) are audited with the removed references; evidence is
untouched. Export: `GET /api/workbench/folders/{id}/export?format=json|csv&recursive=`
(browser download) or `python -m server.app.cli workbench-folder-export ID OUTPUT [--format] [--recursive]`
(new file only; refused inside `EW_SCAN_ROOTS`, the store or the database).
JSON keeps notes exactly; CSV prefixes cells starting with `= + - @ TAB CR`
with `'` (spreadsheet formula guard). Exports contain references and status
only (`schema: ageds.workbench-folder-references/v1`, `references_only`,
`source_bytes_included: false`, `text_included: false`, inert locators such
as `ageds:citation/5@derived_text/9`).

## 5. Timeline bookmarks — contract for the timeline page

A bookmark is a user-chosen time point or range on the common timeline (a
human annotation), not an event time. Item:

```json
{"id": 1, "label": "Spotkanie", "note": null,
 "start": {"raw": "2026-03-01T08:00:00+01:00", "utc": "2026-03-01T07:00:00Z", "local": "2026-03-01T08:00:00"},
 "end": null, "tz_status": "explicit_offset",
 "ref": {"kind": "event", "id": 7, "version_id": null, "segment_index": null, "exists": true},
 "view": {"zoom": "hour"}, "created_at": "...", "updated_at": "...",
 "epistemic_layer": "human_annotation", "time_semantics": "user-chosen timeline position; not an observed event time"}
```

| Route | Body / query | Notes |
|---|---|---|
| `GET /api/workbench/bookmarks` | `from`, `to` (ISO with offset or `Z`), `ref_kind`, `ref_id`, `include_unzoned`, `limit` (1–500, default 200) | overlap of `[start, end or start]` with `[from, to]` on UTC instants; order by instant, then id; `has_more`; `unzoned_excluded` counts zone-less bookmarks left out of a window |
| `POST /api/workbench/bookmarks` | `start` (required), `end`, `label`, `note`, `ref`, `view` | 201 with the item |
| `GET/PATCH/DELETE /api/workbench/bookmarks/{id}` | any of the create fields | update/delete audited with the previous content |

`ref.kind`: `event`, `artifact`, `citation` (by id) or `transcript_segment`
(`id` = artifact id, `version_id` = transcript `derived_text.id`,
`segment_index` checked against that version). `view` is opaque client state
(≤ 16 KiB JSON, no NaN). Raw text is kept; `+01:00`/`Z` give a UTC instant;
a zone-less value keeps `tz_status: "unknown"`, has no instant, is never
guessed and cannot be placed in an instant window (asking for both is 422).
Integration 2026-10-16: `/timeline` draws the bookmarks of its window in a strip
above the lanes (see [TIMELINE.md](TIMELINE.md) §6); `/workbench/bookmarks`
remains the plain list/create page.

## 6. Knowledge summary, quick jump, citation text

`artifact_knowledge(id)` counts what the tables record — content identity,
acquisitions by kind/source/locator, the same sha256 under other locators
(duplicates, linked), derived texts by kind with epistemic labels, runs by
status — and lists what is not recorded or `unknown`: missing artifact
fields, source times no acquisition recorded, legacy snapshots, versions
without a run, run fields recorded as `unknown`, unclassified kinds, untimed
events, `event_times` without a zone, failed jobs. Fixed `not_claimed`
statements (hash ≠ authorship/truth/time, language probability ≠
correctness, no signature/custody, membership ≠ evidence, word timing ≠
verified alignment). `scores` is always `null`. Shown as a collapsible panel
on `/artifact/{id}` (failure never breaks the evidence page) and on
`/workbench/artifacts/{id}`.

`GET /api/workbench/jump?q=` resolves `a:12`, `e#7`, `v 9`, `c:5`, `n:3`,
`f:2`, `b:1`, a bare number (every kind that has that id — ids are per table)
or a hex prefix of ≥ 6 characters (artifact sha256 by index range; citation
quote sha256), at most 20 candidates. `GET /api/workbench/citations/{id}/text?lang=`
returns the quote verbatim with the source, version, model, range, inert
locator, hashes and a localized caveat that the text is model output not
verified by listening.

## 7. Keyboard shortcuts

`/static/workbench-keys.mjs` exports `KeyRegistry` (registration with
conflict detection — equal or prefix sequences in overlapping scopes are
recorded, first registration wins), `normalizeCombo`, `comboFromEvent`,
`isEditable` and `renderHelp` (text nodes only). Keys are never intercepted
in inputs, textareas, selects or editable content. Defaults live in
`/static/workbench-keys.json` (edit the file; no hidden browser state):

| Keys | Action |
|---|---|
| `?` | show/hide the shortcut overlay |
| `Ctrl+K` / `Cmd+K` | command palette (navigation, actions, recent pages, quick jump, "search all layers") |
| `/` | focus the page's search field (`[data-wb-search]` or `input[name=q]`) |
| `g h`, `g w`, `g f`, `g b` | go to start, workbench, folders, bookmarks |
| `g t`, `g c` | go to timeline, catalogue (only when the nav has the page) |
| `g d` | switch theme |
| `Escape` | close the overlay (overlay scope only) |

Reserved (documented, conflict-checked, not handled by the shell): `/timeline`
uses `ArrowLeft ArrowRight + = - _ Escape`; the browser keeps `Tab`,
`Shift+Tab`, `Enter`, `Space`. A page can add bindings by creating its own
`KeyRegistry` or by adding entries with `"scope": "/its/path"` to the JSON.

## 8. Theme

`app.css` colours are custom properties (`--bg`, `--surface`, `--text`,
`--muted`, `--link`, `--code`, `--accent`, `--on-accent`, `--border`,
`--mark-*`, `--danger`, `--warning`, `--ok`, `--badge-*`, …). Dark is the
default look. A page opts in to light with `<html data-theme-support="light dark">`
(the Jinja helper `ui_html_attrs()` renders it with `lang` and an explicit
`data-theme` from the `ageds_theme` cookie). Then `prefers-color-scheme`
applies unless the viewer chose `light` or `dark` with the nav toggle (cycle
auto → light → dark). Pages that still hard-code dark colours (today
`timeline.css`) omit the attribute and stay dark; the toggle is disabled
there. Storage access is wrapped; without cookies the page renders auto/dark
and the toggle still works for the session.

Adopting tokens elsewhere: `#0d1117`→`--bg`/`--input-bg`, `#161b22`→`--surface`,
`#21262d`→`--neutral-button`/`--border-subtle`, `#30363d`→`--border`,
`#e6edf3`→`--text`, `#8b949e`→`--muted`, `#58a6ff`→`--link`,
`#79c0ff`→`--code`, `#1f6feb`→`--focus`, `#d29922`→`--mark-bg`/`--warning`,
`#3fb950`→`--ok`.

Text-token contrast (WCAG 2.x; computed from `app.css` by `theme.contrast_report()`,
CLI `workbench-theme-report`; all ≥ 4.5):

| Pair | Dark | Ratio | Light | Ratio |
|---|---|---:|---|---:|
| `--text` on `--bg` | #e6edf3 / #0d1117 | 16.02 | #1f2328 / #ffffff | 15.80 |
| `--text` on `--surface` | #e6edf3 / #161b22 | 14.64 | #1f2328 / #f6f8fa | 14.84 |
| `--text` on `--neutral-button` | #e6edf3 / #21262d | 12.88 | #1f2328 / #eaeef2 | 13.55 |
| `--muted` on `--bg` | #8b949e / #0d1117 | 6.15 | #59636e / #ffffff | 6.11 |
| `--muted` on `--surface` | #8b949e / #161b22 | 5.62 | #59636e / #f6f8fa | 5.74 |
| `--link` on `--bg` | #58a6ff / #0d1117 | 7.49 | #0969da / #ffffff | 5.19 |
| `--link` on `--surface` | #58a6ff / #161b22 | 6.85 | #0969da / #f6f8fa | 4.88 |
| `--code` on `--surface` | #79c0ff / #161b22 | 8.89 | #0550ae / #f6f8fa | 7.13 |
| `--on-accent` on `--accent` | #ffffff / #1a7f37 | 5.08 | #ffffff / #1a7f37 | 5.08 |
| `--on-accent` on `--accent-hover` | #ffffff / #238636 | 4.63 | #ffffff / #116329 | 7.39 |
| `--mark-text` on `--mark-bg` | #0d1117 / #d29922 | 7.50 | #1f2328 / #fff8c5 | 14.66 |
| `--danger` on `--surface` | #ff7b72 / #161b22 | 6.86 | #cf222e / #f6f8fa | 5.03 |
| `--warning` / `--badge-model` on `--surface` | #e3b341 / #161b22 | 8.89 | #9a6700 / #f6f8fa | 4.57 |
| `--ok` / `--badge-human` on `--surface` | #56d364 / #161b22 | 8.98 | #1a7f37 / #f6f8fa | 4.77 |
| `--badge-raw` on `--surface` | #d2a8ff / #161b22 | 8.88 | #8250df / #f6f8fa | 4.74 |

The receipt holds all 22 pairs per theme. One deliberate change to the dark
look: button text was `#e6edf3` on `#238636` (3.92:1, below AA); buttons are
now `#ffffff` on `#1a7f37` (5.08:1), hover `#238636` (4.63:1).

## 9. i18n (PL default, NL, EN)

Catalogs: `server/app/static/i18n/{pl,nl,en}.json`, flat keys, values are
strings with `{name}` placeholders or CLDR plural objects (`one/few/many/other`,
selected by `n`; Polish rules 1 / 2–4 except 12–14 / other). Language
precedence per request: `?lang=` (that request only) → cookie `ageds_lang`
(written by the nav selector) → `Accept-Language` (q-values, primary
subtag, ≤ 1,024 bytes / 32 entries) → `pl`. Unsupported values are ignored.
A key missing in NL/EN shows the Polish text and is logged once (WARNING); a
key missing everywhere renders `⟦key⟧` and is logged (ERROR); a value is never
empty. `workbench-i18n-check` / `catalog_report()` checks completeness and
placeholder parity (CI-able exit code).

Mechanism for other areas (no edits to this area needed):

- Jinja (registered on the shared `templates` env): `{{ t('key', n=3) }}`,
  `{{ t_nav(path, label) }}` (used by `_nav.html`: the area's own Polish label
  is the source; `nav.<path>` keys translate it), `<html {{ ui_html_attrs() }}>`,
  `ui_lang()`, `ui_shell_config()`.
- JS: `import {loadTranslator} from '/static/workbench-i18n.mjs'` →
  `const t = await loadTranslator(lang)`; `negotiate`, `pluralCategory`,
  `createTranslator` for tests.
- Add keys with an area prefix (`timeline.*`, `catalog.*`, …) to all three
  catalogs; run `workbench-i18n-check`.

Applied to `_nav.html`, `index.html`, `artifact.html`, `integrations.html`
(proposal #17 of the ecosystem adapters) and the workbench pages. Not
translated: source text, stored data, model output, user notes, field names
in provenance tables, and the citation-workspace list items that
`citations.mjs` also renders (`Cytat #…`, `transkrypt #…`, `model nieznany`,
history status messages) — they stay Polish in every language until
`citations.mjs` adopts the JS helper, so server- and client-rendered rows
never mix languages.

## 10. Proposals to maximize functionality

Goals: catalogue everything across devices/accounts, one common timeline, time
budgets, hypothesis analysis, lawyer/court reports with citations. Invariant
check: originals read-only (R), layers separate (L), no fabricated certainty
(C), local-first (P), explicit opt-in for providers (O).

| # | Proposal | Value | Cost | Risk | Invariant check | Status |
|---|---|---|---|---|---|---|
| 1 | Command palette (`Ctrl/Cmd+K`) | one entry point to pages, actions, recent items, jump, search | small | none | R L C P O: navigation only | implemented |
| 2 | Quick jump by `kind:id`, bare id, SHA-256 or quote-hash prefix | paste an id/hash from a report or packet and land on it | small | ids collide across tables — all candidates listed, labelled | hash prefix ≠ identity proof; read-only | implemented |
| 3 | Recently viewed (per browser, 20 entries, localStorage) | resume work across sessions | tiny | shared browser shows paths | P: never leaves the browser; no server state | implemented |
| 4 | Copy citation as text with inert locator, hashes and caveat | paste into a lawyer's draft without losing the anchor | small | prose may be edited downstream | quote verbatim; caveat labels ASR output | implemented |
| 5 | "What we know / don't know" per artifact | epistemic disclosure next to the evidence | small | could be read as completeness | counts only, `scores: null`, fixed `not_claimed` list | implemented |
| 6 | Duplicate surfacing (same sha256 under other locators) | see every acquisition path of the same bytes | tiny | equal bytes ≠ same context | content vs provenance kept separate | implemented (artifact level); catalogue deep link needs a `sha256` filter contract from area A |
| 7 | Epistemic badge on every search hit + legend | users see which layer a match came from | small | none | L | implemented |
| 8 | Layer facets + "search all layers" from the start page | discover annotations/OCR hits | small | facets are lower bounds above 5,000 hits | flagged `facets_complete` | implemented |
| 9 | Index check / rebuild CLI | trust the derived index | small | rebuild cost on large DBs | touches workbench tables only | implemented |
| 10 | Folder item status (unchanged/changed/missing) | notice that an annotation was edited after selection | small | none | identity recomputed, never repaired | implemented |
| 11 | Same-origin write guard on workbench routes | CSRF hardening on a LAN | tiny | none | — | implemented here; proposed for `main.py` forms |
| 12 | Saved-search "new since last run" delta | monitor material as imports grow | small (counts log exists) | counts ≠ same hits | counts only | not yet |
| 13 | Bulk "add selected hits to folder" | build a lawyer folder straight from search | small | none | references only | not yet |
| 14 | Folder → multi-citation package / report input | one click from selection to report (area F) | medium | scope creep into reports | F owns manifests | contract offered (folder JSON export) |
| 15 | Folder-scoped read tokens | share one case folder with ChatADHD/partner | medium | snippet leakage if filtering incomplete | G's #12, explicit scope | not yet |
| 16 | Bookmarks drawn on the swimlanes; bookmark from a selection | annotate the common timeline in place | small for C | none | human annotation layer | contract done; rendering by C |
| 17 | Undo a bulk tag operation from its audit row | safe experimentation | small | state may have changed since | verify current state before reversing | not yet |
| 18 | Corpus-wide "unknowns" dashboard (legacy runs, zone-less times, untimed events, unclassified kinds) | prioritise what to clarify before a report | small (SQL counts) | none | C: counts, not judgments | not yet |
| 19 | Conflict annotation template with two locators | record contradictions as A11 requires | medium (schema) | template could imply resolution | keeps both locators, no auto-resolution | not yet |
| 20 | Result keyboard navigation (`j`/`k`, `x` to select) | faster bulk tagging | small | key conflicts | registry detects them | not yet |
| 21 | Semantic search as a separate measured layer (A08) | recall across PL/NL/EN paraphrases | large | model download (HF blocked here), fabricated similarity | O: local model opt-in, adapter `unavailable`; separate run metadata | not yet |
| 22 | Adopt tokens in `timeline.css`/catalogue CSS; `citations.mjs` on the JS i18n helper | light theme and NL/EN everywhere | small per area | regressions in owners' tests | mapping table above | owners |

## Conflicts with the prompt and decisions

- "FTS po wszystkich warstwach" is implemented as a new index and endpoint;
  `/api/search` keeps its contract (no annotations there).
- Prompt lists "notatki": notes already exist as `annotations`; folder item,
  folder and bookmark notes are workbench working notes (human annotation
  layer) in workbench tables. The metadata package carries them as declared
  extension tables (`packages.EXTENSION_TABLES`); the search index stays excluded.
- ł/Ł folding exists only in the index; a column filter keeps exact
  semantics. No other normalisation is applied.
- The theme change touches the dark look once (button contrast, above).
- `test_search_rendering.py` asserted "no `<script>` on the start page"; the
  shared shell adds two script tags (module from `/static/`, inert JSON
  config). The assertion now allows exactly those and still forbids any
  other script/img/svg/mark and `on*` attributes.

## Shared edits

`server/app/templates/_nav.html` (translated labels, tools, shell scripts),
`server/app/templates/integrations.html` (i18n; loop variable renamed from
`t` to `tk` to avoid shadowing `t()`), `server/tests/test_search_rendering.py`
(assertion above), `docs/README.md` and `docs/validation/README.md` (one
line each).

## Boundaries

- No authentication beyond the existing trusted-network model; the
  same-origin guard is not authentication.
- Index triggers add work to every evidence insert (one upsert + FTS insert);
  no large-corpus benchmark was run.
- Folder status detects changes only in the identity fields listed above.
- Bookmark windows compare UTC instants; zone-less bookmarks stay outside
  windows. No DST assumption is made.
- Facets above 5,000 matches are lower bounds. Snippets are plain text.
- Keyboard handling was exercised in Chromium only; no screen-reader audit.

# Reports and export

Task `AGEDS-TASK-20261016-F` (Etap 6). Verification:
[receipt](validation/REPORTS_2026-10-16.md).

A report is a computed document rendered from generic templates. HTML is the
primary format. The same computed values are written as JSON, tables as CSV and
XLSX, and optionally PDF printed from the HTML. Every report has a method
section (layer 1 of the standard, *minimum metadata*) and a manifest with the
SHA-256 and size of every file. The manifest is **not signed**; a digest detects
change against a known value. It does not authenticate the producer and does
not prove authorship, truth or custody.

## Use

```bash
python -m server.app.cli report-types
python -m server.app.cli report-create --type timeline --params '{"from":"2026-03-01","to":"2026-04-01","counterparty":"Urząd"}' --output /private/reports
python -m server.app.cli report-list [--type timeline] [--limit N]
python -m server.app.cli report-verify /private/reports/ageds-report-…/manifest.json [--expected-manifest-sha256 HEX]
python -m server.app.cli citation-bundle-export NEW_DIR (--report ID | --citation ARTIFACT:ANCHOR ...)
python -m server.app.cli citation-bundle-inspect NEW_DIR
python server/app/reports/bundle_consumer.py NEW_DIR      # standalone, standard library only
```

`report-verify` and `citation-bundle-inspect` read only the given files. They
import no database or configuration and do not access any source. Exit codes
follow `metadata-verify`: `0` valid, `1` invalid, `2` unreadable or refused.
`report-create` prints one JSON object. Incomplete sections are also
announced on stderr.

HTTP API:

| Route | Purpose |
|---|---|
| `GET /api/reports/types` | Types, sections, parameter defaults. |
| `GET /api/reports?type=&limit=` | Register (newest first). |
| `POST /api/reports` `{"type", "params", "output_subdir"}` | Create a report below `EW_REPORTS_DIR` (201). |
| `GET /api/reports/{id}` and `GET /api/reports/{id}/verify` | Register row and current verification. |
| `GET /reports`, `GET /reports/new?from=&to=&type=&counterparty=&topic=` | Page with the register and one creation form per type. The timeline view links to `/reports/new` with its range. |
| `GET /reports/{id}` | Verification, sections, parameters, files, and a sandboxed preview frame. |
| `GET /reports/{id}/files/{path}`, `GET /reports/{id}/view/{path}`, `GET /reports/{id}/manifest` | Download, inline view for the preview, and the manifest. |
| `GET /api/read/v1/reports` | Token-protected read contract: the register without private output paths ([read API](READ_API.md)). |

HTTP creation writes only below the operator-configured private directory
`EW_REPORTS_DIR` and an optional relative `output_subdir`. Without it, HTTP
creation is disabled (403) and the page explains why; the CLI remains
available. Files are served only for registered reports and only when listed
in a manifest whose bytes still match the register digest. The file's own
SHA-256 must also match; otherwise the server answers 409. The preview and the
inline view carry `Content-Security-Policy: sandbox; default-src 'none'; …` and
`X-Content-Type-Options: nosniff`. The preview `<iframe>` has an empty
`sandbox` attribute, so it runs no scripts and has an opaque origin.

## Report types

Templates are generic: `templates/reports/base.html` plus one short file per
type with its reading note. No case data lives in the repository.

| Type | Sections (default order; `params.sections` can choose others) |
|---|---|
| `catalog` | `catalog` (area A: collections, types, ranges and gaps per time basis, content groups, name collisions, issue codes), `sources` (artifacts, acquisitions, content groups by SHA-256; this is also the fallback without A), `chat_records` (exports and the cross-export dedup report), `media` (runs with recipes, text versions, jobs), `integrations` (proposals, quarantined hypotheses). |
| `timeline` | `timeline` (area C's projection: display rule id/version/SHA-256, projection build, placement by basis and precision, flags, transcript segments in the range, correlation hypotheses with their review status — hypotheses, never facts, no probability — and the chronology with quotes), `communications` (counts by type, direction, counterparty, chatbot; untimed by reason), `citations` (anchors on the selected material or `anchor_ids`), `annotations`. |
| `time_budget` | `time_budget` (area E1: a stored budget run given by `budget_run_id` — run identity and result-hash check, window, time source, category totals and candles with lower/probable/upper, wick and body, the `nieobserwowane` band, sensitivity ranges and flips, category definitions with rule provenance but no rule values, exclusions, parameters; without an id or for a missing run `unavailable`). Prints the hashed interpretive disclaimer and the label "unobserved is not inactivity". |
| `hypothesis_dossier` | `hypothesis_dossier` (area E2, `hypothesis_id`: registered versions with the hash chain and integrity check, operationalization, dossier items with human classification and source check, theses with typology and computed flags such as `unsupported`, event-study runs, windows and warnings, exclusions, disclosure quadrants; without an id `unavailable`). Prints the hashed disclaimer label (E2's own, re-hashed), the label "co-occurrence is not causation", and `citations` (exactly the anchors pinned in the dossier, also bundled). |
| `lawyer_package` | `counterparties` (grouped by counterparty, e-mail domain or collection, with a chronology per group), `citations` (key quotes: `anchor_ids` or all), `documents` (artifacts in scope with SHA-256, size, source, acquisitions), `annotations`, `open_questions` (typed by a person + gaps counted by code). |

Grouping is literal. Two spellings of one institution stay two groups; merging
them is a human decision. Counterparty and topic filters are case-folded
substrings, not semantic matching.

### Parameters (defaults are presets)

`from`, `to` (ISO with an offset or `Z`; a bare date means 00:00 UTC; a time
without an offset is refused), `language` (`pl`/`en` labels), `assume_zone`
(IANA; recorded on every affected item) with the mandatory `assumption_rule`
(a named assumption, as on the timeline; one without the other is refused), `event_types`, `counterparty`, `topic`,
`include_hidden_chat_parts` (false), `max_rows` (2000 rows per table),
`max_events` (200000 scanned), `quote_chars` (1000), `per_group_rows` (200),
`formats` (`html`, `json`, `csv`, `xlsx`; add `pdf`), `include_citation_bundle`
(true), `anchor_ids`, `open_questions`, `group_by`, `collection_id`,
`min_gap_days` (30), `budget_run_id`, `hypothesis_id`, `workbench_base_url`
(adds `…/artifact/{id}` links), `title` (human text), `sections`.

Unknown parameters are refused rather than ignored. The full parameter object is
recorded with its SHA-256. Generating again with changed parameters creates a
new report and a new directory; the old row and files stay. The `reports`
table refuses `UPDATE` and `DELETE` by trigger.

## Numbers come from code

- Providers read the database and return computed values. Templates receive only
  the computed document (`report.json`). They do not compute or format numbers
  themselves.
- The template emits every dynamic value as `<span data-k="/json/pointer">`.
  The tests parse each report's HTML, resolve every pointer in `report.json` and
  compare the text. They also require that no numeral appears outside these
  spans. Static template text has no digits, and table-of-contents entries are
  unnumbered.
- Prose is a code template with `{slot}` placeholders resolved from computed
  values (`blocks.prose`). A prose template whose author is `model` is refused
  if it contains any numeric character (Unicode `isnumeric`) or an undeclared
  slot. **No model-written prose exists in this revision** (`model_prose:
  none`); the check guards future adapters. `human` prose, such as typed open
  questions, may contain digits and is labelled as human input.

## Method section (layer 1)

`report.json` → `method` and its rendering list: AGEDS version (FastAPI app
version), git commit or `unknown`, and whether the tree had uncommitted changes.
They also list: generation time (UTC); report type; template id, version and
SHA-256 of each template file; parameters and their SHA-256; renderer versions;
each section's provider, version, status, reason, snapshot and time-placement
rule; model runs (tool, provider, model, versions, parameter hash, recipe, or
`unknown`); unknown fields; requested range and observed range per section and
time basis; exclusions and limits (untimed by reason, unknown zone, hidden chat
parts, filtered rows, scan or row limits, catalog unsupported, damaged or
encrypted entries, incomplete chat parses, packets not exported); the hashes of
every cited artifact, text version and anchor; and the input rows as
`{table: [{id, row_sha256}]}` with a per-table list digest. `row_sha256` is the
SHA-256 of the `SELECT *` row in the same canonical JSON as the metadata
package, so inputs can be checked against a metadata export of the same rows.

The **report hash** (`integrity.content_sha256`) is the SHA-256 of the
canonical document without its `integrity` field (domain `ageds.report/v1\n`).
It is shown in the header and the method section. A file cannot contain its
own hash, so file hashes are in the manifest.

Sections fed by one read-only transaction say `snapshot:
report_read_transaction`. The timeline projection (`timeline_items`) is a
rebuildable **derived cache**. Before the snapshot, the service refreshes it in
its own short write transaction and then requires it to be fresh inside the
snapshot. If new data arrived in between, the snapshot is retried up to three
times and then refused (`ProjectionNotFresh`). `method.derived_caches` (and the
method table) records the rule id, version and SHA-256, the projection parameters,
the projection and build id, the build mode and time, and whether this report
triggered the build. That refresh and the register/audit rows are the only
writes a report causes. Evidence and analysis tables are not written (tested by
digesting every other table before and after). Upstream functions that open their own connection
(the catalog coverage, the chat dedup report) say
`separate_read_transaction`. Citation packets are each read in their own
transaction, as the single-packet export does.

## Output location and files

The output is a new directory `ageds-report-<type>-<UTC>-<uid12>` below the
chosen directory. Its mode is 0700 and files are 0600. The path is refused when
it resolves (including through symlinks) into the repository checkout, the
content store, or a configured scan root (`EW_SCAN_ROOTS`). The name is reserved
with an exclusive `mkdir`. Files are written into a staging directory and
renamed onto the still-empty reservation. A failure removes the staging files
and the reservation, and nothing existing is overwritten. Then the register row
(`reports`, schema version 60) and one `audit_log` row are written.

| File | Content |
|---|---|
| `report.html` | Primary. Self-contained: inline CSS, no script, no external asset, CSP `default-src 'none'`. Source text, including HTML from sources, is escaped data. |
| `report.json` | The computed document (sections, method, labels, integrity). |
| `tables/NNN-<section>__<block>.csv` | One per table. Header is the column keys. Text cells starting with `= + - @` TAB CR get an apostrophe (guard `apostrophe_prefix_v1`, recorded in the manifest with the count). |
| `report.xlsx` | One sheet per table plus `index`. String cells are forced to the string type, so `=…` never becomes a formula. Characters XLSX cannot store are written as `\xNN` and counted. Needs openpyxl; otherwise the format is `unavailable`. |
| `report.pdf` | Optional. `chromium --headless --print-to-pdf` of `report.html` when `EW_PDF_CHROMIUM` names an executable. Otherwise the format is `unavailable` with the reason. A failed print is `failed`. |
| `citations/` | Citation bundle (below). |
| `manifest.json` | Canonical JSON: every other file with SHA-256, size, media type and role; format statuses; inputs; method; bundle reference; limitations; payload digest. |

Exact values are always in `report.json`, the HTML and XLSX. Only the CSV text
is guarded.

### Verification

`report-verify` (also `GET /api/reports/{id}/verify` and the detail page)
checks the following:

- the manifest bytes against the registered SHA-256, when given;
- strict JSON, schema, `signed: false`, and the manifest payload digest;
- each listed file's path safety, size and SHA-256;
- files present but not listed (`unlisted_file`);
- the `report.json` content digest and identity, and that its method equals the
  manifest's;
- the citation bundle with the independent consumer.

It reports `digest_valid`, `files_valid`, `content_valid` and `bundle_valid`,
and always the warning `unsigned_manifest`.

## Multi-citation bundles

`citations/citation-bundle.json` (`ageds.citation-bundle/v1`) lists one
`ageds.citation-evidence/v1` packet per pinned citation. Each packet is
byte-identical to `citation-export` for the same pair. The bundle lists each
packet's file SHA-256, size, payload digest, quote hash and ids. A citation
that cannot form a packet is listed under `excluded` with its reason, and the
report's packet link is then empty. The inspector (`bundle_consumer.py`) uses
only the standard library and the standalone `exchange_consumer.py`. It checks
the bundle digest, every packet file, and each packet with the single-packet
consumer, and it rejects unlisted files.

## Provider contract (for integration)

`server/app/reports/providers/` has one module per section type, a registry
and the documented dict contract `ageds.report-section/v1` (see the module
docstring). Each section returns `status` (`ok`, `partial`, `empty`,
`unavailable`, `error`), `reason`, blocks, inputs, model runs, cited hashes,
citations, unknowns, exclusions, data range, snapshot and rule. A missing
upstream module is an explicit `unavailable` section, never an empty success.
An exception in another area's code is an `error` section with the message.

Upstream functions are looked up lazily:

| Section | Upstream (first importable wins) | Expected call |
|---|---|---|
| `catalog` | `server.app.catalog.coverage.coverage_report`, `server.app.catalog.store.list_groups`/`list_collisions` (wired) | `coverage_report(collection_id, min_gap_days=)` |
| `timeline` | `server.app.timeline.report.report_section` (wired) | `(db, *, from_utc, to_utc, event_ids, artifact_ids, assume_zone, assumption_rule, include_segments, topic, limit, max_scan) -> dict`: rule, projection, placement, flags, segments, correlations, inputs; reads the cache without refreshing it |
| `time_budget` | `server.app.analysis.budget_report.report_payload` (wired) | `(db, budget_run_id) -> dict`: the stored `analysis_runs` row flattened into rows, result hash rechecked; `LookupError` for a missing run → `unavailable` |
| `hypothesis_dossier` | `server.app.analysis.dossier_report.report_payload` (wired) | `(db, hypothesis_id) -> dict`: versions, dossier, theses, runs, windows, warnings, exclusions, disclosure, anchors, inputs |

The disclaimer is taken from `server.app.analysis.hypotheses.disclaimer()` when
present, after checking its declared SHA-256 against the text. Otherwise the
same wording is used from `reports/disclaimers.py`, marked
`reports_fallback`. An upstream dict is rendered by a generic adapter: scalars
become key/value rows, lists of objects become tables, and nested objects are
bounded. Values are not changed. An integrator can also wire a callable with
`providers.register(section, fn)`.

## Extension tables in the metadata package

`export_metadata_package()` (CLI `metadata-export`, `/export/manifest.json`)
adds a versioned section `extension_tables`
(`ageds.metadata-extension-tables/v1`). It has the declared list
(`packages.EXTENSION_TABLES`), rows in primary-key order, and per-table
SHA-256. It lists excluded tables and columns with their reasons:
`api_tokens` (key, salt and secret digest) and
`annotation_proposals.token_id`. It also lists missing declared tables and
**undeclared** tables (named, never exported). Credential-like column names
refuse export. The core `tables`/`integrity.table_sha256` format, the schema id
and core validation are unchanged. The payload digest covers the new section.
Packages without the section validate exactly as before. Validation checks the
section's structure, the digests, the absence of excluded tables and columns,
and missing-table consistency. It does not cross-check extension rows against
core rows; their semantics belong to the owning area. The inert archive keeps
the section byte-exactly in its envelope. A test fails when a migrated table is
neither declared, excluded nor internal (`sqlite_*`, `search_fts*`). This
tripwire makes every new table an explicit decision at integration.

Including extension rows makes metadata packages larger (for example
`event_times` and `logical_occurrences` after chat imports). The existing size,
row and node limits of `metadata-export` and the inert archive still apply and
are flags. `export_metadata_package(include_extension_tables=False)` produces
the previous core-only package.

`token_key` is a public token identifier and also appears in the core
`audit_log` (area G's audit rows). Salts, secret digests and token secrets never
appear in the package.

## Decisions, conflicts and boundaries

| Decision (reversible) | Reason / trigger to change |
|---|---|
| The manifest is unsigned; the register keeps its SHA-256. | The prompt mentions a "signed manifest"; the repository declares no signature (invariant wins). Add a signature only with a key-custody design. |
| A new `report-verify` command in the `metadata-verify` style. | The prompt says "the existing CLI"; no existing command understands report files. |
| CSV text is guarded and the original value is kept elsewhere. | Coordinator rule shared with the catalog area; XLSX/JSON/HTML stay exact. |
| HTTP creation only below `EW_REPORTS_DIR`. | Same pattern as `EW_SCAN_ROOTS`: the browser never chooses arbitrary server paths. |
| The chronology uses the analysis source seam, which delegates to C's projection. | Integration 2026-10-16: the same projection and snapshot feed C's `report_section`; the interim rule remains only for a checkout without the timeline package. |
| A report refreshes the derived timeline cache before its read-only snapshot. | A stale cache must neither be written from a read-only connection nor read silently; reconsider if the cache becomes independently versioned per snapshot. |
| `formats` must contain `html` and `json`. | HTML is primary and JSON is the machine record that HTML values point into. |

Not done or not verified: the C, E1 and E2 sections are wired (integration
receipt [INTEGRATION_2026-10-16](validation/INTEGRATION_2026-10-16.md)), but a
time-budget report renders one stored run and does not start a run; this revision has no model-written prose
and no LLM call; English labels fall back to keys where untranslated; the PDF
layout of very wide tables is not tuned; the report directory is not
re-verified automatically after creation, so verification runs on request.
Without `EW_REQUIRE_TOKEN` the `/reports/…` HTML and file routes follow the
workbench trust model (trusted network). With `EW_REQUIRE_TOKEN=1` they need the
token like `/api/reports*` (review 2 finding 6: `/reports/{id}/files/*`, `/view/*`
and `/manifest` serve report data); browsers use the session cookie.

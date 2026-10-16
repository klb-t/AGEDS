# Source catalog (Katalog)

Task `AGEDS-TASK-20261016-A`, stage 1 of the
[Evidence Timeline plan](plans/EVIDENCE_TIMELINE_2026-10-16.md). Validation:
[receipt](validation/CATALOG_2026-10-16.json) and [report](validation/CATALOG_2026-10-16.md).

The catalog answers "what material do we have, from which device/account/export,
of which real type, where are the same bytes, and where are the gaps" for
directories and archives from many phones, accounts and drives, without touching
the originals.

## Concepts and boundaries

| Concept | Meaning | Not |
|---|---|---|
| Collection | Private, user-described batch (label, device/account label, export date as written, acquisition method, operator, notes, priority, parent). | An identity claim about a person or device. |
| Catalog scan | One read-only walk of a root (directory or single archive file) into a **new** scan with new entries. | An acquisition: no bytes are copied and no `source_observations` row is written. |
| Catalog entry | Scan observation of one file or archive member at a virtual locator: size, SHA-256 (or why not), signature type, extension claim, header times, status, issues. Immutable once the scan has finished. | A parser result or interpretation. |
| Content group | All observations of identical bytes, keyed by (case, SHA-256), across scans, collections, archive depths and existing `artifacts`. | Proof of authorship, truth or time of creation. |
| Ingest from catalog | Explicit acquisition of selected entries through `ingest_file(..., collection_id, source_locator)`; the entry links to the artifact. | Part of scanning. |

**Conflict with the generated prompt, resolved by the repo invariant.** The prompt
says "every file gets a `source_observation` with its collection". `AGENTS.md`
separates scan observations from acquisition observations (`source_observations`
describe preserved bytes). The catalog therefore stores scan observations in its
own tables; `source_observations` rows (with `collection_id`) appear only when an
entry is ingested. The prompt's "identical bytes → one artifact" is likewise kept as
a content-group projection above `artifacts` (unique per `(sha256, source_locator)`).

## Data model (schema versions 11–13, `server/app/schema/catalog.py`)

- v11 `catalog_scans`, `catalog_entries`, `catalog_entry_times`, `catalog_issues`,
  `catalog_content_groups`. Triggers: scans/entries are never deleted; a finished
  scan and its entries cannot be updated (updates are allowed only while the scan
  is `running`); groups, times and issues are append-only.
- v12 `catalog_entry_artifacts` (ingest links) and `catalog_marks` (priority/note
  per `(collection, locator)`, append-only history, survives re-scans).
- v13 `catalog_profiles`, `catalog_description_runs`, `catalog_annotations`,
  `catalog_annotation_decisions` (all append-only).
- v10 `collections` is unchanged. Collection edits update the row and write an
  `audit_log` entry with `previous` and `new` values; parent cycles are refused.

"Current" scans: per collection and root path, the newest scan whose status is
`completed` or `partial`. Lists, groups, collisions and coverage use current scans
unless a scan id is given. A scan left `running` by a killed process is never
current and stays visible as `running`.

## Virtual locators

```
locator     := disk_path ( "!/" member_name [ "!" N ] )*
examples    := Takeout/outer.zip!/inner/dir/file.txt
               a.zip!/b.zip!/c.txt
               archives/evil.zip!/dup.txt!2        (second member with the same raw name)
```

Each name is escaped byte-wise from its **raw bytes**: `%` → `%25`, every literal
`!` → `%21` (so `!/` inside a name, e.g. `odd!/name.txt` → `odd%21/name.txt`, can
never look like an archive boundary), bytes that are not valid UTF-8 and C0/C1
controls, DEL and invisible format characters (bidi overrides) → `%XX` per byte.
Everything else is literal, including `/`, `\`, `..` and a leading `/`
(`evil.zip!//abs/path.txt`). `locator.parse_locator()` inverts the syntax.
Locators are inert identifiers and are never used as filesystem paths.

## Type recognition

The signature table is data: [`server/app/catalog/data/signatures.json`](../server/app/catalog/data/signatures.json)
(`schema: ageds.catalog-signatures/v1`, `version`). Each scan records the table
version. Rules are evaluated in order; structural refinements cover ISO-BMFF
brands (mp4/m4a/m4b/m4v/mov/3gp/3g2/heic/heif/avif), Ogg codec header (opus/vorbis),
EBML doctype (webm/mkv), ID3 payload (mp3/aac), ZIP members (docx/xlsx/pptx by
`[Content_Types].xml` + part prefix; odt/ods/odp/epub by the `mimetype` member;
apk) and the OLE compound-file directory (xls/doc/ppt/msg, bounded FAT/directory
read). Also: wav/RF64, webp, avi, flac, amr, jpeg, png, gif, tiff, pdf (also after
leading junk), gzip/bzip2/xz/7z/rar/tar, sqlite. MPEG frame sync is reported only
for Layer III (Layer I/II patterns occur in other data, e.g. the UTF-16 BOM).

Text classes are **heuristics** (`sniff_basis: heuristic`): JSON (BOM/whitespace
allowed; validated only when the whole content fits the prefix), JSON Lines, HTML,
XML/SVG, mbox (`From ` line followed by headers), EML (RFC 822 header block with
at least two mail headers), vCard, iCalendar, CSV/TSV (consistent delimiter count)
and plain text.

Name vs bytes (`type_relation`, `type_conflict`): `match`; `variant` (same
container family, e.g. `.m4a` holding an `isom` MP4 — flagged); `mismatch`
(`.jpg` holding PNG — flagged); `opaque_extension` (`.dat`, `.bin`, … holding a
recognized type — flagged, e.g. ChatGPT export `.dat` that is a PDF);
`unregistered_extension` (flagged only for byte signatures); `text_variant`
(heuristic text classes, not flagged); `unrecognized_content`; `no_extension`.
Both claims are stored; nothing is renamed or corrected.

## Archives

| Format | Adapter | Notes |
|---|---|---|
| ZIP (incl. nested, zip64) | `zipfile` | Raw names from the central directory (`orig_filename`); flag bit 11 → UTF-8, otherwise cp437 per spec; unflagged valid UTF-8 is displayed as UTF-8 with the cp437 alternative kept. DOS time, extended timestamp (0x5455) and NTFS (0x000A) times. Encrypted members (bit 0) are listed, never read. Unsupported methods → `unsupported`. CRC failure → `damaged`. |
| ZIP with unreadable central directory | local-header walk | Container `damaged`; members recovered from local headers (stored/deflate, CRC checked); a cut member is `damaged` (`member_data_truncated`). |
| TAR, TAR.GZ/TGZ, TAR.BZ2, TAR.XZ | `tarfile` | `encoding=utf-8, errors=surrogateescape`; symlink/hardlink members recorded with their link text, never followed; a missing end-of-archive marker marks the container `damaged`. |
| gzip/bzip2/xz that is not a tar | single stream | One member; gzip `FNAME` and `MTIME` are recorded. |
| 7z, RAR | `py7zr`, `rarfile` (optional) | Not installed in this session → entry `unsupported`, issue `adapter_unavailable` (container bytes are still hashed). The adapter code paths are **not run**. |

Document containers (docx/xlsx/pptx/odt/ods/odp/epub/apk) are recognized by their
members and not expanded unless `expand_document_containers` is set. Nested
archives are spooled to memory or an unnamed temporary file in a directory that
must be outside the source root (checked before anything is created). The
database and content store must also be outside the scanned root.

## Limits

All limits are explicit (`server/app/catalog/limits.py`, CLI `--max-…`), defaults
are presets. A hit produces a visible `limit_exceeded` entry and/or a scan issue,
and the scan status becomes `partial` with `coverage.incomplete_reasons`.

| Limit | Default | Effect when hit |
|---|---|---|
| `max_files`, `max_directory_entries`, `max_total_entries`, `max_directory_depth` | 200 000, 100 000, 1 000 000, 64 | scanner walk issue (`file_limit`, …) |
| `max_hash_bytes`, `max_total_hash_bytes` | 8 GiB, 1 TiB | disk file not hashed (`skipped_size_limit`) |
| `max_archive_depth` | 8 | container at that depth hashed but not expanded |
| `max_members_per_archive`, `max_total_members` | 100 000, 1 000 000 | container `limit_exceeded`, members after the limit not listed |
| `max_expanded_bytes` | 256 GiB | members not read; compressed tar listing stops |
| `max_compression_ratio`, `ratio_check_min_bytes` | 200, 1 MiB | declared or observed ratio → member not hashed (`compression_ratio[_declared]`) |
| `max_member_hash_bytes`, `max_nested_archive_bytes` | 4 GiB, 8 GiB | member not hashed / nested archive not expanded |
| `max_zip_directory_bytes` | 64 MiB | ZIP not opened (central directory too large) |
| `spool_memory_bytes`, `sniff_bytes` | 32 MiB, 64 KiB | memory before spilling to the temp file; signature prefix |

Encrypted content, damaged/unsupported members, containers not expanded and
unstable/unreadable files are also listed as incompleteness reasons.

## Time

Every time is a `timeinfo.TimeCandidate` stored in `catalog_entry_times`:

| Source | Kind / zone | Note |
|---|---|---|
| `filesystem_stat` | `file_mtime`, `utc_epoch` | filesystem modification, not an event time |
| `zip_dos_datetime` (`_local_header` when recovered) | `file_mtime`, **`unknown` zone**, `local` wall clock | 2-second granularity; `1980-01-01 00:00:00` flagged as placeholder |
| `zip_extended_timestamp`, `zip_ntfs_timestamp` | `utc_epoch` | when present |
| `tar_header`, `gzip_header` | `utc_epoch` | tar mtime 0 flagged as placeholder |

## Duplicates and collisions

Content groups cover catalog entries at any depth in any scan/collection plus
`artifacts` with the same SHA-256; the group view lists every catalog observation
(collection, locator, scan, root), the artifacts and their acquisition
observations. Groups whose observations are all in non-current scans do not count
as duplicates, so re-scanning never double counts. Empty files form one flagged
group (`empty_content`), hidden from the duplicate list unless requested. Filename
collisions (same name, different bytes; unhashed entries excluded as unknown) are
a separate list. Logical chat deduplication is track B's job.

## Coverage and gaps

`server.app.catalog.coverage.coverage_report(collection_id=None, *, scan_ids=None,
min_gap_days=30, case_id=None) -> dict` is the plain-data contract for other areas
(reports). It returns, per collection × effective type (signature type, else the
extension's claim, else `unknown`): entries, bytes, statuses, conflicts,
containers, hashed count, `type_from_extension_only`, and per **time basis**
`(kind, source, tz_status)` the first/last value, observed days, excluded
placeholders and gaps. Bases are never merged. A gap is a run of at least
`min_gap_days` calendar days (UTC date for epoch/offset values, local date for
unknown-zone values) without any observation between two observed days. The
result echoes its parameters and a `method` section. A gap means "nothing
catalogued carries a time there on this basis", not inactivity.

## Description: profile → explain → promote

- `build_profile()` — deterministic statistics (no labels or device names, so
  nothing private reaches a provider by default), stored append-only with SHA-256;
  identical profiles are reused.
- `explain(provider=…)` — provider adapters: `none` (default, nothing generated),
  `fake` (deterministic test double). Other adapters must be registered in code; an
  adapter with `external = True` requires `allow_external=True` for that run, and
  the opt-in is recorded. **No real model is called by this increment.** The model
  may only write placeholders such as `{{totals.entries}}`; the pipeline resolves
  them from the profile. Digits written by the model and unresolved placeholders
  are flagged as warnings. A provider exception is a recorded `failed` run.
- Sidecar `<output>/collection-<id>.annotations.ndjson` (append-only, outside every
  source root; HTTP uses `<EW_DATA_DIR>/catalog-annotations/`). Hypothesis line:
  `format: ageds.catalog-annotations/v1`, `format_compat: "unknown"`,
  `record/status: hypothesis`, `annotation_id`, `run_id`, `collection_id`, `target`,
  `provider`, `model`, `model_version`, `params`, `external`, `opt_in`,
  `prompt_template_version`, `prompt_sha256`, `input_profile_sha256`, `profile_id`,
  `scan_ids`, `model_text`, `resolved_text`, `placeholders`, `warnings`,
  `created_at`. Decision line: `record: decision`, `decision` (`promoted` /
  `rejected`), `actor`, `reason`, `decided_at`.
- `decide()` — explicit human decision with a required actor, stored append-only
  with an audit entry; the latest decision is the current status.

UCAF 0.1 / `chatarchive` were not available in this session (see the plan, §2), so
the sidecar is AGEDS's own format and claims no compatibility.

## Ingest from the catalog

`ingest.ingest_entries(ids)` re-reads each entry (disk file, or the chain of
containers by member index and raw name), copies it to a temporary directory
outside the source, verifies the catalog SHA-256 (a changed source is refused and
nothing is ingested), and calls `ingest_file` with
`source_locator = catalog:collection/<id>/<virtual locator>`, the collection and
metadata (entry, scan, container chain). Entries without a verified hash
(`encrypted`, `limit_exceeded`, `damaged`, `skipped`) are reported as
`not_ingestible`. Same bytes at another locator remain a separate artifact row;
repeated ingest adds an acquisition observation, not a new artifact. The ingested
path is a temporary copy, so its own mtime is never recorded: the observation
declares the **catalogued** modification instant (`source_stat='declared'`; the
`filesystem_stat` time of a disk file, else an archive header time with a UTC basis)
with `source_modified_at_basis = catalog_entry_times:<id> (<source>, <tz_status>)`,
or `NULL` when the catalog has no instant (e.g. a zone-less ZIP DOS time) — review
finding 1.

## CLI

```bash
python -m server.app.cli catalog-collection-add "Phone A export 2026-09" --device-or-account phone-a --priority 1
python -m server.app.cli catalog-collection-update 1 --notes "checked" --clear priority
python -m server.app.cli catalog-collection-list
python -m server.app.cli catalog-scan /path/to/source --collection 1 --temp-dir /path/outside --max-archive-depth 4
python -m server.app.cli catalog-scans --collection 1
python -m server.app.cli catalog-entries --collection 1 --conflict --limit 100 [--after-id N]
python -m server.app.cli catalog-coverage --collection 1 --min-gap-days 30
python -m server.app.cli catalog-duplicates [--group-id N] ; catalog-collisions
python -m server.app.cli catalog-export /path/outside/catalog.xlsx --format xlsx [--collection 1] [--max-rows N]
python -m server.app.cli catalog-describe --collection 1 [--provider fake --output-dir /path/outside] [--param k=v]
python -m server.app.cli catalog-promote ANNOTATION_ID --decision promote|reject --actor NAME [--reason TEXT]
python -m server.app.cli catalog-mark 1 'docs/report.pdf' --priority 2 --note "key document"
python -m server.app.cli catalog-ingest ENTRY_ID [ENTRY_ID ...] --temp-dir /path/outside
```

Each command prints one JSON object; errors go to stderr with exit code 2.
Exports never overwrite (exclusive hard-link publication) and are refused inside
any scanned root. Source text is data: XLSX cells are typed as strings (a name
starting with `=` is not a formula), the HTML export is escaped and contains no
script (CSP `default-src 'none'`), control characters in text cells are shown as
`\xNN` (locators and `name_raw_hex` keep exact bytes). CSV keeps raw text values;
open it as text, or prefer XLSX in spreadsheet software.

## HTTP and browser view

`/catalog` (navigation label **Katalog**): collection tree with counts and
priority, collection details with audited notes/priority editing and history,
scans, coverage and gaps (adjustable minimum gap), description hypotheses with
promote/reject, entries table with server-side filters (collection, type, status,
type conflict, duplicate group, text) and keyset pagination, per-entry
priority/note marks, duplicate groups, collisions and export links. Server-rendered
with Jinja autoescaping plus a small module (`static/catalog.mjs`).

API (`server/app/api/catalog.py`): `GET/POST /api/catalog/collections`,
`GET/PATCH /api/catalog/collections/{id}`, `POST /api/catalog/scans` (only under
`EW_SCAN_ROOTS`, like `/api/source-scans`; synchronous), `GET /api/catalog/scans`,
`GET /api/catalog/entries` (≤ 500 per page, `afterId`), `GET /api/catalog/entries/{id}`,
`POST /api/catalog/marks`, `GET /api/catalog/groups[/{id}]`, `GET /api/catalog/collisions`,
`GET /api/catalog/coverage`, `GET /api/catalog/export?format=csv|xlsx|html`
(`X-AGEDS-Rows`, `X-AGEDS-Truncated`), `POST /api/catalog/collections/{id}/explain`,
`GET /api/catalog/annotations`, `POST /api/catalog/annotations/{id}/decision`,
`POST /api/catalog/ingest`. There is no API authentication (see HANDOFF): use a
trusted/private network.

## Decisions (default; reversal condition)

| Decision | Reverse when |
|---|---|
| Reuse `scanner._Scan.walk` by subclassing and replace only the per-file step (one read for hash + sniff). | The scanner exposes a public per-file hook; then switch to it (no behavior change expected). |
| Content groups per case; current-scan view for counts. | Cross-case comparison is requested explicitly. |
| Document containers not expanded by default. | A user needs their embedded media catalogued (`--expand-document-containers`). |
| Display unflagged valid UTF-8 ZIP names as UTF-8 (cp437 kept as alternative). | A corpus shows real cp437 names that happen to be valid UTF-8. |
| HTTP scans are synchronous. | Real roots make requests time out; then move scans to the job queue. |
| Each catalog write (entry with its times, issue, entry update) is its own short transaction on a `synchronous=NORMAL` WAL connection; no write lock is held while content is read or hashed (review finding 4; measured: 5,000 small files 1.1 s → 2.2 s). Entries of a running scan become visible as they are written; they stay immutable once the scan finishes. | Commit cost dominates on real roots; then buffer rows read-ahead and write them in short batches after reading. |
| CSV keeps raw text (no formula guard prefix). | Users open CSV directly in spreadsheet software and need the guard; XLSX is the safe default meanwhile. |

## Not done / not verified

- 7z/RAR adapters are written but not executed (libraries absent); RAR member
  extraction also needs an external `unrar` tool.
- No real LLM provider exists; only `none` and `fake` were run.
- No Android changes; no timing on large real roots (all tests use small
  synthetic data); no concurrency test of two simultaneous scans.
- HTTP scan endpoint blocks for the duration of the scan.

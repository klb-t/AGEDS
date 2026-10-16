# Communication, Takeout and media importers

Task `AGEDS-TASK-20261016-B` part 2 (Etap 2.1). Parallel part 1 covers ChatGPT/Claude
chat exports. This page describes what each importer keeps, what it adds, what it
leaves out and how to run it. Acceptance evidence: [receipt](validation/IMPORT_COMM_2026-10-16.md).

## Common contract

| Rule | Implementation |
|---|---|
| Acquisition | Every input file goes through `evidence.ingest_file` (optional `collection_id`). Extracted children (email attachments/inline parts, WhatsApp media, a chat `.txt` inside a zip) are acquired with `parent_artifact_id`. Identical bytes share one stored copy and one SHA-256 while each provenance keeps its own artifact row and observation. |
| Originals | Read only. Extraction writes to a private system temporary directory, never next to the source. Tests compare SHA-256 of every corpus file before and after all imports. |
| Source times of acquisitions | `ingest_file(..., source_stat=)` says what the path is (review finding 1): `path` (default; the original, its mtime becomes `source_modified_at`), `temporary_copy` (uploads, extracted attachments/media, a chat text taken out of a zip: `source_modified_at` stays `NULL`) or `declared` (the caller supplies the instant and its basis). Upload routes wrap importers in `evidence.temporary_copy_acquisitions()`; `acquire_bytes` always acquires a temporary copy. The observation metadata names the scope (`filesystem_stat_scope`). Existing rows are not rewritten; the timeline ignores older rows with temporary markers (see [timeline](TIMELINE.md)). |
| Raw fields | Raw headers, attributes, cells, content lines or JSON records are kept in event/observation metadata, bounded by `CommLimits` (default 4096 characters per string, 200 items per container); truncation is counted. The preserved original remains the authority. |
| Locators | Byte range + message index (MBOX), component index + physical lines (ICS/VCF), XML element ordinal, table/row/raw record (billing), record index or HTML line (Takeout), chat line (WhatsApp), container path `zip@sha256:<zip hash>!/member` or `mbox@sha256:<mbox hash>!/message/N/mime-part/P`. Since parser version 2 of `email_rfc5322` and `whatsapp_media` (review finding 5) a child locator names the container's content identity, so two exports with the same file name (e.g. two Takeouts' `All mail Including Spam and Trash.mbox`) never share a child locator; identical child bytes get one stored copy and one artifact row per container. A container that a version-1 import already acquired keeps version 1 (`<container>!/…`) on re-import, so re-importing it stays idempotent; existing rows are never rewritten. WhatsApp folder exports keep their filesystem paths. |
| Parents | A child acquired again at the same locator under a **different** parent is no longer refused (409): the artifact keeps its first `parent_artifact_id`, and the new observation records `parent_artifact_id`, `parent_relation: additional_parent_observed` and `artifact_parent_artifact_id` (also in the `ingest` audit row), so both provenances stay visible. |
| Idempotence | Re-importing the same file at the same locator reuses the artifact, events (same `external_id` and payload), time candidates and observation rows; a changed interpretation under the same parser version is a visible `IntegrityError`, never an overwrite. A new acquisition observation is still appended, as for every `ingest_file`. |
| Time | Every timestamp a source states is a `TimeCandidate` in `event_times` with kind, raw text, zone status and precision. `events.ts_start/ts_end` are filled only when the source fixes the UTC instant (offset, declared zone without DST fold, epoch); otherwise they stay empty and the candidate keeps the wall-clock value. The importing machine's zone is never used. |
| Issues and coverage | Each result lists issue codes with exact counts (listing bounded) and `coverage.complete`. Reaching a limit (records, message bytes, attachment size, archive members, walk entries, document size) makes coverage partial. One append-only `audit_log` row (`action='import_run'`) records importer, version, options/opt-ins, counts, coverage and issue counts. |
| Confidence | `events.confidence` stays `NULL`: parsing success is not a probability of truth. |

### Time kinds used

| Source value | Kind | Zone status |
|---|---|---|
| Email `Date` | `header_date` | explicit for numeric/RFC 5322 §4.3 zones; `unknown` when missing or unrecognized |
| Email `Received` (each) | `received` | as above |
| MBOX separator line | `other` (`semantics: mbox_separator_time_writer_defined`) | as above |
| ICS `DTSTART` / `DTEND` | `start` / `end` | `Z` → UTC; `TZID` → declared zone (`explicit_offset`, `zone_declaration` says `vtimezone_in_file` or `iana_tzdata_by_tzid_name`); floating/all-day → `unknown` |
| ICS `DURATION` without `DTEND` | `end` with `projection: dtstart_plus_duration` | as start |
| ICS `CREATED` / `LAST-MODIFIED` / `DTSTAMP` / `RECURRENCE-ID` | `created` / `updated` / `other` / `other` | as above |
| `calls.xml` `date` | `start` | `utc_epoch` |
| `calls.xml` `date` + `duration` | `end` with `projection: start_plus_duration_seconds` | `utc_epoch` |
| Billing date/time | `start` (and `end` projection) | `unknown`; optional profile `assumed_zone` adds a separate `assumed_zone` candidate |
| Recording file name | `filename_derived` | `unknown` |
| Takeout My Activity `time` (JSON) / display time (HTML) | `created` (activity record time) | JSON `Z` → explicit; HTML abbreviation (`CET`) → `unknown`, numeric `GMT+01:00` → explicit |
| Location segments | `start` / `end` | explicit (ISO with `Z`/offset) or `utc_epoch` (ms) |
| Chrome `time_usec` | `created` | `utc_epoch`, microsecond precision |
| EXIF `DateTimeOriginal` / `DateTimeDigitized` / `DateTime` | `exif` | `unknown` unless the matching `OffsetTime*` tag exists |

## Importers

### Email: MBOX and EML (`email_mbox.py`, event `email`)

* Streaming MBOX framing: a `From ` line at file start or after a blank line; a strict
  asctime-shaped `From ` line without a blank line is accepted and flagged. `>From`
  lines are unescaped mboxrd-style (one `>` removed) in the parsed copy only; byte
  offsets locate the escaped original. Messages larger than `max_message_bytes` are
  truncated and flagged.
* Headers are unfolded from raw bytes (UTF-8, else Latin-1 flagged) and kept raw
  (bounded). RFC 2047 encoded words are decoded per word; unknown charsets or
  malformed words stay verbatim and are flagged. Addresses are parsed from the
  undecoded value, so a decoded display name with a comma cannot split an address.
* `external_id` = `Message-ID`. Missing → `content-sha256:<hash of the message bytes>`
  (flagged). A repeated identity in one file gets `#occurrence-N`, never merged.
* Threads: `thread_id` = `gmail-thrid:<X-GM-THRID>` when present, else the
  reconstructed root (`References[0]`, else the `In-Reply-To` chain within the file,
  else the message itself). Metadata keeps the Gmail thread id, header root, method,
  `In-Reply-To` and `References`. Gmail labels: raw `X-Gmail-Labels` plus a split list
  honouring quoted labels.
* Body: first non-attachment `text/plain`; otherwise a text projection of the HTML part
  (`html.parser` text nodes, scripts/styles dropped). HTML parts are kept as bounded
  data with `rendering: never_rendered_as_trusted_markup`. A `multipart/*` without a
  usable boundary keeps its undecoded payload as body view, flagged. MIME defects of
  every part are recorded.
* Attachments and inline parts → child artifacts with filename (RFC 2231/2047 decoded),
  content type, size and SHA-256, plus `event_media_references` rows. `message/rfc822`
  parts are re-serialized (`is_raw=0`, flagged not byte-exact). Parts over
  `max_attachment_bytes` are `not_extracted`.

Date rules (RFC 5322 §3.3/§4.3):

| Zone text | Result |
|---|---|
| `+hhmm` / `-hhmm` | explicit offset (minutes ≥ 60 → `unknown`, flagged) |
| `-0000` | UTC instant known; `tz='-00:00'`, local zone of the sender unknown (§3.3) |
| `UT`, `GMT`, `EST/EDT/CST/CDT/MST/MDT/PST/PDT` | the §4.3 offsets, `obsolete_zone` recorded |
| `Z` | UTC (RFC 822 and the §4.3 "-0000" reading agree) |
| other letters (`A`, `CET`, `CEST`, …) | **not converted**: `unknown`, raw zone kept; the §4.3 "treat as -0000" reading is stored as `rfc5322_4_3_reading` with `adopted: false` (deliberate deviation: adopting it would silently shift e.g. `CET` by an hour) |
| missing zone (asctime, broken clients) | `unknown` |
| two/three-digit years | §4.3 rule (00–49 → 20xx, 50–99 and three digits → +1900), `year_rule` recorded |
| second `60`, impossible dates, unknown month | no candidate; status recorded |

### Calendar: ICS (`ics.py`, event `calendar_event`)

Line unfolding, parameter parsing with quoted values, TEXT unescaping (`\,` `\;` `\n`)
and splitting on unescaped commas (`CATEGORIES`). VTIMEZONE blocks are parsed with
`python-dateutil`'s `tzical` and preferred over the IANA database (RFC 5545 semantics);
when both exist and disagree, the IANA reading is kept as `iana_alternative_utc` and
flagged. DST gaps stay `unknown` (`dst: nonexistent_local_time`); folds keep both
instants (`utc_alternatives`) and leave `ts_start` empty. `RRULE`/`RDATE`/`EXDATE` are
kept raw; **nothing is expanded on import**. `expand_recurrence(event_id, max_instances,
until)` (CLI `expand-calendar-recurrence`, API `GET …/calendar/{id}/occurrences`) returns
a bounded, unsaved projection labelled `ics_recurrence_projection/v2` with the dateutil
version; occurrences are generated in wall-clock time and mapped through the declared
zone, EXDATEs are removed and `RECURRENCE-ID` overrides are marked, not merged. Since v2
(review finding 11) `UNTIL`, `EXDATE` and `RECURRENCE-ID` values with `Z` or their own
`TZID` are converted to DTSTART's wall clock before comparison (RFC 5545 instants); only
floating values are compared as wall-clock time. The `until` query bound may carry an
offset; an unparsable bound is a 422. Overrides are separate
events sharing `thread_id = uid:<UID>`; missing UID → content-derived id.

### Contacts: VCF (`vcf.py`, table `contact_observations`)

vCard 2.1/3.0/4.0: folded lines, 2.1 quoted-printable soft line breaks and `CHARSET`,
bare 2.1 parameters (`TEL;CELL;PREF`), `TYPE=` lists, 4.0 `tel:` URIs, groups. Contacts
are **not events**. Each card keeps its raw lines, `FN`, `N` parts, `ORG`, UID or a
content-derived id; each TEL/EMAIL is a row in `contact_observation_values` with a
syntactic `normalized_candidate` (digits with optional `+`, no country inference;
lower-cased e-mail domain). Matching a contact to a participant is a later, revocable
hypothesis made elsewhere. Rows are append-only.

### Call log: `calls.xml` (`call_log.py`, event `call`)

Streaming `XMLPullParser`; DTD/entity declarations are rejected. Type/presentation codes
map through [`call_log_codes.v1.json`](../server/app/importers/data/call_log_codes.v1.json)
(Android `CallLog.Calls` constants as documented); unmapped codes stay raw and flagged.
`readable_date` is the app's display string in an unknown zone and is kept raw only. The
older `sms_backup` importer also reads `calls.xml`; when both projections exist for one
artifact the result reports `other_parser_projection_exists`.

### Operator billing: CSV/TSV/XLSX/XLS (`billing.py`, events `call`/mapped)

Tables are read by the scanner's bounded readers (`scanner._Scan.parse_table`: dialect
detection, raw CSV records, XLSX formula text without evaluation, XLS cached values).
When no profile matches the sniffed dialect (e.g. a preamble line misled it), the file is
re-read in memory with an explicit separator hint and the hint is recorded as
`delimiter_source`. Column mapping comes from a profile in
[`billing_profiles.v1.json`](../server/app/importers/data/billing_profiles.v1.json) —
**examples only**, not verified against any operator — or a private file (`--profiles`).

* Day/month order: `day_first`, `month_first`, or `infer_from_file` (only unambiguous rows
  count as evidence; conflicting or no evidence → `undetermined`). An ambiguous row under
  an undetermined order records **both** readings as separate candidates
  (`…:day_first`, `…:month_first`). Year-first dates are unambiguous; two-digit years are
  not given a century. Typed spreadsheet dates have no order ambiguity.
* Durations: `seconds`, `mm:ss`, `hh:mm:ss` or `auto`; a value contradicting the profile
  format is read by its shape and flagged; typed time cells are `hh:mm:ss`.
* Services mapped to `null` (e.g. data sessions) are skipped and counted; unmapped values
  give `billing_record`. Numeric number cells are flagged (lost `+`/leading zero).

### Recording names (`recording_names.py`, table `recording_name_observations`, event `recording`)

File names from paths (directory walk without reading or hashing audio), from acquired
artifacts (`--artifact-id`, catalog entries) or from a posted list are matched against
[`recording_name_patterns.v1.json`](../server/app/importers/data/recording_name_patterns.v1.json)
(versioned; named groups `number`, `name`, `direction`, `date`, `time`; **example
patterns**, no claim about any app version). Several differing specific matches →
`ambiguous` with alternatives. A recognised time yields a `recording` event with one
`filename_derived` candidate (zone unknown). Number/name/direction are stored as
`correlation_hypothesis_from_file_name`, never in the event's factual columns. Matching a
recording to a call-log entry is left to the timeline track's revocable correlations.

### Google Takeout My Activity (`takeout_activity.py`, event `activity`)

JSON (`header`, `title`, `titleUrl`, `time`, `products`, `subtitles`, `details`,
`safeHtmlItem` …) and HTML (`outer-cell` records: header, content lines, links, caption
products). Gemini Apps response HTML is kept as data with a text projection in the body.
HTML display times are parsed with localized month names (EN/PL/NL examples in
[`takeout_activity.v1.json`](../server/app/importers/data/takeout_activity.v1.json)), AM/PM and
narrow no-break spaces; zone abbreviations are kept raw (`unknown`). Records of
browsing-like products (Chrome, YouTube, YouTube Music) are skipped and counted unless
`enable_browser_history` is set. `content_key` (hash of header/title/URL/time text) is a
deterministic aid for later logical deduplication; it is not a merge.

### Location history (`location.py`, event `presence`) — opt-in

Refuses to run (and acquires nothing) without `enable_location`
(`--enable-location-history`). `Records.json` points are aggregated into runs of the same
coarse cell; Semantic Location History `placeVisit`/`activitySegment` and on-device
`Timeline.json` `semanticSegments` (visit/activity/path) become one event each.
Coordinates are rounded to `coarse_decimals` (default 2, ≈1 km). Exact coordinates,
addresses, place names and place IDs are **not** copied into metadata; they remain only in
the preserved original. `rawSignals`/`userLocationProfile` are reported as not imported.

### Browser history (`browser_history.py`, event `browser_visit`) — opt-in

Refuses without `enable_browser_history` (`--enable-browser-history`). Chrome
`BrowserHistory.json` (`time_usec`) → visits with microsecond UTC candidates; My Activity
formatted YouTube/Chrome histories are delegated to the My Activity importer with the
same opt-in. The opt-in is recorded on each event and in the run audit.

### WhatsApp media (`whatsapp_media.py`)

Folder or zip export. The chat `.txt` is imported by the unchanged `whatsapp_txt` parser
(version 2). Message body lines are matched against attachment/omission markers in
[`whatsapp_media_patterns.v1.json`](../server/app/importers/data/whatsapp_media_patterns.v1.json)
(Android `(file attached)`, iOS `<attached: …>`, `<Media omitted>`, `image omitted`, EN/NL/PL
examples). Each marker becomes an `event_media_references` row anchored to the message
event with its physical line: `present` (child artifact), `missing_from_export` or
`omitted_by_export`. Parent: the zip artifact, or the chat `.txt` for a folder export
(`parent_relation` recorded). Unreferenced media files are acquired and listed. Several
`.txt` files require `chat_member`.

### EXIF (`exif.py`, event `image_capture`)

Pure-Python, bounded (`max_exif_bytes`, default 1 MiB) reader for JPEG APP1, bare TIFF and
PNG `eXIf`: `DateTimeOriginal`, `DateTimeDigitized`, `DateTime`, `OffsetTime*`,
`SubSecTime*`, `Make`/`Model`/`Software` and GPS **presence** only (coordinates never
decoded). IFD loops, out-of-bounds values and truncated segments are flagged. Works on new
image files or on already acquired artifacts (e.g. WhatsApp media).

## Schema (`server/app/schema/records.py`, versions 25–27)

| Version | Tables |
|---|---|
| 25 | `contact_observations`, `contact_observation_values` (append-only) |
| 26 | `recording_name_observations` (append-only, unique per source/locator/pattern version) |
| 27 | `event_media_references` (append-only; `present` ⇔ artifact set) |

Shared edit: `evidence.ingest_file(..., parent_artifact_id=None)`. The parent must exist
in the same case; `None` means "not stated" and reuses an existing artifact; naming a
different parent than the stored one raises `IntegrityError`. Existing callers are
unchanged.

## CLI and HTTP

```bash
python -m server.app.cli import-email takeout.mbox [--format mbox|eml]
python -m server.app.cli import-ics calendar.ics
python -m server.app.cli expand-calendar-recurrence EVENT_ID --max-instances 50
python -m server.app.cli import-vcf contacts.vcf
python -m server.app.cli import-calls-xml calls.xml
python -m server.app.cli import-billing bill.csv [--profile ID] [--profiles private.json]
python -m server.app.cli observe-recording-names /recordings [--artifact-id N] [--patterns private.json]
python -m server.app.cli import-takeout-activity MyActivity.json [--enable-browser-history]
python -m server.app.cli import-location-history Records.json --enable-location-history [--coarse-decimals 2]
python -m server.app.cli import-browser-history BrowserHistory.json --enable-browser-history
python -m server.app.cli import-whatsapp-export export.zip [--chat-member NAME]
python -m server.app.cli import-exif photo.jpg [--artifact-id N]
```

Common options: `--label`, `--collection-id`, `--source-locator`, `--max-records`. Output
is one JSON object; errors go to stderr with exit code 2.

HTTP (`/api/imports/comm`): `POST /{kind}` upload (kinds `email`, `ics`, `vcf`,
`calls-xml`, `billing`, `takeout-activity`, `location-history`, `browser-history`,
`whatsapp-export` (zip), `exif`; form flags `enable_browser_history`,
`enable_location_history`, `coarse_decimals`, `profile`, `chat_member`); `POST
/{kind}/path` with `{rootId, relativePath, …}` confined to `EW_SCAN_ROOTS` (also
`recording-names` and WhatsApp folders); `POST /recording-names` with `{names: [...]}`;
`GET /calendar/{event_id}/occurrences`, `GET /events/{event_id}/media-references`,
`GET /events/{event_id}/times`, `GET /contacts`, `GET /recording-names`. Opt-in refusals
are 403, conflicts 409, invalid input 422. The API has no authentication.

## Transformations (A03)

| Input → result | Preserved | Lost | Added | Reversibility |
|---|---|---|---|---|
| MBOX/EML → `email` events + children | bytes, raw headers (bounded), byte range, attachments' exact bytes | exact header serialization beyond bounds; re-serialized `message/rfc822` | decoded text, parsed addresses, thread reconstruction, time candidates | re-parse from the stored MBOX/EML |
| ICS → `calendar_event` | raw content lines (bounded), recurrence raw | nothing expanded; VTIMEZONE text only in the original | zone resolution, projections | re-parse; expansion is recomputed on demand |
| VCF → contact observations | raw lines, all TEL/EMAIL values | photos (length only) | normalized candidates | re-parse |
| `calls.xml`/billing → `call` | raw attributes/cells/record, row locators | spreadsheet formatting; formulas not evaluated | labels from data mappings, end projections, ambiguous readings | re-parse with another mapping/profile version |
| File name → recording observation | name, locator, pattern version | — (audio not read) | hypotheses, `filename_derived` time | re-run with another pattern set |
| Takeout/location/browser → events | bounded raw records; location only coarse | exact coordinates and place labels (kept only in original) | parsed times, text projections | re-parse from the original |
| WhatsApp export → references | marker line and locator | — | reference status | re-run |
| Image → EXIF candidates | tags read, container | other tags, GPS coordinates | candidates | re-read |

## Decisions and conflicts with the prompt

* Unknown RFC 5322 zones are not read as UTC (see table) — invariant "unknown stays
  unknown" wins over a literal RFC reading; the RFC reading is kept visibly.
* A source-declared zone (ICS `TZID`) uses `tz_status='explicit_offset'` with
  `zone_declaration`; the `event_times` CHECK has no separate "declared zone" status.
* `events.ts_start` holds only source-fixed UTC instants (`…Z`); wall-clock-only values
  live in `event_times`. Older importers still write their own formats.
* Recording names create `recording` events so their candidates fit `event_times`;
  hypotheses stay on the observation row.
* Contacts are observations, not events, as requested.

## Not done / boundaries

* No correlation is written (recording ↔ call, contact ↔ participant, JSON ↔ HTML
  activity): inputs are prepared for the timeline track's revocable hypotheses.
* Billing, recording-name, Takeout and WhatsApp patterns are examples; real exports may
  differ. Localized HTML dates beyond EN/PL/NL month names are unparsed and flagged.
* MBOX `>From` unescaping assumes mboxrd; an mboxo file with literal `>From ` lines gets
  one `>` removed in the parsed text (count recorded; original intact).
* No MBOX/EML signature (DKIM/S/MIME) verification; TNEF (`winmail.dat`) and
  uuencoded bodies are not decoded (kept as attachments/text).
* Location/browser data are processed locally only; there is no UI for these importers.
* Large JSON/ICS/VCF files are read whole up to `max_json_bytes` (512 MiB default).
* A WhatsApp chat `.txt` inside a zip (or imported with a collection) is acquired twice per
  run: once with its parent/collection by `whatsapp_media`, once by the unchanged
  `whatsapp_txt` importer. Both observations point to the same artifact.
* A file that fails to parse (e.g. `calls.xml` with a DTD) has already been acquired; the
  acquisition stays, no events are written and no `import_run` audit row is added.

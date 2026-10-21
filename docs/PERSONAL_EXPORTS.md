# Personal-data exports and file metadata (digital traces)

Task `AGEDS-TASK-20261017-L2` of the [digital traces wave](plans/DIGITAL_TRACES_2026-10-17.md)
(shared contract and priority table there). Verification:
[receipt](validation/TRACES_2026-10-17.md). Code: `server/app/traces/` (shared
mechanics), `server/app/importers/{photos_takeout,facebook,instagram,telegram,google_chat,twitter_x,linkedin,account_activity,doc_metadata,google_keep,browser_profiles}.py`,
data mappings `server/app/importers/data/*.v1.json`, schema
`server/app/schema/traces.py` (versions 110–114).

The importers cover traces that almost everyone leaves, in order of prevalence:
photo metadata, messaging/social downloads, account login activity, document
metadata, notes and local browser profiles. Location history itself is the
location area's work (L1); photo geotags are stored here and offered to L1
through a documented read contract (`traces.geotags.iter_geotags`).

## Use

```bash
python -m server.app.cli traces-kinds
python -m server.app.cli import-trace photos-takeout "Takeout/Google Photos" [--enable-location] [--coordinate-decimals N]
python -m server.app.cli import-trace facebook facebook-export-folder/ [--owner-name NAME] [--export-label L]
python -m server.app.cli import-trace instagram instagram-export-folder/ [--owner-name NAME]
python -m server.app.cli import-trace telegram DataExport_2024-01-01/ [--owner-id ID]
python -m server.app.cli import-trace google-chat "Takeout/Google Chat" [--owner-id EMAIL]
python -m server.app.cli import-trace x-archive twitter-archive/ [--owner-id ACCOUNT_ID]
python -m server.app.cli import-trace linkedin messages.csv [--owner-name NAME]
python -m server.app.cli import-trace account-activity login_activity.json [--enable-location]
python -m server.app.cli import-trace documents folder-or-file.pdf
python -m server.app.cli import-trace keep Takeout/Keep
python -m server.app.cli import-trace browser-profile ~/.config/chromium/Default --enable-browser-history [--session-gap-minutes 30]
python -m server.app.cli traces-summary
python -m server.app.cli traces-geotags [--since ISO] [--until ISO] [--limit N]
```

Every command prints one JSON object (`source_bytes_written: false`). Common
options: `--label`, `--collection-id`, `--source-locator`, `--max-records`.
An option a kind does not accept is refused, never silently dropped.

HTTP (`/api/traces`): `GET /kinds`, `POST /import/{kind}` (multipart `file` plus
form flags `enable_location`, `coordinate_decimals`, `enable_browser_history`,
`owner_name`, `owner_id`, `export_label`), `POST /import/{kind}/path`
(`{rootId, relativePath, enableLocation, coordinateDecimals, enableBrowserHistory, ownerName, ownerId, exportLabel, sessionGapMinutes}`,
confined to `EW_SCAN_ROOTS`), `GET /summary`, `GET /geotags`,
`GET /photo-sidecars?include_people=false`, `GET /documents`,
`GET /browser-visits`. Opt-in refusals are 403, conflicts 409, invalid input 422.
The page **Ślady** (`/traces`) shows counts per event type, platform, geotag
status, sidecar match status, document status and the recent import runs with
their options.

Import plan: detector rules in
[`detectors.d/traces.v1.json`](../server/app/importplan/data/detectors.d/traces.v1.json),
adapters in [`adapters_ext/traces.py`](../server/app/importplan/adapters_ext/traces.py):
`google_photos_sidecar`, `photo_geotags` (alternative to `exif` for images),
`facebook_messages`, `instagram_messages`, `telegram_desktop`, `google_chat`,
`twitter_x_archive`, `linkedin_messages`, `account_activity`,
`document_metadata`, `google_keep`, `browser_profile` (required opt-in
`enable_browser_history`). Location (`enable_location`) is an optional opt-in,
off by default for every item.

## Common contract

| Rule | Implementation |
|---|---|
| Acquisition | Every input goes through `evidence.ingest_file` (collection, locator, `source_stat` semantics of the 2026-10-16 review fixes); plan runs use the catalog locator and declared catalog mtime. Referenced media are acquired as children (`parent_artifact_id`) in folder mode. Originals are read only; tests compare SHA-256 (and for browser profiles also mtimes and the absence of `-shm`) before and after. |
| Layers | Raw values stay in the preserved file; events keep bounded raw copies (`CommLimits`: 4096 chars per string, 200 items per container, truncation counted, lone surrogates replaced by U+FFFD with the original hash). A decoded text layer names its rule (`fb_latin1_utf8_v1`) and keeps the exported string per field. Provider inferences (Google's `geoData`, a provider's city for a login) are labelled as such and kept apart from raw samples. |
| `metadata.signal` | `{class, basis, provider, device_or_account}` per the plan: messages/posts `active`/`raw_sample` (`provider_user_edit` for edited Telegram messages), login records `passive`, notes and document metadata `declared`, browser sessions `active`/`derived`, photo sidecars `active`. |
| Time | Every stated time is a `TimeCandidate` (`timeinfo`) with kind, raw text, zone status and precision; `events.ts_start` only for a source-fixed UTC instant. Epoch units are the ones each format declares (s, ms, µs, WebKit µs since 1601, PRTime µs); a value implausible in its declared unit (outside 1800–2200) has no candidate and is reported; a value before the service existed (e.g. ms written as s) is kept and flagged `time_before_service_start`. The unit is never guessed from the magnitude. |
| Privacy defaults | Coordinates (photo geotags, a provider's location label of a login) are stored only with `enable_location`; browser profiles refuse to run without `enable_browser_history`. No reverse geocoding, IP geolocation or map tiles. Third-party names in photo `people` stay raw in `trace_photo_sidecars` and are never indexed; cookie values are never stored (presence + 16-hex SHA-256 prefix); sharee e-mails in Keep are counted only; Telegram `personal_information` and shared locations are not stored; IP addresses stay in metadata, not in bodies or the search index. |
| Identity and idempotence | Re-importing the same file at the same locator creates no new events, candidates, rows or references (append-only rows; a changed interpretation under the same parser version raises instead of overwriting). Messaging uses logical records across exports (`records.add_occurrence`, record kinds `message`/`thread`; source ids where the format has them, otherwise content-derived ids with a named basis and `~dupN`). |
| Issues, coverage, runs | Bounded issue lists with exact per-code counts; limits make coverage partial; every run appends one `import_run` audit row with importer, version, options/opt-ins, counts, coverage and issue counts. |
| Confidence | `events.confidence` stays `NULL`. Matching hypotheses carry rule ids, not probabilities. |

## Formats

### Google Photos Takeout (`photos_takeout.py`, event `photo_metadata`)

Sidecar JSON next to the media: `title`, `description`, `photoTakenTime` →
`created`, `creationTime` (item creation/upload in Google Photos) → `other`,
`photoLastModifiedTime` → `updated` (timestamps are epoch seconds; the localized
`formatted` text is kept raw on the candidate, not parsed), `geoData` and
`geoDataExif` (both kept; `0,0` is Google's "no location" placeholder; the
sidecar row records `agree`/`differ`/`one_side_only` with the tolerance from
the rules file), `people` (raw, never indexed), `url`.

Name rules (data: [`photos_takeout.v1.json`](../server/app/importers/data/photos_takeout.v1.json),
NFC normalization on both sides):

| Rule id | Strength | Sidecar ↔ media |
|---|---|---|
| `sidecar_name_exact_v1` | strong | `IMG.jpg.json`, `IMG.jpg.supplemental-metadata.json` ↔ `IMG.jpg` |
| `duplicate_counter_v1` | strong | `IMG.jpg(1).json`, `IMG.jpg.supplemental-metadata(1).json` ↔ `IMG(1).jpg` |
| `title_exact_v1` | strong | `title` equals the media name (only without a counter) |
| `base_without_extension_v1` | strong | `IMG.json` ↔ `IMG.<media ext>` (e.g. a live-photo pair: two candidates) |
| `truncated_sidecar_prefix_v1` | weak | sidecar name at the observed 51-character limit: its cut base is a prefix of the media name |
| `truncated_media_name_v1` | weak | media name at the observed 47-character limit: its stem is a prefix of the title stem, same extension |
| `edited_variant_v1` | variant | `IMG-edited.jpg` (and localized suffixes) listed as `edited_variant`, never as the described file |

A match row (`trace_photo_matches`) is computed over an explicit, hashed scope
(the files of the same directory, or the catalogued siblings in a plan run; the
hash covers names, SHA-256 and which files are already acquired, so a later run
after more files were imported appends a new match instead of conflicting):
strong rules decide when they propose anything, weak rules otherwise; one
candidate → `single_candidate`, several → `ambiguous` (nothing chosen), none →
`no_candidate`. Media without any sidecar are listed (`media_without_sidecar`);
album `metadata.json` and other JSON are skipped and counted.

### EXIF GPS (`photos_takeout.observe_media`, parser `exif_gps` v1)

The unchanged `exif_minimal` v1 reader records EXIF time claims; the GPS IFD is
read by the shared-edit option `exif.read_exif(..., gps=True)`: latitude and
longitude (degree/minute/second rationals, hemisphere references required —
a missing reference is `invalid`, never guessed), altitude (below sea level by
`GPSAltitudeRef`), `GPSHPositioningError`, `GPSMapDatum`. `GPSDateStamp` +
`GPSTimeStamp` (UTC by the EXIF definition) is a separate `exif` candidate
(`source='exif_gps@1:GPSDateStamp+GPSTimeStamp'`) appended to the image's
`image_capture` event, or to an own `photo_metadata` event when EXIF has no
time; it may be a stale last fix. HEIC/video containers are listed as
`exif_container_not_supported`.

### Geotag store and read contract for the location area

`trace_geotags` holds one row per (artifact, source kind, parser, stored
precision): `exif_gps` and `takeout_geoDataExif` are `raw_sample`,
`takeout_geoData` is `provider_inference_or_user_edit`. Without
`enable_location` a row says `present_not_stored` and holds no coordinates or
raw values (and does not depend on the run's options, so re-imports stay
idempotent); with it, coordinates are stored at source precision or rounded to
`coordinate_decimals` (`stored_precision` records which).

`server.app.traces.geotags.iter_geotags(since=, until=, source_kinds=, include_provider=, collection_id=, limit=)`
yields only stored rows, ordered by time, each with `geotag_id`, `source_kind`,
`basis`, `lat`, `lon`, `alt`, `accuracy_m`, `stored_precision`, `time_utc`,
`time_basis` (`exif_gps_receiver_utc` or `google_photos_photoTakenTime`),
`artifact_id`, `media_sha256` (EXIF: the artifact; a sidecar: only through a
single described match), `sidecar_id`, `event_id`, `collection_ids`, `parser`,
`parser_version`, `opt_in` (full contract in the module docstring). A geotag is
a claim of a file or account, not an observation of a person; place matching
should record its own versioned rule, distance and the tag's basis.

### Messaging and social downloads (`chat_message` / `social_post`)

Shared writer `traces/messaging.py`, one transaction per thread. Event columns:
`thread_id` = `<namespace>:<thread id>`, `contact_label` = platform lane
(`facebook_messenger`, `instagram`, `telegram`, `google_chat`, `twitter_x`,
`linkedin`), `sender` as exported (decoded layer), `direction` only with a known
owner (`direction_basis`: parameter, Telegram `personal_information` or X
`account.js`), `body` = text. `metadata.chat` adds platform, conversation id and
title, content kind (`text`, `media`, `share`, `call`, `service`, `unsent`,
`reaction`, `post`, `retweet`, `reply`, `empty`, `location`), record id and its
basis, reactions, replies/forwards, edits. Attachments are
`event_media_references`: folder imports acquire present files as children;
plan runs reference catalogued entries (`not_extracted`, with entry id and
SHA-256) or report `missing_from_export`; uploads have no files
(`not_extracted`); export placeholders are `omitted_by_export`.

| Format | Input | Times | Notes |
|---|---|---|---|
| Facebook/Messenger | `messages/<folder>/<thread>/message_N.json` (also under `your_activity_across_facebook/`); E2EE "secure storage" JSON | `timestamp_ms` / `timestamp` (epoch ms) → `created` | mojibake rule `fb_latin1_utf8_v1` for the legacy layout only (text above U+00FF or invalid UTF-8 left as exported, status per field); reactions, photos/videos/audio/files/gifs/sticker, share, call duration, unsent, unmapped `type` flagged; thread id from `thread_path` or the folder names |
| Instagram | `your_instagram_activity/messages/inbox/<thread>/message_N.json` | as Facebook | same parser, platform `instagram` |
| Telegram Desktop | `result.json` (full: `chats.list`, `left_chats.list`; single chat) | `date` = exporting computer's wall clock (zone **unknown**) and `date_unixtime` (UTC) as separate `created` candidates; `edited`/`edited_unixtime` → `updated` | text string or entity list (body = concatenation, entity types/hrefs kept), replies, forwards, service records (calls), reactions; "(File not included…)" → `omitted_by_export`; shared locations not stored; HTML export refused as unsupported |
| Google Chat | Takeout `Google Chat/Groups/<DM|Space>/messages.json` (+ `group_info.json` name) | localized `created_date`/`updated_date`: weekday and joiner removed (`google_chat_display_v1`), Takeout display patterns; only `UTC`/`GMT`/numeric zones fix the instant | `message_id` record ids, creator e-mail as `sender_id`, `attached_files` by `export_name`, reactions counted |
| X/Twitter | `data/tweets.js` (`tweet.js`), `direct-messages.js`, `direct-messages-group.js` (`window.YTD.<name>.partN =` prefix checked; a file without it is parsed and flagged) | tweets `created_at` (`Wed Oct 10 20:19:24 +0000 2018`, explicit offset), DMs `createdAt` ISO `Z` | tweets → `social_post` (`outgoing`, basis `account_archive_own_posts`; retweet/reply kinds; media `tweets_media/<id>-<file>`); DMs → `chat_message` with source ids, reactions, edit-history count, join/leave/rename events as `service`, unknown events flagged |
| LinkedIn | `messages.csv` | `DATE` (`… UTC` or offset fixes the instant; without zone `unknown`) | HTML content projected to text (never rendered), content-derived record ids, short rows flagged |

HTML variants of these downloads are not parsed (Telegram HTML is refused with a
message; the others are simply not detected).

### Account security and login activity (`account_activity.py`, event `account_activity`)

Facebook (`account_activity_v2`, `account_accesses_v2`, `active_sessions_v2`,
`recognized_devices_v2`, `used_ip_address_v2`, the newer `label_values`
records), Instagram (`account_history_login_history`, `…logout…`,
`…password_change…` with `string_map_data`, older `login_history`) and Google
Takeout Access Log Activity CSVs (activities, devices); layouts in
[`account_activity.v1.json`](../server/app/importers/data/account_activity.v1.json).
Times: activity → `created`, session/device created → `start`, last activity →
`updated`. `metadata.account_activity` holds the action, the IP address raw
with a syntactic check (version, private/global flags, `geolocation:
not_performed`), user agent, device, app/site, session type and language. The
provider's own location labels (city/region/country, "Estimated location",
"Device Last Location") are third-party IP geolocation: the event says they are
present; values go to `trace_provider_locations` only with `enable_location`.
AGEDS performs no IP geolocation; it would be a future explicit opt-in because
it sends IP addresses to a third party.

### Document metadata (`doc_metadata.py`, event `document_metadata`)

OOXML `docProps/core.xml` + `app.xml`, ODF `meta.xml`, PDF `/Info` (direct
object; incremental updates — last definition wins, earlier ones listed and
flagged; object streams through XRef streams; UTF-16BE/UTF-8/PDFDocEncoding
strings; indirect string values) and XMP packets (attribute and element forms,
`rdf:Seq` creators). Dates: W3CDTF with stated precision (year, month, day,
second, fraction; no zone → `unknown`; impossible values unparsed) and PDF
`D:YYYYMMDDHHmmSSOHH'mm'` including partial forms (`D:2020` = year precision),
`Z00'00'`, a missing `D:` prefix and trailing text (flagged), out-of-range
offsets (wall clock kept, `numeric_offset_out_of_range`). Contradicting Info and
XMP dates are both kept as separate candidates. Authors, last-modified-by,
companies and tools are **claims** stored in `trace_document_metadata` (not in
event columns, bodies or the search index). Bounds: metadata members read with
a byte bound (a zip-bomb-shaped member stops at 4 MiB), DOCTYPE/ENTITY refused,
≤ 64 inflated streams of ≤ 16 MiB, ≤ 200,000 objects scanned, XMP ≤ 1 MiB,
PDF scan ≤ 256 MiB. Statuses: `parsed`, `partial`, `no_metadata`, `unreadable`
(corrupt ZIP). Legacy OLE `.doc/.xls/.ppt` SummaryInformation and encrypted PDF
strings are reported as not supported.

### Google Keep (`google_keep.py`, event `note`)

One Takeout JSON per note: `createdTimestampUsec` → `created`,
`userEditedTimestampUsec` → `updated` (epoch microseconds), title, text or
checklist (`[x]`/`[ ]` lines), labels, colour, pinned/archived/trashed flags
(trashed notes are imported and flagged, nothing is hidden), annotations,
attachments (`filePath` next to the JSON) and sharees (count/roles only). The
per-note HTML files are counted, not parsed.

### Local browser profiles (`browser_profiles.py`, opt-in)

Chromium/Edge `History` (`urls`/`visits`, WebKit epoch) and Firefox
`places.sqlite` (`moz_places`/`moz_historyvisits`, PRTime). The database and a
`-wal` file next to it (child artifact) are acquired; parsing opens a **private
temporary copy** of the stored bytes: `mode=ro&immutable=1` without a WAL
(`reading_key='immutable'`), `mode=ro` on the copied pair with a WAL so
committed WAL frames are read (`reading_key='wal:<sha256 prefix>'`). A
`-journal` is reported (`rollback_journal_present_not_applied`). The live
profile is never opened by SQLite; a profile copied while the browser runs may
still be inconsistent, so close the browser first. Every visit is a
`trace_browser_visits` row (bounded URL/title, raw time, UTC, transition code,
referrer visit); the timeline gets `browser_session` events (rule
`browser_session_gap_v1`, gap parameter default 30 minutes; subframe/embed
visits are counted but do not form sessions) with start/end candidates and top
domains. In an import plan the WAL file is a separate catalog entry and is not
part of the unit: use the CLI on the profile folder to include it.

## Schema (versions 110–114, append-only)

| Version | Table | Content |
|---|---|---|
| 110 | `trace_photo_sidecars`, `trace_photo_matches` | parsed sidecars (no coordinates); match hypotheses per hashed scope |
| 111 | `trace_geotags` | photo geotags; coordinates only under `enable_location` (CHECK-enforced consistency of status, flag and values) |
| 112 | `trace_document_metadata` | claims, raw dates with status, tools, bounded raw, issues |
| 113 | `trace_browser_visits` | local profile visits per reading |
| 114 | `trace_provider_locations` | a provider's location label of an account activity record (`provider_inference`), opt-in only |

All six tables are declared in
`packages.EXTENSION_TABLES` (exported in metadata packages with the opt-ins
they were stored under).

## Coverage of almost-universal traces

| Trace | Status here | Why / what is missing |
|---|---|---|
| Photo/video capture time and place (EXIF, Google Photos Takeout) | supported (JPEG/PNG/TIFF EXIF; Takeout sidecars for any media) | HEIC/MP4/MOV embedded metadata (ISO BMFF `Exif`/`mvhd`) not parsed yet: sidecars still give time and place |
| Google location history | location area (L1) | — |
| Facebook/Messenger, Instagram messages | supported (JSON downloads) | HTML downloads not parsed; Messenger E2EE chats only via the secure-storage JSON download |
| WhatsApp, SMS, call logs, e-mail, calendar, contacts | already present (2026-10-16 importers) | — |
| Telegram | supported (Telegram Desktop JSON export) | HTML export unsupported; cloud-only data needs an export |
| Google Chat, X/Twitter, LinkedIn | supported (Takeout / archive / data download) | LinkedIn attachments are not in the export |
| Account login/security activity (Facebook, Instagram, Google access log) | supported | IP geolocation intentionally not performed (future explicit opt-in) |
| Document authorship/edit claims (Office, ODF, PDF) | supported | legacy OLE `.doc/.xls/.ppt` property sets, encrypted PDFs |
| Notes (Google Keep) | supported | Apple Notes, Samsung Notes, OneNote need their own exports |
| Browser history (Chrome/Edge/Firefox profiles, Takeout Chrome) | supported (opt-in) | Safari `History.db`, mobile browsers without root, downloads/searches tables not read |
| Bank/card, health/fitness, transport | ledger area (L3) | — |
| Device usage statistics (Digital Wellbeing / Screen Time) | **out of reach** | stored in app-private databases; no user export, needs root or a forensic extraction |
| Wi-Fi/cell connection logs, Bluetooth pairings | **out of reach** | system logs not exported to users; needs root/forensic tooling |
| Signal messages | **out of reach** | backups are encrypted with a device passphrase and not documented as an export format; decrypting needs the user's key and forensic tooling |
| Apple Significant Locations, iOS KnowledgeC/Biome | **out of reach** | protected system databases; only forensic extraction of a full file-system image |
| Android app-private data (most apps' local databases) | **out of reach** | sandboxed; needs root or a forensic acquisition |

## Transformations (A03)

| Input → result | Kept | Lost | Added | Reversibility |
|---|---|---|---|---|
| Sidecar → event + sidecar row + geotags + match | raw values (bounded), name analysis, both geo fields | coordinates without opt-in (still in the preserved file), formatted display text not parsed | rule-id hypotheses, agreement status | re-parse from the stored sidecar; matching recomputable over a new scope |
| EXIF GPS → geotag + candidate | raw rationals/refs (opt-in) | other GPS tags | decimal degrees, UTC instant | re-read the stored image |
| Message file → events | bounded raw message, raw text per decoded field, locator | exact JSON serialization; shared locations (Telegram) | decoded layer, content kind, direction (with basis), logical record ids | re-parse with another mapping/rule version (new parser version = new events) |
| Login record → event | IP raw, user agent, device, action, bounded raw | cookie values, provider location without opt-in | IP syntax flags, cookie hash prefix | re-parse from the stored file |
| Document → claims + candidates | raw property strings and dates with status | non-metadata content; OLE property sets | parsed instants with stated precision | re-parse |
| Keep note → event | text/checklist, flags, labels | sharee e-mails, HTML variant | checklist text projection | re-parse |
| Browser DB → visits + sessions | every visit row (bounded URL/title), raw times | tables other than visits/places; WAL in plan runs | UTC, session aggregation (parameterized rule) | re-read the stored copy; sessions recomputable with another gap |

## Decisions and conflicts with the prompt

* **Decoded layer in the body.** The mojibake repair is a deterministic decoding
  of a known export defect; the body/sender/title use the decoded layer while
  `metadata.text.fields` keeps every exported string (bounded, SHA-256) and the
  per-field status. A rule change is a new parser version with new events.
* **Sidecar matching tiers.** "Ambiguous matches choose nothing" is applied per
  tier: a strong rule (exact name, counter, title) decides before the
  truncation rules; ambiguity among equally strong candidates chooses nothing.
* **Provider location of logins** is location data (third-party IP
  geolocation), so it follows the location opt-in, in its own table so events
  stay identical across opt-in runs.
* **Opt-in for geotags** governs extraction into queryable rows; the acquired
  image/sidecar bytes contain whatever the export contains (as for every
  preserved original).
* **GPS time on the EXIF event.** The GPS UTC candidate is appended to the
  `exif_minimal` event of the same artifact (distinct `source`), so a photo is
  one timeline item; the display rule may prefer it over the zone-less camera
  clock, both remain visible.
* **Browser sessions** instead of one event per visit (passive high-frequency
  samples live in an area table per the shared contract).

## Shared edits (reported)

* `server/app/importers/exif.py`: `read_exif(..., gps=False)` and
  `_tiff(..., gps=False)` gained the GPS IFD reader (`_gps_ifd`,
  `GPS_PARSER_NAME/VERSION`); the default path and `exif_minimal` v1 output are
  unchanged.
* `server/app/packages.py`: one classification block for the six trace tables.
* `server/tests/corpus/__init__.py`: `'traces'` added to `PARTS`.

## Not done / boundaries

* No real exports were available: layouts follow public observations and are
  encoded as data files (`*.v1.json`); check them against real downloads.
* HEIC/MP4 metadata, OLE property sets, Safari history, Telegram/Facebook HTML
  exports, LinkedIn connections/invitations, Instagram posts/stories/likes,
  Facebook posts/comments/reactions outside Messenger, X likes/followers.
* No reverse geocoding, IP geolocation or place matching (place presence from
  geotags is the location area's projection over `iter_geotags`).
* Large JSON files are read whole within `max_json_bytes` (512 MiB default).

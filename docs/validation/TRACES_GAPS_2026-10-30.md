# Digital traces: open gaps (2026-10-30)

Digital traces wave: L1 location, L2 personal-data exports, L3 ledger/health/transport, integrated on
2026-10-21 (draft PR #6, now part of `claude/rzecznik-design`). This file lists what is still
unverified and what would close it, per gap.

**Data basis.** All results of the wave come from data **generated** at test time. The receipts are
[LOCATION_2026-10-17](LOCATION_2026-10-17.md) (corpus `server/tests/corpus/location.py`),
[TRACES_2026-10-17](TRACES_2026-10-17.md) (`server/tests/corpus/traces.py`) and
[LEDGER_2026-10-17](LEDGER_2026-10-17.md) (`server/tests/corpus/ledger.py`). No real provider export,
bank statement, health export or location history has been imported yet.

The only real material that touched this area was 4 PNG screenshots of the owner's pilot. During
the browser rehearsal of 2026-10-27 the import plan routed them to the `exif` importer, on a
throwaway copy of the pilot database that was deleted afterwards. Their EXIF results were not
reviewed.

A gap is closed when a real export of that format has been imported, its counts and times have been
checked against the provider's own view, and a new receipt records the result. Fixes go into data
files where the layout is data (`*.v1.json`); a new parser version is needed when the code changes.

## Location (L1)

| Gap | Source | Tested on | To close |
|---|---|---|---|
| Google location history: legacy `Records.json`, Semantic Location History, and the on-device Timeline export (2024+) | `server/app/location/google.py`, `jsonstream.py`, rules in `server/app/location/rules/` | generated files, including a 256 MB / 2 M-point stream (synthetic) | One real export of each variant the owner has. Compare point and visit counts and the first and last instant with the Google Maps timeline. Adjust the mapping if field names differ. |
| GPX, KML/KMZ, GeoJSON | `server/app/location/formats.py`, `xmlstream.py` | generated tracks | A real file from each app the owner uses (tracker, My Maps). KMZ reads one `.kml` member; network links and overlays are not followed. |
| FIT (sports devices) | `server/app/importers/fit.py` (via `server/app/location/ingest.py`) | **fake `fitparse` module only** — never real decoding | Add `fitparse` to a requirements file and import one real `.fit` file from the device. |
| Photo geotags in presence | `server/app/location/sources.py` (`traces.photo_geotags` is a read-only projection), `presence.py` | generated photos | A decision whether geotags may feed `presence-run`. It needs a rule version and a test; today `places-match-source` only shows the matches. |
| Unknown-zone wall clocks | `server/app/location/presence.py` | generated | By design they are counted in the import but excluded from presence. Close only if a zone can be documented per device (private seed), never guessed. |
| Reverse geocoding, IP geolocation | none | — | Not planned. Place names come only from the private places file. |

## Personal-data exports (L2)

| Gap | Source | Tested on | To close |
|---|---|---|---|
| Google Photos Takeout (sidecars, truncated names, `(1)` duplicates, edited copies) | `server/app/importers/photos_takeout.py`, `exif.py`, `server/app/traces/geotags.py` | generated Takeout layouts | A real Photos Takeout part. Check the sidecar-to-media matching rate and the geotag counts. HEIC/MP4 metadata is not parsed. |
| Facebook / Messenger, Instagram (JSON) | `server/app/importers/facebook.py`, `instagram.py`, `server/app/traces/messaging.py` | generated JSON (mojibake, E2EE JSON, RTL/emoji) | Real downloads in **JSON** format; the HTML format is not parsed and must not be chosen when downloading. Check message counts per thread against the app. |
| Telegram Desktop `result.json` | `server/app/importers/telegram.py` | generated | A real `result.json`. The HTML export is not parsed. |
| Google Chat, Keep, My Activity / account activity | `server/app/importers/google_chat.py`, `google_keep.py`, `account_activity.py`, `takeout_activity.py` | generated Takeout folders | Real Takeout parts for these products. Localized and abbreviated zone names were covered with generated data only. |
| X (Twitter) archive, LinkedIn | `server/app/importers/twitter_x.py`, `linkedin.py` | generated | Real archives. Likes, followers, connections and invitations are not parsed. |
| Browser profiles (Chrome/Firefox SQLite) | `server/app/importers/browser_profiles.py` | generated profile databases | A real profile copy, taken with the browser closed. Safari is not supported. |
| Document metadata (PDF, OOXML, ODF) | `server/app/importers/doc_metadata.py` | generated documents | Real documents from the case. OLE property sets (`.doc`, `.xls`) are not read. |
| Data-file layouts in general | `server/app/importers/data/*.v1.json` | generated | Each mapping is an assumption from public observations. A real file either confirms it or changes it, as data. |

## Ledger, health, transport (L3)

| Gap | Source | Tested on | To close |
|---|---|---|---|
| Bank CSV/XLSX statements | `server/app/importers/tabular_profiles.py`, profiles in `server/app/importers/data/tabular_profiles.v1.json` | generated CSV in CP1250, ISO-8859-2, UTF-8 and CP1252, with decimal comma and point | One real statement per bank of the owner, then a private profile for it. Check the balance against the bank. XLS/XLSX amounts arrive as floats (flagged). |
| CAMT.053 and MT940 | `server/app/importers/camt053.py`, `mt940.py`, `server/app/ledger/bank.py` | generated v2/v8 CAMT and MT940 with envelopes | A real file from the bank. The balance check reports mismatches and never corrects them. |
| Transport and rides (taps, journeys) | `tabular_profiles.py` (travel profiles) | generated, including a DST-crossing ride | Real exports from the operators the owner uses. |
| Health: Apple, Fitbit, Google Fit, Samsung | `server/app/importers/health_apple.py`, `health_fitbit.py`, `health_google_fit.py`, `health_samsung.py`, `server/app/ledger/health.py` | generated exports; opt-in `enable_health` | A real export per device, only with the owner's explicit opt-in (special-category data, processed locally). ECG CSVs, `export_cda.xml`, clinical records and Fitbit minute files are not interpreted. |
| ~~Plan-time validation of `profile_id`~~ **closed 2026-10-30** | `server/app/importplan/plan.py` (`_validate_parameters`, `ledger_tabular`) | generated plan items (`test_ledger_importplan.py`) | Done: an unknown tabular profile is refused when the plan is decided, using the same `ledger_profiles_path` as the run. |

## Shared with the traces area

| Gap | Source | Tested on | To close |
|---|---|---|---|
| 7z and RAR archives | `server/app/catalog/archives.py` | **not executed** (libraries absent) | Install the libraries in a test environment and catalogue one real archive of each. |
| Large JSON read whole up to `max_json_bytes` (512 MiB) | `server/app/traces/common.py` | generated | Measure on the largest real export; stream if needed. |

The coordinator's import check on real export **formats** (Gmail, WhatsApp, 2026-10-30) is a separate
receipt in `docs/validation/` and covers communications, not this area.

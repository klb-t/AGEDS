# Location history, private places and presence

Task `AGEDS-TASK-20261017-L1` (digital traces wave, shared contract:
[DIGITAL_TRACES_2026-10-17](plans/DIGITAL_TRACES_2026-10-17.md)). Code: `server/app/location/`,
importers `server/app/importers/{location,gpx,kml,geojson,fit}.py`, schema versions 100–102
(`server/app/schema/location.py`), routes `server/app/api/location.py` (page `/location`, nav
“Lokalizacja”), CLI `server/app/commands/location.py`, import-plan adapters
`server/app/importplan/adapters_ext/location.py` with detector rules
`server/app/importplan/data/detectors.d/location.v1.json`. Receipt:
[LOCATION_2026-10-17](validation/LOCATION_2026-10-17.md).

> **A position is a device's or an account's position, not proof of a person's presence.**
> A provider's visit or activity is the provider's model output (an inference), not an
> observation. A presence interval is our own derivation from stored samples and a private
> place set. Every result here says which of these it is (`metadata.signal`).

## 1. Use

```bash
python -m server.app.cli location-import FILE --enable-location [--format auto|google|gpx|kml|geojson|fit] \
    [--store-decimals 0-9] [--stream KEY] [--provider-place-text keep|omit] [--gpx-naive-time unknown|utc_per_schema] \
    [--geojson-options JSON] [--sample-session-gap-s 1800] [--collection-id N] [--source-locator L] [--label L] \
    [--max-fixes N] [--max-events N] [--max-element-bytes N]
python -m server.app.cli places-check [--places PRIVATE.json]           # or AGEDS_LOCATION_PLACES
python -m server.app.cli presence-run [--places PRIVATE.json] [--from ISO --to ISO] [--params JSON] [--publish]
python -m server.app.cli presence-show RUN_ID
python -m server.app.cli location-window NEW_OUTPUT.json [--from --to --stream --max-points --display-decimals --presence-run]
python -m server.app.cli location-correlate --rule presence_calendar|presence_communication --presence-run ID [--params JSON]
python -m server.app.cli location-correlation-review HYPOTHESIS_ID accept|reject|revoke --reason TEXT [--actor A]
python -m server.app.cli location-status
```

Every importer refuses to run (and acquires nothing) without the per-run opt-in
`--enable-location` / `enable_location=true`; the opt-in is recorded on every event, on the
acquisition observation and in the `import_run` audit row. Outputs are one JSON object; the
window export writes a new file only.

| Route | Purpose |
|---|---|
| `GET /location` | map, time slider, presence table, lanes, tile opt-in (served with a strict CSP, §9) |
| `POST /api/location/import` | multipart upload (`enable_location`, `format`, `store_decimals`, `stream`, `provider_place_text`, `gpx_naive_time`, `geojson_options`, `label`, `collection_id`); 403 without opt-in, 501 for an unavailable adapter, 422 for an unrecognised format |
| `GET /api/location/summary` | streams, imports, event counts, place sets, presence runs, lanes, adapters (`fit`), geotag sources |
| `GET /api/location/window` | `from`/`to` (UTC with offset), `stream`, `max_points` (≤ 20 000, deterministic thinning reported), `display_decimals`, `presence_run`, `basis` |
| `GET /api/location/lanes` | lane items through the timeline seam (times with `t_lo`/`t_hi`) |
| `POST /api/location/places/check` | validate inline places JSON (`{places: {...}}`) or `use_server_places` |
| `POST /api/location/presence/runs`, `GET …/runs[/{id}]` | presence runs (inline places only; a request never names a path) |
| `POST /api/location/correlations/runs`, `GET …/runs[/{id}]`, `POST …/hypotheses/{id}/review` | correlation runs and review |
| `POST /api/location/tiles/opt-in`, `GET …/tiles/opt-ins` | recorded tile opt-in (§9) |

## 2. Formats

Layouts follow public observations of export formats; a real export may differ (unknown
sections and keys are reported, never silently dropped).

| Format (detected by bytes) | What is read | Becomes |
|---|---|---|
| Takeout `Records.json` (`{"locations": [...]}`) | `latitudeE7`/`longitudeE7`, `accuracy`, `altitude`, `verticalAccuracy`, `velocity`, `heading`, `source`, `deviceTag`, `platformType`…, `activity` lists, `timestamp` (ISO) or `timestampMs` (epoch ms; both present → disagreement flagged) | raw samples `raw_position`; provider activity guesses kept bounded on the sample; one `location_samples` event per sampling session |
| Semantic Location History (`timelineObjects`) | `placeVisit` (location, duration ISO or `*Ms`, `placeConfidence`, `visitConfidence`, `locationConfidence`, `otherCandidateLocations`, `editConfirmationStatus`, `simplifiedRawPath`, `childVisits` ≤ depth 3), `activitySegment` (start/end, `activities` with probabilities, `distance`, `waypointPath`, `simplifiedRawPath`, `parkingEvent`) | `location_visit` / `location_segment` events; waypoint paths as `provider_inference` samples, simplified raw paths as `raw_sample` samples |
| Android on-device Timeline (`semanticSegments`, `rawSignals`, `userLocationProfile`) | `visit`/`activity`/`timelinePath`/`timelineMemory` with `"48.1°, 11.5°"` strings and `start/endTimeTimezoneUtcOffsetMinutes`; `position` (`LatLng`, accuracy, altitude, speed, source), `wifiScan` (device count and strongest RSSI only), `activityRecord` | events as above; `timeline_path` samples (`provider_inference`); `raw_position` samples; `location_signals` rows; one undated `location_profile` event (frequent places, persona) |
| iOS Google Maps `location-history.json` (top-level array) | string values, `"geo:lat,lon"`, `placeID`, `timelinePath` with `durationMinutesOffsetFromStartTime` | events as above; path times = start + offset (minute precision, `projection` recorded) |
| GPX 1.0/1.1 | `trk/trkseg/trkpt`, `rte/rtept`, `wpt`; `ele`, `time` (optional), `hdop/vdop/pdop/sat/fix` kept raw, `<extensions>` kept raw (bounded), not interpreted | `track_point` raw samples + `location_track`; `route_point` + `location_route` and `waypoint` + `location_waypoint` (`signal.class=declared`) |
| KML / KMZ | `Placemark` with `Point`, `LineString`, `gx:Track` (`when` ↔ `gx:coord` by position), `gx:MultiTrack`, `TimeStamp`, `TimeSpan`; polygons/models skipped and counted. KMZ: root `doc.kml`, else the first `.kml`, member-size limit | `gx:Track` → raw samples + `location_track`; `LineString` → `line_vertex` (declared) + `location_route`; `Point` → `location_waypoint` |
| GeoJSON | `FeatureCollection`/`Feature`/geometry; `Point`, `MultiPoint`, `LineString`, `MultiLineString`, `GeometryCollection`; time property **names are parameters** (`time_properties`, `start_properties`, `end_properties`, `coord_times_properties` incl. dotted paths, `accuracy_properties`, `epoch_unit`) | timed points → raw samples; untimed points → declared samples (map only); lines with per-vertex times → `location_track`, without → `location_route` |
| FIT (optional) | through `fitparse` **only if importable**: `record` positions (semicircles → degrees), timestamps (UTC by FIT specification), device `session` summaries kept raw | raw samples + `location_track`; without `fitparse` the import is refused as unavailable before anything is acquired |

**Time.** Every stated time is a `TimeCandidate` (`event_times` for events; on samples the
same fields in columns). ISO with `Z`/offset or an epoch fixes the UTC instant; a wall clock
without an offset stays `unknown` (GPX may opt in to `gpx_naive_time=utc_per_schema`, which
records `zone_declaration: gpx_schema_declares_utc`). Google's
`*TimezoneUtcOffsetMinutes` are kept on the candidate (`provider_utc_offset_minutes`); a value
that disagrees with the timestamp's own offset is flagged, not applied. Mixed offsets and DST
changes inside one segment are therefore exact (tested across the 2024-03-31 EU change).

**Malformed input.** NaN/Infinity, huge numbers, out-of-range coordinates, non-integer E7,
missing coordinates, unparsable times, truncated JSON/XML, unknown sections, and records that
are not objects are counted by issue code; a value shaped like a 32-bit overflow of an E7
coordinate is flagged (`possible_int32_overflow`) and **not** reinterpreted. A document type
declaration in GPX/KML is refused (no entity expansion).

**Bounded streaming.** JSON is read by `location/jsonstream.py`: the top level is walked and
each array element is decoded by the C decoder from a buffer of at most `chunk + max element`
characters (default element limit 64 MiB, `--max-element-bytes`), so a Records.json of
several GB never sits in memory; a test measures the peak on a 5 MB file below half its size
(`tracemalloc`). XML is streamed by pyexpat with coordinate text split incrementally. Other
limits: `max_fixes` (50 M), `max_signals` (10 M), `max_events` (1 M), `max_path_points` per
event (100 000), `max_sessions` (200 000), KMZ member bytes and member count. Reaching a limit
makes `coverage.complete=false` and names the limit.

## 3. Layers and storage (append-only)

| Layer | Storage | `metadata.signal` |
|---|---|---|
| Raw device/provider positions | `location_fixes` (bulk) + `location_fix_observations` (one row per export locator) | sample `signal_basis` `raw_sample` / `provider_inference` / `declared` |
| Raw non-positional signals | `location_signals` + `location_signal_observations` | `raw_sample` (Wi-Fi scan summary), `provider_inference` (activity record) |
| Sampling sessions (when a device recorded positions) | events `location_samples` | `passive`, `derived` (gap rule), `samples_basis: raw_sample` |
| Provider inferences | events `location_visit`, `location_segment`, `location_profile` | `passive`, `provider_inference`, or `provider_user_edit` when `editConfirmationStatus` is `CONFIRMED`/`EDITED` (flagged in `user_edit`) |
| Tracks, routes, waypoints | events `location_track` (`passive`, `raw_sample`), `location_route` / `location_waypoint` (`declared`) | statistics computed by code: points, segments, haversine distance inside segments, duration, time-order violations, bounding box |
| Our derivation | `location_presence_runs` / `location_presence_intervals`; optional events `place_presence` | `passive`, `derived` |

Provider confidences and probabilities are copied as **provider labels**
(`provider_labels`, `provider_probability`), never as our probability; `events.confidence` stays
`NULL`. Place ids of the provider (`placeId`) are kept raw (`resolution:
provider_identifier_kept_raw_never_resolved`) and never resolved online; provider place names
and addresses are kept bounded unless `provider_place_text=omit`.

**Streams and deduplication.** A stream is the device/account a sample belongs to:
`--stream KEY` when given, else `collection:<id>`, else `source:<id>`; a provider device tag is a
sub-stream (`<stream>/device:<tag>`). Samples are deduplicated by content identity **within one
stream** (SHA-256 of the stored representation: UTC instant — or the wall-clock/raw text when
no instant is fixed —, latitude/longitude at the stored precision, accuracy, altitude, speed,
heading, kind, basis, provider source, device tag). Two overlapping exports of one account in
one stream share each sample while both keep their observation rows; different streams are
never merged. Re-importing the same bytes with the same parameters reuses the import row,
samples, observations and events (each run still gets a `location_import_runs` row and an
audit entry).

**Precision.** `store_decimals` (recorded per import; default `null` = as the source states
it: 7 for E7, the text's decimals otherwise) rounds **before** hashing, so a coarse import never
stores a hash of the exact position. Display rounding (`display_decimals`) is separate and
never affects matching.

| Transformation (A03) | Kept | Lost | Added | Reversibility |
|---|---|---|---|---|
| export → samples | stated values at stored precision, locator per export, stated time with zone status | positions finer than `store_decimals`; raw extras of later exports of an identical sample (the first observing import keeps them); Wi-Fi device identifiers (only counts) | content identity, stream key | re-import from the preserved original (any precision, new parser version) |
| export → provider events | provider labels, place ids, bounded raw fields, user-edit flag | fields outside the whitelist (original keeps them) | time candidates, rounded locations | re-parse |
| samples → sessions / tracks | first/last sample, counts, bounding box, distance | per-sample detail in the event | sessions by a gap rule (recorded) | recomputed from samples |
| samples + places → presence | rule, parameters, place-set hash, input fingerprint, per-interval evidence | — | inside/uncertain classification, bounds | new run with other parameters; runs are append-only |

## 4. Private places and geofence matching

The places file is user data outside the repository (`--places`, `AGEDS_LOCATION_PLACES`, or
inline JSON over HTTP — a request never names a path):

```json
{"schema": "ageds.location-places.v1", "version": "my-2024-01",
 "places": [{"id": "home", "label": "…", "lat": 0.001, "lon": 0.002, "radius_m": 100, "kind": "home"}]}
```

Results record its canonical SHA-256, the file's byte hash and the declared version
(`location_place_sets`). `places-check` validates and lists overlapping pairs without labels.

**Rule `place_match.v1`** (`places.classify`): `d` = haversine distance to the centre (sphere
R = 6 371 008.8 m, recorded as `distance_model`), `R` = radius, `u` = `accuracy_m ×
accuracy_multiplier` + the rounding error of the stored coordinate (0 at source precision).
`inside` if `d + u ≤ R`, `outside` if `d − u > R`, else `uncertain` (the accuracy circle
overlaps the boundary). Missing accuracy uses `missing_accuracy_m` when given, else the point
itself (`accuracy_basis: not_stated`, reported). Antimeridian and poles need no special case
(tested).

**API for other areas.** `server.app.location.places.match_points(points, place_set,
params=MatchParams(), include_outside=False)` takes `{id, lat, lon, accuracy_m?, decimals?,
source_decimals?}` and returns per point the inside/uncertain places with distance,
uncertainty and margin (nothing is written). `places.register_geotag_source(key, label=,
signal_class=, basis=, loader=)` registers a provider of positioned points
(`loader(db, start_us, end_us, limit)` yielding `{t_us, lat, lon, accuracy_m?, decimals?,
ref}`); the location area registers `location_fixes`. The traces area (photo geotags) can
register its table there so the coordinator can wire geotags into presence later.

## 5. Presence (rule `place_presence.v1`)

Per stream, samples with a fixed UTC instant are taken in time order (default only
`signal_basis=raw_sample`) and classified against each place. A presence interval is a
maximal run of consecutive inside/uncertain samples of one place with neighbouring samples
at most `max_gap_s` apart; an outside sample or a longer gap ends it. Status `inside` needs
`min_inside_fixes` inside samples, else `uncertain`. Inner bounds = first/last matching
sample; outer bounds = the neighbouring samples of the stream (none at the data/window
edge, flagged `window_edge`). Intervals shorter than `min_dwell_s` are dropped and counted.

Parameters (presets, recorded with their hash): `min_dwell_s` 300, `max_gap_s` 3600,
`min_inside_fixes` 1, `accuracy_multiplier` 1, `missing_accuracy_m` null, `max_accuracy_m`
null, `signal_bases` [raw_sample], `fix_kinds` null, `streams` null, `merge_streams` false,
`max_fixes`, `max_intervals`. An identical recomputation (same set, parameters, input
fingerprint, result, window and publish flag) returns the stored run (`reused: true`).

`--publish` also writes one `place_presence` event per interval (no artifact; source
`location_presence`), keyed by rule, place-set hash, parameter hash, stream, place and the
interval, so the same interval is never duplicated and a different parameter set gives its
own events. Earlier events stay (append-only).

## 6. Timeline, lanes and correlations

The location events use the released display rule `display_time@1` unchanged: `start` is
chosen before `end` and `end` draws the duration bar, so visits, segments, tracks, sessions
and presence appear on `/timeline` with their intervals (lane grouping “type”). The location
page adds data-driven lanes ([`lanes.v1.json`](../server/app/location/data/lanes.v1.json):
presence, provider visits, travel, declared routes/points, sampling, legacy v1 presence),
filled through the analysis seam so every item shows its projected time and uncertainty
interval (`t_lo`/`t_hi`).

Correlation rules are data, pinned by SHA-256 like the timeline rules
([`presence_calendar.v1`](../server/app/location/rules/presence_calendar.v1.json),
[`presence_communication.v1`](../server/app/location/rules/presence_communication.v1.json)).
A run pairs the intervals of one presence run with calendar entries or calls/messages/e-mails
whose projected range lies within the interval ± `tolerance_s`, and stores per-criterion
evidence (inner overlap and gap, within outer bounds, calendar `LOCATION` text containing the
place label, time basis, `same_stream_hint: unknown`), ambiguity groups and unmatched lists —
**no probability**. Review is an append-only history (`accepted`/`rejected`/`revoked`, actor
self-declared). Nothing writes links or changes events. A device at a place during a meeting
does not show attendance.

## 7. Time budget and detection bias (shared edits)

* **Category rule `place`** (`analysis/categories.py`): `{"kind": "place", "values": ["home"],
  "weights": {"dom": 1}}` matches `place_presence` events by private place id; provenance names
  the rule as for every other kind. Analysis items carry `signal` and `place_ids`
  (`analysis/source.py`, additive keys).
* **`time_budget.v4`** (`analysis/budget.py`): a start–end range of an item whose
  `signal.basis` is `provider_inference`/`provider_user_edit` (provider travel segments and
  visits) or `derived` (place presence, sampling sessions) is an **estimate in `probable`**
  (roles `provider_inference` / `derived`), never confirmed `lower` time; items without
  `metadata.signal` are computed exactly as in v3. So presence shifts `probable` and `upper`,
  while `lower` stays confirmed-only and `nieobserwowane` keeps its meaning. The preset
  `excluded_event_types` now also lists `location_samples` (sampling sessions describe a
  device, not an activity; override per run).
* **Density default** (`analysis/preregistration.py`, `eventstudy.py`, method
  `ageds.event-study-method/v3`): new definitions get `analysis.density_excludes_signal_classes =
  ["passive"]`, so passive samples, sessions and presence never inflate documentation density.
  Definitions registered before lack the key and keep their stored behaviour (no class
  excluded; `parameters.density_signal_basis: legacy_definition_without_key`); re-registering the
  same raw definition now yields a new version (its normalized form gained the key). Results
  report `input.density_signal_exclusion`.

## 8. Import plan

Detector rules (`detectors.d/location.v1.json`): XML root `gpx` → `gpx_track`, root `kml`
(also `doc.kml` inside a catalogued KMZ) → `kml_track`, GeoJSON objects → `geojson_track`, a
JSON array of timeline segments → `location_history` (iOS), `.FIT` signature → `fit_track`.
Google Takeout layouts keep the base rules and the importer key `location_history`, which now
runs parser v2 (parameters `store_decimals`, `stream`); a plan item that still carries the v1
parameter `coarse_decimals` runs v1 unchanged (shared edit in `importplan/adapters.py`). Every
location adapter requires the item opt-in `enable_location`.

## 9. Map view and tiles

`/location` draws samples, tracks (consecutive samples of one stream ≤ 15 min apart), provider
visits/segments, declared points and place circles in a local equirectangular or Web-Mercator
projection (`static/location-math.mjs`, unit-tested in Node); a time slider maps the data
extent and links to `/timeline?from=&to=`; lanes show presence/visits/travel/sampling; data text
is set with `textContent` only. The page is served with `img-src 'self' data:` and
`connect-src 'self'`: by default the browser cannot contact any other server (the real-Chromium
script asserts zero external requests). Map tiles need an explicit opt-in: a template
`https://host/…/{z}/{x}/{y}…` plus the acknowledgement of the visible warning that tile requests
disclose the viewed area, IP address and time to that server; the opt-in is recorded in
`audit_log` (`location_tile_opt_in`) and only a page opened with `?tile_opt_in=<id>` allows that
one origin in its CSP.

## 10. Decisions and conflicts with the prompt

* v1 (`google_location` v1, coarse `presence` events) is kept callable and its rows are never
  reinterpreted; v2 adds separate rows. Both can coexist on the timeline (lane “legacy”).
* Sampling sessions are `derived` events (the span is our gap rule), not one event per fix.
* `location_fixes` and `location_signals` are excluded from metadata packages (bulk; rebuilt by
  re-import with the recorded parser version and parameters); imports, place sets, presence and
  correlation runs are exported.
* The released display rule was not versioned: lane grouping by event type already separates
  presence and travel, and a `display_time@2` would rebuild every projection. The lane config is
  data owned by this area.
* Missing accuracy is not invented: points are matched as stated and flagged unless the
  operator sets `missing_accuracy_m`.
* GPX times without an offset stay `unknown` by default although the GPX schema says UTC
  (many writers emit local time); the schema reading is an explicit per-run option.

## 11. Not done / boundaries

* No real export was available: layouts follow public observations; FIT decoding was exercised
  only with a fake `fitparse` module (the package is not installed here).
* No reverse geocoding, IP geolocation or address lookup exists; place names come only from the
  private places file (or the provider's own text, kept raw).
* KMZ reads one `.kml` member; network links, overlays and nested KMZ are not followed.
* Photo geotags of the traces area are not yet wired into presence (registry and `match_points`
  are the contract).
* Presence ignores samples without a fixed UTC instant; unknown-zone wall clocks are counted
  only in the import.
* `/timeline` has no location-specific lane grouping; the location page shows the lanes.
* The page has not opted in to the light theme (like `/timeline`).

## Photo geotags as a point source (integration 2026-10-21)

`server/app/location/sources.py` registers the traces area's photo geotags (EXIF GPS and Google
Photos sidecar `geoData`/`geoDataExif`, stored only under `enable_location`) as the point source
`traces.photo_geotags`. `places-match-source --source traces.photo_geotags --places PATH
[--from --to]` classifies each timed geotag against the private places (rule
`place_match_source.v1`, accuracy-aware like `place_match.v1`) and returns the matches with the
place-set hash. It is a read-only projection: nothing is stored, and presence runs (`presence-run`)
still use `location_fixes` only. A photo geotag is a file or provider claim about a camera position at
an instant; sidecar `geoData` keeps its basis `provider_inference_or_user_edit` per point.

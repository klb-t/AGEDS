# Digital traces wave — plan (2026-10-17)

User request (2026-10-17): add location history (Google and other map apps) for
timeline reconstruction, and every other digital trace a person leaves, starting
with those almost everyone has. Base: `ccr-d25c3cb3-qgbhql` after the
2026-10-16 increment ([HANDOFF](../../HANDOFF.md)).

## Priority (prevalence × value for the timeline × cost)

| Rank | Trace | Almost everyone has it? | Value | Track |
|---|---|---|---|---|
| 1 | Photo/video metadata (EXIF time, GPS; Google Photos Takeout sidecars) | yes | high: time + place, often the only trace of a day | L2 (+ L1 store) |
| 2 | Location history: Google (legacy Takeout `Records.json`, Semantic Location History, on-device Timeline export Android/iOS), GPX/KML/KMZ/GeoJSON tracks from other map/sport apps | most smartphone users | high: presence and travel | L1 |
| 3 | Messaging/social data downloads: Facebook/Messenger, Instagram, Telegram, Google Chat, X/Twitter, LinkedIn | most | high: communication time | L2 |
| 4 | Account security/login activity (Google, Facebook, Instagram): time, IP, device | most | medium: presence of a session, device | L2 |
| 5 | Document metadata (DOCX/XLSX/PPTX, ODF, PDF Info/XMP): created/modified/author claims | yes | medium: authorship/edit time claims | L2 |
| 6 | Bank/card statements (CSV profiles, CAMT.053, MT940) | yes | high: time (often day only), place via merchant | L3 |
| 7 | Health/fitness (Apple Health, Google Fit, Samsung Health, Fitbit): sleep, workouts, steps | many (special category data) | high for time budget (sleep/rest) | L3 |
| 8 | Transport and rides (public-transport card histories, ride-hailing trip CSVs) | many | medium: travel segments | L3 |
| 9 | Notes (Google Keep), local browser profiles (Chrome `History`, Firefox `places.sqlite`) | many | medium: activity signal | L2 |
| — | Already present: SMS, call logs, WhatsApp, e-mail, calendar, contacts, My Activity, Chrome/YouTube via Takeout | | | done |

Not reachable without root/forensic tools and therefore out of scope: device
usage statistics (Digital Wellbeing), Wi-Fi/cell logs, Signal (encrypted backup),
Apple Significant Locations.

## Shared contract

- **Layers.** Raw device/provider samples (a position fix with accuracy, a health
  sample) are *observations of a device or account*, not of a person. Provider
  inferences (Google's visits and activity types, a fitness app's sleep stages,
  a bank's merchant category) are **third-party model output**: stored with the
  provider's own confidence as a label (never as our probability), separately
  from raw samples. User edits recorded by the provider (e.g. Timeline
  `editConfirmationStatus`) are flagged as such. Our own derivations (place
  presence from geofences) are projections with a versioned rule.
- **Event metadata.** `metadata.signal = {"class": "passive"|"active"|"declared",
  "basis": "raw_sample"|"provider_inference"|"provider_user_edit"|"derived",
  "provider": "...", "device_or_account": "..."}`. Passive high-frequency samples
  live in area tables, not one event each; the timeline gets aggregated events
  (visits, segments, sessions).
- **Time.** Every timestamp through `timeinfo` with kind/zone status/precision;
  bank dates are `precision='day'` with booking/value/transaction kinds;
  provider offsets kept explicit.
- **Privacy defaults.** Location, health and browser history require a per-run
  opt-in recorded in metadata (existing pattern). Processing is local. No reverse
  geocoding, IP geolocation or map tiles by default: each is an explicit opt-in
  because it sends coordinates/IPs to a third party. Place names come from a
  private places file (geofences) outside the repository.
- **Precision.** Stored precision is a recorded parameter (the user decides);
  geofence matching runs at source precision and stores the match, distance and
  accuracy, so coarse display does not lose place presence.
- **Detection bias.** Passive sources change documentation density by orders of
  magnitude; analyses exclude `signal.class=passive` from density controls by
  default (a recorded parameter) and timelines show them in their own lanes.
- **Extension points.** Importers register in `importplan/adapters_ext/<area>.py`
  and `importplan/data/detectors.d/<area>.v1.json`; migrations in
  `schema/{location,traces,ledger}.py` (100–129); routers/commands in
  `api|commands/{location,traces,ledger}.py`; corpus parts in
  `server/tests/corpus/`; new tables classified in `server/app/packages.py`.

## Tasks

| ID | Scope |
|---|---|
| AGEDS-TASK-20261017-F1 | Coordinator: detector/importer extension points, area registrations, this plan. |
| AGEDS-TASK-20261017-L1 | Location: all Google formats incl. raw signals and iOS export, GPX/KML/KMZ/GeoJSON, optional FIT; location fixes store; private places + local geofence presence; presence/travel lanes; map view without external tiles; correlations (presence ↔ calendar/call/e-mail); place rules for time-budget labelling; passive-density default in analyses. |
| AGEDS-TASK-20261017-L2 | Personal-data exports: Google Photos sidecars + EXIF GPS; Facebook/Messenger/Instagram (incl. mojibake repair as a separate decoded layer), Telegram, Google Chat, X, LinkedIn; login/security activity; document metadata; Google Keep; local browser profiles (opt-in). |
| AGEDS-TASK-20261017-L3 | Ledger: generic tabular profiles (bank CSV, transport, rides), CAMT.053, MT940; health/fitness exports (opt-in, special category); sleep/workout sessions for the time budget. |

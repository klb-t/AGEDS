"""Additive schema owned by the traces area (personal-data exports and document metadata); see ``schema/__init__.py``.

Versions 110–114 (AGEDS-TASK-20261017-L2):

* ``trace_photo_sidecars`` — one row per parsed Google Photos Takeout sidecar
  (title, raw time objects, description, third-party ``people`` names kept raw and
  never indexed, url, geo *presence*). Coordinates never live here.
* ``trace_photo_matches`` — sidecar → media file matching as a hypothesis: every
  candidate with the rule id that produced it, over a hashed candidate scope.
* ``trace_geotags`` — photo geotags (EXIF GPS, sidecar ``geoData`` and
  ``geoDataExif`` kept side by side). Coordinates are stored only under the
  per-run ``enable_location`` opt-in; without it a row records presence only.
* ``trace_document_metadata`` — OOXML/ODF/PDF metadata per artifact: authors and
  tools as claims, dates raw with parse status.
* ``trace_browser_visits`` — local browser profile visits (opt-in), one row per
  visit; the timeline gets aggregated ``browser_session`` events.

All tables are append-only: re-observing identical values reuses a row, a
changed interpretation is a new parser version.
"""
_APPEND_ONLY = '''
CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
  BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
  BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
'''

MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (110, 'trace_photo_sidecars_v1', r'''
CREATE TABLE IF NOT EXISTS trace_photo_sidecars (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  event_id INTEGER REFERENCES events(id),
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  sidecar_name TEXT NOT NULL,
  name_variant TEXT NOT NULL,
  title TEXT,
  description TEXT,
  people_json TEXT NOT NULL DEFAULT '[]',
  url TEXT,
  times_json TEXT NOT NULL DEFAULT '{}',
  geo_json TEXT NOT NULL DEFAULT '{}',
  raw_json TEXT NOT NULL DEFAULT '{}',
  issues_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(artifact_id, parser, parser_version)
);
CREATE INDEX IF NOT EXISTS idx_trace_photo_sidecars_title ON trace_photo_sidecars(title);
CREATE TABLE IF NOT EXISTS trace_photo_matches (
  id INTEGER PRIMARY KEY,
  sidecar_id INTEGER NOT NULL REFERENCES trace_photo_sidecars(id),
  status TEXT NOT NULL CHECK(status IN ('single_candidate','ambiguous','no_candidate')),
  scope TEXT NOT NULL,
  scope_sha256 TEXT NOT NULL,
  rules_version TEXT NOT NULL,
  candidates_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(sidecar_id, scope_sha256, rules_version)
);
CREATE INDEX IF NOT EXISTS idx_trace_photo_matches_sidecar ON trace_photo_matches(sidecar_id, id);
''' + _APPEND_ONLY.format(table='trace_photo_sidecars') + _APPEND_ONLY.format(table='trace_photo_matches')),
    (111, 'trace_geotags_v1', r'''
CREATE TABLE IF NOT EXISTS trace_geotags (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  media_sha256 TEXT,
  sidecar_id INTEGER REFERENCES trace_photo_sidecars(id),
  event_id INTEGER REFERENCES events(id),
  source_kind TEXT NOT NULL CHECK(source_kind IN ('exif_gps','takeout_geoData','takeout_geoDataExif')),
  basis TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('stored','present_not_stored','zero_placeholder','invalid')),
  coordinates_stored INTEGER NOT NULL CHECK(coordinates_stored IN (0,1)),
  stored_precision TEXT NOT NULL,
  lat REAL,
  lon REAL,
  alt REAL,
  time_utc TEXT,
  time_raw TEXT,
  time_basis TEXT,
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  opt_in_json TEXT NOT NULL DEFAULT '{}',
  raw_json TEXT NOT NULL DEFAULT '{}',
  issues_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK((coordinates_stored = 1) = (lat IS NOT NULL AND lon IS NOT NULL)),
  CHECK((coordinates_stored = 1) = (status = 'stored')),
  UNIQUE(artifact_id, source_kind, parser, parser_version, stored_precision)
);
CREATE INDEX IF NOT EXISTS idx_trace_geotags_time ON trace_geotags(time_utc);
CREATE INDEX IF NOT EXISTS idx_trace_geotags_media ON trace_geotags(media_sha256);
''' + _APPEND_ONLY.format(table='trace_geotags')),
    (112, 'trace_document_metadata_v1', r'''
CREATE TABLE IF NOT EXISTS trace_document_metadata (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  event_id INTEGER REFERENCES events(id),
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  format TEXT NOT NULL CHECK(format IN ('ooxml','odf','pdf')),
  status TEXT NOT NULL CHECK(status IN ('parsed','partial','no_metadata','unreadable')),
  claims_json TEXT NOT NULL DEFAULT '{}',
  times_json TEXT NOT NULL DEFAULT '{}',
  tools_json TEXT NOT NULL DEFAULT '{}',
  raw_json TEXT NOT NULL DEFAULT '{}',
  issues_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(artifact_id, parser, parser_version)
);
''' + _APPEND_ONLY.format(table='trace_document_metadata')),
    (113, 'trace_browser_visits_v1', r'''
CREATE TABLE IF NOT EXISTS trace_browser_visits (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  browser TEXT NOT NULL CHECK(browser IN ('chromium','firefox')),
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  reading_key TEXT NOT NULL,
  visit_id INTEGER NOT NULL,
  url TEXT,
  url_truncated INTEGER NOT NULL DEFAULT 0,
  title TEXT,
  visit_time_raw INTEGER,
  visit_utc TEXT,
  epoch TEXT NOT NULL CHECK(epoch IN ('webkit_1601_us','prtime_1970_us')),
  time_status TEXT NOT NULL,
  transition_raw INTEGER,
  from_visit INTEGER,
  duration_us INTEGER,
  opt_in_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(artifact_id, parser, parser_version, reading_key, visit_id)
);
CREATE INDEX IF NOT EXISTS idx_trace_browser_visits_time ON trace_browser_visits(visit_utc);
''' + _APPEND_ONLY.format(table='trace_browser_visits')),
)

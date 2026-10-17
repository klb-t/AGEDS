"""Additive schema owned by the location area (location fixes, user places and presence); see ``schema/__init__.py``.

Versions 100–109. Every table is append-only (triggers refuse UPDATE and DELETE):

* 100 — ``location_imports`` (one row per artifact x importer x parser version x
  parameters) with one ``location_import_runs`` row per execution, raw samples
  ``location_fixes`` (positions) and ``location_signals``
  (non-positional raw signals: Wi-Fi scans, activity records), each deduplicated by
  content identity per stream, with one observation row per export locator.
* 101 — private place sets (hashed, versioned), presence runs (rule version,
  parameters, input fingerprint) and their presence intervals.
* 102 — correlation runs between presence and other events; hypotheses with
  per-criterion evidence (no probability) and an append-only review history.
"""


def _append_only(*tables: str) -> str:
    return ''.join(f'''
CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} BEGIN
  SELECT RAISE(ABORT, '{table} is append-only');
END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} BEGIN
  SELECT RAISE(ABORT, '{table} is append-only');
END;
''' for table in tables)


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (100, 'location_imports_fixes', '''
CREATE TABLE IF NOT EXISTS location_imports (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  importer TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  format TEXT NOT NULL,
  stream_key TEXT NOT NULL,
  params_json TEXT NOT NULL,
  params_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(artifact_id, importer, parser_version, params_sha256)
);

CREATE TABLE IF NOT EXISTS location_import_runs (
  id INTEGER PRIMARY KEY,
  import_id INTEGER NOT NULL REFERENCES location_imports(id),
  counts_json TEXT NOT NULL,
  coverage_json TEXT NOT NULL,
  issues_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS location_fixes (
  id INTEGER PRIMARY KEY,
  stream_key TEXT NOT NULL,
  fix_sha256 TEXT NOT NULL,
  decimals INTEGER NOT NULL CHECK(decimals BETWEEN 0 AND 9),
  source_decimals INTEGER NOT NULL CHECK(source_decimals BETWEEN 0 AND 9),
  t_us INTEGER,
  time_raw TEXT,
  time_utc TEXT,
  time_local TEXT,
  time_zone TEXT,
  tz_status TEXT NOT NULL,
  time_precision TEXT NOT NULL,
  lat REAL NOT NULL CHECK(lat BETWEEN -90 AND 90),
  lon REAL NOT NULL CHECK(lon BETWEEN -180 AND 180),
  accuracy_m REAL,
  altitude_m REAL,
  vertical_accuracy_m REAL,
  speed_mps REAL,
  heading_deg REAL,
  fix_kind TEXT NOT NULL,
  signal_basis TEXT NOT NULL CHECK(signal_basis IN ('raw_sample','provider_inference','provider_user_edit','declared')),
  provider_source TEXT,
  device_tag TEXT,
  extra_json TEXT NOT NULL DEFAULT '{}',
  first_import_id INTEGER NOT NULL REFERENCES location_imports(id),
  UNIQUE(stream_key, fix_sha256, decimals)
);
CREATE INDEX IF NOT EXISTS idx_location_fixes_time ON location_fixes(t_us);
CREATE INDEX IF NOT EXISTS idx_location_fixes_stream_time ON location_fixes(stream_key, t_us);

CREATE TABLE IF NOT EXISTS location_fix_observations (
  import_id INTEGER NOT NULL REFERENCES location_imports(id),
  locator TEXT NOT NULL,
  fix_id INTEGER NOT NULL REFERENCES location_fixes(id),
  PRIMARY KEY(import_id, locator)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_location_fix_observations_fix ON location_fix_observations(fix_id);

CREATE TABLE IF NOT EXISTS location_signals (
  id INTEGER PRIMARY KEY,
  stream_key TEXT NOT NULL,
  signal_sha256 TEXT NOT NULL,
  signal_kind TEXT NOT NULL,
  signal_basis TEXT NOT NULL CHECK(signal_basis IN ('raw_sample','provider_inference')),
  t_us INTEGER,
  time_raw TEXT,
  time_utc TEXT,
  tz_status TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  first_import_id INTEGER NOT NULL REFERENCES location_imports(id),
  UNIQUE(stream_key, signal_sha256)
);
CREATE INDEX IF NOT EXISTS idx_location_signals_time ON location_signals(stream_key, t_us);

CREATE TABLE IF NOT EXISTS location_signal_observations (
  import_id INTEGER NOT NULL REFERENCES location_imports(id),
  locator TEXT NOT NULL,
  signal_id INTEGER NOT NULL REFERENCES location_signals(id),
  PRIMARY KEY(import_id, locator)
) WITHOUT ROWID;
''' + _append_only('location_imports', 'location_import_runs', 'location_fixes', 'location_fix_observations', 'location_signals',
                   'location_signal_observations')),
    (101, 'location_places_presence', '''
CREATE TABLE IF NOT EXISTS location_place_sets (
  id INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE,
  schema_id TEXT NOT NULL,
  version TEXT NOT NULL,
  origin TEXT NOT NULL,
  file_sha256 TEXT,
  places_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS location_presence_runs (
  id INTEGER PRIMARY KEY,
  rule TEXT NOT NULL,
  place_set_id INTEGER NOT NULL REFERENCES location_place_sets(id),
  params_json TEXT NOT NULL,
  params_sha256 TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  window_json TEXT NOT NULL,
  counts_json TEXT NOT NULL,
  coverage_json TEXT NOT NULL,
  result_sha256 TEXT NOT NULL,
  published INTEGER NOT NULL CHECK(published IN (0,1)),
  code_version TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_location_presence_runs_identity
  ON location_presence_runs(place_set_id, params_sha256, input_sha256, result_sha256, published);

CREATE TABLE IF NOT EXISTS location_presence_intervals (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES location_presence_runs(id),
  place_id TEXT NOT NULL,
  stream_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('inside','uncertain')),
  start_us INTEGER NOT NULL,
  end_us INTEGER NOT NULL,
  outer_start_us INTEGER,
  outer_end_us INTEGER,
  fixes INTEGER NOT NULL,
  inside_fixes INTEGER NOT NULL,
  uncertain_fixes INTEGER NOT NULL,
  min_distance_m REAL,
  max_uncertainty_m REAL,
  evidence_json TEXT NOT NULL,
  event_id INTEGER REFERENCES events(id),
  CHECK(end_us >= start_us)
);
CREATE INDEX IF NOT EXISTS idx_location_presence_intervals_run ON location_presence_intervals(run_id, start_us);
''' + _append_only('location_place_sets', 'location_presence_runs', 'location_presence_intervals')),
    (102, 'location_correlations', '''
CREATE TABLE IF NOT EXISTS location_correlation_runs (
  id INTEGER PRIMARY KEY,
  rule_id TEXT NOT NULL,
  rule_version INTEGER NOT NULL,
  rule_sha256 TEXT NOT NULL,
  presence_run_id INTEGER NOT NULL REFERENCES location_presence_runs(id),
  params_json TEXT NOT NULL,
  source_rule TEXT,
  source_rule_sha256 TEXT,
  counts_json TEXT NOT NULL,
  unmatched_json TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS location_correlation_hypotheses (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES location_correlation_runs(id),
  interval_id INTEGER NOT NULL REFERENCES location_presence_intervals(id),
  event_id INTEGER NOT NULL REFERENCES events(id),
  relation TEXT NOT NULL,
  ambiguity_group TEXT NOT NULL,
  candidate_count INTEGER NOT NULL,
  evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_location_correlation_hypotheses_run ON location_correlation_hypotheses(run_id);

CREATE TABLE IF NOT EXISTS location_correlation_status (
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES location_correlation_hypotheses(id),
  status TEXT NOT NULL CHECK(status IN ('accepted','rejected','revoked')),
  actor TEXT NOT NULL,
  actor_verified INTEGER NOT NULL DEFAULT 0 CHECK(actor_verified = 0),
  reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
''' + _append_only('location_correlation_runs', 'location_correlation_hypotheses', 'location_correlation_status')),
)

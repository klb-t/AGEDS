"""Collections and the source catalog.

A collection is a private, user-described acquisition batch (device, account,
export). Its labels are case data stored only in the local database.
``source_observations`` stays append-only: the collection is fixed when the
acquisition row is written (v10).

The catalog (v11+) records *scan observations*: what a read-only walk saw at a
virtual locator (file or archive member) — size, hash, signature, archive header
times and limits. A catalog entry is not an acquisition: no bytes are copied.
Ingest from the catalog is a separate, explicit acquisition that writes a normal
``source_observations`` row and links it back here (``catalog_entry_artifacts``).

Entries are immutable once their scan has finished; a re-scan creates a new scan
and new entries. Content groups are keyed by (case, sha256) and are shared by
every scan, so re-scanning never duplicates a group.
"""
MIGRATIONS = (
    (10, 'collections_v1', r'''
CREATE TABLE IF NOT EXISTS collections (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  parent_collection_id INTEGER REFERENCES collections(id),
  label TEXT NOT NULL,
  device_or_account TEXT,
  export_date_raw TEXT,
  acquisition_method TEXT,
  operator TEXT,
  notes TEXT,
  priority INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_collections_case ON collections(case_id, id);
ALTER TABLE source_observations ADD COLUMN collection_id INTEGER REFERENCES collections(id);
CREATE INDEX IF NOT EXISTS idx_observations_collection ON source_observations(collection_id, id);
'''),
    (11, 'catalog_scans_entries_v1', r'''
CREATE TABLE IF NOT EXISTS catalog_scans (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  collection_id INTEGER NOT NULL REFERENCES collections(id),
  root_path TEXT NOT NULL,
  root_kind TEXT NOT NULL CHECK(root_kind IN ('directory','file')),
  status TEXT NOT NULL CHECK(status IN ('running','completed','partial','failed')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  catalog_version TEXT NOT NULL,
  signature_table_version TEXT NOT NULL,
  scanner_version TEXT NOT NULL,
  limits_json TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  adapters_json TEXT NOT NULL DEFAULT '{}',
  coverage_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalog_scans_collection ON catalog_scans(collection_id, root_path, id);

CREATE TABLE IF NOT EXISTS catalog_content_groups (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  sha256 TEXT NOT NULL,
  size_bytes INTEGER,
  first_scan_id INTEGER REFERENCES catalog_scans(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(case_id, sha256)
);

CREATE TABLE IF NOT EXISTS catalog_entries (
  id INTEGER PRIMARY KEY,
  scan_id INTEGER NOT NULL REFERENCES catalog_scans(id),
  collection_id INTEGER NOT NULL REFERENCES collections(id),
  parent_entry_id INTEGER REFERENCES catalog_entries(id),
  locator TEXT NOT NULL,
  depth INTEGER NOT NULL CHECK(depth >= 0),
  origin TEXT NOT NULL,
  member_index INTEGER,
  name TEXT NOT NULL,
  name_raw_hex TEXT,
  name_encoding TEXT,
  name_flags_json TEXT NOT NULL DEFAULT '[]',
  extension TEXT,
  extension_type TEXT,
  sniffed_type TEXT,
  sniffed_mime TEXT,
  sniff_basis TEXT NOT NULL DEFAULT 'none',
  sniff_rule TEXT,
  type_relation TEXT NOT NULL,
  type_conflict INTEGER NOT NULL DEFAULT 0 CHECK(type_conflict IN (0,1)),
  family TEXT,
  size_bytes INTEGER,
  compressed_size INTEGER,
  sha256 TEXT,
  hash_status TEXT NOT NULL,
  content_group_id INTEGER REFERENCES catalog_content_groups(id),
  is_container INTEGER NOT NULL DEFAULT 0 CHECK(is_container IN (0,1)),
  status TEXT NOT NULL CHECK(status IN ('ok','unsupported','damaged','limit_exceeded','encrypted','skipped','unreadable','unstable')),
  issue_code TEXT,
  issues_json TEXT NOT NULL DEFAULT '[]',
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(scan_id, locator)
);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_scan ON catalog_entries(scan_id, id);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_collection ON catalog_entries(collection_id, id);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_sha ON catalog_entries(sha256);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_group ON catalog_entries(content_group_id, id);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_name ON catalog_entries(name);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_parent ON catalog_entries(parent_entry_id);

CREATE TABLE IF NOT EXISTS catalog_entry_times (
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  raw TEXT,
  utc TEXT,
  local TEXT,
  tz_status TEXT NOT NULL CHECK(tz_status IN ('explicit_offset','utc_epoch','assumed_zone','unknown')),
  tz TEXT,
  precision TEXT NOT NULL DEFAULT 'unknown',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_catalog_times_entry ON catalog_entry_times(entry_id);
CREATE INDEX IF NOT EXISTS idx_catalog_times_basis ON catalog_entry_times(kind, source, tz_status);

CREATE TABLE IF NOT EXISTS catalog_issues (
  id INTEGER PRIMARY KEY,
  scan_id INTEGER NOT NULL REFERENCES catalog_scans(id),
  entry_id INTEGER REFERENCES catalog_entries(id),
  code TEXT NOT NULL,
  locator TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalog_issues_scan ON catalog_issues(scan_id, id);

CREATE TRIGGER IF NOT EXISTS catalog_scans_no_delete
  BEFORE DELETE ON catalog_scans BEGIN
    SELECT RAISE(ABORT, 'catalog scans are retained');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_scans_frozen
  BEFORE UPDATE ON catalog_scans WHEN OLD.status <> 'running' BEGIN
    SELECT RAISE(ABORT, 'a finished catalog scan is immutable');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_entries_no_delete
  BEFORE DELETE ON catalog_entries BEGIN
    SELECT RAISE(ABORT, 'catalog entries are retained');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_entries_frozen
  BEFORE UPDATE ON catalog_entries
  WHEN (SELECT status FROM catalog_scans WHERE id=OLD.scan_id) <> 'running' BEGIN
    SELECT RAISE(ABORT, 'entries of a finished catalog scan are immutable');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_groups_no_update
  BEFORE UPDATE ON catalog_content_groups BEGIN
    SELECT RAISE(ABORT, 'content groups are immutable');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_groups_no_delete
  BEFORE DELETE ON catalog_content_groups BEGIN
    SELECT RAISE(ABORT, 'content groups are immutable');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_times_no_update
  BEFORE UPDATE ON catalog_entry_times BEGIN
    SELECT RAISE(ABORT, 'catalog time observations are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_times_no_delete
  BEFORE DELETE ON catalog_entry_times BEGIN
    SELECT RAISE(ABORT, 'catalog time observations are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_issues_no_update
  BEFORE UPDATE ON catalog_issues BEGIN
    SELECT RAISE(ABORT, 'catalog issues are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_issues_no_delete
  BEFORE DELETE ON catalog_issues BEGIN
    SELECT RAISE(ABORT, 'catalog issues are append-only');
  END;
'''),
    (12, 'catalog_links_marks_v1', r'''
CREATE TABLE IF NOT EXISTS catalog_entry_artifacts (
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  observation_id INTEGER REFERENCES source_observations(id),
  source_locator TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalog_entry_artifacts_entry ON catalog_entry_artifacts(entry_id, id);
CREATE INDEX IF NOT EXISTS idx_catalog_entry_artifacts_artifact ON catalog_entry_artifacts(artifact_id, id);
CREATE TRIGGER IF NOT EXISTS catalog_entry_artifacts_no_update
  BEFORE UPDATE ON catalog_entry_artifacts BEGIN
    SELECT RAISE(ABORT, 'catalog ingest links are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_entry_artifacts_no_delete
  BEFORE DELETE ON catalog_entry_artifacts BEGIN
    SELECT RAISE(ABORT, 'catalog ingest links are append-only');
  END;

-- Human marks are keyed by (collection, locator) so they survive re-scans.
-- Each change appends a row; the latest row per key is the current mark.
CREATE TABLE IF NOT EXISTS catalog_marks (
  id INTEGER PRIMARY KEY,
  collection_id INTEGER NOT NULL REFERENCES collections(id),
  locator TEXT NOT NULL,
  priority INTEGER,
  note TEXT,
  actor TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalog_marks_key ON catalog_marks(collection_id, locator, id);
CREATE TRIGGER IF NOT EXISTS catalog_marks_no_update
  BEFORE UPDATE ON catalog_marks BEGIN
    SELECT RAISE(ABORT, 'catalog marks are append-only; add a new mark');
  END;
CREATE TRIGGER IF NOT EXISTS catalog_marks_no_delete
  BEFORE DELETE ON catalog_marks BEGIN
    SELECT RAISE(ABORT, 'catalog marks are append-only; add a new mark');
  END;
'''),
    (13, 'catalog_descriptions_v1', r'''
CREATE TABLE IF NOT EXISTS catalog_profiles (
  id INTEGER PRIMARY KEY,
  collection_id INTEGER NOT NULL REFERENCES collections(id),
  scan_ids_json TEXT NOT NULL,
  parameters_json TEXT NOT NULL DEFAULT '{}',
  profile_json TEXT NOT NULL,
  profile_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalog_profiles_collection ON catalog_profiles(collection_id, id);

-- One row per explain attempt; a later attempt is a new run, never an overwrite.
CREATE TABLE IF NOT EXISTS catalog_description_runs (
  id INTEGER PRIMARY KEY,
  run_uid TEXT NOT NULL UNIQUE,
  collection_id INTEGER NOT NULL REFERENCES collections(id),
  profile_id INTEGER NOT NULL REFERENCES catalog_profiles(id),
  provider TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT 'unknown',
  model_version TEXT NOT NULL DEFAULT 'unknown',
  params_json TEXT NOT NULL DEFAULT '{}',
  external INTEGER NOT NULL DEFAULT 0 CHECK(external IN (0,1)),
  opt_in_json TEXT NOT NULL DEFAULT '{}',
  prompt_template_version TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  input_profile_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('completed','failed')),
  error TEXT,
  sidecar_path TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_annotations (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES catalog_description_runs(id),
  collection_id INTEGER NOT NULL REFERENCES collections(id),
  target_kind TEXT NOT NULL,
  target_id INTEGER,
  model_text TEXT NOT NULL,
  resolved_text TEXT NOT NULL,
  placeholders_json TEXT NOT NULL DEFAULT '{}',
  warnings_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'hypothesis' CHECK(status = 'hypothesis'),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalog_annotations_collection ON catalog_annotations(collection_id, id);

CREATE TABLE IF NOT EXISTS catalog_annotation_decisions (
  id INTEGER PRIMARY KEY,
  annotation_id INTEGER NOT NULL REFERENCES catalog_annotations(id),
  decision TEXT NOT NULL CHECK(decision IN ('promoted','rejected')),
  actor TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalog_decisions_annotation ON catalog_annotation_decisions(annotation_id, id);

CREATE TRIGGER IF NOT EXISTS catalog_profiles_no_update BEFORE UPDATE ON catalog_profiles BEGIN
  SELECT RAISE(ABORT, 'catalog profiles are append-only'); END;
CREATE TRIGGER IF NOT EXISTS catalog_profiles_no_delete BEFORE DELETE ON catalog_profiles BEGIN
  SELECT RAISE(ABORT, 'catalog profiles are append-only'); END;
CREATE TRIGGER IF NOT EXISTS catalog_runs_no_update BEFORE UPDATE ON catalog_description_runs BEGIN
  SELECT RAISE(ABORT, 'description runs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS catalog_runs_no_delete BEFORE DELETE ON catalog_description_runs BEGIN
  SELECT RAISE(ABORT, 'description runs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS catalog_annotations_no_update BEFORE UPDATE ON catalog_annotations BEGIN
  SELECT RAISE(ABORT, 'annotations are append-only; record a decision'); END;
CREATE TRIGGER IF NOT EXISTS catalog_annotations_no_delete BEFORE DELETE ON catalog_annotations BEGIN
  SELECT RAISE(ABORT, 'annotations are append-only; record a decision'); END;
CREATE TRIGGER IF NOT EXISTS catalog_decisions_no_update BEFORE UPDATE ON catalog_annotation_decisions BEGIN
  SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS catalog_decisions_no_delete BEFORE DELETE ON catalog_annotation_decisions BEGIN
  SELECT RAISE(ABORT, 'decisions are append-only'); END;
'''),
)

"""Import records: observed time candidates and logical record identity.

``event_times`` keeps every timestamp an importer observed for an event, with
its kind, raw text, zone status and precision. A display/sort choice between
candidates is a versioned projection rule (timeline area), never an overwrite.

Versions 21–23 (chat importers, logical records across exports):

* ``record_exports`` names one logical export (a set of files) per namespace;
  ``record_export_files`` lists its ingested artifacts (shard index, role);
  ``record_export_parses`` keeps each distinct parse result (interpretation,
  limits, coverage, bounded issues). Identical re-parses reuse their row.
* ``logical_records`` is the identity (namespace, container id, record id);
  ``logical_occurrences`` binds it to an export, artifact, event, JSON locator,
  interpretation and a versioned content/structure hash. Absence from an export
  is computed by the report as "not observed", never stored as deletion.
* ``chat_attachment_refs`` keeps every attachment reference and how it was (or
  was not) matched against the files available in that import.

All tables are append-only; a changed interpretation is a new row.
"""
MIGRATIONS = (
    (20, 'event_time_candidates_v1', r'''
CREATE TABLE IF NOT EXISTS event_times (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  kind TEXT NOT NULL,
  raw TEXT,
  utc TEXT,
  local TEXT,
  tz_status TEXT NOT NULL CHECK(tz_status IN ('explicit_offset','utc_epoch','assumed_zone','unknown')),
  tz TEXT,
  precision TEXT NOT NULL DEFAULT 'unknown',
  source TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(event_id, kind, source, raw)
);
CREATE INDEX IF NOT EXISTS idx_event_times_utc ON event_times(utc);
CREATE INDEX IF NOT EXISTS idx_event_times_local ON event_times(local);
CREATE INDEX IF NOT EXISTS idx_event_times_event ON event_times(event_id, id);
CREATE TRIGGER IF NOT EXISTS event_times_no_update
  BEFORE UPDATE ON event_times BEGIN
    SELECT RAISE(ABORT, 'event time candidates are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS event_times_no_delete
  BEFORE DELETE ON event_times BEGIN
    SELECT RAISE(ABORT, 'event time candidates are append-only');
  END;
'''),
    (21, 'record_exports_v1', r'''
CREATE TABLE IF NOT EXISTS record_exports (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  namespace TEXT NOT NULL,
  label TEXT NOT NULL,
  label_origin TEXT NOT NULL CHECK(label_origin IN ('user','derived_from_file_hashes')),
  collection_id INTEGER REFERENCES collections(id),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(case_id, namespace, label)
);
CREATE TABLE IF NOT EXISTS record_export_files (
  id INTEGER PRIMARY KEY,
  export_id INTEGER NOT NULL REFERENCES record_exports(id),
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  file_role TEXT NOT NULL,
  shard_index INTEGER,
  file_name TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(export_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS idx_record_export_files_artifact ON record_export_files(artifact_id, export_id);
CREATE TABLE IF NOT EXISTS record_export_parses (
  id INTEGER PRIMARY KEY,
  export_file_id INTEGER NOT NULL REFERENCES record_export_files(id),
  interpretation TEXT NOT NULL,
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  mapping_version TEXT,
  mapping_sha256 TEXT,
  normalization_version TEXT NOT NULL,
  limits_json TEXT NOT NULL DEFAULT '{}',
  coverage TEXT NOT NULL CHECK(coverage IN ('complete','partial','failed')),
  stats_json TEXT NOT NULL DEFAULT '{}',
  issues_json TEXT NOT NULL DEFAULT '[]',
  result_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(export_file_id, interpretation, result_sha256)
);
CREATE TRIGGER IF NOT EXISTS record_exports_no_update
  BEFORE UPDATE ON record_exports BEGIN
    SELECT RAISE(ABORT, 'record_exports rows are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS record_exports_no_delete
  BEFORE DELETE ON record_exports BEGIN
    SELECT RAISE(ABORT, 'record_exports rows are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS record_export_files_no_update
  BEFORE UPDATE ON record_export_files BEGIN
    SELECT RAISE(ABORT, 'record_export_files rows are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS record_export_files_no_delete
  BEFORE DELETE ON record_export_files BEGIN
    SELECT RAISE(ABORT, 'record_export_files rows are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS record_export_parses_no_update
  BEFORE UPDATE ON record_export_parses BEGIN
    SELECT RAISE(ABORT, 'record_export_parses rows are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS record_export_parses_no_delete
  BEFORE DELETE ON record_export_parses BEGIN
    SELECT RAISE(ABORT, 'record_export_parses rows are append-only');
  END;
'''),
    (22, 'logical_records_v1', r'''
CREATE TABLE IF NOT EXISTS logical_records (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  namespace TEXT NOT NULL,
  record_kind TEXT NOT NULL,
  container_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(case_id, namespace, record_kind, container_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_logical_records_container ON logical_records(case_id, namespace, container_id, record_kind);
CREATE TABLE IF NOT EXISTS logical_occurrences (
  id INTEGER PRIMARY KEY,
  logical_record_id INTEGER NOT NULL REFERENCES logical_records(id),
  export_id INTEGER NOT NULL REFERENCES record_exports(id),
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  event_id INTEGER REFERENCES events(id),
  locator TEXT NOT NULL,
  interpretation TEXT NOT NULL,
  normalization_version TEXT NOT NULL,
  content_sha256 TEXT,
  structure_sha256 TEXT,
  structure_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(logical_record_id, export_id, artifact_id, locator, interpretation)
);
CREATE INDEX IF NOT EXISTS idx_logical_occurrences_export ON logical_occurrences(export_id, logical_record_id);
CREATE INDEX IF NOT EXISTS idx_logical_occurrences_event ON logical_occurrences(event_id);
CREATE TRIGGER IF NOT EXISTS logical_records_no_update
  BEFORE UPDATE ON logical_records BEGIN
    SELECT RAISE(ABORT, 'logical_records rows are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS logical_records_no_delete
  BEFORE DELETE ON logical_records BEGIN
    SELECT RAISE(ABORT, 'logical_records rows are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS logical_occurrences_no_update
  BEFORE UPDATE ON logical_occurrences BEGIN
    SELECT RAISE(ABORT, 'logical_occurrences rows are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS logical_occurrences_no_delete
  BEFORE DELETE ON logical_occurrences BEGIN
    SELECT RAISE(ABORT, 'logical_occurrences rows are append-only');
  END;
'''),
    (23, 'chat_attachment_refs_v1', r'''
CREATE TABLE IF NOT EXISTS chat_attachment_refs (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  export_id INTEGER NOT NULL REFERENCES record_exports(id),
  ref_kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  file_id TEXT,
  name TEXT,
  locator TEXT NOT NULL,
  resolution TEXT NOT NULL CHECK(resolution IN ('resolved','unresolved','ambiguous','not_ingested_limit')),
  resolved_artifact_id INTEGER REFERENCES artifacts(id),
  candidates_json TEXT NOT NULL DEFAULT '[]',
  match_rule TEXT NOT NULL,
  available_files_sha256 TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(event_id, export_id, locator, ref, available_files_sha256)
);
CREATE INDEX IF NOT EXISTS idx_chat_attachment_refs_event ON chat_attachment_refs(event_id, id);
CREATE TRIGGER IF NOT EXISTS chat_attachment_refs_no_update
  BEFORE UPDATE ON chat_attachment_refs BEGIN
    SELECT RAISE(ABORT, 'chat_attachment_refs rows are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS chat_attachment_refs_no_delete
  BEFORE DELETE ON chat_attachment_refs BEGIN
    SELECT RAISE(ABORT, 'chat_attachment_refs rows are append-only');
  END;
'''),
)

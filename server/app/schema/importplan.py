"""Import plans: catalog entries -> reviewable plan -> confirmed execution (90-99).

A plan (``import_plans``) is generated from pinned catalog scans by a versioned
detector table. Its items (``import_plan_items`` and their member entries) are
immutable; human review never edits them. Every review step appends a plan
version (``import_plan_versions``) with per-item decisions
(``import_plan_decisions``); the state of an item at version V is its latest
decision at or below V. Executions are runs (``import_plan_runs``) made of
item attempts (``import_plan_attempts``) that are written ``running`` first and
may change exactly once to a terminal status, fenced by a lease token.

``catalog_scan_abandonments`` records the explicit, audited human decision
that a catalog scan left ``running`` by a dead process is abandoned. The catalog
scan row keeps its entries; its status CHECK has no ``abandoned`` value, so the
row is closed as ``failed`` with an ``abandoned:`` error prefix and this table
holds the decision (see docs/IMPORT_PLAN.md).
"""
MIGRATIONS = (
    (90, 'import_plans_v1', r'''
CREATE TABLE IF NOT EXISTS import_plans (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  planner_version TEXT NOT NULL,
  detector_schema TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  detector_sha256 TEXT NOT NULL,
  scan_ids_json TEXT NOT NULL,
  collection_ids_json TEXT NOT NULL,
  options_json TEXT NOT NULL DEFAULT '{}',
  limits_json TEXT NOT NULL DEFAULT '{}',
  coverage_json TEXT NOT NULL DEFAULT '{}',
  cross_export_json TEXT NOT NULL DEFAULT '{}',
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_import_plans_case ON import_plans(case_id, id);

CREATE TABLE IF NOT EXISTS import_plan_items (
  id INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES import_plans(id),
  ordinal INTEGER NOT NULL,
  item_key TEXT NOT NULL,
  unit_kind TEXT NOT NULL CHECK(unit_kind IN ('file','archive','directory')),
  collection_id INTEGER NOT NULL REFERENCES collections(id),
  scan_id INTEGER NOT NULL REFERENCES catalog_scans(id),
  entry_id INTEGER REFERENCES catalog_entries(id),
  locator TEXT NOT NULL,
  name TEXT NOT NULL,
  effective_type TEXT,
  candidates_json TEXT NOT NULL,
  ambiguous INTEGER NOT NULL CHECK(ambiguous IN (0,1)),
  default_importer TEXT,
  default_parameters_json TEXT NOT NULL DEFAULT '{}',
  default_opt_ins_json TEXT NOT NULL DEFAULT '{}',
  detection_json TEXT NOT NULL,
  member_count INTEGER NOT NULL,
  estimated_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(plan_id, item_key),
  UNIQUE(plan_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_import_plan_items_plan ON import_plan_items(plan_id, ordinal);

CREATE TABLE IF NOT EXISTS import_plan_item_members (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES import_plan_items(id),
  entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),
  role TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  UNIQUE(item_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_import_plan_members_item ON import_plan_item_members(item_id, id);

CREATE TABLE IF NOT EXISTS import_plan_versions (
  id INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES import_plans(id),
  version INTEGER NOT NULL CHECK(version >= 1),
  actor TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(plan_id, version)
);

CREATE TABLE IF NOT EXISTS import_plan_decisions (
  id INTEGER PRIMARY KEY,
  version_id INTEGER NOT NULL REFERENCES import_plan_versions(id),
  item_id INTEGER NOT NULL REFERENCES import_plan_items(id),
  status TEXT NOT NULL CHECK(status IN ('proposed','approved','skipped')),
  importer TEXT,
  parameters_json TEXT NOT NULL DEFAULT '{}',
  opt_ins_json TEXT NOT NULL DEFAULT '{}',
  reason TEXT,
  UNIQUE(version_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_import_plan_decisions_item ON import_plan_decisions(item_id, version_id);

CREATE TABLE IF NOT EXISTS import_plan_runs (
  id INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES import_plans(id),
  plan_version INTEGER NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('sync','queue')),
  status TEXT NOT NULL CHECK(status IN ('running','completed','partial','failed','interrupted')),
  actor TEXT NOT NULL,
  options_json TEXT NOT NULL DEFAULT '{}',
  summary_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_plan_runs_plan ON import_plan_runs(plan_id, id);

CREATE TABLE IF NOT EXISTS import_plan_attempts (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES import_plan_runs(id),
  item_id INTEGER NOT NULL REFERENCES import_plan_items(id),
  attempt INTEGER NOT NULL CHECK(attempt >= 1),
  status TEXT NOT NULL CHECK(status IN ('running','done','failed','refused','interrupted')),
  importer TEXT NOT NULL,
  config_sha256 TEXT NOT NULL,
  parameters_json TEXT NOT NULL DEFAULT '{}',
  opt_ins_json TEXT NOT NULL DEFAULT '{}',
  lease_token TEXT NOT NULL,
  lease_expires_at REAL NOT NULL,
  worker TEXT,
  job_id INTEGER REFERENCES jobs(id),
  workdir TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_plan_attempts_item ON import_plan_attempts(item_id, id);
CREATE INDEX IF NOT EXISTS idx_import_plan_attempts_run ON import_plan_attempts(run_id, id);
CREATE INDEX IF NOT EXISTS idx_import_plan_attempts_running ON import_plan_attempts(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS catalog_scan_abandonments (
  id INTEGER PRIMARY KEY,
  scan_id INTEGER NOT NULL UNIQUE REFERENCES catalog_scans(id),
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  last_activity_at TEXT,
  min_idle_seconds INTEGER NOT NULL,
  entries_at_abandonment INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Footprint queries look artifacts up by their virtual source locator.
CREATE INDEX IF NOT EXISTS idx_artifacts_source_locator ON artifacts(source_locator);

CREATE TRIGGER IF NOT EXISTS import_plans_no_update BEFORE UPDATE ON import_plans BEGIN
  SELECT RAISE(ABORT, 'import plans are immutable; generate a new plan');
END;
CREATE TRIGGER IF NOT EXISTS import_plans_no_delete BEFORE DELETE ON import_plans BEGIN
  SELECT RAISE(ABORT, 'import plans are retained');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_items_no_update BEFORE UPDATE ON import_plan_items BEGIN
  SELECT RAISE(ABORT, 'import plan items are immutable; decisions create a new plan version');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_items_no_delete BEFORE DELETE ON import_plan_items BEGIN
  SELECT RAISE(ABORT, 'import plan items are retained');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_members_no_update BEFORE UPDATE ON import_plan_item_members BEGIN
  SELECT RAISE(ABORT, 'import plan item members are immutable');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_members_no_delete BEFORE DELETE ON import_plan_item_members BEGIN
  SELECT RAISE(ABORT, 'import plan item members are retained');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_versions_no_update BEFORE UPDATE ON import_plan_versions BEGIN
  SELECT RAISE(ABORT, 'import plan versions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_versions_no_delete BEFORE DELETE ON import_plan_versions BEGIN
  SELECT RAISE(ABORT, 'import plan versions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_decisions_no_update BEFORE UPDATE ON import_plan_decisions BEGIN
  SELECT RAISE(ABORT, 'import plan decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_decisions_no_delete BEFORE DELETE ON import_plan_decisions BEGIN
  SELECT RAISE(ABORT, 'import plan decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_runs_no_delete BEFORE DELETE ON import_plan_runs BEGIN
  SELECT RAISE(ABORT, 'import plan runs are retained');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_runs_frozen BEFORE UPDATE ON import_plan_runs
  WHEN OLD.status <> 'running' BEGIN
  SELECT RAISE(ABORT, 'a finished import plan run is immutable');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_attempts_no_delete BEFORE DELETE ON import_plan_attempts BEGIN
  SELECT RAISE(ABORT, 'import plan attempts are retained');
END;
CREATE TRIGGER IF NOT EXISTS import_plan_attempts_frozen BEFORE UPDATE ON import_plan_attempts
  WHEN OLD.status <> 'running' BEGIN
  SELECT RAISE(ABORT, 'a finished import plan attempt is immutable');
END;
CREATE TRIGGER IF NOT EXISTS catalog_scan_abandonments_no_update BEFORE UPDATE ON catalog_scan_abandonments BEGIN
  SELECT RAISE(ABORT, 'catalog scan abandonments are append-only');
END;
CREATE TRIGGER IF NOT EXISTS catalog_scan_abandonments_no_delete BEFORE DELETE ON catalog_scan_abandonments BEGIN
  SELECT RAISE(ABORT, 'catalog scan abandonments are append-only');
END;
'''),
)

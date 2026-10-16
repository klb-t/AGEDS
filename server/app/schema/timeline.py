"""Additive schema owned by the timeline area; see schema/__init__.py for ranges.

``timeline_projections`` identifies one display rule version (id, version,
file hash) together with its operator parameters (for example an assumed zone
and the id of the assumption). ``timeline_items`` is a rebuildable cache of
that projection: one row per event or transcript segment with the selected
sort time and its uncertainty interval. Rows of different rule versions never
share a projection id, so a new rule version never overwrites an older one.
``timeline_projection_builds`` is an append-only build log.

Correlation hypotheses are proposals with per-criterion evidence, never facts:
``timeline_correlation_runs`` records each execution of a rule (with its
parameters), ``timeline_hypotheses`` the proposed pairs of that run, and
``timeline_hypothesis_status`` the append-only decision history.
"""
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (30, 'timeline_projection_v1', r'''
CREATE TABLE IF NOT EXISTS timeline_projections (
  id INTEGER PRIMARY KEY,
  rule_id TEXT NOT NULL,
  rule_version INTEGER NOT NULL,
  rule_sha256 TEXT NOT NULL,
  params_json TEXT NOT NULL DEFAULT '{}',
  params_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(rule_id, rule_version, rule_sha256, params_sha256)
);
CREATE TABLE IF NOT EXISTS timeline_projection_builds (
  id INTEGER PRIMARY KEY,
  projection_id INTEGER NOT NULL REFERENCES timeline_projections(id),
  mode TEXT NOT NULL CHECK(mode IN ('full','incremental')),
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  watermarks_json TEXT NOT NULL,
  counts_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_timeline_builds_projection ON timeline_projection_builds(projection_id, id);
CREATE TRIGGER IF NOT EXISTS timeline_projection_builds_no_update
  BEFORE UPDATE ON timeline_projection_builds BEGIN
    SELECT RAISE(ABORT, 'timeline projection build log is append-only');
  END;
CREATE TRIGGER IF NOT EXISTS timeline_projection_builds_no_delete
  BEFORE DELETE ON timeline_projection_builds BEGIN
    SELECT RAISE(ABORT, 'timeline projection build log is append-only');
  END;
CREATE TABLE IF NOT EXISTS timeline_items (
  projection_id INTEGER NOT NULL REFERENCES timeline_projections(id),
  item_kind TEXT NOT NULL CHECK(item_kind IN ('event','transcript_segment')),
  ref_id INTEGER NOT NULL,
  sub_index INTEGER NOT NULL DEFAULT 0,
  sort_us INTEGER,
  lo_us INTEGER,
  hi_us INTEGER,
  end_us INTEGER,
  basis TEXT NOT NULL,
  precision TEXT NOT NULL DEFAULT 'unknown',
  candidate_ref TEXT,
  candidate_kind TEXT,
  event_type TEXT,
  source_id INTEGER,
  artifact_id INTEGER,
  collection_id INTEGER,
  counterparty TEXT,
  chatbot TEXT,
  thread_id TEXT,
  is_latest INTEGER NOT NULL DEFAULT 1,
  flags_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(projection_id, item_kind, ref_id, sub_index)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_timeline_items_sort
  ON timeline_items(projection_id, sort_us, item_kind, ref_id, sub_index);
CREATE INDEX IF NOT EXISTS idx_timeline_items_artifact
  ON timeline_items(projection_id, artifact_id);
'''),
    (31, 'timeline_correlations_v1', r'''
CREATE TABLE IF NOT EXISTS timeline_correlation_runs (
  id INTEGER PRIMARY KEY,
  rule_id TEXT NOT NULL,
  rule_version INTEGER NOT NULL,
  rule_sha256 TEXT NOT NULL,
  params_json TEXT NOT NULL,
  display_projection_id INTEGER REFERENCES timeline_projections(id),
  actor TEXT NOT NULL DEFAULT 'unknown',
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('completed','partial','skipped')),
  result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TRIGGER IF NOT EXISTS timeline_correlation_runs_no_update
  BEFORE UPDATE ON timeline_correlation_runs BEGIN
    SELECT RAISE(ABORT, 'correlation runs are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS timeline_correlation_runs_no_delete
  BEFORE DELETE ON timeline_correlation_runs BEGIN
    SELECT RAISE(ABORT, 'correlation runs are append-only');
  END;
CREATE TABLE IF NOT EXISTS timeline_hypotheses (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES timeline_correlation_runs(id),
  rule_id TEXT NOT NULL,
  rule_version INTEGER NOT NULL,
  relation TEXT NOT NULL,
  left_kind TEXT NOT NULL,
  left_id INTEGER NOT NULL,
  right_kind TEXT NOT NULL,
  right_id INTEGER NOT NULL,
  ambiguity_group TEXT NOT NULL,
  candidate_count INTEGER NOT NULL,
  criteria_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_timeline_hypotheses_run ON timeline_hypotheses(run_id, id);
CREATE INDEX IF NOT EXISTS idx_timeline_hypotheses_left ON timeline_hypotheses(left_kind, left_id);
CREATE INDEX IF NOT EXISTS idx_timeline_hypotheses_right ON timeline_hypotheses(right_kind, right_id);
CREATE TRIGGER IF NOT EXISTS timeline_hypotheses_no_update
  BEFORE UPDATE ON timeline_hypotheses BEGIN
    SELECT RAISE(ABORT, 'correlation hypotheses are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS timeline_hypotheses_no_delete
  BEFORE DELETE ON timeline_hypotheses BEGIN
    SELECT RAISE(ABORT, 'correlation hypotheses are append-only');
  END;
CREATE TABLE IF NOT EXISTS timeline_hypothesis_status (
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES timeline_hypotheses(id),
  status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected','revoked')),
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  link_id INTEGER,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_timeline_hypothesis_status ON timeline_hypothesis_status(hypothesis_id, id);
CREATE TRIGGER IF NOT EXISTS timeline_hypothesis_status_no_update
  BEFORE UPDATE ON timeline_hypothesis_status BEGIN
    SELECT RAISE(ABORT, 'hypothesis status history is append-only');
  END;
CREATE TRIGGER IF NOT EXISTS timeline_hypothesis_status_no_delete
  BEFORE DELETE ON timeline_hypothesis_status BEGIN
    SELECT RAISE(ABORT, 'hypothesis status history is append-only');
  END;
'''),
)

"""Additive schema owned by the analysis area; see schema/__init__.py for ranges.

Versions 50–54 belong to the time budget / feature series track (E1); 55–59 to
the hypothesis track (E2). Every table here is append-only (triggers refuse
UPDATE and DELETE): a new parameter set, configuration or correction is a new
row and earlier results stay readable.
"""


def _append_only(table: str) -> str:
    return f'''
CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} BEGIN
  SELECT RAISE(ABORT, '{table} is append-only');
END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} BEGIN
  SELECT RAISE(ABORT, '{table} is append-only');
END;
'''


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (50, 'analysis_runs', '''
CREATE TABLE IF NOT EXISTS analysis_runs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  artifact_class TEXT NOT NULL DEFAULT 'interpretive',
  parameters_json TEXT NOT NULL,
  parameters_sha256 TEXT NOT NULL,
  config_sha256 TEXT,
  config_version TEXT,
  code_version TEXT NOT NULL DEFAULT 'unknown',
  code_sha256 TEXT,
  source_rule TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  label_run_id INTEGER,
  result_json TEXT NOT NULL,
  result_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_kind ON analysis_runs(kind, id);
''' + _append_only('analysis_runs')),
    (51, 'analysis_labels', '''
CREATE TABLE IF NOT EXISTS analysis_label_runs (
  id INTEGER PRIMARY KEY,
  labeller TEXT NOT NULL,
  config_sha256 TEXT NOT NULL,
  config_version TEXT NOT NULL,
  code_version TEXT NOT NULL DEFAULT 'unknown',
  parameters_json TEXT NOT NULL DEFAULT '{}',
  input_sha256 TEXT NOT NULL,
  labels_sha256 TEXT NOT NULL,
  event_count INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS analysis_event_labels (
  id INTEGER PRIMARY KEY,
  label_run_id INTEGER NOT NULL REFERENCES analysis_label_runs(id),
  event_id INTEGER NOT NULL REFERENCES events(id),
  category TEXT NOT NULL,
  weight REAL NOT NULL CHECK(weight > 0 AND weight <= 1),
  path TEXT NOT NULL CHECK(path IN ('annotation','rule','model','default')),
  rules TEXT,
  annotation_id INTEGER,
  model_run_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_analysis_event_labels_run ON analysis_event_labels(label_run_id, event_id);
CREATE INDEX IF NOT EXISTS idx_analysis_event_labels_event ON analysis_event_labels(event_id, label_run_id);
''' + _append_only('analysis_label_runs') + _append_only('analysis_event_labels')),
    (52, 'analysis_label_annotations', '''
CREATE TABLE IF NOT EXISTS analysis_label_annotations (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  categories_json TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT 'unknown',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_analysis_label_annotations_event ON analysis_label_annotations(event_id, id);
''' + _append_only('analysis_label_annotations')),
    (53, 'analysis_model_labels', '''
CREATE TABLE IF NOT EXISTS analysis_model_runs (
  id INTEGER PRIMARY KEY,
  purpose TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT 'unknown',
  model_version TEXT NOT NULL DEFAULT 'unknown',
  opt_in INTEGER NOT NULL CHECK(opt_in IN (0,1)),
  status TEXT NOT NULL,
  parameters_json TEXT NOT NULL DEFAULT '{}',
  config_sha256 TEXT,
  input_sha256 TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS analysis_model_outputs (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES analysis_model_runs(id),
  event_id INTEGER NOT NULL REFERENCES events(id),
  output_json TEXT NOT NULL,
  rationale TEXT
);
CREATE INDEX IF NOT EXISTS idx_analysis_model_outputs_run ON analysis_model_outputs(run_id, event_id);
''' + _append_only('analysis_model_runs') + _append_only('analysis_model_outputs')),
)

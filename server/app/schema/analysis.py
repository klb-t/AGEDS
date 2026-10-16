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

# ---------------------------------------------------------------- hypotheses (track E2, versions 55–59)
# 55–57 used, 58–59 reserved. Append-only: a definition change is a new linked version, a decision is a
# new row, and runs must reference a registered definition hash registered before the run.

_HYP_APPEND_ONLY = '''
CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} BEGIN
  SELECT RAISE(ABORT, '{label} are append-only');
END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} BEGIN
  SELECT RAISE(ABORT, '{label} are append-only');
END;
'''


def _hyp_append_only(*pairs: tuple[str, str]) -> str:
    return ''.join(_HYP_APPEND_ONLY.format(table=table, label=label) for table, label in pairs)


HYPOTHESES_PREREGISTRATION = '''
CREATE TABLE IF NOT EXISTS hypotheses (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'unknown'
);
CREATE TABLE IF NOT EXISTS hypothesis_versions (
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  version_number INTEGER NOT NULL CHECK(version_number >= 1),
  previous_version_id INTEGER REFERENCES hypothesis_versions(id),
  definition_json TEXT NOT NULL,
  definition_sha256 TEXT NOT NULL CHECK(length(definition_sha256) = 64),
  canonicalization TEXT NOT NULL,
  registered_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'unknown',
  change_note TEXT,
  previous_record_sha256 TEXT,
  record_sha256 TEXT NOT NULL CHECK(length(record_sha256) = 64),
  UNIQUE(hypothesis_id, version_number),
  CHECK((version_number = 1) = (previous_version_id IS NULL)),
  CHECK((previous_version_id IS NULL) = (previous_record_sha256 IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_hypothesis_versions_hypothesis ON hypothesis_versions(hypothesis_id, version_number);
CREATE TRIGGER IF NOT EXISTS hypothesis_versions_chain BEFORE INSERT ON hypothesis_versions
WHEN NEW.version_number > 1 AND NOT EXISTS (
  SELECT 1 FROM hypothesis_versions p WHERE p.id = NEW.previous_version_id
    AND p.hypothesis_id = NEW.hypothesis_id AND p.version_number = NEW.version_number - 1
    AND p.record_sha256 IS NEW.previous_record_sha256)
BEGIN
  SELECT RAISE(ABORT, 'a hypothesis version must extend the latest version of the same hypothesis');
END;
''' + _hyp_append_only(('hypotheses', 'hypotheses'), ('hypothesis_versions', 'hypothesis versions'))

HYPOTHESIS_RUNS = '''
CREATE TABLE IF NOT EXISTS hypothesis_runs (
  id INTEGER PRIMARY KEY,
  hypothesis_version_id INTEGER NOT NULL REFERENCES hypothesis_versions(id),
  kind TEXT NOT NULL CHECK(kind IN ('event_study', 'candidate_search')),
  definition_sha256 TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  seed INTEGER,
  code_version TEXT NOT NULL DEFAULT 'unknown',
  input_sha256 TEXT NOT NULL,
  input_count INTEGER NOT NULL CHECK(input_count >= 0),
  provider TEXT NOT NULL DEFAULT 'none',
  provider_opt_in INTEGER NOT NULL DEFAULT 0 CHECK(provider_opt_in IN (0, 1)),
  result_json TEXT NOT NULL,
  result_sha256 TEXT NOT NULL CHECK(length(result_sha256) = 64),
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'unknown',
  CHECK(provider = 'none' OR provider_opt_in = 1)
);
CREATE INDEX IF NOT EXISTS idx_hypothesis_runs_version ON hypothesis_runs(hypothesis_version_id, kind, id);
CREATE TRIGGER IF NOT EXISTS hypothesis_runs_registered BEFORE INSERT ON hypothesis_runs
WHEN NOT EXISTS (
  SELECT 1 FROM hypothesis_versions v WHERE v.id = NEW.hypothesis_version_id
    AND v.definition_sha256 = NEW.definition_sha256 AND v.registered_at <= NEW.created_at)
BEGIN
  SELECT RAISE(ABORT, 'an analysis run requires a registered definition hash registered before the run');
END;
''' + _hyp_append_only(('hypothesis_runs', 'hypothesis runs'))

HYPOTHESIS_DOSSIER = '''
CREATE TABLE IF NOT EXISTS hypothesis_dossier_items (
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  target_kind TEXT NOT NULL CHECK(target_kind IN ('event', 'artifact', 'derived_text', 'anchor', 'annotation')),
  target_id INTEGER NOT NULL,
  locator_json TEXT NOT NULL,
  locator_sha256 TEXT NOT NULL CHECK(length(locator_sha256) = 64),
  excerpt TEXT,
  origin TEXT NOT NULL CHECK(origin IN ('human', 'fts', 'rule', 'llm')),
  origin_run_id INTEGER REFERENCES hypothesis_runs(id),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'unknown',
  UNIQUE(hypothesis_id, locator_sha256),
  CHECK((origin = 'human') = (origin_run_id IS NULL))
);
CREATE TABLE IF NOT EXISTS hypothesis_dossier_decisions (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES hypothesis_dossier_items(id),
  hypothesis_version_id INTEGER NOT NULL REFERENCES hypothesis_versions(id),
  classification TEXT NOT NULL CHECK(classification IN ('candidate', 'supporting', 'contradicting', 'context', 'rejected')),
  decided_by TEXT NOT NULL CHECK(decided_by IN ('human', 'system')),
  rationale TEXT,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'unknown',
  CHECK(decided_by = 'human' OR classification = 'candidate')
);
CREATE INDEX IF NOT EXISTS idx_dossier_decisions_item ON hypothesis_dossier_decisions(item_id, id);
CREATE TRIGGER IF NOT EXISTS hypothesis_dossier_decisions_same_hypothesis BEFORE INSERT ON hypothesis_dossier_decisions
WHEN NOT EXISTS (
  SELECT 1 FROM hypothesis_dossier_items i JOIN hypothesis_versions v ON v.hypothesis_id = i.hypothesis_id
  WHERE i.id = NEW.item_id AND v.id = NEW.hypothesis_version_id)
BEGIN
  SELECT RAISE(ABORT, 'a dossier decision must name a version of the item hypothesis');
END;
CREATE TABLE IF NOT EXISTS hypothesis_theses (
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  previous_thesis_id INTEGER REFERENCES hypothesis_theses(id),
  status_type TEXT NOT NULL CHECK(status_type IN
    ('quote', 'paraphrase', 'inference_chain', 'analogy', 'speculation', 'counterfactual', 'normative')),
  text TEXT NOT NULL CHECK(length(text) > 0),
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'unknown'
);
CREATE TABLE IF NOT EXISTS hypothesis_thesis_evidence (
  thesis_id INTEGER NOT NULL REFERENCES hypothesis_theses(id),
  item_id INTEGER NOT NULL REFERENCES hypothesis_dossier_items(id),
  PRIMARY KEY(thesis_id, item_id)
);
CREATE TRIGGER IF NOT EXISTS hypothesis_thesis_evidence_same_hypothesis BEFORE INSERT ON hypothesis_thesis_evidence
WHEN NOT EXISTS (
  SELECT 1 FROM hypothesis_theses t JOIN hypothesis_dossier_items i ON i.hypothesis_id = t.hypothesis_id
  WHERE t.id = NEW.thesis_id AND i.id = NEW.item_id)
BEGIN
  SELECT RAISE(ABORT, 'thesis evidence must be a dossier item of the same hypothesis');
END;
CREATE TABLE IF NOT EXISTS hypothesis_disclosures (
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  quadrant TEXT NOT NULL CHECK(quadrant IN ('known_knowns', 'known_unknowns', 'unknown_knowns', 'unknown_unknowns')),
  text TEXT NOT NULL,
  previous_id INTEGER REFERENCES hypothesis_disclosures(id),
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'unknown'
);
''' + _hyp_append_only(('hypothesis_dossier_items', 'dossier items'), ('hypothesis_dossier_decisions', 'dossier decisions'),
                   ('hypothesis_theses', 'theses'), ('hypothesis_thesis_evidence', 'thesis evidence links'),
                   ('hypothesis_disclosures', 'disclosures'))

MIGRATIONS = MIGRATIONS + (
    (55, 'hypotheses_preregistration', HYPOTHESES_PREREGISTRATION),
    (56, 'hypothesis_runs', HYPOTHESIS_RUNS),
    (57, 'hypothesis_dossier_theses_disclosure', HYPOTHESIS_DOSSIER),
)

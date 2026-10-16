"""Collections: private, user-described acquisition batches (device, account, export).

A collection groups acquisition observations. Its labels are private case data;
the schema stores them in the local database only. ``source_observations`` stays
append-only: the collection is fixed when the observation row is written.
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
)

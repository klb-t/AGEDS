from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .config import settings

SCHEMA = r'''
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  locator TEXT,
  account TEXT,
  imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY,
  source_id INTEGER REFERENCES sources(id),
  parent_artifact_id INTEGER REFERENCES artifacts(id),
  sha256 TEXT,
  original_name TEXT NOT NULL,
  mime_type TEXT,
  size_bytes INTEGER,
  source_locator TEXT,
  source_created_at TEXT,
  source_modified_at TEXT,
  captured_at TEXT,
  stored_path TEXT,
  is_raw INTEGER NOT NULL DEFAULT 1,
  confidence REAL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(sha256, source_locator)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER REFERENCES artifacts(id),
  source_id INTEGER REFERENCES sources(id),
  event_type TEXT NOT NULL,
  ts_start TEXT,
  ts_end TEXT,
  direction TEXT,
  sender TEXT,
  recipients TEXT,
  contact_label TEXT,
  phone_or_address TEXT,
  subject TEXT,
  body TEXT,
  external_id TEXT,
  thread_id TEXT,
  confidence REAL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS derived_text (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  kind TEXT NOT NULL,
  model TEXT,
  language TEXT,
  text TEXT NOT NULL,
  segments_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS annotations (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER REFERENCES artifacts(id),
  event_id INTEGER REFERENCES events(id),
  derived_text_id INTEGER REFERENCES derived_text(id),
  kind TEXT NOT NULL DEFAULT 'note',
  label TEXT,
  body TEXT NOT NULL,
  start_ms INTEGER,
  end_ms INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS artifact_tags (
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY(artifact_id, tag_id)
);
CREATE TABLE IF NOT EXISTS event_tags (
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY(event_id, tag_id)
);

CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY,
  left_kind TEXT NOT NULL,
  left_id INTEGER NOT NULL,
  right_kind TEXT NOT NULL,
  right_id INTEGER NOT NULL,
  relation TEXT NOT NULL,
  confidence REAL,
  rationale TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  artifact_id INTEGER REFERENCES artifacts(id),
  status TEXT NOT NULL DEFAULT 'queued',
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  actor TEXT NOT NULL DEFAULT 'system',
  action TEXT NOT NULL,
  object_kind TEXT,
  object_id INTEGER,
  details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  object_kind UNINDEXED,
  object_id UNINDEXED,
  title,
  content,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE INDEX IF NOT EXISTS idx_artifacts_sha ON artifacts(sha256);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_start);
CREATE INDEX IF NOT EXISTS idx_events_phone ON events(phone_or_address);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority DESC, id);
'''

def connect(path: Path | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(str(path or settings.db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db

@contextmanager
def session():
    db = connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

ADDITIVE_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- An acquisition is an observation of bytes in a source, not a truth claim.
-- An artifact may have several observations within its original case.
CREATE TABLE IF NOT EXISTS source_observations (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  source_id INTEGER REFERENCES sources(id),
  source_locator TEXT,
  observed_at TEXT NOT NULL,
  acquisition_kind TEXT NOT NULL DEFAULT 'ingest',
  original_name TEXT NOT NULL,
  mime_type TEXT,
  size_bytes INTEGER,
  sha256 TEXT,
  source_created_at TEXT,
  source_modified_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_observations_artifact ON source_observations(artifact_id, id);
CREATE INDEX IF NOT EXISTS idx_observations_source ON source_observations(source_id, id);
CREATE INDEX IF NOT EXISTS idx_events_external_identity ON events(artifact_id,source_id,event_type,external_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_observations_legacy_snapshot
  ON source_observations(artifact_id) WHERE acquisition_kind='legacy_snapshot';
CREATE TRIGGER IF NOT EXISTS source_observations_no_update
  BEFORE UPDATE ON source_observations BEGIN
    SELECT RAISE(ABORT, 'source observations are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS source_observations_no_delete
  BEFORE DELETE ON source_observations BEGIN
    SELECT RAISE(ABORT, 'source observations are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
  BEFORE UPDATE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
  END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
  BEFORE DELETE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
  END;

CREATE TABLE IF NOT EXISTS processing_runs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  artifact_id INTEGER REFERENCES artifacts(id),
  lease_token TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('running','done','failed','abandoned')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  tool TEXT NOT NULL DEFAULT 'unknown',
  tool_version TEXT NOT NULL DEFAULT 'unknown',
  provider TEXT NOT NULL DEFAULT 'unknown',
  model TEXT NOT NULL DEFAULT 'unknown',
  model_version TEXT NOT NULL DEFAULT 'unknown',
  parameters_json TEXT NOT NULL DEFAULT '{}',
  provenance_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_job ON processing_runs(job_id, id);

CREATE TABLE IF NOT EXISTS evidence_anchors (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  derived_text_id INTEGER NOT NULL REFERENCES derived_text(id),
  start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
  end_ms INTEGER NOT NULL CHECK(end_ms >= start_ms),
  quote_text TEXT NOT NULL,
  quote_sha256 TEXT NOT NULL,
  selector_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_anchors_version ON evidence_anchors(derived_text_id, id);
CREATE TRIGGER IF NOT EXISTS evidence_anchors_no_update
  BEFORE UPDATE ON evidence_anchors BEGIN
    SELECT RAISE(ABORT, 'evidence anchors are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS evidence_anchors_no_delete
  BEFORE DELETE ON evidence_anchors BEGIN
    SELECT RAISE(ABORT, 'evidence anchors are append-only');
  END;
'''


def _add_column(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    """Only static, module-owned identifiers are passed to this migration helper."""
    columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
    if name not in columns:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


def migrate_db(db: sqlite3.Connection) -> None:
    """Add contracts without rebuilding evidence tables or rewriting old records."""
    # executescript commits an existing transaction; execute statements individually
    # so DDL, the marked legacy backfill and migration ledger roll back together.
    db.execute('BEGIN IMMEDIATE')
    try:
        statement = ''
        for line in ADDITIVE_SCHEMA.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                db.execute(statement)
                statement = ''
        for name, definition in (
            ('lease_token', 'TEXT'), ('lease_expires_at', 'REAL'),
            ('heartbeat_at', 'TEXT'), ('worker_id', 'TEXT'),
        ):
            _add_column(db, 'jobs', name, definition)
        _add_column(db, 'derived_text', 'run_id', 'INTEGER REFERENCES processing_runs(id)')
        _add_column(db, 'derived_text', 'metadata_json', "TEXT NOT NULL DEFAULT '{}'")
        if not db.execute('SELECT 1 FROM schema_migrations WHERE version=1').fetchone():
            # Historical acquisition count, original observation time and missing
            # source identities cannot be recovered from the previous schema.
            for artifact in db.execute('SELECT * FROM artifacts ORDER BY id').fetchall():
                if db.execute('SELECT 1 FROM source_observations WHERE artifact_id=?', (artifact['id'],)).fetchone():
                    continue
                metadata = {
                    'legacy_artifact_metadata_json': artifact['metadata_json'],
                    'migration': 'source_observations_v1',
                    'observation_time_status': 'legacy_artifact_created_at_not_acquisition_time',
                    'historical_acquisition_count': 'unknown',
                }
                db.execute('''INSERT INTO source_observations(
                    artifact_id,source_id,source_locator,observed_at,acquisition_kind,
                    original_name,mime_type,size_bytes,sha256,source_created_at,
                    source_modified_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (artifact['id'], artifact['source_id'], artifact['source_locator'],
                     artifact['created_at'], 'legacy_snapshot', artifact['original_name'],
                     artifact['mime_type'], artifact['size_bytes'], artifact['sha256'],
                     artifact['source_created_at'], artifact['source_modified_at'],
                     json.dumps(metadata, ensure_ascii=False)))
            db.execute("INSERT INTO schema_migrations(version,name) VALUES(1,'source_observations_and_processing_runs')")
        db.commit()
    except Exception:
        db.rollback()
        raise


def init_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
        row = db.execute("SELECT id FROM cases ORDER BY id LIMIT 1").fetchone()
        if not row:
            db.execute("INSERT INTO cases(name) VALUES (?)", (settings.case_name,))
        db.commit()
        migrate_db(db)

from __future__ import annotations
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

def init_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
        row = db.execute("SELECT id FROM cases ORDER BY id LIMIT 1").fetchone()
        if not row:
            db.execute("INSERT INTO cases(name) VALUES (?)", (settings.case_name,))
        db.commit()

"""Import records: observed time candidates and logical record identity.

``event_times`` keeps every timestamp an importer observed for an event, with
its kind, raw text, zone status and precision. A display/sort choice between
candidates is a versioned projection rule (timeline area), never an overwrite.
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
)

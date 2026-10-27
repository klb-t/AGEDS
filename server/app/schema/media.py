"""Additive schema owned by the media area; see schema/__init__.py for ranges.

* ``speaker_turns`` — diarization hypotheses, one row per reported turn, linked
  to the processing run that produced them. Never merged into transcript text.
* ``speaker_assignments`` — human speaker assignments (a human annotation
  layer, append-only; a later row supersedes an earlier one in display).
* ``event_candidates`` — sender/time/text candidates parsed from OCR of a chat
  screenshot, with box locators. Status is always ``candidate``: they are not
  events and nothing promotes them automatically.
* ``derived_text_status`` — a human or operator marks a text version superseded by
  another (with the reason) or reinstates it; append-only, the latest row counts.
  The version itself is never changed or hidden from history.
"""
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (40, 'media_speaker_turns', '''
CREATE TABLE IF NOT EXISTS speaker_turns (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES processing_runs(id),
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  turn_index INTEGER NOT NULL CHECK(turn_index >= 0),
  start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
  end_ms INTEGER NOT NULL CHECK(end_ms >= start_ms),
  speaker_label TEXT NOT NULL,
  source_start REAL,
  source_end REAL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_speaker_turns_artifact ON speaker_turns(artifact_id, run_id, start_ms);
CREATE TRIGGER IF NOT EXISTS speaker_turns_no_update
  BEFORE UPDATE ON speaker_turns BEGIN
    SELECT RAISE(ABORT, 'speaker turns are append-only model hypotheses');
  END;
CREATE TRIGGER IF NOT EXISTS speaker_turns_no_delete
  BEFORE DELETE ON speaker_turns BEGIN
    SELECT RAISE(ABORT, 'speaker turns are append-only model hypotheses');
  END;
'''),
    (41, 'media_speaker_assignments', '''
CREATE TABLE IF NOT EXISTS speaker_assignments (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  scope TEXT NOT NULL CHECK(scope IN ('segment', 'diarization_label')),
  derived_text_id INTEGER REFERENCES derived_text(id),
  segment_index INTEGER CHECK(segment_index IS NULL OR segment_index >= 0),
  diarization_run_id INTEGER REFERENCES processing_runs(id),
  speaker_label TEXT,
  speaker TEXT NOT NULL CHECK(length(trim(speaker)) > 0),
  note TEXT,
  actor TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK((scope = 'segment' AND derived_text_id IS NOT NULL AND segment_index IS NOT NULL
         AND diarization_run_id IS NULL AND speaker_label IS NULL)
     OR (scope = 'diarization_label' AND diarization_run_id IS NOT NULL AND speaker_label IS NOT NULL
         AND derived_text_id IS NULL AND segment_index IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_speaker_assignments_artifact ON speaker_assignments(artifact_id, id);
CREATE TRIGGER IF NOT EXISTS speaker_assignments_no_update
  BEFORE UPDATE ON speaker_assignments BEGIN
    SELECT RAISE(ABORT, 'speaker assignments are append-only; add a newer assignment');
  END;
CREATE TRIGGER IF NOT EXISTS speaker_assignments_no_delete
  BEFORE DELETE ON speaker_assignments BEGIN
    SELECT RAISE(ABORT, 'speaker assignments are append-only; add a newer assignment');
  END;
'''),
    (42, 'media_event_candidates', '''
CREATE TABLE IF NOT EXISTS event_candidates (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  derived_text_id INTEGER NOT NULL REFERENCES derived_text(id),
  run_id INTEGER NOT NULL REFERENCES processing_runs(id),
  candidate_index INTEGER NOT NULL CHECK(candidate_index >= 0),
  status TEXT NOT NULL DEFAULT 'candidate' CHECK(status = 'candidate'),
  parser TEXT NOT NULL,
  sender_text TEXT,
  time_text TEXT,
  body_text TEXT,
  layout_side TEXT NOT NULL DEFAULT 'unknown' CHECK(layout_side IN ('left', 'right', 'center', 'unknown')),
  locators_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(derived_text_id, candidate_index)
);
CREATE INDEX IF NOT EXISTS idx_event_candidates_artifact ON event_candidates(artifact_id, id);
CREATE TRIGGER IF NOT EXISTS event_candidates_no_update
  BEFORE UPDATE ON event_candidates BEGIN
    SELECT RAISE(ABORT, 'event candidates are append-only parser output');
  END;
CREATE TRIGGER IF NOT EXISTS event_candidates_no_delete
  BEFORE DELETE ON event_candidates BEGIN
    SELECT RAISE(ABORT, 'event candidates are append-only parser output');
  END;
'''),
    (43, 'media_job_and_run_indexes', '''
CREATE INDEX IF NOT EXISTS idx_jobs_kind_artifact_status ON jobs(kind, artifact_id, status, id);
CREATE INDEX IF NOT EXISTS idx_runs_artifact ON processing_runs(artifact_id, id);
'''),
    (44, 'media_derived_text_status', '''
CREATE TABLE IF NOT EXISTS derived_text_status (
  id INTEGER PRIMARY KEY,
  derived_text_id INTEGER NOT NULL REFERENCES derived_text(id),
  status TEXT NOT NULL CHECK(status IN ('superseded', 'reinstated')),
  superseded_by_id INTEGER REFERENCES derived_text(id),
  reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
  actor TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK((status = 'superseded') = (superseded_by_id IS NOT NULL)),
  CHECK(superseded_by_id IS NULL OR superseded_by_id <> derived_text_id)
);
CREATE INDEX IF NOT EXISTS idx_derived_text_status ON derived_text_status(derived_text_id, id);
CREATE TRIGGER IF NOT EXISTS derived_text_status_no_update
  BEFORE UPDATE ON derived_text_status BEGIN
    SELECT RAISE(ABORT, 'version status is append-only; add a reinstated row instead');
  END;
CREATE TRIGGER IF NOT EXISTS derived_text_status_no_delete
  BEFORE DELETE ON derived_text_status BEGIN
    SELECT RAISE(ABORT, 'version status is append-only; add a reinstated row instead');
  END;
'''),
)

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


# Communications/Takeout/media importers (AGEDS-TASK-20261016-B part 2) own
# versions 25-29. Kept as a separate block so the chat-import versions (21-24)
# can be appended to the tuple above without overlapping edits.
_APPEND_ONLY = '''
CREATE TRIGGER IF NOT EXISTS {table}_no_update
  BEFORE UPDATE ON {table} BEGIN
    SELECT RAISE(ABORT, '{table} rows are append-only observations');
  END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete
  BEFORE DELETE ON {table} BEGIN
    SELECT RAISE(ABORT, '{table} rows are append-only observations');
  END;
'''

COMM_MIGRATIONS = (
    (25, 'contact_observations_v1', r'''
-- A vCard is an observation of what an address book said, not an identity.
-- Matching a contact to an event participant is a later, revocable hypothesis.
CREATE TABLE IF NOT EXISTS contact_observations (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  card_index INTEGER NOT NULL,
  external_id TEXT NOT NULL,
  external_id_kind TEXT NOT NULL CHECK(external_id_kind IN ('uid','content_derived')),
  vcard_version TEXT,
  formatted_name TEXT,
  name_parts_json TEXT NOT NULL DEFAULT '[]',
  organization TEXT,
  raw_lines_json TEXT NOT NULL DEFAULT '[]',
  locator_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(artifact_id, parser, parser_version, card_index)
);
CREATE TABLE IF NOT EXISTS contact_observation_values (
  id INTEGER PRIMARY KEY,
  contact_observation_id INTEGER NOT NULL REFERENCES contact_observations(id),
  value_kind TEXT NOT NULL CHECK(value_kind IN ('tel','email')),
  position INTEGER NOT NULL,
  raw_value TEXT NOT NULL,
  normalized_candidate TEXT,
  types_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(contact_observation_id, value_kind, position)
);
CREATE INDEX IF NOT EXISTS idx_contact_values_normalized ON contact_observation_values(value_kind, normalized_candidate);
CREATE INDEX IF NOT EXISTS idx_contact_observations_artifact ON contact_observations(artifact_id, card_index);
''' + _APPEND_ONLY.format(table='contact_observations') + _APPEND_ONLY.format(table='contact_observation_values')),
    (26, 'recording_name_observations_v1', r'''
-- What a recording's file name appears to say under a versioned, example
-- pattern set. Number/name/direction are correlation hypotheses, not facts.
CREATE TABLE IF NOT EXISTS recording_name_observations (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  artifact_id INTEGER REFERENCES artifacts(id),
  event_id INTEGER REFERENCES events(id),
  locator TEXT NOT NULL,
  file_name TEXT NOT NULL,
  pattern_set TEXT NOT NULL,
  pattern_set_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('matched','unmatched','ambiguous')),
  pattern_id TEXT,
  groups_json TEXT NOT NULL DEFAULT '{}',
  hypotheses_json TEXT NOT NULL DEFAULT '{}',
  alternatives_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_id, locator, pattern_set, pattern_set_version)
);
CREATE INDEX IF NOT EXISTS idx_recording_names_event ON recording_name_observations(event_id);
CREATE INDEX IF NOT EXISTS idx_recording_names_artifact ON recording_name_observations(artifact_id);
''' + _APPEND_ONLY.format(table='recording_name_observations')),
    (27, 'event_media_references_v1', r'''
-- A message/email names an attachment; this row anchors that reference to the
-- event with a locator and says whether bytes were present. Absent media stays
-- explicit ('missing_from_export', 'omitted_by_export'), never silently dropped.
CREATE TABLE IF NOT EXISTS event_media_references (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  artifact_id INTEGER REFERENCES artifacts(id),
  reference_kind TEXT NOT NULL,
  reference_key TEXT NOT NULL,
  reference_name TEXT,
  status TEXT NOT NULL CHECK(status IN ('present','missing_from_export','omitted_by_export','not_extracted','unreadable')),
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  locator_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK((status = 'present') = (artifact_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_media_refs_event ON event_media_references(event_id, id);
CREATE INDEX IF NOT EXISTS idx_media_refs_artifact ON event_media_references(artifact_id);
''' + _APPEND_ONLY.format(table='event_media_references')),
)

MIGRATIONS = MIGRATIONS + COMM_MIGRATIONS

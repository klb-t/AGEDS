from __future__ import annotations
import json
import sqlite3
from contextlib import closing, contextmanager
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
CREATE INDEX IF NOT EXISTS idx_derived_artifact_kind_id ON derived_text(artifact_id,kind,id);
CREATE INDEX IF NOT EXISTS idx_annotations_artifact_id ON annotations(artifact_id,id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_start);
CREATE INDEX IF NOT EXISTS idx_events_phone ON events(phone_or_address);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority DESC, id);
'''

def connect(path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    database_path = path or settings.db_path
    if read_only:
        # as_uri percent-escapes literal ?, # and % in filenames; they must not
        # become SQLite URI parameters. mode=ro never creates a missing database.
        uri = Path(database_path).resolve().as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=True)
    else:
        db = sqlite3.connect(str(database_path))
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
CREATE INDEX IF NOT EXISTS idx_anchors_artifact_id ON evidence_anchors(artifact_id,id);
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


def _execute_script(db: sqlite3.Connection, script: str) -> None:
    statement = ''
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ''


# Model for the evidence advocate (docs/design/RZECZNIK_MODEL_2026-10-25.md,
# package P1). Meaning exists only in a case-scoped assignment; the source,
# its versions and its citations are never rewritten by assignment changes.
# Targets are polymorphic references without cascades toward the material.
EVIDENCE_MODEL_SCHEMA = r'''
CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_uid ON cases(uid) WHERE uid IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_uid ON artifacts(uid) WHERE uid IS NOT NULL;
-- Rows created before this migration keep uid NULL ("before the model");
-- ensure_uid() assigns one on first use instead of a silent backfill.
CREATE TRIGGER IF NOT EXISTS cases_assign_uid AFTER INSERT ON cases
  WHEN NEW.uid IS NULL BEGIN
    UPDATE cases SET uid=lower(hex(randomblob(16))) WHERE id=NEW.id;
  END;
CREATE TRIGGER IF NOT EXISTS artifacts_assign_uid AFTER INSERT ON artifacts
  WHEN NEW.uid IS NULL BEGIN
    UPDATE artifacts SET uid=lower(hex(randomblob(16))) WHERE id=NEW.id;
  END;

-- Roles are not account types: capabilities come from grants (package P2).
CREATE TABLE IF NOT EXISTS principals (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fragments (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  derived_text_id INTEGER REFERENCES derived_text(id),
  anchor_id INTEGER REFERENCES evidence_anchors(id),
  kind TEXT NOT NULL,
  selector_json TEXT NOT NULL DEFAULT '{}',
  created_by_principal_id INTEGER REFERENCES principals(id),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fragments_artifact ON fragments(artifact_id, id);
CREATE TRIGGER IF NOT EXISTS fragments_no_update
  BEFORE UPDATE ON fragments BEGIN
    SELECT RAISE(ABORT, 'fragments are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS fragments_no_delete
  BEFORE DELETE ON fragments BEGIN
    SELECT RAISE(ABORT, 'fragments are append-only');
  END;

CREATE TABLE IF NOT EXISTS artifact_versions (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  prev_artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  relation TEXT NOT NULL DEFAULT 'supersedes',
  observed_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK(artifact_id <> prev_artifact_id)
);
CREATE INDEX IF NOT EXISTS idx_artifact_versions_artifact ON artifact_versions(artifact_id, id);
CREATE INDEX IF NOT EXISTS idx_artifact_versions_prev ON artifact_versions(prev_artifact_id, id);
CREATE TRIGGER IF NOT EXISTS artifact_versions_no_update
  BEFORE UPDATE ON artifact_versions BEGIN
    SELECT RAISE(ABORT, 'artifact versions are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS artifact_versions_no_delete
  BEFORE DELETE ON artifact_versions BEGIN
    SELECT RAISE(ABORT, 'artifact versions are append-only');
  END;

-- Related cases are data. A relation never grants access (package P2).
CREATE TABLE IF NOT EXISTS case_relations (
  id INTEGER PRIMARY KEY,
  case_a INTEGER NOT NULL REFERENCES cases(id),
  case_b INTEGER NOT NULL REFERENCES cases(id),
  relation TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_case_relations_a ON case_relations(case_a, id);
CREATE INDEX IF NOT EXISTS idx_case_relations_b ON case_relations(case_b, id);

-- visibility_scope_id refers to scopes from package P2; NULL = this case.
CREATE TABLE IF NOT EXISTS assignments (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  target_kind TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  issue_ref_json TEXT NOT NULL DEFAULT 'null',
  meaning_text TEXT,
  rationale_text TEXT,
  status TEXT NOT NULL DEFAULT 'unknown',
  status_changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  author_principal_id INTEGER REFERENCES principals(id),
  author_kind TEXT NOT NULL DEFAULT 'unknown',
  model_run_id INTEGER REFERENCES processing_runs(id),
  visibility_scope_id INTEGER,
  withdrawn_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_assignments_case ON assignments(case_id, id);
CREATE INDEX IF NOT EXISTS idx_assignments_target ON assignments(target_kind, target_id, id);
CREATE TRIGGER IF NOT EXISTS assignments_identity_fixed
  BEFORE UPDATE OF uid, case_id, target_kind, target_id, author_principal_id, author_kind, model_run_id, created_at
  ON assignments BEGIN
    SELECT RAISE(ABORT, 'assignment identity, target and authorship are fixed');
  END;
'''


# Authorization (package P2). A scope names what a grant covers; a grant gives a
# principal an open set of capabilities on one scope. Default is deny; nothing
# in code bypasses grants. A grant row is immutable except one revocation.
AUTHZ_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS scopes (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  spec_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER IF NOT EXISTS scopes_no_update
  BEFORE UPDATE ON scopes BEGIN
    SELECT RAISE(ABORT, 'scopes are immutable; create a new scope');
  END;
CREATE TRIGGER IF NOT EXISTS scopes_no_delete
  BEFORE DELETE ON scopes BEGIN
    SELECT RAISE(ABORT, 'scopes are immutable; revoke grants instead');
  END;

CREATE TABLE IF NOT EXISTS grants (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  principal_id INTEGER NOT NULL REFERENCES principals(id),
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  granted_by TEXT NOT NULL DEFAULT 'system',
  granted_at TEXT NOT NULL,
  valid_from TEXT,
  valid_to TEXT,
  revoked_at TEXT,
  revoked_by TEXT,
  conditions_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_grants_principal ON grants(principal_id, id);
CREATE TRIGGER IF NOT EXISTS grants_revoke_only
  BEFORE UPDATE ON grants
  WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
    OR NEW.uid IS NOT OLD.uid OR NEW.principal_id IS NOT OLD.principal_id
    OR NEW.scope_id IS NOT OLD.scope_id OR NEW.capabilities_json IS NOT OLD.capabilities_json
    OR NEW.granted_by IS NOT OLD.granted_by OR NEW.granted_at IS NOT OLD.granted_at
    OR NEW.valid_from IS NOT OLD.valid_from OR NEW.valid_to IS NOT OLD.valid_to
    OR NEW.conditions_json IS NOT OLD.conditions_json OR NEW.metadata_json IS NOT OLD.metadata_json
  BEGIN
    SELECT RAISE(ABORT, 'grants change only by a single revocation');
  END;
CREATE TRIGGER IF NOT EXISTS grants_no_delete
  BEFORE DELETE ON grants BEGIN
    SELECT RAISE(ABORT, 'grants are kept; revoke instead');
  END;

-- Capabilities the code understands. A grant may hold other names; they are
-- kept and grant nothing until registered.
CREATE TABLE IF NOT EXISTS capability_registry (
  name TEXT PRIMARY KEY,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS authz_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auth_methods (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  principal_id INTEGER NOT NULL REFERENCES principals(id),
  kind TEXT NOT NULL DEFAULT 'token',
  label TEXT,
  secret_salt TEXT NOT NULL,
  secret_hash TEXT NOT NULL,
  kdf TEXT NOT NULL,
  kdf_params_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_methods_principal ON auth_methods(principal_id, id);
'''

# Presets, not a closed list; the registry is data.
CAPABILITY_PRESETS = (
    ('read_metadata', 'see rows describing an object'),
    ('read_text', 'read derived text and quotes'),
    ('read_bytes', 'download original bytes'),
    ('cite', 'create citations'),
    ('annotate', 'add annotations'),
    ('propose', 'propose assignments or corrections'),
    ('invite', 'grant own access to others'),
    ('chat_with_advocate', 'talk to the evidence advocate'),
    ('export', 'export what is visible'),
    ('ingest', 'add material to the installation'),
    ('process', 'queue processing of material'),
    ('administer', 'manage principals, grants and settings'),
)
SETTING_PRESETS = (
    ('legacy_scope_policy', '"owner_only"'),
    ('denial.reveal_existence', '"never"'),
    ('count_disclosure', '"none"'),
    ('auth.mode', '"legacy_open"'),
)


def _migrate_authz(db: sqlite3.Connection) -> None:
    _execute_script(db, AUTHZ_SCHEMA)
    db.executemany('INSERT OR IGNORE INTO capability_registry(name,description) VALUES(?,?)', CAPABILITY_PRESETS)
    db.executemany('INSERT OR IGNORE INTO authz_settings(key,value_json) VALUES(?,?)', SETTING_PRESETS)
    # Predicate vocabulary seed: data from the start, extensible without code (see authz).
    from .authz import PREDICATE_FIELDS_SEED, PREDICATE_MAX_DEPTH_SEED, PREDICATE_OPERATORS_SEED
    db.executemany('INSERT OR IGNORE INTO authz_settings(key,value_json) VALUES(?,?)', [
        ('predicate.fields', json.dumps(PREDICATE_FIELDS_SEED, sort_keys=True)),
        ('predicate.operators', json.dumps(PREDICATE_OPERATORS_SEED)),
        ('predicate.max_depth', json.dumps(PREDICATE_MAX_DEPTH_SEED))])
    if not db.execute('SELECT 1 FROM schema_migrations WHERE version=3').fetchone():
        db.execute("INSERT INTO schema_migrations(version,name) VALUES(3,'authz_scopes_grants_tokens')")


# Packages (P5): a set of references and ranges, never a copy of bytes. Sharing
# a package is a grant on a scope of kind 'package'. A snapshot freezes the
# exact object versions reachable through the package at that moment.
PACKAGE_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS packages (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  owner_principal_id INTEGER REFERENCES principals(id),
  mode TEXT NOT NULL DEFAULT 'snapshot',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS package_items (
  id INTEGER PRIMARY KEY,
  package_id INTEGER NOT NULL REFERENCES packages(id),
  item_kind TEXT NOT NULL,
  item_id INTEGER NOT NULL,
  selector_json TEXT NOT NULL DEFAULT '{}',
  include_json TEXT NOT NULL DEFAULT '{}',
  added_at TEXT NOT NULL,
  withdrawn_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_package_items_package ON package_items(package_id, id);
CREATE TABLE IF NOT EXISTS package_snapshots (
  id INTEGER PRIMARY KEY,
  package_id INTEGER NOT NULL REFERENCES packages(id),
  taken_at TEXT NOT NULL,
  objects_json TEXT NOT NULL,
  objects_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_package_snapshots_package ON package_snapshots(package_id, id);
CREATE TRIGGER IF NOT EXISTS package_snapshots_no_update
  BEFORE UPDATE ON package_snapshots BEGIN
    SELECT RAISE(ABORT, 'package snapshots are append-only');
  END;
CREATE TRIGGER IF NOT EXISTS package_snapshots_no_delete
  BEFORE DELETE ON package_snapshots BEGIN
    SELECT RAISE(ABORT, 'package snapshots are append-only');
  END;
'''


def _migrate_packages(db: sqlite3.Connection) -> None:
    _execute_script(db, PACKAGE_SCHEMA)
    if not db.execute('SELECT 1 FROM schema_migrations WHERE version=4').fetchone():
        db.execute("INSERT INTO schema_migrations(version,name) VALUES(4,'scoped_packages_and_snapshots')")


# Advocate (P6). A conversation belongs to one principal. Turns and answers are
# append-only; an answer stores its basis as references with version hashes and
# its derived scope (all inputs). Reading it again re-authorizes every input.
ADVOCATE_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  principal_id INTEGER NOT NULL REFERENCES principals(id),
  title TEXT,
  settings_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_conversations_principal ON conversations(principal_id, id);
CREATE TABLE IF NOT EXISTS advocate_turns (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  question TEXT NOT NULL,
  retrieval_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON advocate_turns(conversation_id, id);
CREATE TABLE IF NOT EXISTS advocate_answers (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  turn_id INTEGER NOT NULL REFERENCES advocate_turns(id),
  mode TEXT NOT NULL,
  claims_json TEXT NOT NULL,
  basis_json TEXT NOT NULL,
  derived_scope_json TEXT NOT NULL,
  basis_sha256 TEXT NOT NULL,
  verification_json TEXT NOT NULL DEFAULT '{}',
  provider_call_id INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_answers_turn ON advocate_answers(turn_id, id);
CREATE TABLE IF NOT EXISTS derived_states (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  created_by_principal_id INTEGER REFERENCES principals(id),
  content_json TEXT NOT NULL,
  input_refs_json TEXT NOT NULL,
  transformation_json TEXT NOT NULL DEFAULT '{}',
  provider_call_id INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
'''
CREATE_PROVIDER_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS provider_consents (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  provider_name TEXT NOT NULL,
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  granted_by TEXT NOT NULL,
  basis_text TEXT NOT NULL,
  data_policy_json TEXT NOT NULL DEFAULT '{}',
  granted_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS provider_calls (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  provider_name TEXT NOT NULL,
  model TEXT NOT NULL,
  principal_id INTEGER REFERENCES principals(id),
  purpose TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  input_refs_json TEXT NOT NULL,
  prompt_content TEXT,
  status TEXT NOT NULL,
  error TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_tokens INTEGER,
  cost_eur REAL,
  cost_basis_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_calls_started ON provider_calls(started_at);
'''
APPEND_ONLY_ADVOCATE = ('advocate_turns', 'advocate_answers', 'derived_states', 'provider_calls')


def _migrate_advocate(db: sqlite3.Connection) -> None:
    _execute_script(db, ADVOCATE_SCHEMA)
    _execute_script(db, CREATE_PROVIDER_SCHEMA)
    for table in APPEND_ONLY_ADVOCATE:
        for event in ('UPDATE', 'DELETE'):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()} BEFORE {event} ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} are append-only'); END""")
    if not db.execute('SELECT 1 FROM schema_migrations WHERE version=5').fetchone():
        db.execute("INSERT INTO schema_migrations(version,name) VALUES(5,'advocate_conversations_answers_derived_states')")
    if not db.execute('SELECT 1 FROM schema_migrations WHERE version=6').fetchone():
        db.execute("INSERT INTO schema_migrations(version,name) VALUES(6,'provider_consents_and_calls')")


def _migrate_evidence_model(db: sqlite3.Connection) -> None:
    for table in ('cases', 'artifacts'):
        _add_column(db, table, 'uid', 'TEXT')
    _add_column(db, 'cases', 'metadata_json', "TEXT NOT NULL DEFAULT '{}'")
    _add_column(db, 'cases', 'owner_principal_id', 'INTEGER REFERENCES principals(id)')
    # A reference without stored bytes is valid; stored_path may stay NULL.
    _add_column(db, 'artifacts', 'external_ref_json', 'TEXT')
    for name, definition in (
        ('case_id', 'INTEGER REFERENCES cases(id)'),
        ('author_principal_id', 'INTEGER REFERENCES principals(id)'),
        ('visibility_scope_id', 'INTEGER'), ('status', 'TEXT'),
    ):
        _add_column(db, 'links', name, definition)
    _execute_script(db, EVIDENCE_MODEL_SCHEMA)
    if not db.execute('SELECT 1 FROM schema_migrations WHERE version=2').fetchone():
        db.execute("INSERT INTO schema_migrations(version,name) VALUES(2,'evidence_model_assignments_fragments')")


def migrate_db(db: sqlite3.Connection) -> None:
    """Add contracts without rebuilding evidence tables or rewriting old records."""
    # executescript commits an existing transaction; execute statements individually
    # so DDL, the marked legacy backfill and migration ledger roll back together.
    db.execute('BEGIN IMMEDIATE')
    try:
        _execute_script(db, ADDITIVE_SCHEMA)
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
        _migrate_evidence_model(db)
        _migrate_authz(db)
        _migrate_packages(db)
        _migrate_advocate(db)
        db.commit()
    except Exception:
        db.rollback()
        raise


def init_db() -> None:
    # sqlite3's connection context commits/rolls back but does not close. A
    # leaked startup connection keeps WAL state alive until nondeterministic GC.
    with closing(connect()) as db, db:
        db.executescript(SCHEMA)
        row = db.execute("SELECT id FROM cases ORDER BY id LIMIT 1").fetchone()
        if not row:
            db.execute("INSERT INTO cases(name) VALUES (?)", (settings.case_name,))
        db.commit()
        migrate_db(db)

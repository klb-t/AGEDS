"""Additive schema owned by the ledger area (financial, health and transport records); see ``schema/__init__.py``.

Version 120 (``ledger_v1``), all tables append-only:

* ``ledger_statements`` — one row per bank statement in a CAMT.053/MT940 file:
  account, opening/closing balances as stated (raw + exact decimal), and the
  balance check *computed by code* (``match``/``mismatch``/``not_checkable``).
  A mismatch is reported, never corrected.
* ``ledger_entries`` — one row per source record (table row, ``Ntry``,
  ``:61:`` line, transit tap, ride) with typed exact amounts: the decimal text,
  currency, integer minor units and the currency exponent used. Counterparty
  name/account and description are the source's text. Each row points to the
  event it was projected to (a transit segment groups two tap rows).
  ``content_key``/``content_occurrence`` are deterministic aids for comparing
  exports (identical rows at different positions are different records).
* ``health_samples`` — health/fitness samples (special category data, opt-in per
  run): provider type, value, unit, start/end with zone status, source/device
  raw and the basis (``raw_sample``, ``provider_inference``,
  ``provider_user_edit``). Sessions are events (``health_session``);
  samples belonging to a session reference it.

Idempotence: the unique key is (artifact, parser, parser version, reading key,
source position). The reading key is a hash of the reading parameters that
change the interpretation (profile, encoding, delimiter, date-order and decimal
policies; MT940 year rule), so another reading of the same file is a new
result next to the old one. ``row_sha256`` lets a re-import detect a changed
interpretation under the same parser version and reading (a visible error,
never an overwrite).
"""
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (120, 'ledger_v1', r'''
CREATE TABLE IF NOT EXISTS ledger_statements (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  reading_key TEXT NOT NULL DEFAULT '',
  statement_key TEXT NOT NULL,
  account_raw TEXT,
  currency TEXT,
  opening_json TEXT NOT NULL DEFAULT '{}',
  closing_json TEXT NOT NULL DEFAULT '{}',
  entry_count INTEGER NOT NULL,
  balance_check TEXT NOT NULL CHECK(balance_check IN ('match','mismatch','not_checkable')),
  balance_check_json TEXT NOT NULL DEFAULT '{}',
  locator_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  row_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(artifact_id, parser, parser_version, reading_key, statement_key)
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  statement_id INTEGER REFERENCES ledger_statements(id),
  record_kind TEXT NOT NULL CHECK(record_kind IN ('financial_transaction','travel_tap','travel_segment','ride')),
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  profile_id TEXT,
  reading_key TEXT NOT NULL DEFAULT '',
  source_position TEXT NOT NULL,
  amount_exact TEXT,
  currency TEXT,
  amount_minor INTEGER,
  currency_exponent INTEGER,
  flow TEXT CHECK(flow IN ('credit','debit') OR flow IS NULL),
  counterparty_name TEXT,
  counterparty_account TEXT,
  description TEXT,
  duration_s INTEGER,
  content_key TEXT NOT NULL,
  content_occurrence INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  row_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(artifact_id, parser, parser_version, reading_key, source_position)
);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_event ON ledger_entries(event_id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_content ON ledger_entries(content_key, content_occurrence);
CREATE TABLE IF NOT EXISTS health_samples (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  parser TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  source_position TEXT NOT NULL,
  sample_type TEXT NOT NULL,
  sample_class TEXT NOT NULL,
  value_raw TEXT,
  value_exact TEXT,
  unit TEXT,
  start_raw TEXT,
  start_utc TEXT,
  start_local TEXT,
  start_tz_status TEXT NOT NULL CHECK(start_tz_status IN ('explicit_offset','utc_epoch','assumed_zone','unknown')),
  start_tz TEXT,
  end_raw TEXT,
  end_utc TEXT,
  end_local TEXT,
  end_tz_status TEXT CHECK(end_tz_status IN ('explicit_offset','utc_epoch','assumed_zone','unknown') OR end_tz_status IS NULL),
  end_tz TEXT,
  precision TEXT NOT NULL DEFAULT 'unknown',
  source_name TEXT,
  source_version TEXT,
  device_raw TEXT,
  basis TEXT NOT NULL CHECK(basis IN ('raw_sample','provider_inference','provider_user_edit')),
  session_event_id INTEGER REFERENCES events(id),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  row_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(artifact_id, parser, parser_version, source_position)
);
CREATE INDEX IF NOT EXISTS idx_health_samples_start ON health_samples(start_utc);
CREATE INDEX IF NOT EXISTS idx_health_samples_type ON health_samples(sample_type, artifact_id);
CREATE INDEX IF NOT EXISTS idx_health_samples_session ON health_samples(session_event_id);
CREATE TRIGGER IF NOT EXISTS ledger_statements_no_update BEFORE UPDATE ON ledger_statements
  BEGIN SELECT RAISE(ABORT, 'ledger statements are append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_statements_no_delete BEFORE DELETE ON ledger_statements
  BEGIN SELECT RAISE(ABORT, 'ledger statements are append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_entries_no_update BEFORE UPDATE ON ledger_entries
  BEGIN SELECT RAISE(ABORT, 'ledger entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_entries_no_delete BEFORE DELETE ON ledger_entries
  BEGIN SELECT RAISE(ABORT, 'ledger entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS health_samples_no_update BEFORE UPDATE ON health_samples
  BEGIN SELECT RAISE(ABORT, 'health samples are append-only'); END;
CREATE TRIGGER IF NOT EXISTS health_samples_no_delete BEFORE DELETE ON health_samples
  BEGIN SELECT RAISE(ABORT, 'health samples are append-only'); END;
'''),
)

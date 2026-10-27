"""Additive schema owned by account connectors; see schema/__init__.py for ranges.

* ``gmail_api_fetches`` — one row per Gmail message fetched through the read-only API: the
  Gmail ids, labels, internalDate and historyId as the API reported them at fetch time, the
  SHA-256 of the raw RFC 822 bytes and the artifact they were acquired as. Labels change in
  the account; a later fetch adds a row, nothing is rewritten.
"""
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (130, 'connectors_gmail_api_fetches', '''
CREATE TABLE IF NOT EXISTS gmail_api_fetches (
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL,
  gmail_message_id TEXT NOT NULL,
  gmail_thread_id TEXT,
  label_ids_json TEXT NOT NULL DEFAULT '[]',
  internal_date_ms TEXT,
  history_id TEXT,
  size_estimate INTEGER,
  raw_sha256 TEXT NOT NULL,
  rfc822_message_id TEXT,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  request_json TEXT NOT NULL DEFAULT '{}',
  fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_gmail_api_fetches_message ON gmail_api_fetches(account, gmail_message_id, raw_sha256);
CREATE TRIGGER IF NOT EXISTS gmail_api_fetches_no_update BEFORE UPDATE ON gmail_api_fetches BEGIN
  SELECT RAISE(ABORT, 'gmail fetch observations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS gmail_api_fetches_no_delete BEFORE DELETE ON gmail_api_fetches BEGIN
  SELECT RAISE(ABORT, 'gmail fetch observations are append-only');
END;
'''),
)

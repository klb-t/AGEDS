"""Additive schema owned by the integrations area (versions 70–79).

``api_tokens`` stores local access tokens as salted SHA-256 digests only; the
token value is printed once at creation and never written anywhere. Scopes are
data (``read``, ``propose``). Revocation and expiry are timestamps, never row
deletion, so the audit trail keeps the token identity.

``annotation_proposals`` quarantines annotations proposed by an external client
(for example ChatADHD). A proposal is never applied automatically; a human
accepts or rejects it, and acceptance creates the real ``annotations`` row with
its provenance recorded here (``annotation_id``) and in ``audit_log``.

``hypothesis_imports`` / ``relation_hypotheses`` quarantine relations returned by
an external graph tool (for example Loom). Nothing is written into ``links``
until a human promotes a row; the promoted ``link_id`` keeps the origin visible.
"""
MIGRATIONS = (
    (70, 'integration_tokens_v1', r'''
CREATE TABLE IF NOT EXISTS api_tokens (
  id INTEGER PRIMARY KEY,
  token_key TEXT NOT NULL UNIQUE,
  salt TEXT NOT NULL,
  secret_sha256 TEXT NOT NULL,
  label TEXT NOT NULL,
  client_id TEXT,
  scopes_json TEXT NOT NULL DEFAULT '["read"]',
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT,
  last_used_at TEXT,
  use_count INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_key ON api_tokens(token_key);
'''),
    (71, 'integration_annotation_proposals_v1', r'''
CREATE TABLE IF NOT EXISTS annotation_proposals (
  id INTEGER PRIMARY KEY,
  token_id INTEGER REFERENCES api_tokens(id),
  client_id TEXT NOT NULL,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  derived_text_id INTEGER REFERENCES derived_text(id),
  kind TEXT NOT NULL DEFAULT 'note',
  label TEXT,
  body TEXT NOT NULL,
  start_ms INTEGER,
  end_ms INTEGER,
  basis_json TEXT NOT NULL DEFAULT '[]',
  mode TEXT NOT NULL DEFAULT 'anchored',
  provider TEXT NOT NULL DEFAULT 'unknown',
  model TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','accepted','rejected')),
  decided_at TEXT,
  decided_by TEXT,
  decision_note TEXT,
  annotation_id INTEGER REFERENCES annotations(id),
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_annotation_proposals_status ON annotation_proposals(status, id);
CREATE INDEX IF NOT EXISTS idx_annotation_proposals_artifact ON annotation_proposals(artifact_id, id);
'''),
    (72, 'integration_relation_hypotheses_v1', r'''
CREATE TABLE IF NOT EXISTS hypothesis_imports (
  id INTEGER PRIMARY KEY,
  origin TEXT NOT NULL,
  schema TEXT NOT NULL,
  file_name TEXT,
  file_sha256 TEXT NOT NULL,
  counts_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relation_hypotheses (
  id INTEGER PRIMARY KEY,
  import_id INTEGER NOT NULL REFERENCES hypothesis_imports(id),
  origin TEXT NOT NULL,
  external_id TEXT,
  left_kind TEXT NOT NULL,
  left_id INTEGER NOT NULL,
  right_kind TEXT NOT NULL,
  right_id INTEGER NOT NULL,
  relation TEXT NOT NULL,
  confidence REAL,
  rationale TEXT,
  epistemic_status TEXT NOT NULL DEFAULT 'hypothesis',
  provenance_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'quarantined' CHECK(status IN ('quarantined','promoted','rejected')),
  decided_at TEXT,
  decided_by TEXT,
  decision_note TEXT,
  link_id INTEGER REFERENCES links(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relation_hypotheses_status ON relation_hypotheses(status, id);
CREATE INDEX IF NOT EXISTS idx_relation_hypotheses_import ON relation_hypotheses(import_id, id);
'''),
    # Review finding 8: rejected bearer tokens are counted per UTC hour, key and
    # reason (one audit_log row per first occurrence) instead of one row each.
    (73, 'integration_token_rejection_counts_v1', r'''
CREATE TABLE IF NOT EXISTS api_token_rejections (
  id INTEGER PRIMARY KEY,
  bucket TEXT NOT NULL,
  token_key TEXT NOT NULL,
  reason TEXT NOT NULL,
  token_id INTEGER REFERENCES api_tokens(id),
  count INTEGER NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE(bucket, token_key, reason)
);
CREATE TRIGGER IF NOT EXISTS api_token_rejections_no_delete
  BEFORE DELETE ON api_token_rejections BEGIN
    SELECT RAISE(ABORT, 'token rejection counts are retained');
  END;
'''),
)

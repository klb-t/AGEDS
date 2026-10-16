"""Additive schema owned by the reports area (versions 60–69); see schema/__init__.py.

``reports`` is an append-only register of generated reports. A row is written
only after the report directory was published completely; it names the private
output directory, the canonical parameters and the SHA-256 of the report
manifest and of the computed report content. Regenerating with changed
parameters creates a new row and a new directory; old rows and files stay.
A digest detects change against a known value; it is not a signature.
"""
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (60, 'reports_register_v1', r'''
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY,
  report_uid TEXT NOT NULL UNIQUE,
  report_type TEXT NOT NULL,
  template_id TEXT NOT NULL,
  template_version TEXT NOT NULL,
  params_json TEXT NOT NULL,
  params_sha256 TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  output_dir TEXT NOT NULL,
  manifest_name TEXT NOT NULL DEFAULT 'manifest.json',
  formats_json TEXT NOT NULL DEFAULT '{}',
  sections_json TEXT NOT NULL DEFAULT '[]',
  citations_json TEXT NOT NULL DEFAULT '[]',
  ageds_version TEXT NOT NULL DEFAULT 'unknown',
  git_commit TEXT NOT NULL DEFAULT 'unknown',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_type ON reports(report_type, id);
CREATE TRIGGER IF NOT EXISTS reports_no_update
  BEFORE UPDATE ON reports BEGIN
    SELECT RAISE(ABORT, 'reports rows are append-only; regenerate to create a new report');
  END;
CREATE TRIGGER IF NOT EXISTS reports_no_delete
  BEFORE DELETE ON reports BEGIN
    SELECT RAISE(ABORT, 'reports rows are append-only; regenerate to create a new report');
  END;
'''),
)

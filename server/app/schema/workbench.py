"""Additive schema owned by the workbench area; see schema/__init__.py for ranges.

80  layered search index: a separate external-content FTS5 index over artifact
    metadata, events, derived text and annotations, maintained by triggers on
    those tables and backfilled from existing rows. The legacy ``search_fts``
    and ``/api/search`` stay untouched.
81  saved searches (query + filters) and an append-only log of run counts.
82  working folders ("teczki") and their reference items.
83  timeline bookmarks.

Released SQL is never edited; a change gets a new version. Index triggers only
write workbench-owned tables and never constrain evidence writes: every insert
is an upsert keyed by (object_kind, object_id) without foreign keys.
"""
from __future__ import annotations

# Search folding added *in the index only* (stored text is never changed):
# unicode61 remove_diacritics 2 folds ą/ć/ę/ń/ó/ś/ź/ż but not ł/Ł, which have no
# Unicode decomposition. A third indexed column carries ł→l, Ł→L when present.
_FOLDED = """CASE WHEN instr(title || content, 'ł') > 0 OR instr(title || content, 'Ł') > 0
    THEN replace(replace(title || char(10) || content, 'ł', 'l'), 'Ł', 'L') ELSE '' END"""

_SOURCES = {
    'artifact': """SELECT 'artifact' AS object_kind, id AS object_id, mime_type AS subkind, id AS artifact_id,
        COALESCE(original_name, '') AS title,
        COALESCE(source_locator, '') || char(10) || COALESCE(mime_type, '') || char(10) || COALESCE(metadata_json, '') AS content
        FROM artifacts""",
    'event': """SELECT 'event' AS object_kind, id AS object_id, event_type AS subkind, artifact_id AS artifact_id,
        substr(COALESCE(' | ' || NULLIF(event_type, ''), '') || COALESCE(' | ' || NULLIF(contact_label, ''), '')
          || COALESCE(' | ' || NULLIF(phone_or_address, ''), '') || COALESCE(' | ' || NULLIF(sender, ''), '')
          || COALESCE(' | ' || NULLIF(recipients, ''), '') || COALESCE(' | ' || NULLIF(subject, ''), ''), 4) AS title,
        COALESCE(body, '') AS content
        FROM events""",
    'derived_text': """SELECT 'derived_text' AS object_kind, d.id AS object_id, d.kind AS subkind, d.artifact_id AS artifact_id,
        COALESCE(d.kind, '') || ': ' || COALESCE(a.original_name, '') AS title,
        COALESCE(d.text, '') AS content
        FROM derived_text d LEFT JOIN artifacts a ON a.id = d.artifact_id""",
    'annotation': """SELECT 'annotation' AS object_kind, id AS object_id, kind AS subkind, artifact_id AS artifact_id,
        COALESCE(label, '') AS title, COALESCE(body, '') AS content
        FROM annotations""",
}
_TABLES = {'artifact': 'artifacts', 'event': 'events', 'derived_text': 'derived_text', 'annotation': 'annotations'}


def _view(kind: str) -> str:
    return (f'CREATE VIEW IF NOT EXISTS workbench_search_source_{kind} AS\n'
            f'SELECT object_kind, object_id, subkind, artifact_id, title, content,\n  {_FOLDED} AS folded\n'
            f'FROM ({_SOURCES[kind]});\n')


def _upsert(kind: str, where: str) -> str:
    return (f'INSERT INTO workbench_search_docs(object_kind, object_id, subkind, artifact_id, title, content, folded)\n'
            f'  SELECT object_kind, object_id, subkind, artifact_id, title, content, folded\n'
            f'  FROM workbench_search_source_{kind} WHERE {where}\n'
            f'  ON CONFLICT(object_kind, object_id) DO UPDATE SET subkind=excluded.subkind, artifact_id=excluded.artifact_id,\n'
            f'    title=excluded.title, content=excluded.content, folded=excluded.folded;\n')


def _base_triggers(kind: str) -> str:
    table = _TABLES[kind]
    return (f'CREATE TRIGGER IF NOT EXISTS workbench_index_{kind}_ai AFTER INSERT ON {table} BEGIN\n'
            f'{_upsert(kind, "object_id = new.id")}END;\n'
            f'CREATE TRIGGER IF NOT EXISTS workbench_index_{kind}_au AFTER UPDATE ON {table} BEGIN\n'
            f"  DELETE FROM workbench_search_docs WHERE object_kind = '{kind}' AND object_id = old.id AND old.id IS NOT new.id;\n"
            f'{_upsert(kind, "object_id = new.id")}END;\n'
            f'CREATE TRIGGER IF NOT EXISTS workbench_index_{kind}_ad AFTER DELETE ON {table} BEGIN\n'
            f"  DELETE FROM workbench_search_docs WHERE object_kind = '{kind}' AND object_id = old.id;\n"
            f'END;\n')


SEARCH_INDEX_SQL = ("""
CREATE TABLE IF NOT EXISTS workbench_search_docs (
  id INTEGER PRIMARY KEY,
  object_kind TEXT NOT NULL CHECK(object_kind IN ('artifact','event','derived_text','annotation')),
  object_id INTEGER NOT NULL,
  subkind TEXT,
  artifact_id INTEGER,
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  folded TEXT NOT NULL DEFAULT '',
  UNIQUE(object_kind, object_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS workbench_fts USING fts5(
  object_kind UNINDEXED, object_id UNINDEXED, title, content, folded,
  content='workbench_search_docs', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS workbench_search_docs_ai AFTER INSERT ON workbench_search_docs BEGIN
  INSERT INTO workbench_fts(rowid, object_kind, object_id, title, content, folded)
    VALUES (new.id, new.object_kind, new.object_id, new.title, new.content, new.folded);
END;
CREATE TRIGGER IF NOT EXISTS workbench_search_docs_ad AFTER DELETE ON workbench_search_docs BEGIN
  INSERT INTO workbench_fts(workbench_fts, rowid, object_kind, object_id, title, content, folded)
    VALUES ('delete', old.id, old.object_kind, old.object_id, old.title, old.content, old.folded);
END;
CREATE TRIGGER IF NOT EXISTS workbench_search_docs_au AFTER UPDATE ON workbench_search_docs BEGIN
  INSERT INTO workbench_fts(workbench_fts, rowid, object_kind, object_id, title, content, folded)
    VALUES ('delete', old.id, old.object_kind, old.object_id, old.title, old.content, old.folded);
  INSERT INTO workbench_fts(rowid, object_kind, object_id, title, content, folded)
    VALUES (new.id, new.object_kind, new.object_id, new.title, new.content, new.folded);
END;
""" + ''.join(_view(kind) for kind in _SOURCES)
    + ''.join(_upsert(kind, '1') for kind in _SOURCES)
    + ''.join(_base_triggers(kind) for kind in _SOURCES))

SAVED_SEARCH_SQL = """
CREATE TABLE IF NOT EXISTS workbench_saved_searches (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  query TEXT NOT NULL,
  filters_json TEXT NOT NULL DEFAULT '{}',
  result_limit INTEGER NOT NULL DEFAULT 100 CHECK(result_limit BETWEEN 1 AND 200),
  note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_run_at TEXT,
  last_run_json TEXT
);
-- Counts only (computed by code); result rows are never stored as truth.
CREATE TABLE IF NOT EXISTS workbench_saved_search_runs (
  id INTEGER PRIMARY KEY,
  saved_search_id INTEGER NOT NULL,
  run_at TEXT NOT NULL,
  query TEXT NOT NULL,
  filters_json TEXT NOT NULL,
  counts_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workbench_saved_runs ON workbench_saved_search_runs(saved_search_id, id);
CREATE TRIGGER IF NOT EXISTS workbench_saved_search_runs_no_update
  BEFORE UPDATE ON workbench_saved_search_runs BEGIN
    SELECT RAISE(ABORT, 'saved search run log is append-only');
  END;
"""

FOLDER_SQL = """
CREATE TABLE IF NOT EXISTS workbench_folders (
  id INTEGER PRIMARY KEY,
  parent_id INTEGER REFERENCES workbench_folders(id),
  name TEXT NOT NULL,
  note TEXT,
  position INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workbench_folders_sibling_name ON workbench_folders(COALESCE(parent_id, 0), name);
CREATE INDEX IF NOT EXISTS idx_workbench_folders_parent ON workbench_folders(parent_id, position, id);
-- A membership is a reference to evidence, never a copy or a change of it.
CREATE TABLE IF NOT EXISTS workbench_folder_items (
  id INTEGER PRIMARY KEY,
  folder_id INTEGER NOT NULL REFERENCES workbench_folders(id),
  item_kind TEXT NOT NULL CHECK(item_kind IN ('artifact','event','citation','text_version','annotation')),
  item_id INTEGER NOT NULL,
  pinned_version_id INTEGER,
  pinned_json TEXT NOT NULL DEFAULT '{}',
  position INTEGER NOT NULL,
  note TEXT,
  added_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(folder_id, item_kind, item_id)
);
CREATE INDEX IF NOT EXISTS idx_workbench_folder_items_order ON workbench_folder_items(folder_id, position, id);
CREATE INDEX IF NOT EXISTS idx_workbench_folder_items_target ON workbench_folder_items(item_kind, item_id);
"""

BOOKMARK_SQL = """
CREATE TABLE IF NOT EXISTS workbench_bookmarks (
  id INTEGER PRIMARY KEY,
  label TEXT,
  note TEXT,
  start_raw TEXT NOT NULL,
  start_utc TEXT,
  start_local TEXT,
  start_key TEXT,
  end_raw TEXT,
  end_utc TEXT,
  end_local TEXT,
  end_key TEXT,
  tz_status TEXT NOT NULL CHECK(tz_status IN ('explicit_offset','unknown')),
  ref_kind TEXT CHECK(ref_kind IS NULL OR ref_kind IN ('event','transcript_segment','artifact','citation')),
  ref_id INTEGER,
  ref_version_id INTEGER,
  ref_segment_index INTEGER,
  view_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workbench_bookmarks_window ON workbench_bookmarks(start_key, id);
CREATE INDEX IF NOT EXISTS idx_workbench_bookmarks_ref ON workbench_bookmarks(ref_kind, ref_id);
"""

MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (80, 'workbench_layered_search_index', SEARCH_INDEX_SQL),
    (81, 'workbench_saved_searches', SAVED_SEARCH_SQL),
    (82, 'workbench_folders', FOLDER_SQL),
    (83, 'workbench_timeline_bookmarks', BOOKMARK_SQL),
)

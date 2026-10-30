"""Additive schema owned by the advocate (rzecznik); see schema/__init__.py for ranges.

The advocate's core tables (conversations, turns, answers) are core migrations in ``db.py``.

* ``advocate_speech`` — the Text-to-Speech audio of one answer for one exact text, voice and
  language. A replay is served from here, so reading an answer aloud again does not call or pay
  the provider again. The audio is derived from a private conversation and can be synthesized
  again, so it is excluded from whole-database exports.
"""
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (140, 'advocate_speech_cache', '''
CREATE TABLE IF NOT EXISTS advocate_speech (
  id INTEGER PRIMARY KEY,
  answer_id INTEGER NOT NULL REFERENCES advocate_answers(id),
  text_sha256 TEXT NOT NULL,
  voice TEXT NOT NULL,
  language TEXT NOT NULL,
  audio BLOB NOT NULL,
  provider_call_uid TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(answer_id, text_sha256, voice, language)
);
'''),
)

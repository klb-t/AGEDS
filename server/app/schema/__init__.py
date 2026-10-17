"""Additive schema extensions owned by separate feature areas.

Each module listed in ``EXTENSION_MODULES`` exposes ``MIGRATIONS`` as a tuple of
``(version, name, sql)``. Versions are globally unique and reserved per area so
parallel work never needs to edit another area's file:

=========  =====================================  ==========================
range      module                                 area
=========  =====================================  ==========================
10–19      ``schema.catalog``                     collections and catalog
20–29      ``schema.records``                     import records, time candidates
30–39      ``schema.timeline``                    timeline projection, correlations
40–49      ``schema.media``                       ASR strategies, OCR, diarization
50–59      ``schema.analysis``                    budgets, features, hypotheses
60–69      ``schema.reports``                     generated reports
70–79      ``schema.integrations``                read API tokens, ecosystem adapters
80–89      ``schema.workbench``                   saved searches, folders, bookmarks
90–99      ``schema.importplan``                  catalog → import plans and runs
=========  =====================================  ==========================

Migrations only add tables, columns, indexes and triggers. Each version runs in
its own transaction together with its ``schema_migrations`` row, so a failure
leaves earlier versions applied and later versions untouched.
"""
from __future__ import annotations

import importlib
import sqlite3

EXTENSION_MODULES = (
    'catalog', 'records', 'timeline', 'media', 'analysis', 'reports', 'integrations', 'workbench',
    'importplan', 'location', 'traces', 'ledger',
)
_RANGES = {
    'catalog': (10, 19), 'records': (20, 29), 'timeline': (30, 39), 'media': (40, 49),
    'analysis': (50, 59), 'reports': (60, 69), 'integrations': (70, 79), 'workbench': (80, 89),
    'importplan': (90, 99), 'location': (100, 109), 'traces': (110, 119), 'ledger': (120, 129),
}


def extension_migrations() -> list[tuple[int, str, str]]:
    found: dict[int, tuple[str, str, str]] = {}
    for module_name in EXTENSION_MODULES:
        module = importlib.import_module(f'{__name__}.{module_name}')
        low, high = _RANGES[module_name]
        for version, name, sql in getattr(module, 'MIGRATIONS', ()):
            if not low <= version <= high:
                raise RuntimeError(f'migration {version} outside reserved range of {module_name}')
            if version in found:
                raise RuntimeError(f'duplicate migration version {version}')
            found[version] = (module_name, name, sql)
    return [(version, name, sql) for version, (_, name, sql) in sorted(found.items())]


def _statements(sql: str):
    statement = ''
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ''
    if statement.strip():
        raise RuntimeError('incomplete SQL statement in migration')


def apply_extension_migrations(db: sqlite3.Connection) -> list[int]:
    applied = []
    for version, name, sql in extension_migrations():
        db.execute('BEGIN IMMEDIATE')
        try:
            if db.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
                db.rollback()
                continue
            for statement in _statements(sql):
                db.execute(statement)
            db.execute('INSERT INTO schema_migrations(version,name) VALUES(?,?)', (version, name))
            db.commit()
            applied.append(version)
        except Exception:
            db.rollback()
            raise
    return applied


def add_column(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    """Idempotent ALTER TABLE for module-owned static identifiers only."""
    columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
    if name not in columns:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')

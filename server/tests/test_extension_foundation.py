import json
import sqlite3
from datetime import datetime

import pytest

from server.app import timeinfo
from server.app.schema import EXTENSION_MODULES, _RANGES, extension_migrations


def test_extension_versions_are_unique_and_in_reserved_ranges():
    versions = [v for v, _, _ in extension_migrations()]
    assert versions == sorted(set(versions))
    assert set(EXTENSION_MODULES) == set(_RANGES)


def test_migrations_apply_once_and_collections_attach_to_observations(tmp_path, monkeypatch):
    from server.app import db as dbmod, evidence
    from server.app.config import Settings
    test_settings = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    dbmod.init_db()  # idempotent
    with dbmod.session() as db:
        applied = [r[0] for r in db.execute('SELECT version FROM schema_migrations ORDER BY version')]
        assert applied[0] == 1 and 10 in applied and 20 in applied
        case_id = db.execute('SELECT id FROM cases').fetchone()[0]
        collection_id = db.execute("INSERT INTO collections(case_id,label) VALUES(?, 'Phone A export')", (case_id,)).lastrowid
    source = evidence.ensure_source('test', 'synthetic')
    sample = tmp_path / 'sample.txt'
    sample.write_bytes(b'synthetic')
    artifact = evidence.ingest_file(sample, source_id=source, source_locator='synthetic:1', collection_id=collection_id)
    with dbmod.session() as db:
        row = db.execute('SELECT collection_id FROM source_observations WHERE artifact_id=?', (artifact,)).fetchone()
        assert row['collection_id'] == collection_id
    with pytest.raises(evidence.IntegrityError):
        evidence.ingest_file(sample, source_id=source, source_locator='synthetic:2', collection_id=999)


def test_time_candidates_keep_unknown_zone_and_dst_ambiguity():
    local = timeinfo.from_iso('sent', '2025-10-26 02:30', 'test:field')
    assert local.utc is None and local.tz_status == 'unknown' and local.precision == 'minute'
    fold = timeinfo.assume_zone(local, 'Europe/Amsterdam', rule='test_rule_v1')
    assert fold.metadata['dst'] == 'ambiguous_fold'
    assert fold.metadata['utc_alternatives'] == ['2025-10-26T00:30:00Z', '2025-10-26T01:30:00Z']
    gap = timeinfo.assume_zone(timeinfo.from_iso('sent', '2025-03-30T02:30:00', 'test'), 'Europe/Warsaw', rule='r')
    assert gap.utc is None and gap.metadata['dst'] == 'nonexistent_local_time'
    offset = timeinfo.from_iso('sent', '2025-01-01T10:00:00.5+01:00', 'test')
    assert offset.utc == '2025-01-01T09:00:00.500000Z' and offset.precision == 'millisecond'
    epoch = timeinfo.from_epoch('created', 1700000000.123, 'chatgpt:create_time')
    assert epoch.utc.startswith('2023-11-14T22:13:20.12') and epoch.tz_status == 'utc_epoch'
    with pytest.raises(ValueError):
        timeinfo.TimeCandidate('sent', 'x', 's', utc='2020-01-01T00:00:00Z', tz_status='unknown')


def test_record_event_times_is_append_only_and_conflict_visible():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY)')
    db.execute('INSERT INTO events(id) VALUES (1)')
    from server.app.schema.records import MIGRATIONS
    from server.app.schema import _statements
    for statement in _statements(MIGRATIONS[0][2]):
        db.execute(statement)
    c = timeinfo.from_iso('sent', '2024-02-29T23:59:59Z', 'p:v1')
    first = timeinfo.record_event_times(db, 1, [c])
    assert timeinfo.record_event_times(db, 1, [c]) == first
    other = timeinfo.TimeCandidate('sent', '2024-02-29T23:59:59Z', 'p:v1', local='2024-02-29T23:59:59', precision='second')
    with pytest.raises(ValueError):
        timeinfo.record_event_times(db, 1, [other])
    with pytest.raises(sqlite3.DatabaseError):
        db.execute('DELETE FROM event_times')


def test_routers_and_cli_extensions_are_registered():
    from fastapi.testclient import TestClient
    from server.app.main import app
    from server.app.cli import _parser
    assert _parser() is not None
    paths = {getattr(route, 'path', None) for route in app.routes}
    assert '/api/timeline' in paths

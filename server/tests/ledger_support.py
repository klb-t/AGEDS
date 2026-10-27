"""Shared fixture helpers for the ledger tests (temporary synthetic database, corpus queries)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.config import settings
from server.tests.corpus import ledger as corpus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_settings = replace(settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store')
    test_settings.store_dir.mkdir(parents=True)
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    manifest = corpus.build(tmp_path / 'corpus')
    return {'root': tmp_path / 'corpus', 'm': manifest, 'tmp': tmp_path}


def rows(sql, params=()):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


def events(artifact_id, event_type=None):
    sql, params = 'SELECT * FROM events WHERE artifact_id=?', [artifact_id]
    if event_type:
        sql += ' AND event_type=?'
        params.append(event_type)
    out = rows(sql + ' ORDER BY id', params)
    for event in out:
        event['meta'] = json.loads(event['metadata_json'])
        event['times'] = times(event['id'])
        event['entries'] = rows('SELECT * FROM ledger_entries WHERE event_id=? ORDER BY id', (event['id'],))
    return out


def times(event_id):
    out = rows('SELECT * FROM event_times WHERE event_id=? ORDER BY id', (event_id,))
    for t in out:
        t['meta'] = json.loads(t['metadata_json'])
    return out


def counts():
    tables = ('artifacts', 'events', 'event_times', 'ledger_entries', 'ledger_statements', 'health_samples')
    return {t: rows(f'SELECT COUNT(*) n FROM {t}')[0]['n'] for t in tables}


def sha_tree(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}

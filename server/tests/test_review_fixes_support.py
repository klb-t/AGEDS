"""Shared fixtures for the review-fix regression tests (AGEDS-TASK-20261016-R).

Every fixture builds a private temporary database and content store; nothing
here depends on scratch directories or on the reviewer's reproducer scripts.
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import db as dbmod
from server.app import evidence, main, worker
from server.app.api import imports_comm


@dataclass
class Env:
    root: Path
    settings: object

    def rows(self, sql: str, params=()) -> list[dict]:
        with dbmod.session() as db:
            return [dict(r) for r in db.execute(sql, params)]

    def one(self, sql: str, params=()):
        with dbmod.session() as db:
            row = db.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def client(self, **kwargs) -> TestClient:
        return TestClient(main.app, **kwargs)


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    test_settings = replace(dbmod.settings, data_dir=data, db_path=data / 'e.db', store_dir=data / 'store',
                            scan_roots=())
    test_settings.store_dir.mkdir(parents=True)
    for module in (dbmod, evidence, main, imports_comm, worker):
        monkeypatch.setattr(module, 'settings', test_settings)
    monkeypatch.delenv('EW_REQUIRE_TOKEN', raising=False)
    dbmod.init_db()
    return Env(tmp_path, test_settings)


def wav_bytes(seconds: int = 1, rate: int = 8000) -> bytes:
    data = b'\0\0' * rate * seconds
    return (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16)
            + b'data' + struct.pack('<I', len(data)) + data)


def mbox_message(message_id: int, body: str, attachment: bytes | None = None, *, date: str = 'Mon, 1 Jan 2024 10:00:00 +0000',
                 extra_headers: str = '') -> bytes:
    """One synthetic MBOX record (example.org addresses only)."""
    head = (f'From a@example.org Mon Jan  1 10:00:00 2024\nFrom: a@example.org\nTo: b@example.org\n'
            f'Message-ID: <{message_id}@example.org>\n{extra_headers}Date: {date}\n')
    if attachment is None:
        return (head + f'\n{body}\n\n').encode()
    encoded = base64.b64encode(attachment).decode()
    return (head + 'MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary="b"\n\n'
            f'--b\nContent-Type: text/plain\n\n{body}\n--b\nContent-Type: application/pdf\n'
            'Content-Disposition: attachment; filename="invoice.pdf"\nContent-Transfer-Encoding: base64\n\n'
            f'{encoded}\n--b--\n\n').encode()

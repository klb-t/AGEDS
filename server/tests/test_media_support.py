"""Shared fixtures for media-area tests (no tests here). Generated data only."""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from server.app import config, db, worker
from server.tests.corpus import audio


class MediaCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.root / 'store'
        self.store.mkdir()
        self.settings = replace(db.settings, db_path=self.root / 'evidence.db', store_dir=self.store,
                                data_dir=self.root, whisper_model='small', whisper_device='cpu',
                                whisper_compute_type='int8')
        for module in (db, worker, config):
            patcher = patch.object(module, 'settings', self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        self.corpus = audio.build(self.root / 'corpus')

    def use_settings(self, **changes):
        self.settings = replace(self.settings, **changes)
        for module in (db, worker, config):
            patcher = patch.object(module, 'settings', self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_whisper(self) -> audio.FakeFasterWhisper:
        fake = audio.FakeFasterWhisper()
        patcher = patch.dict(sys.modules, fake.modules())
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def add_artifact(self, data: bytes, name: str, mime: str) -> int:
        digest = hashlib.sha256(data).hexdigest()
        stored = self.store / f'{digest}{Path(name).suffix}'
        stored.write_bytes(data)
        with db.session() as conn:
            return int(conn.execute(
                'INSERT INTO artifacts(original_name,mime_type,stored_path,sha256,size_bytes) VALUES (?,?,?,?,?)',
                (name, mime, str(stored), digest, len(data))).lastrowid)

    def add_audio(self, key: str = 'audio_codeswitch') -> int:
        path = Path(self.corpus[key])
        return self.add_artifact(path.read_bytes(), path.name, 'audio/wav')

    def rows(self, table: str) -> list[dict]:
        with db.session() as conn:
            return [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]

"""Startup must release its SQLite handle on success and migration failure."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from server.app import db


class StartupLifecycleTests(unittest.TestCase):
    def test_startup_connection_closes_on_success_and_failure(self):
        for failing in (False, True):
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = replace(db.settings, db_path=root / 'startup.sqlite', data_dir=root, store_dir=root / 'store')
                connections = []
                original = db.connect
                def tracked():
                    connection = original()
                    connections.append(connection)
                    return connection
                with patch.object(db, 'settings', settings), patch.object(db, 'connect', tracked):
                    if failing:
                        with patch.object(db, 'migrate_db', side_effect=RuntimeError('synthetic migration failure')):
                            with self.assertRaisesRegex(RuntimeError, 'synthetic migration failure'):
                                db.init_db()
                    else:
                        db.init_db()
                self.assertEqual(len(connections), 1)
                with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
                    connections[0].execute('SELECT 1')
                # No GC is needed to release the final startup writer/WAL.
                self.assertFalse(Path(str(settings.db_path) + '-wal').exists())

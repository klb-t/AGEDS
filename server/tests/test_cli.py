from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.app.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]


def snapshot(root: Path):
    return {str(path.relative_to(root)): ("directory" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest())
            for path in root.rglob('*')}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONDONTWRITEBYTECODE='1')

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'server.app.cli', *map(str, args)], cwd=self.source,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_scan_from_cwd_inside_source_creates_no_data_or_source_writes(self):
        (self.source / 'billing.csv').write_text('Phone,Date\n+48123456789,2026-09-30 08:15\n')
        (self.source / 'clip.mp3').write_bytes(b'opaque recording')
        before = snapshot(self.source)
        output = self.base / 'manifest.json'
        process = self.command('scan', self.source, '--output', output)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(snapshot(self.source), before)
        self.assertFalse((self.source / 'data').exists())
        status = json.loads(process.stdout)
        self.assertFalse(status['source_bytes_written'])
        self.assertTrue(status['coverage']['complete'])
        self.assertEqual(json.loads(output.read_text())['schema_version'], 'ageds.source-scan/v1')

    def test_cli_module_import_does_not_load_config_db_or_packages(self):
        code = 'import server.app.cli,sys,json; print(json.dumps({n:n in sys.modules for n in ["server.app.config","server.app.db","server.app.packages"]}))'
        process = subprocess.run([sys.executable, '-c', code], cwd=self.source, env=self.env,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {'server.app.config': False, 'server.app.db': False, 'server.app.packages': False})
        self.assertEqual(snapshot(self.source), {})

    def test_scan_caps_report_partial_coverage(self):
        (self.source / 'one.txt').write_text('one')
        (self.source / 'two.txt').write_text('two')
        process = self.command('scan', self.source, '--output', self.base / 'manifest.json', '--max-files', '1')
        self.assertEqual(process.returncode, 0, process.stderr)
        status = json.loads(process.stdout)
        self.assertFalse(status['coverage']['complete'])
        self.assertGreater(status['issue_count'], 0)

    def test_scan_invalid_output_and_invalid_limit_fail_without_source_writes(self):
        (self.source / 'one.txt').write_text('one')
        before = snapshot(self.source)
        process = self.command('scan', self.source, '--output', self.source / 'manifest.json')
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(snapshot(self.source), before)
        process = self.command('scan', self.source, '--output', self.base / 'manifest.json', '--max-files', '0')
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(snapshot(self.source), before)

    def test_verify_invalid_package_has_nonzero_exit_and_no_data_creation(self):
        input_path = self.source / 'invalid.json'
        input_path.write_text('{"schema":"wrong"}')
        before = snapshot(self.source)
        process = self.command('metadata-verify', input_path)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertFalse(json.loads(process.stdout)['valid'])
        self.assertEqual(snapshot(self.source), before)

    def test_verify_input_size_and_malformed_json_fail(self):
        input_path = self.source / 'invalid.json'
        input_path.write_text('{oops}')
        process = self.command('metadata-verify', input_path)
        self.assertNotEqual(process.returncode, 0)
        process = self.command('metadata-verify', input_path, '--max-input-bytes', '1')
        self.assertNotEqual(process.returncode, 0)

    def test_real_metadata_export_and_verify_valid_then_tampered(self):
        database_dir = self.base / 'runtime-data'
        self.env.update(EW_DATA_DIR=str(database_dir), EW_STORE_DIR=str(database_dir / 'store'), EW_DB_PATH=str(database_dir / 'evidence.db'))
        initialized = subprocess.run([sys.executable, '-c', 'from server.app.db import init_db; init_db()'], cwd=self.source,
                                     env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        before = snapshot(self.source)
        output = self.base / 'metadata.json'
        exported = self.command('metadata-export', output)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        verified = self.command('metadata-verify', output)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)['valid'])
        value = json.loads(output.read_text())
        value['exported_at'] = 'tampered'
        output.write_text(json.dumps(value))
        invalid = self.command('metadata-verify', output)
        self.assertEqual(invalid.returncode, 1, invalid.stderr)
        self.assertFalse(json.loads(invalid.stdout)['digest_valid'])
        self.assertEqual(snapshot(self.source), before)

    def test_metadata_export_never_overwrites_existing_file(self):
        output = self.base / 'existing.db'
        output.write_bytes(b'database bytes')
        package = {'schema': 'ageds.metadata-package/v1', 'metadata_only': True, 'replay_supported': False, 'signed': False}
        with patch('server.app.packages.export_metadata_package', return_value=package):
            self.assertNotEqual(main(['metadata-export', str(output)]), 0)
        self.assertEqual(output.read_bytes(), b'database bytes')

    def test_metadata_export_missing_database_fails_without_creating_database_or_output(self):
        private_runtime = self.base / 'runtime-data'
        missing = private_runtime / 'missing.db'
        self.env.update(EW_DATA_DIR=str(private_runtime), EW_STORE_DIR=str(private_runtime/'store'), EW_DB_PATH=str(missing))
        before = snapshot(self.source)
        output = self.base / 'metadata.json'
        process = self.command('metadata-export', output)
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertEqual(json.loads(process.stderr)['error'], 'OperationalError')
        self.assertNotIn('Traceback', process.stderr)
        self.assertFalse(missing.exists())
        self.assertFalse(output.exists())
        self.assertEqual(snapshot(self.source), before)

    def test_metadata_export_writes_only_metadata_and_reports_capabilities(self):
        output = self.base / 'snapshot.json'
        package = {'schema': 'ageds.metadata-package/v1', 'metadata_only': True, 'replay_supported': False, 'signed': False}
        with patch('server.app.packages.export_metadata_package', return_value=package):
            self.assertEqual(main(['metadata-export', str(output)]), 0)
        self.assertEqual(json.loads(output.read_text()), package)

    def test_archive_cli_roundtrip_is_exact_and_does_not_create_live_storage(self):
        from server.tests.test_metadata_archive import fixture
        from server.app.packages import canonical_json
        package = fixture()
        source = self.source / 'metadata.json'
        source.write_bytes(canonical_json(package))
        before = snapshot(self.source)
        archived = self.base / 'history.sqlite'
        output = self.base / 'roundtrip.json'
        imported = self.command('metadata-archive-import', source, archived)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertTrue(json.loads(imported.stdout)['inert'])
        self.assertFalse(json.loads(imported.stdout)['jobs_resumed'])
        exported = self.command('metadata-archive-export', archived, output)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(canonical_json(json.loads(output.read_bytes())), canonical_json(package))
        self.assertEqual(snapshot(self.source), before)
        again = self.command('metadata-archive-import', source, archived)
        self.assertEqual(again.returncode, 2)
        self.assertNotIn('Traceback', again.stderr)

    def test_duplicate_json_keys_nonfinite_and_limits_fail_cleanly(self):
        source = self.source / 'metadata.json'
        target = self.base / 'never.sqlite'
        for raw in ('{"schema":1,"schema":2}', '{"number":NaN}', '{"number":1e9999}'):
            source.write_text(raw)
            for args in (('metadata-verify', source), ('metadata-archive-import', source, target)):
                with self.subTest(args=args, raw=raw):
                    process = self.command(*args)
                    self.assertEqual(process.returncode, 2, process.stderr)
                    self.assertNotIn('Traceback', process.stderr)
            self.assertFalse(target.exists())
        from server.tests.test_metadata_archive import fixture
        source.write_text(json.dumps(fixture()))
        limited = self.command('metadata-archive-import', source, target, '--max-rows', 1)
        self.assertEqual(limited.returncode, 2)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
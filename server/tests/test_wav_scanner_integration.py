"""Generated WAV/header integration; no recording corpus or copies of source data."""
import hashlib
import io
from pathlib import Path
import struct
import tempfile
import unittest

from server.app.scanner import ScanLimits, _Scan, scan_sources


def wav(data_bytes=16000):
    return b'RIFF' + struct.pack('<I', 36 + data_bytes) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 8000, 16000, 2, 16) + b'data' + struct.pack('<I', data_bytes) + bytes(data_bytes)


class WavScannerIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_full_hash_and_declared_header_duration_have_distinct_scope(self):
        data = wav()
        path = self.root / 'one.wav'
        path.write_bytes(data)
        manifest = scan_sources(self.root)
        file = manifest['files'][0]
        self.assertEqual(hashlib.sha256(data).hexdigest(), file['sha256'])
        self.assertEqual(1.0, file['audio_metadata']['duration_seconds'])
        self.assertEqual('header_prefix_only', file['audio_metadata']['scope'])
        self.assertFalse(file['audio_metadata']['body_validated'])
        self.assertEqual(len(data), manifest['coverage']['wav_header_bytes_read'])
        self.assertEqual(len(data), manifest['coverage']['hash_bytes_read'])
        self.assertEqual(data, path.read_bytes())

    def test_truncated_44_byte_header_keeps_declarations_but_never_duration(self):
        (self.root / 'cut.wav').write_bytes(wav()[:44])
        result = scan_sources(self.root)
        file = result['files'][0]
        report = file['audio_metadata']['header_probe']
        self.assertEqual(16000, report['data_declared_bytes'])
        self.assertEqual(16044, report['riff_declared_bytes'])
        self.assertIsNone(report['declared_duration_sec'])
        self.assertNotIn('duration_seconds', file['audio_metadata'])
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual('complete', file['hash_status'])
        self.assertEqual('failed', file['parse_status'])
        self.assertFalse(any(o['kind'] == 'duration' for o in result['observations']))

    def test_header_caps_are_separate_from_hash_caps_and_aggregate(self):
        for name in ('a.wav', 'b.wav', 'c.wav'):
            (self.root / name).write_bytes(wav())
        result = scan_sources(self.root, limits=ScanLimits(max_hash_bytes=1, max_wav_header_bytes=100, max_total_wav_header_bytes=150))
        self.assertEqual(0, result['coverage']['hash_bytes_read'])
        self.assertEqual(150, result['coverage']['wav_header_bytes_read'])
        self.assertEqual([100, 50, 0], [f['audio_metadata']['header_bytes_read'] for f in result['files']])
        self.assertTrue(all(f['sha256'] is None for f in result['files']))

    def test_short_reads_and_failure_keep_consumed_budget_charge(self):
        class FailsAfterPrefix(io.BytesIO):
            def __init__(self):
                super().__init__(wav())
                self.calls = 0
            def read(self, size=-1):
                self.calls += 1
                if self.calls == 3:
                    raise OSError('synthetic failure')
                return super().read(min(size, 7))
        scanner = _Scan(self.root, ScanLimits(max_total_wav_header_bytes=20))
        file = {'file_id': 'synthetic', 'relative_path': 'failed.wav'}
        scanner.wav_header(file, FailsAfterPrefix(), 16044)
        self.assertEqual(14, scanner.wav_header_bytes)
        self.assertEqual(14, file['audio_metadata']['header_bytes_read'])
        self.assertFalse(file['audio_metadata']['header_probe']['end_of_input'])
        second = {'file_id': 'second', 'relative_path': 'second.wav'}
        scanner.wav_header(second, io.BytesIO(wav()), 16044)
        self.assertEqual(6, second['audio_metadata']['header_bytes_read'])
        self.assertEqual(20, scanner.wav_header_bytes)

    def test_per_file_probe_limit_is_hard_and_positive(self):
        for overrides in ({'max_wav_header_bytes': 65537}, {'max_wav_header_bytes': 0},
                          {'max_total_wav_header_bytes': 0}, {'max_wav_header_bytes': True}):
            with self.assertRaises(ValueError):
                ScanLimits(**overrides)
        self.assertEqual(65536, ScanLimits().max_wav_header_bytes)
        self.assertEqual(8 * 1024 * 1024, ScanLimits().max_total_wav_header_bytes)


if __name__ == '__main__':
    unittest.main()

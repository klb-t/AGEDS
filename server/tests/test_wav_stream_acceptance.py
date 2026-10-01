"""Independent production scanner read accounting; logical returned bytes, not disk I/O."""
from __future__ import annotations

import hashlib
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.app import scanner


def wav(data_length=100000):
    fmt = struct.pack('<HHIIHH', 1, 1, 8000, 16000, 2, 16)
    return b'RIFF' + struct.pack('<I', 36 + data_length) + b'WAVEfmt ' + struct.pack('<I', 16) + fmt + b'data' + struct.pack('<I', data_length) + bytes((i % 251 for i in range(data_length)))


class TrackedHandle:
    def __init__(self, raw, name, hook=None, fail_after=None, max_header_chunk=None, close_error=False):
        self.raw, self.name, self.hook = raw, name, hook
        self.fail_after, self.max_header_chunk = fail_after, max_header_chunk
        self.phase = 'hash'
        self.returned = {'hash': 0, 'header': 0}
        self.requests = {'hash': [], 'header': []}
        self.close_calls = 0
        self.close_error = close_error
        self.seek_calls = []

    def read(self, size=-1):
        self.requests[self.phase].append(size)
        if self.phase == 'header' and self.fail_after is not None and self.returned['header'] >= self.fail_after:
            raise OSError('independent injected header failure')
        actual_size = min(size, self.max_header_chunk) if self.phase == 'header' and self.max_header_chunk else size
        data = self.raw.read(actual_size)
        self.returned[self.phase] += len(data)
        if self.hook and self.phase == 'header' and data:
            hook, self.hook = self.hook, None
            hook(self)
        return data

    def seek(self, offset, whence=0):
        self.seek_calls.append((offset, whence))
        result = self.raw.seek(offset, whence)
        if offset == 0 and whence == 0:
            self.phase = 'header'
        return result

    def fileno(self):
        return self.raw.fileno()

    def close(self):
        self.close_calls += 1
        self.raw.close()
        if self.close_error:
            raise OSError("synthetic close failure")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self.raw, name)


class WavStreamAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        self.handles = []
        self.open_modes = []

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, *, per_file=65536, aggregate=8*1024*1024, hash_enabled=False, settings=None):
        original = os.fdopen
        settings = settings or {}
        def fdopen(fd, mode='r', *args, **kwargs):
            raw = original(fd, mode, *args, **kwargs)
            name = Path(os.readlink(f'/proc/self/fd/{fd}')).name
            self.open_modes.append((name, mode, kwargs.get('buffering', args[0] if args else None)))
            configuration = dict(settings.get(name, {}))
            on_open = configuration.pop('on_open', None)
            if on_open:
                on_open()
            handle = TrackedHandle(raw, name, **configuration)
            self.handles.append(handle)
            return handle
        limits = scanner.ScanLimits(max_wav_header_bytes=per_file,
                                    max_total_wav_header_bytes=aggregate,
                                    max_hash_bytes=1000000 if hash_enabled else 1)
        with patch.object(scanner.os, 'fdopen', side_effect=fdopen):
            return scanner.scan_sources(self.root, limits=limits)

    def file(self, name='a.wav', length=100000):
        path = self.root / name
        path.write_bytes(wav(length))
        return path

    def assert_closed_once(self):
        for h in self.handles:
            self.assertEqual(h.close_calls, 1)
            self.assertTrue(h.raw.closed)

    def test_actual_header_bytes_never_exceed_per_file_cap(self):
        self.file()
        result = self.scan(per_file=63)
        self.assertEqual(sum(h.returned['header'] for h in self.handles), 63)
        self.assertEqual(result['coverage']['wav_header_bytes_read'], 63)
        self.assertTrue(all(0 < n <= 63 for h in self.handles for n in h.requests['header']))
        self.assertEqual(result['files'][0]['audio_metadata']['header_bytes_read'], 63)
        self.assert_closed_once()

    def test_default_header_prefix_stops_at_64k_even_for_large_file(self):
        self.file()
        result = self.scan()
        self.assertEqual(sum(h.returned['header'] for h in self.handles), 65536)
        self.assertEqual(result['coverage']['wav_header_bytes_read'], 65536)
        self.assertFalse(result['files'][0]['audio_metadata']['header_probe']['body_validated'])

    def test_aggregate_budget_limits_later_files_and_prevents_extra_read(self):
        self.file('a.wav'); self.file('b.wav'); self.file('c.wav')
        result = self.scan(per_file=40, aggregate=63)
        self.assertEqual([h.returned['header'] for h in self.handles], [40, 23, 0])
        self.assertEqual(sum(h.returned['header'] for h in self.handles), 63)
        self.assertEqual(result['coverage']['wav_header_bytes_read'], 63)
        self.assertEqual(self.handles[-1].requests['header'], [])
        self.assert_closed_once()

    def test_hash_and_header_are_separate_metrics_on_same_single_descriptor(self):
        path = self.file(length=100)
        before = path.read_bytes()
        result = self.scan(per_file=63, hash_enabled=True)
        self.assertEqual(len(self.handles), 1)
        handle = self.handles[0]
        self.assertEqual(handle.returned, {'hash': len(before), 'header': 63})
        self.assertEqual(result['coverage']['hash_bytes_read'], len(before))
        self.assertEqual(result['coverage']['wav_header_bytes_read'], 63)
        self.assertEqual(result['files'][0]['sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(handle.seek_calls, [(0, 0)])
        self.assert_closed_once()

    def test_partial_header_failure_is_charged_before_following_file(self):
        self.file('a.wav'); self.file('b.wav')
        result = self.scan(per_file=40, aggregate=40,
                           settings={'a.wav': {'fail_after': 8, 'max_header_chunk': 8}})
        self.assertEqual([h.returned['header'] for h in self.handles], [8, 32])
        self.assertEqual(result['coverage']['wav_header_bytes_read'], 40)
        self.assertNotEqual(result['files'][0]['parse_status'], 'complete')
        self.assertFalse(result['coverage']['complete'])
        self.assert_closed_once()

    def test_immediate_header_error_uses_no_budget_and_closes(self):
        self.file()
        result = self.scan(settings={'a.wav': {'fail_after': 0}})
        self.assertEqual(result['coverage']['wav_header_bytes_read'], 0)
        self.assertEqual(self.handles[0].returned['header'], 0)
        self.assertFalse(result['coverage']['complete'])
        self.assert_closed_once()

    def test_short_eof_counts_only_returned_bytes_not_requested_capacity(self):
        self.file(length=2)
        result = self.scan(per_file=65536)
        self.assertEqual(self.handles[0].returned['header'], 46)
        self.assertEqual(result['coverage']['wav_header_bytes_read'], 46)
        self.assertTrue(result['files'][0]['audio_metadata']['header_probe']['end_of_input'])
        self.assert_closed_once()

    def test_exact_cap_does_not_read_sentinel_to_assert_eof(self):
        self.file(length=20)
        result = self.scan(per_file=64)
        self.assertEqual(self.handles[0].returned['header'], 64)
        self.assertEqual(len(self.handles[0].requests['header']), 1)
        self.assertEqual(result['coverage']['wav_header_bytes_read'], 64)

    def test_mutation_during_header_read_invalidates_full_hash_and_completion(self):
        path = self.file(length=100)
        def mutate(_):
            with path.open('ab') as handle:
                handle.write(b'changed')
        result = self.scan(hash_enabled=True, settings={'a.wav': {'hook': mutate}})
        file = result['files'][0]
        self.assertEqual(file['hash_status'], 'unstable')
        self.assertEqual(file['parse_status'], 'unstable')
        self.assertEqual(result['coverage']['files_hashed'], 0)
        self.assertIsNone(file['sha256'])
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(any(i['code'] == 'source_changed_during_read' for i in result['issues']))
        duration_rows = [o for o in result['observations'] if o['kind'] == 'duration' and o['file_id'] == file['file_id']]
        self.assertTrue(all(o['status'] == 'source_changed_during_read' and o.get('seconds') is None for o in duration_rows))
        self.assert_closed_once()

    def test_same_size_mutation_is_detected_by_descriptor_metadata(self):
        path = self.file(length=100)
        before = path.stat()
        def mutate(_):
            with path.open('r+b') as handle:
                handle.seek(50); handle.write(b'Z')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        result = self.scan(hash_enabled=True, settings={'a.wav': {'hook': mutate}})
        self.assertEqual(result['files'][0]['hash_status'], 'unstable')
        self.assertIsNone(result['files'][0]['sha256'])
        self.assertFalse(result['coverage']['complete'])

    def test_scan_never_reopens_copies_or_writes_audio(self):
        paths = [self.file('a.wav', 100), self.file('b.wav', 200)]
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        self.scan(hash_enabled=True)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['a.wav', 'b.wav'])
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})
        self.assertEqual([h.name for h in self.handles], ['a.wav', 'b.wav'])
        self.assertTrue(all(mode == 'rb' for _, mode, _ in self.open_modes))
        self.assert_closed_once()

    def test_overlarge_header_limit_is_rejected_before_scanning(self):
        with self.assertRaises(ValueError):
            scanner.ScanLimits(max_wav_header_bytes=65537)

    def test_enumeration_to_open_same_inode_change_keeps_prior_observation_explicit(self):
        path = self.file(length=100)
        original = path.stat()
        def mutate_before_first_fstat():
            with path.open('ab') as handle:
                handle.write(b'changed-before-first-fstat')
        result = self.scan(hash_enabled=True, settings={'a.wav': {'on_open': mutate_before_first_fstat}})
        self.assertEqual(result['files'][0]['size_bytes'], original.st_size)
        self.assertEqual(result['files'][0]['mtime_ns'], original.st_mtime_ns)
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(any(i['code'] == 'source_changed_before_read' for i in result['issues']))
        self.assert_closed_once()

    def test_fdopen_failure_does_not_leak_acquired_descriptor(self):
        self.file(length=100)
        acquired = []
        def cannot_wrap(fd, *args, **kwargs):
            acquired.append(fd)
            raise OSError('synthetic fdopen failure')
        with patch.object(scanner.os, 'fdopen', side_effect=cannot_wrap):
            result = scanner.scan_sources(self.root)
        self.assertEqual(len(acquired), 1)
        try:
            with self.assertRaises(OSError):
                os.fstat(acquired[0])
        finally:
            # A failing production regression must not leak its test descriptor.
            try:
                os.close(acquired[0])
            except OSError:
                pass
        self.assertFalse(result['coverage']['complete'])

    def test_close_error_invalidates_accepted_hash_count_and_derived_duration(self):
        self.file(length=100)
        result = self.scan(hash_enabled=True, settings={'a.wav': {'close_error': True}})
        file = result['files'][0]
        self.assertEqual(file['hash_status'], 'unreadable')
        self.assertIsNone(file['sha256'])
        self.assertEqual(result['coverage']['files_hashed'], 0)
        self.assertFalse(result['coverage']['complete'])
        self.assertNotIn('duration_seconds', file.get('audio_metadata', {}))
        self.assertTrue(all(o.get('seconds') is None for o in result['observations'] if o['kind'] == 'duration'))
        self.assert_closed_once()


if __name__ == '__main__':
    unittest.main()

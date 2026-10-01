"""Generated-byte file-like semantics and decoder boundary verification."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import verified_reader as vr


class VerifiedReaderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'synthetic.bin'
        self.data = bytes(range(256)) * 4099
        self.path.write_bytes(self.data)
        self.digest = hashlib.sha256(self.data).hexdigest()

    def reader(self):
        return vr.VerifiedReader(self.path, self.digest, len(self.data), store_root=self.root)

    def test_seek_read_tell_and_readinto_match_binary_file(self):
        with self.reader() as source:
            self.assertTrue(source.readable())
            self.assertTrue(source.seekable())
            self.assertFalse(source.writable())
            self.assertEqual(source.read(13), self.data[:13])
            self.assertEqual(source.tell(), 13)
            self.assertEqual(source.seek(-3, os.SEEK_CUR), 10)
            target = bytearray(400000)
            self.assertEqual(source.readinto(target), len(target))
            self.assertEqual(target, self.data[10:400010])
            self.assertEqual(source.seek(-12, os.SEEK_END), len(self.data) - 12)
            self.assertEqual(source.read(), self.data[-12:])
            self.assertEqual(source.read(3), b'')
            source.seek(len(self.data) + 50)
            self.assertEqual(source.read(), b'')
            position = source.tell()
            self.assertIsNone(source.verify_unchanged())
            self.assertEqual(source.tell(), position)
        self.assertTrue(source.closed)
        with self.assertRaises(ValueError):
            source.read(1)

    def test_unbounded_or_large_read_never_silently_returns_partial(self):
        for size in (-1, None, vr.MAX_READ_BYTES + 1):
            with self.subTest(size=size), self.reader() as source:
                with self.assertRaises(vr.MediaLimitError):
                    source.read(size)
                self.assertEqual(source.tell(), 0)
                with self.assertRaises(vr.MediaIntegrityError):
                    source.verify_unchanged()
        with self.reader() as source:
            source.seek(len(self.data) - 5)
            self.assertEqual(source.read(None), self.data[-5:])

    def test_path_replacement_retains_initial_file_and_final_identity(self):
        with self.reader() as source:
            self.path.rename(self.root / 'old')
            self.path.write_bytes(b'x' * len(self.data))
            self.assertEqual(source.read(100), self.data[:100])
            source.verify_unchanged()

    def test_readinto_is_atomic_when_later_covering_chunk_changes(self):
        with self.reader() as source:
            with self.path.open('r+b') as stream:
                stream.seek(vr.CHUNK_SIZE)
                stream.write(b'BAD')
            target = bytearray(b'?' * (vr.CHUNK_SIZE + 10))
            with self.assertRaises(vr.MediaIntegrityError):
                source.readinto(target)
            self.assertEqual(target, b'?' * len(target))
            self.assertEqual(source.tell(), 0)
            self.path.write_bytes(self.data)
            with self.assertRaises(vr.MediaIntegrityError):
                source.verify_unchanged()

    def test_final_verification_checks_unread_bytes_and_size(self):
        with self.reader() as source:
            self.assertEqual(source.read(16), self.data[:16])
            with self.path.open('r+b') as stream:
                stream.seek(vr.CHUNK_SIZE * 2)
                stream.write(b'changed')
            with self.assertRaises(vr.MediaIntegrityError):
                source.verify_unchanged()
        self.path.write_bytes(b'')
        with vr.VerifiedReader(self.path, hashlib.sha256(b'').hexdigest(), 0, store_root=self.root) as source:
            self.path.write_bytes(b'new')
            with self.assertRaises(vr.MediaIntegrityError):
                source.verify_unchanged()

    def test_no_descriptor_path_or_unbounded_line_protocol(self):
        with self.reader() as source:
            self.assertFalse(hasattr(source, 'name'))
            self.assertFalse(hasattr(source, 'path'))
            with self.assertRaises(io.UnsupportedOperation):
                source.fileno()
            with self.assertRaises(io.UnsupportedOperation):
                source.readline()
            with self.assertRaises(io.UnsupportedOperation):
                source.readlines()
            with self.assertRaises(ValueError):
                source.seek(-1)
            with self.assertRaises(OverflowError):
                source.seek(2**63)
            with self.assertRaises(ValueError):
                source.seek(0, 99)

    def test_context_exception_closes_and_missing_root_fails(self):
        with self.assertRaises(RuntimeError):
            with self.reader() as source:
                descriptor = source._media.fd
                raise RuntimeError('decoder failed')
        self.assertTrue(source.closed)
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        with self.assertRaises(vr.MediaUnavailable):
            vr.VerifiedReader(self.path, self.digest, len(self.data), store_root=None)

    def test_no_path_reopen_for_decoder_reads_or_final_verification(self):
        with self.reader() as source:
            with patch('builtins.open', side_effect=AssertionError('no pathname reopen')), patch.object(os, 'open', side_effect=AssertionError('no pathname reopen')):
                self.assertEqual(source.read(32768), self.data[:32768])
                source.seek(0)
                source.verify_unchanged()

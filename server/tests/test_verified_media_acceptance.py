"""Independent deterministic media delivery races; synthetic bytes only (N24)."""
import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.app import db, main


async def collect(response):
    chunks = []
    try:
        async for chunk in response.body_iterator:
            chunks.append(chunk)
    finally:
        response.media.close()
    return b''.join(chunks)


class VerifiedMediaAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root/'store'
        self.store.mkdir()
        self.path = self.store/'synthetic.wav'
        self.original = b'0123456789abcdef' * 40000
        self.path.write_bytes(self.original)
        self.digest = hashlib.sha256(self.original).hexdigest()
        self.settings = replace(db.settings, data_dir=self.root, db_path=self.root/'fixture.sqlite', store_dir=self.store)
        for module in (db, main):
            item = patch.object(module, 'settings', self.settings)
            item.start()
            self.addCleanup(item.stop)
        db.init_db()
        with db.session() as conn:
            self.aid = conn.execute('INSERT INTO artifacts(original_name,mime_type,stored_path,sha256,size_bytes) VALUES (?,?,?,?,?)',
                                    ('é synthetic.wav', 'audio/wav', str(self.path), self.digest, len(self.original))).lastrowid
        self.url = f'/api/artifacts/{self.aid}/content'

    def response(self, **kwargs):
        from server.app.verified_media import verified_media_response
        return verified_media_response(self.path, self.digest, len(self.original),
                                       media_type='audio/wav', filename='é synthetic.wav', store_root=self.store, **kwargs)

    def test_replacing_path_after_preparation_never_serves_replacement(self):
        response = self.response()
        replacement = self.store/'replacement'
        replacement.write_bytes(b'X'*len(self.original))
        replacement.replace(self.path)
        self.assertEqual(asyncio.run(collect(response)), self.original)

    def test_same_inode_mutation_before_body_never_yields_changed_bytes(self):
        response = self.response()
        with self.path.open('r+b') as stream:
            stream.write(b'FORGED')
        with self.assertRaises(ValueError):
            asyncio.run(collect(response))

    def test_future_chunk_change_aborts_after_only_verified_prefix(self):
        from server.app import verified_media as media
        response = self.response()
        async def exercise():
            stream = response.body_iterator
            prefix = await anext(stream)
            self.assertEqual(prefix, self.original[:len(prefix)])
            with self.path.open('r+b') as source:
                source.seek(media.CHUNK_SIZE)
                source.write(b'FORGED')
            try:
                with self.assertRaises(ValueError):
                    await anext(stream)
            finally:
                await stream.aclose()
                response.media.close()
        asyncio.run(exercise())

    def test_range_checks_whole_covering_chunk_not_only_requested_bytes(self):
        response = self.response(range_header='bytes=10-19')
        with self.path.open('r+b') as stream:
            stream.write(b'X')
        with self.assertRaises(ValueError):
            asyncio.run(collect(response))

    def test_http_full_head_and_ranges_return_exact_bytes(self):
        with TestClient(main.app) as client:
            full = client.get(self.url)
            self.assertEqual(full.status_code, 200, full.text[:100])
            self.assertEqual(full.content, self.original)
            self.assertEqual(full.headers['accept-ranges'], 'bytes')
            self.assertEqual(full.headers['content-length'], str(len(self.original)))
            self.assertIn("filename*=utf-8''", full.headers['content-disposition'])
            for value, expected in [('bytes=2-7', self.original[2:8]),
                                    ('bytes=639990-', self.original[639990:]),
                                    ('bytes=-7', self.original[-7:]),
                                    ('bytes=639995-999999', self.original[639995:])]:
                with self.subTest(value=value):
                    response = client.get(self.url, headers={'Range': value})
                    self.assertEqual(response.status_code, 206, response.text[:100])
                    self.assertEqual(response.content, expected)
                    self.assertEqual(int(response.headers['content-length']), len(expected))
                    self.assertTrue(response.headers['content-range'].endswith('/640000'))
            head = client.head(self.url, headers={'Range':'bytes=1-3'})
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b'')
            self.assertEqual(head.headers['content-length'], str(len(self.original)))

    def test_http_invalid_ranges_cleanly_rejected(self):
        with TestClient(main.app) as client:
            for value in ('bytes=640000-', 'bytes=9-2', 'bytes=0-1,3-4', 'bytes=-0',
                          'bytes=NaN-', 'items=1-2', 'bytes=+1-2', 'bytes=1.5-2'):
                with self.subTest(value=value):
                    response = client.get(self.url, headers={'Range':value})
                    self.assertEqual(response.status_code, 416, response.text[:100])
                    self.assertEqual(response.headers['content-range'], 'bytes */640000')
                    self.assertNotIn(self.original[:32], response.content)

    def test_http_corrupt_source_is_409_without_media_or_database_changes(self):
        self.path.write_bytes(b'X'*len(self.original))
        with TestClient(main.app) as client:
            before = self.settings.db_path.read_bytes()
            response = client.get(self.url)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.settings.db_path.read_bytes(), before)
            self.assertNotIn(b'X'*32, response.content)
        self.assertEqual(self.path.read_bytes(), b'X'*len(self.original))

    def test_empty_media_has_full_empty_response_and_unsatisfiable_range(self):
        self.path.write_bytes(b'')
        with db.session() as conn:
            conn.execute('UPDATE artifacts SET size_bytes=0,sha256=? WHERE id=?',
                         (hashlib.sha256(b'').hexdigest(), self.aid))
        with TestClient(main.app) as client:
            response = client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b'')
            self.assertEqual(response.headers['content-length'], '0')
            response = client.get(self.url, headers={'Range':'bytes=0-'})
            self.assertEqual(response.status_code, 416)
            self.assertEqual(response.headers['content-range'], 'bytes */0')

    def test_final_symlink_fifo_and_directory_refused_without_hanging(self):
        original = self.path.with_name('original')
        self.path.rename(original)
        self.path.symlink_to(original)
        with self.assertRaises((OSError, ValueError)):
            self.response()
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises((OSError, ValueError)):
            self.response()
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises((OSError, ValueError)):
            self.response()

    def test_size_and_chunk_budget_reject_before_streaming(self):
        from server.app import verified_media as media
        with patch.object(media, 'MAX_CHUNKS', 1), self.assertRaises(ValueError):
            self.response()
        with self.assertRaises(ValueError):
            media.verified_media_response(self.path, self.digest, len(self.original)+1)

    def test_asgi_send_cancellation_closes_source_descriptor(self):
        from starlette.requests import ClientDisconnect
        response = self.response()
        inode = self.path.stat().st_ino
        def owned_descriptors():
            result = []
            for entry in Path('/proc/self/fd').iterdir():
                try:
                    if os.fstat(int(entry.name)).st_ino == inode:
                        result.append(int(entry.name))
                except OSError:
                    pass
            return result
        self.assertTrue(owned_descriptors())
        sent = []
        async def run():
            async def send(message):
                sent.append(message)
                if message['type'] == 'http.response.body':
                    raise OSError('synthetic disconnected client')
            async def receive():
                return {'type':'http.disconnect'}
            with self.assertRaises((OSError, ClientDisconnect)):
                await response({'type':'http','asgi':{'spec_version':'2.4'}}, receive, send)
        asyncio.run(run())
        self.assertFalse(owned_descriptors())
        self.assertTrue(any(message['type']=='http.response.body' for message in sent))
        for message in sent:
            if message['type']=='http.response.body':
                self.assertTrue(self.original.startswith(message.get('body', b'')))

    def test_failures_and_head_do_not_leak_descriptors(self):
        from server.app import verified_media as media
        def descriptors():
            return set(os.listdir('/proc/self/fd'))
        before = descriptors()
        for _ in range(10):
            with self.assertRaises(ValueError):
                media.verified_media_response(self.path, '0'*64, len(self.original))
            self.response(head=True)
        self.assertEqual(descriptors(), before)

    def test_parent_directory_replaced_after_open_cannot_redirect_descriptor(self):
        from server.app import verified_media as media
        nested = self.store/'nested'
        nested.mkdir()
        self.path.rename(nested/'synthetic.wav')
        self.path = nested/'synthetic.wav'
        outside = self.root/'outside'
        outside.mkdir()
        (outside/'synthetic.wav').write_bytes(b'X'*len(self.original))
        real_open = os.open
        swapped = []
        def swap_on_final(path, flags, *args, **kwargs):
            if path == 'synthetic.wav' and not swapped:
                nested.rename(self.store/'original-dir')
                nested.symlink_to(outside, target_is_directory=True)
                swapped.append(True)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(media.os, 'open', swap_on_final):
            response = self.response()
        self.assertEqual(swapped, [True])
        self.assertEqual(asyncio.run(collect(response)), self.original)

    def test_parent_symlink_swap_before_open_is_rejected_before_any_read(self):
        from server.app import verified_media as media
        nested = self.store/'nested'
        nested.mkdir()
        self.path.rename(nested/'synthetic.wav')
        self.path = nested/'synthetic.wav'
        outside = self.root/'outside'
        outside.mkdir()
        (outside/'synthetic.wav').write_bytes(self.original)
        real_open = os.open
        swapped = []
        def swap_parent(path, flags, *args, **kwargs):
            if path == 'nested' and not swapped:
                nested.rename(self.store/'original-dir')
                nested.symlink_to(outside, target_is_directory=True)
                swapped.append(True)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(media.os, 'open', swap_parent), \
             patch.object(media.os, 'pread', side_effect=AssertionError('outside bytes must not be read')):
            with self.assertRaises(ValueError):
                self.response()
        self.assertEqual(swapped, [True])

    def test_if_range_validator_matches_or_falls_back_to_full_body(self):
        with TestClient(main.app) as client:
            etag = client.head(self.url).headers['etag']
            for validator, status in [(etag,206), ('"different"',200), ('W/'+etag,200)]:
                with self.subTest(validator=validator):
                    response = client.get(self.url, headers={'Range':'bytes=2-5','If-Range':validator})
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.content, self.original[2:6] if status==206 else self.original)
                    self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_http_path_swap_after_hash_preparation_serves_original_descriptor(self):
        from server.app import verified_media as media
        prepare = media.verified_media_response
        replacement = self.store/'replacement'
        replacement.write_bytes(b'X'*len(self.original))
        def prepare_then_swap(*args, **kwargs):
            response = prepare(*args, **kwargs)
            replacement.replace(self.path)
            return response
        with TestClient(main.app) as client, patch.object(main, 'verified_media_response', prepare_then_swap):
            response = client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, self.original)
        self.assertEqual(self.path.read_bytes(), b'X'*len(self.original))

    def test_disconnect_during_worker_read_waits_before_closing_descriptor(self):
        import threading
        import anyio
        response = self.response()
        owned = response.media
        original_read = owned.read_chunk
        entered, release = threading.Event(), threading.Event()
        observed = []
        def held_read(index):
            entered.set()
            if not release.wait(3):
                raise AssertionError('cancelled worker was never released')
            observed.append(owned.fd is not None)
            return original_read(index)
        async def exercise():
            async def send(message):
                await anyio.sleep(0)
            async def receive():
                started = await anyio.to_thread.run_sync(entered.wait, 2)
                self.assertTrue(started)
                threading.Timer(.05, release.set).start()
                return {'type':'http.disconnect'}
            with patch.object(owned, 'read_chunk', held_read):
                await response({'type':'http','asgi':{'spec_version':'2.3'}}, receive, send)
        try:
            asyncio.run(exercise())
        finally:
            release.set()
            owned.close()
        self.assertEqual(observed, [True])
        self.assertIsNone(owned.fd)

    def test_short_reads_are_reassembled_within_verified_chunk_budget(self):
        from server.app import verified_media as media
        original_pread = os.pread
        sizes = []
        def short_read(fd, size, offset):
            sizes.append(size)
            return original_pread(fd, min(size, 8191), offset)
        with patch.object(media.os, 'pread', short_read):
            response = self.response(range_header='bytes=262140-262150')
            actual = asyncio.run(collect(response))
        self.assertEqual(actual, self.original[262140:262151])
        self.assertLessEqual(max(sizes), media.CHUNK_SIZE)
        self.assertGreater(len(sizes), 10)

    def test_http_size_budget_and_outside_store_refuse_before_source_read(self):
        from server.app import verified_media as media
        with TestClient(main.app) as client, patch.object(media, 'MAX_MEDIA_BYTES', 100):
            self.assertEqual(client.get(self.url).status_code, 413)
        outside = self.root/'outside.wav'
        outside.write_bytes(self.original)
        with db.session() as conn:
            conn.execute('UPDATE artifacts SET stored_path=? WHERE id=?', (str(outside), self.aid))
        with TestClient(main.app) as client, patch.object(media.os, 'pread', side_effect=AssertionError('outside read')):
            self.assertEqual(client.get(self.url).status_code, 409)

    def test_truncation_or_append_after_preparation_aborts_without_changed_bytes(self):
        for operation in ('truncate', 'append'):
            self.path.write_bytes(self.original)
            response = self.response()
            with self.path.open('r+b') as stream:
                if operation == 'truncate':
                    stream.truncate(17)
                else:
                    stream.seek(0, 2)
                    stream.write(b'ADDED')
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                asyncio.run(collect(response))

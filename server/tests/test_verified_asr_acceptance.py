"""N38 independent verified ASR input races; no model/private corpus/network."""
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from server.app import db, jobs, transcription, worker


class VerifiedAsrAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=self.root/'store'
        self.store.mkdir()
        self.path=self.store/'synthetic.wav'
        self.original=b'VERIFIED synthetic bytes\x00\xff '*24000
        self.path.write_bytes(self.original)
        self.digest=hashlib.sha256(self.original).hexdigest()
        settings=replace(db.settings,data_dir=self.root,db_path=self.root/'fixture.sqlite',store_dir=self.store)
        for module in (db,worker):
            item=patch.object(module,'settings',settings)
            item.start()
            self.addCleanup(item.stop)
        db.init_db()
        with db.session() as conn:
            self.aid=conn.execute('INSERT INTO artifacts(original_name,mime_type,stored_path,sha256,size_bytes) VALUES (?,?,?,?,?)',
                                 ('synthetic.wav','audio/wav',str(self.path),self.digest,len(self.original))).lastrowid
        transcription.queue_transcription(self.aid)
        self.claim=jobs.claim_job(worker_id='N38-independent')
        self.inputs=[]

    def adapter(self,action):
        inputs=self.inputs
        class Adapter:
            def describe(self):
                return {'model':'synthetic-test','provider':'N38','tool':'synthetic-reader'}
            def transcribe(self,source):
                inputs.append(source)
                return action(source)
        return Adapter()

    @staticmethod
    def result(text='synthetic accepted text'):
        return worker.TranscriptionResult(text,'pl',[{'start':0,'end':1,'text':text}],{'synthetic':True})

    def rows(self,table):
        with db.session() as conn:
            return [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]

    def assert_failure_recorded(self):
        self.assertEqual(self.rows('derived_text'),[])
        job=self.rows('jobs')[0]
        run=self.rows('processing_runs')[0]
        self.assertEqual(job['status'],'failed')
        self.assertEqual(run['status'],'failed')
        self.assertTrue(job['error'])
        self.assertTrue(run['error'])
        for source in self.inputs:
            self.assertTrue(source.closed)

    def test_adapter_only_path_replacement_reads_original_and_publishes_its_hash(self):
        def action(source):
            self.assertNotIsInstance(source,(str,bytes,os.PathLike))
            with self.assertRaises(TypeError):
                os.fspath(source)
            with self.assertRaises(io.UnsupportedOperation):
                source.fileno()
            backup=self.store/'retained-original'
            self.path.rename(backup)
            try:
                self.path.write_bytes(b'WRONG synthetic bytes'*20000)
                observed=source.read()
            finally:
                self.path.unlink()
                backup.rename(self.path)
            self.assertEqual(observed,self.original)
            return self.result(hashlib.sha256(observed).hexdigest())
        tid=worker.process_job(self.claim,self.adapter(action))
        row=self.rows('derived_text')[0]
        self.assertEqual(row['id'],tid)
        self.assertEqual(row['text'],self.digest)
        self.assertEqual(json.loads(row['metadata_json'])['input_sha256'],self.digest)
        self.assertEqual(self.rows('jobs')[0]['status'],'done')
        self.assertTrue(self.inputs[0].closed)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_same_inode_change_fails_before_any_bytes_return_to_adapter(self):
        returned=[]
        def action(source):
            with self.path.open('r+b') as destination:
                destination.write(b'UNVERIFIED')
            returned.append(source.read(100))
            return self.result()
        with self.assertRaises(ValueError):
            worker.process_job(self.claim,self.adapter(action))
        self.assertEqual(returned,[])
        self.assert_failure_recorded()

    def test_final_verification_rejects_mutation_in_unread_tail(self):
        observed=[]
        def action(source):
            observed.append(source.read(1))
            with self.path.open('r+b') as destination:
                destination.seek(-10,2)
                destination.write(b'ALTERED!!!')
            return self.result('must never publish')
        with self.assertRaises(ValueError):
            worker.process_job(self.claim,self.adapter(action))
        self.assertEqual(observed,[self.original[:1]])
        self.assert_failure_recorded()
        provenance=json.loads(self.rows('processing_runs')[0]['provenance_json'])
        self.assertEqual(provenance['input_verification']['initial_full_sha256'],'passed')
        self.assertEqual(provenance['input_verification']['final_same_descriptor'],'failed')

    def test_adapter_exception_preserves_run_failure_and_closes_reader(self):
        def action(source):
            self.assertEqual(source.read(9),self.original[:9])
            raise RuntimeError('synthetic ASR failure Zażółć')
        with self.assertRaisesRegex(RuntimeError,'synthetic ASR failure'):
            worker.process_job(self.claim,self.adapter(action))
        self.assert_failure_recorded()
        self.assertIn('Zażółć',self.rows('processing_runs')[0]['error'])

    def test_outside_store_path_is_rejected_before_adapter_or_source_reads(self):
        from server.app import verified_media
        outside=self.root/'outside.wav'
        outside.write_bytes(self.original)
        with db.session() as conn:
            conn.execute('UPDATE artifacts SET stored_path=? WHERE id=?',(str(outside),self.aid))
        with patch.object(verified_media.os,'pread',side_effect=AssertionError('outside read')):
            with self.assertRaises(ValueError):
                worker.process_job(self.claim,self.adapter(lambda source:self.result()))
        self.assertEqual(self.inputs,[])
        self.assert_failure_recorded()

    def test_forged_hash_or_unknown_size_does_not_reach_adapter(self):
        with db.session() as conn:
            conn.execute('UPDATE artifacts SET sha256=? WHERE id=?',('0'*64,self.aid))
        with self.assertRaises(ValueError):
            worker.process_job(self.claim,self.adapter(lambda source:self.result()))
        self.assertEqual(self.inputs,[])
        self.assert_failure_recorded()

    def test_lease_reclaimed_after_verified_read_cannot_publish_stale_result(self):
        replacement=[]
        def action(source):
            self.assertEqual(source.read(10),self.original[:10])
            with db.session() as conn:
                conn.execute('UPDATE jobs SET lease_expires_at=? WHERE id=?',(time.time()-10,self.claim['id']))
            replacement.append(jobs.claim_job(worker_id='new-owner'))
            return self.result('stale must not publish')
        with self.assertRaises(jobs.LeaseLost):
            worker.process_job(self.claim,self.adapter(action))
        self.assertEqual(self.rows('derived_text'),[])
        self.assertEqual([row['status'] for row in self.rows('processing_runs')],['abandoned','running'])
        self.assertEqual(self.rows('jobs')[0]['status'],'running')
        self.assertIsNone(self.rows('jobs')[0]['error'])
        self.assertTrue(self.inputs[0].closed)
        self.assertNotEqual(replacement[0]['lease_token'],self.claim['lease_token'])

    def reader(self):
        from server.app.verified_reader import VerifiedReader
        return VerifiedReader(self.path,self.digest,len(self.original),store_root=self.store)

    def test_reader_uses_pinned_descriptor_without_path_reopens(self):
        source=self.reader()
        try:
            with patch('builtins.open',side_effect=AssertionError('path reopen')), \
                 patch.object(os,'open',side_effect=AssertionError('descriptor reopen')):
                self.assertEqual(source.read(31),self.original[:31])
                source.seek(300000)
                self.assertEqual(source.read(97),self.original[300000:300097])
                source.verify_unchanged()
                self.assertEqual(source.tell(),300097)
        finally:
            source.close()

    def test_readinto_keeps_destination_and_position_unchanged_on_later_chunk_failure(self):
        from server.app.verified_media import CHUNK_SIZE
        with self.reader() as source:
            with self.path.open('r+b') as destination:
                destination.seek(CHUNK_SIZE+5)
                destination.write(b'CHANGED')
            output=bytearray(b'?'*(CHUNK_SIZE+100))
            before=bytes(output)
            with self.assertRaises(ValueError):
                source.readinto(output)
            self.assertEqual(bytes(output),before)
            self.assertEqual(source.tell(),0)

    def test_reader_caps_and_seek_preserve_exact_verified_bytes(self):
        from server.app import verified_reader as reader
        with self.reader() as source,patch.object(reader,'MAX_READ_BYTES',16):
            self.assertEqual(source.read(16),self.original[:16])
            with self.assertRaises(ValueError):
                source.read(17)
            self.assertEqual(source.tell(),16)
            with self.assertRaises(ValueError):
                source.verify_unchanged()
        with self.reader() as source,patch.object(reader,'MAX_READ_BYTES',16):
            with self.assertRaises(ValueError):
                source.read()
            self.assertEqual(source.tell(),0)
        with self.reader() as source:
            self.assertEqual(source.seek(-8,os.SEEK_END),len(self.original)-8)
            target=bytearray(8)
            self.assertEqual(source.readinto(target),8)
            self.assertEqual(target,self.original[-8:])
            self.assertEqual(source.read(1),b'')
            source.verify_unchanged()
            self.assertEqual(source.tell(),len(self.original))

    def test_unknown_size_fails_before_adapter_and_records_failure(self):
        with db.session() as conn:
            conn.execute('UPDATE artifacts SET size_bytes=NULL WHERE id=?',(self.aid,))
        with self.assertRaises(ValueError):
            worker.process_job(self.claim,self.adapter(lambda source:self.result()))
        self.assertEqual(self.inputs,[])
        self.assert_failure_recorded()

    def test_adapter_cannot_swallow_read_failure_restore_bytes_and_publish(self):
        caught=[]
        def action(source):
            with self.path.open('r+b') as destination:
                destination.write(b'UNVERIFIED')
            try:
                source.read(100)
            except ValueError:
                caught.append(True)
            self.path.write_bytes(self.original)
            return self.result('ignored failure must not publish')
        with self.assertRaises(ValueError):
            worker.process_job(self.claim,self.adapter(action))
        self.assertEqual(caught,[True])
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assert_failure_recorded()

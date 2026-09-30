"""HTTP integration checks using synthetic sources and no actual ASR model."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.app import db, evidence, jobs, main


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = self.root/'store'
        self.store.mkdir()
        self.source_root = self.root/'sources'
        self.source_root.mkdir()
        self.settings = replace(db.settings, data_dir=self.root, db_path=self.root/'db.sqlite',
                                store_dir=self.store, scan_roots=(self.source_root,))
        self.patches = [patch.object(module, 'settings', self.settings) for module in (db,evidence,main)]
        for p in self.patches:
            p.start()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None,None,None)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def upload(self, locator='content://synthetic/one', label='synthetic'):
        response = self.client.post('/api/artifacts/upload',
            files={'file':('same.wav',b'synthetic audio bytes','audio/wav')},
            data={'source_locator':locator,'source_label':label,
                  'metadata_json':json.dumps({'client_relative_path':'folder/same.wav'})})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['artifact_id']

    def transcript(self, artifact):
        return evidence.add_derived_text(artifact,'transcript',' A  B',segments=[
            {'start':0.25,'end':1.5,'text':' A '},{'start':1.5,'end':2.0,'text':' B'}])

    def test_upload_retains_locator_contexts_and_distinct_locations(self):
        first = self.upload()
        self.assertEqual(first,self.upload(label='second source context'))
        other = self.upload(locator='content://synthetic/two')
        self.assertNotEqual(first,other)
        observations = self.client.get(f'/api/artifacts/{first}/source-observations').json()
        self.assertEqual(len(observations),2)
        self.assertNotEqual(observations[0]['source_id'],observations[1]['source_id'])
        self.assertEqual(observations[0]['source_locator'],'content://synthetic/one')
        metadata = json.loads(observations[0]['metadata_json'])['source_metadata']
        self.assertEqual(metadata['client_metadata']['client_relative_path'],'folder/same.wav')
        self.assertIn('temporary_upload',metadata['filesystem_stat_scope'])
        self.assertEqual(self.client.get(f'/api/artifacts/{first}/content').content,b'synthetic audio bytes')

    def test_invalid_upload_metadata_is_rejected_before_ingest(self):
        for value in ('[]','{"x":NaN}','{"x":1e999}','{"nested":[1e999]}','not-json'):
            response = self.client.post('/api/artifacts/upload',files={'file':('a.wav',b'bytes','audio/wav')},data={'metadata_json':value})
            self.assertEqual(response.status_code,422)
        with db.session() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM artifacts').fetchone()[0],0)

    def test_repeated_sms_http_import_uses_stable_original_locator(self):
        xml = b'<smses count="2"><sms date="1000" type="1" address="+44123" body="same"/><sms date="1000" type="1" address="+44123" body="same"/></smses>'
        for _ in range(2):
            response = self.client.post('/import/sms-backup',files={'file':('messages.xml',xml,'application/xml')})
            self.assertEqual(response.status_code,200,response.text)
        with db.session() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM artifacts').fetchone()[0],1)
            self.assertEqual(connection.execute('SELECT count(*) FROM events').fetchone()[0],2)
            self.assertEqual(connection.execute('SELECT count(*) FROM source_observations').fetchone()[0],2)
            self.assertEqual(connection.execute('SELECT original_name FROM artifacts').fetchone()[0],'messages.xml')

    def test_repeated_whatsapp_http_import_is_idempotent(self):
        txt = b'[30/09/2026, 8:15 PM] Alice: message\ncontinuation\n'
        for _ in range(2):
            response = self.client.post('/import/whatsapp',files={'file':('chat.txt',txt,'text/plain')})
            self.assertEqual(response.status_code,200,response.text)
        with db.session() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM artifacts').fetchone()[0],1)
            self.assertEqual(connection.execute('SELECT count(*) FROM events').fetchone()[0],1)

    def test_annotation_and_citation_pin_the_displayed_version(self):
        artifact = self.upload()
        version = self.transcript(artifact)
        annotation = self.client.post(f'/api/artifacts/{artifact}/annotations',json={
            'body':'interpretation','derivedTextId':version,'startMs':250,'endMs':1500})
        self.assertEqual(annotation.status_code,200,annotation.text)
        self.assertEqual(annotation.json()['derivedTextId'],version)
        self.assertTrue(annotation.json()['createdAt'])
        citation = self.client.post(f'/api/artifacts/{artifact}/citations',json={
            'derivedTextId':version,'segmentIndices':[0,1]})
        self.assertEqual(citation.status_code,200,citation.text)
        value = citation.json()
        self.assertEqual(value['quote_text'],' A  B')
        self.assertEqual(value['quote_sha256'],hashlib.sha256(b' A  B').hexdigest())
        self.assertEqual(value['audio_verification'],'not_performed')
        newer = evidence.add_derived_text(artifact,'transcript','new',segments=[])
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact}/transcript').json()['id'],newer)
        pinned = self.client.get(f'/api/artifacts/{artifact}/transcript',params={'derived_text_id':version}).json()
        self.assertEqual(pinned['id'],version)
        self.assertEqual(pinned['provenanceStatus'],'legacy_unknown')
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact}/citations').json()[0]['derived_text_id'],version)
        package = self.client.get('/export/manifest.json').json()
        validated = self.client.post('/api/packages/validate',json=package).json()
        self.assertTrue(validated['valid'],validated)
        self.assertEqual(package['tables']['annotations'][0]['derived_text_id'],version)
        self.assertFalse(package['source_bytes_included'])
        self.assertFalse(package['replay_supported'])

    def test_annotation_rejects_wrong_version_invalid_ranges_and_blank_body(self):
        artifact = self.upload()
        other = self.upload(locator='content://synthetic/two')
        version = self.transcript(other)
        bodies = [
            {'body':'x','derivedTextId':version},
            {'body':'x','startMs':0,'endMs':1},
            {'body':'x','startMs':0},
            {'body':'x','startMs':-1,'endMs':0},
            {'body':'x','startMs':2,'endMs':1},
            {'body':'   '}, {'body':'x','derivedTextId':True}, {'body':'x','derivedTextId':10**100}]
        for body in bodies:
            self.assertEqual(self.client.post(f'/api/artifacts/{artifact}/annotations',json=body).status_code,422)
        version = self.transcript(artifact)
        self.assertEqual(self.client.post(f'/api/artifacts/{artifact}/annotations',json={
            'body':'x','derivedTextId':version,'startMs':0,'endMs':2001}).status_code,422)
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact}/annotations').json(),[])

    def test_citation_api_rejects_coerced_indices(self):
        artifact = self.upload()
        version = self.transcript(artifact)
        for index in (True,0.0,'0',-1,10**100):
            response = self.client.post(f'/api/artifacts/{artifact}/citations',json={
                'derivedTextId':version,'segmentIndices':[index]})
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.client.post(f'/api/artifacts/{artifact}/citations',json={
            'derivedTextId':10**100,'segmentIndices':[0]}).status_code,422)
        self.assertEqual(self.client.get(f'/api/artifacts/{10**100}/transcript').status_code,422)

    def test_queue_reports_pending_even_when_old_transcript_exists(self):
        artifact = self.upload()
        self.transcript(artifact)
        response = self.client.post(f'/api/artifacts/{artifact}/transcribe',params={'priority':55})
        self.assertEqual(response.status_code,200,response.text)
        job = response.json()['job_id']
        self.assertEqual(self.client.get('/api/artifacts').json()[0]['transcriptStatus'],'queued')
        self.client.post(f'/api/artifacts/{artifact}/transcribe')
        status = self.client.get(f'/api/jobs/{job}').json()
        self.assertEqual(status['priority'],55)
        self.assertEqual(status['status'],'queued')
        self.assertEqual(self.client.post('/api/artifacts/99999/transcribe').status_code,404)

    def test_old_failure_does_not_override_new_success(self):
        artifact = self.upload()
        self.client.post(f'/api/artifacts/{artifact}/transcribe')
        first = jobs.claim_job()
        jobs.finish_job(first['id'],first['lease_token'],'failed','synthetic error')
        self.client.post(f'/api/artifacts/{artifact}/transcribe')
        latest = jobs.claim_job()
        jobs.update_run_metadata(latest,{'tool':'synthetic','tool_version':'test','model':'fake'})
        version = jobs.publish_transcript(latest,text='completed',segments=[])
        self.assertEqual(self.client.get('/api/artifacts').json()[0]['transcriptStatus'],'done')
        transcript = self.client.get(f'/api/artifacts/{artifact}/transcript').json()
        self.assertEqual(transcript['id'],version)
        self.assertEqual(transcript['runId'],latest['run_id'])
        self.assertEqual(transcript['run']['tool'],'synthetic')
        self.assertNotIn('lease_token',transcript['run'])

    def test_scan_declared_root_leaves_sources_unchanged(self):
        source = self.source_root/'billing.csv'
        source.write_text('recording,phone,duration\nsame.wav,+44123,20\n',encoding='utf-8')
        before = source.read_bytes(),source.stat().st_mtime_ns
        response = self.client.post('/api/source-scans',json={'rootId':0})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['schema_version'],'ageds.source-scan/v1')
        self.assertEqual((source.read_bytes(),source.stat().st_mtime_ns),before)
        self.assertEqual(list(self.source_root.iterdir()),[source])

    def test_scan_scope_cannot_escape_operator_roots(self):
        for relative in ('../store',str(self.store)):
            self.assertEqual(self.client.post('/api/source-scans',json={'rootId':0,'relativePath':relative}).status_code,422)
        self.assertEqual(self.client.post('/api/source-scans',json={'rootId':100}).status_code,404)
        (self.source_root/'escape').symlink_to(self.store,target_is_directory=True)
        self.assertEqual(self.client.post('/api/source-scans',json={'rootId':0,'relativePath':'escape'}).status_code,422)
        with patch.object(main,'settings',replace(self.settings,scan_roots=())):
            self.assertEqual(self.client.post('/api/source-scans',json={'rootId':0}).status_code,403)

    def test_content_integrity_and_source_path_are_checked(self):
        artifact = self.upload()
        with db.session() as connection:
            path = Path(connection.execute('SELECT stored_path FROM artifacts WHERE id=?',(artifact,)).fetchone()[0])
        path.chmod(0o644)
        path.write_bytes(b'tampered')
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact}/content').status_code,409)
        with db.session() as connection:
            connection.execute('UPDATE artifacts SET stored_path=? WHERE id=?',(str(self.source_root/'outside'),artifact))
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact}/content').status_code,409)


if __name__ == '__main__':
    unittest.main()

"""HTTP routes of the media area: JSON contracts and server-rendered pages (fake engines only)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from server.app import main, worker
from server.app.jobs import claim_job
from server.tests.test_media_ocr_diarization import CHAT_WORDS, FakeDiarizer, FakeOCR, png_bytes, tsv
from server.tests.test_media_support import MediaCase


class MediaApiTests(MediaCase):
    def setUp(self):
        super().setUp()
        self.fake_whisper()
        context = TestClient(main.app)
        self.client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.aid = self.add_audio()

    def post(self, url, payload, status=200):
        response = self.client.post(url, json=payload)
        self.assertEqual(status, response.status_code, response.text)
        return response.json()

    def test_enqueue_dedup_versions_compare_and_run_recipe(self):
        first = self.post(f'/api/media/artifacts/{self.aid}/jobs', {})
        again = self.post(f'/api/media/artifacts/{self.aid}/jobs', {'priority': 4})
        self.assertEqual((first['job_id'], False), (again['job_id'], again['created']))
        second = self.post(f'/api/media/artifacts/{self.aid}/jobs', {'recipe': 'per-segment-pl-nl-en'})
        self.assertNotEqual(first['recipe']['sha256'], second['recipe']['sha256'])
        self.post(f'/api/media/artifacts/{self.aid}/jobs', {'recipe': 'nope'}, 422)
        self.post(f'/api/media/artifacts/{self.aid}/jobs', {'overrides': {'id': 'x'}}, 422)
        self.post(f'/api/media/artifacts/{self.aid}/jobs', {'overrides': {'model_options': {'download_root': '/tmp/x'}}}, 422)
        self.post(f'/api/media/artifacts/{self.aid}/jobs', {'overrides': {'model_options': {'local_files_only': False}}}, 422)
        self.post(f'/api/media/artifacts/{self.aid}/jobs', {'kind': 'summarize'}, 422)
        self.post(f'/api/media/artifacts/{self.aid}/jobs', {'unexpected': 1}, 422)
        self.post('/api/media/artifacts/999/jobs', {}, 404)
        queue = self.client.get('/api/media/queue').json()
        self.assertEqual([first['job_id'], second['job_id']], [j['job_id'] for j in queue['jobs']])
        self.assertEqual('unknown', queue['jobs'][0]['estimate']['status'])
        legacy = worker.process_job(claim_job())
        switched = worker.process_job(claim_job())
        versions = self.client.get(f'/api/media/artifacts/{self.aid}/versions').json()['versions']
        self.assertEqual([switched, legacy], [v['id'] for v in versions])
        comparison = self.client.get(f'/api/media/compare?a={legacy}&b={switched}').json()
        self.assertEqual(comparison, self.client.get(f'/api/media/compare?a={legacy}&b={switched}').json())
        self.assertEqual(422, self.client.get(f'/api/media/compare?a={legacy}&b={legacy}').status_code)
        self.assertEqual(404, self.client.get(f'/api/media/compare?a={legacy}&b=999').status_code)
        run_id = versions[0]['run_id']
        recipe = self.client.get(f'/api/media/runs/{run_id}/recipe').json()
        self.assertEqual('hash_matches', recipe['verification']['status'])
        self.assertEqual(second['recipe']['sha256'], recipe['recipe']['sha256'])
        html = self.client.get(f'/media/compare?a={legacy}&b={switched}')
        self.assertEqual(200, html.status_code)
        self.assertIn('<del>', html.text)
        self.assertIn('Porównanie', html.text)

    def test_pages_render_with_navigation_and_escape_model_text(self):
        transcript = self.post(f'/api/media/artifacts/{self.aid}/jobs', {})
        did = worker.process_job(claim_job())
        self.assertTrue(transcript['created'])
        for path in ('/media', f'/media/artifact/{self.aid}', '/media/queue',
                     f'/media/artifact/{self.aid}/speakers?derived_text_id={did}'):
            with self.subTest(path=path):
                page = self.client.get(path)
                self.assertEqual(200, page.status_code, page.text[:500])
                self.assertIn('href="/media"', page.text)
                self.assertIn('Transkrypcje', page.text)
        image = self.add_artifact(png_bytes(), '<script>.png', 'image/png')
        self.post(f'/api/media/artifacts/{image}/jobs', {'kind': 'ocr', 'recipe': 'ocr-chat-screenshot-pol-nld-eng'})
        words = list(CHAT_WORDS) + [(5, 1, 1, 1, 20, 400, 80, 20, 50.0, '<b>x</b>')]
        worker.process_job(claim_job(), FakeOCR([tsv(360, 640, words)]))
        page = self.client.get(f'/media/artifact/{image}')
        self.assertNotIn('<script>.png', page.text)
        self.assertNotIn('<b>x</b>', page.text)
        self.assertIn('&lt;b&gt;x&lt;/b&gt;', page.text)
        candidates = self.client.get(f'/api/media/artifacts/{image}/event-candidates').json()
        self.assertEqual({'candidate'}, {c['status'] for c in candidates['candidates']})
        self.assertIn('time_box', candidates['candidates'][1]['locators'])

    def test_form_enqueue_priority_and_speaker_assignment(self):
        response = self.client.post(f'/media/artifact/{self.aid}/jobs', data={'kind': 'transcribe', 'recipe': 'legacy'},
                                    follow_redirects=False)
        self.assertEqual(303, response.status_code)
        job_id = self.rows('jobs')[0]['id']
        response = self.client.post(f'/media/jobs/{job_id}/priority', data={'priority': '7'}, follow_redirects=False)
        self.assertEqual((303, 7), (response.status_code, self.rows('jobs')[0]['priority']))
        self.post(f'/api/media/jobs/{job_id}/priority', {'priority': 2})
        self.post(f'/api/media/jobs/{job_id}/priority', {'priority': '2'}, 422)
        did = worker.process_job(claim_job())
        self.post(f'/api/media/jobs/{job_id}/priority', {'priority': 1}, 409)
        self.post(f'/api/media/artifacts/{self.aid}/jobs', {'kind': 'diarize'})
        run_id = worker.process_job(claim_job(), FakeDiarizer([{'start': 0.0, 'end': 9.0, 'speaker': 'SPEAKER_00'},
                                                                {'start': 9.0, 'end': 18.0, 'speaker': 'SPEAKER_01'}]))
        view = self.client.get(f'/api/media/artifacts/{self.aid}/speakers?derived_text_id={did}').json()
        self.assertEqual(run_id, view['diarization_run_id'])
        self.assertEqual('SPEAKER_00', view['segments'][0]['display_speaker'])
        self.post(f'/api/media/artifacts/{self.aid}/speaker-assignments',
                  {'speaker': 'Speaker A', 'diarizationRunId': run_id, 'speakerLabel': 'SPEAKER_00'})
        self.post(f'/api/media/artifacts/{self.aid}/speaker-assignments', {'speaker': 'x', 'derivedTextId': did}, 422)
        response = self.client.post(f'/media/artifact/{self.aid}/speakers',
                                    data={'speaker': 'Speaker B', 'derived_text_id': str(did), 'segment_index': '0'},
                                    follow_redirects=False)
        self.assertEqual(303, response.status_code)
        view = self.client.get(f'/api/media/artifacts/{self.aid}/speakers?derived_text_id={did}').json()
        self.assertEqual(('Speaker B', 'manual_segment', 'SPEAKER_00'),
                         (view['segments'][0]['display_speaker'], view['segments'][0]['display_source'],
                          view['segments'][0]['hypothesis_label']))
        self.assertEqual('manual_label_mapping', view['segments'][1]['display_source'])
        capabilities = self.client.get('/api/media/capabilities').json()
        self.assertIn(capabilities['diarization_pyannote']['status'], ('available', 'unavailable'))
        recipes = self.client.get('/api/media/recipes?kind=ocr').json()['recipes']
        self.assertEqual({'ocr'}, {r['kind'] for r in recipes})

    def test_cancel_over_http_and_from_the_queue_page(self):
        job = self.post(f'/api/media/artifacts/{self.aid}/jobs', {})['job_id']
        page = self.client.get('/media/queue').text
        self.assertIn(f'data-cancel-job="{job}"', page)
        self.assertEqual(self.post(f'/api/media/jobs/{job}/cancel', None)['status'], 'cancelled')
        self.post(f'/api/media/jobs/{job}/cancel', None, 409)
        self.post('/api/media/jobs/999/cancel', None, 404)
        listed = self.client.get('/api/media/queue?status=cancelled').json()['jobs']
        self.assertEqual([(job, 'cancelled')], [(j['job_id'], j['status']) for j in listed])
        other = self.post(f'/api/media/artifacts/{self.aid}/jobs', {})['job_id']
        form = self.client.post(f'/media/jobs/{other}/cancel', follow_redirects=False)
        self.assertEqual((form.status_code, form.headers['location']), (303, '/media/queue'))
        self.assertNotIn(f'data-cancel-job="{other}"', self.client.get('/media/queue').text)

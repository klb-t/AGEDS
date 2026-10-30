"""Queue listing with measured estimates; unknown without data; manual priority."""
from __future__ import annotations

import json

from server.app import db, worker
from server.app.jobs import claim_job
from server.app.media.estimates import cancel_job, queue_listing, set_job_priority
from server.app.media.queue import enqueue_media_job
from server.app.transcription import enqueue_transcription
from server.tests.test_media_support import MediaCase


class QueueEstimateTests(MediaCase):
    def setUp(self):
        super().setUp()
        self.aid = self.add_audio('audio_single_en')      # WAV header declares 4.0 s

    def add_measured_run(self, recipe_sha: str, seconds: int, audio_seconds: float, kind='transcribe'):
        """A completed run on other content, so the queued input's own work amount stays its WAV header."""
        other = self.add_artifact(f'other content {seconds}'.encode(), f'other-{seconds}.wav', 'audio/wav')
        with db.session() as conn:
            job = conn.execute("INSERT INTO jobs(kind,artifact_id,status) VALUES (?,?,'done')", (kind, other)).lastrowid
            conn.execute("""INSERT INTO processing_runs(job_id,artifact_id,lease_token,status,started_at,finished_at,
                            provenance_json,metadata_json) VALUES (?,?,?,'done',?,?,?,?)""",
                         (job, other, f'token-{job}', '2026-10-16T10:00:00+00:00',
                          f'2026-10-16T10:00:{seconds:02d}+00:00', json.dumps({'recipe': {'sha256': recipe_sha}}),
                          json.dumps({'work': {'unit': 'audio_second', 'amount': audio_seconds, 'basis': 'test'}})))

    def test_no_measurement_means_unknown_not_a_guess(self):
        queued = enqueue_transcription(self.aid)
        listing = queue_listing()
        item = listing['jobs'][0]
        self.assertEqual(queued['job_id'], item['job_id'])
        self.assertEqual('unknown', item['estimate']['status'])
        self.assertIn('no completed run with this recipe hash has a measured work amount', item['estimate']['reasons'])
        self.assertEqual('wav_header_declared_duration', item['estimate']['work']['basis'])
        self.assertIsNone(item['queued_seconds_median_through_this_job'])

    def test_measured_rate_per_recipe_hash_with_sample_size_and_spread(self):
        legacy = enqueue_transcription(self.aid)
        other = enqueue_transcription(self.aid, recipe='per-segment-pl-nl-en')
        self.add_measured_run(legacy['recipe']['sha256'], 10, 5.0)    # 2.0 s per audio second
        self.add_measured_run(legacy['recipe']['sha256'], 30, 10.0)   # 3.0 s per audio second
        self.add_measured_run('another-hash', 59, 1.0)
        items = {item['job_id']: item for item in queue_listing()['jobs']}
        estimate = items[legacy['job_id']]['estimate']
        self.assertEqual('measured', estimate['status'])
        self.assertEqual(2, estimate['sample_size'])
        self.assertEqual((8.0, 10.0, 12.0), (estimate['seconds_min'], estimate['seconds_median'], estimate['seconds_max']))
        self.assertEqual(10.0, items[legacy['job_id']]['queued_seconds_median_through_this_job'])
        self.assertEqual('unknown', items[other['job_id']]['estimate']['status'])
        self.assertIsNone(items[other['job_id']]['queued_seconds_median_through_this_job'])

    def test_real_worker_runs_feed_the_estimate_for_the_same_hash(self):
        self.fake_whisper()
        first = enqueue_transcription(self.aid, recipe='per-segment-pl-nl-en')
        worker.process_job(claim_job())
        second = enqueue_transcription(self.aid, recipe='per-segment-pl-nl-en')
        self.assertNotEqual(first['job_id'], second['job_id'])
        item = queue_listing()['jobs'][0]
        self.assertEqual('measured', item['estimate']['status'])
        self.assertEqual(1, item['estimate']['sample_size'])
        self.assertEqual('measured_by_earlier_run_on_same_content_sha256', item['estimate']['work']['basis'])

    def test_ocr_estimates_use_pages_and_pinned_hash(self):
        image = self.add_artifact(b'\x89PNG\r\n\x1a\n' + b'0' * 32, 'page.png', 'image/png')
        queued = enqueue_media_job('ocr', image)
        item = queue_listing()['jobs'][0]
        self.assertEqual(('ocr', 'single_image'), (item['kind'], item['estimate']['work']['basis']))
        self.assertEqual(queued['recipe']['sha256'], item['recipe']['sha256'])

    def test_unpinned_and_running_jobs_are_listed_explicitly(self):
        with db.session() as conn:
            conn.execute("INSERT INTO jobs(kind,artifact_id,priority) VALUES ('transcribe',?,5)", (self.aid,))
        enqueue_media_job('diarize', self.aid, priority=1)
        running = claim_job()
        listing = queue_listing()
        self.assertEqual(['running', 'queued'], [item['status'] for item in listing['jobs']])
        self.assertEqual(running['id'], listing['jobs'][0]['job_id'])
        self.assertEqual('unpinned_pre_recipe_job', listing['jobs'][0]['recipe']['pin_status'])
        self.assertIn('job has no pinned recipe hash', listing['jobs'][0]['estimate']['reasons'])
        with self.assertRaises(ValueError):
            queue_listing(statuses=('lost',))

    def test_manual_priority_changes_claim_order_and_is_audited(self):
        first = enqueue_transcription(self.aid)['job_id']
        second = enqueue_media_job('diarize', self.aid)['job_id']
        self.assertEqual({'job_id': second, 'previous_priority': 0, 'priority': 50}, set_job_priority(second, 50))
        self.assertEqual(second, claim_job()['id'])
        with self.assertRaisesRegex(ValueError, 'only queued'):
            set_job_priority(second, 1)
        with self.assertRaises(LookupError):
            set_job_priority(9999, 1)
        self.assertIn('job_reprioritized', [r['action'] for r in self.rows('audit_log')])
        self.assertEqual(first, claim_job()['id'])

    def test_a_queued_job_can_be_cancelled_and_is_never_claimed(self):
        first = enqueue_transcription(self.aid)['job_id']
        second = enqueue_media_job('diarize', self.aid)['job_id']
        result = cancel_job(first, actor='owner')
        self.assertEqual((result['job_id'], result['status']), (first, 'cancelled'))
        [row] = [r for r in self.rows('jobs') if r['id'] == first]
        self.assertEqual((row['status'], row['error'], row['finished_at']), ('cancelled', 'cancelled by owner', result['finished_at']))
        [audit] = [r for r in self.rows('audit_log') if r['action'] == 'job_cancelled']
        self.assertEqual((audit['actor'], audit['object_id'], json.loads(audit['details_json'])), ('owner', first, {'previous_status': 'queued'}))
        self.assertEqual(second, claim_job()['id'])
        self.assertIsNone(claim_job())
        with self.assertRaisesRegex(ValueError, r'only queued jobs can be cancelled \(job is running\)'):
            cancel_job(second)
        with self.assertRaisesRegex(ValueError, r'\(job is cancelled\)'):
            cancel_job(first)
        with self.assertRaises(LookupError):
            cancel_job(9999)
        # a cancelled job does not block queueing the same work again
        self.assertNotEqual(first, enqueue_transcription(self.aid)['job_id'])


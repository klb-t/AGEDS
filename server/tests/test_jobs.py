"""Lease/fencing tests use synthetic bytes and ASR adapters, never model downloads."""
from __future__ import annotations
import hashlib
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from server.app import db, worker
from server.app.verified_media import MediaIntegrityError
from server.app.jobs import (LeaseLost, claim_job, finish_job, publish_transcript,
                             recover_expired_jobs, renew_lease, update_run_metadata)
from server.app.transcription import queue_all_audio, queue_transcription
from server.app.worker import TranscriptionResult, process_job


class FakeASR:
    def __init__(self, text="synthetic result", delay=0):
        self.text, self.delay, self.calls = text, delay, 0
    def describe(self):
        return {"tool": "synthetic-asr", "tool_version": "test-1", "provider": "local-test",
                "model": "fake-model", "parameters": {"word_timestamps": True}}
    def transcribe(self, path):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return TranscriptionResult(self.text, "pl", [{"start": 0.0, "end": 1.0, "text": self.text}],
                                   {"language_probability": 0.99,
                                    "language_probability_meaning": "language identification only"})


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.settings = replace(db.settings, db_path=self.path / "evidence.db", store_dir=self.path / "store")
        self.settings.store_dir.mkdir()
        self.config_patch = patch.object(db, "settings", self.settings)
        self.config_patch.start()
        self.worker_config_patch = patch.object(worker, "settings", self.settings)
        self.worker_config_patch.start()
        db.init_db()
        self.audio = self.settings.store_dir / "fake.wav"
        self.audio.write_bytes(b"synthetic bytes, not a recording")
        with db.session() as conn:
            cur = conn.execute("INSERT INTO artifacts(original_name,mime_type,stored_path,sha256,size_bytes) VALUES (?,?,?,?,?)",
                               ("fake.wav", "audio/wav", str(self.audio), hashlib.sha256(self.audio.read_bytes()).hexdigest(), self.audio.stat().st_size))
            self.artifact_id = int(cur.lastrowid)

    def tearDown(self):
        self.worker_config_patch.stop()
        self.config_patch.stop()
        self.tmp.cleanup()

    def rows(self, table):
        with db.session() as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]

    def queue_claim(self, **kwargs):
        queue_transcription(self.artifact_id)
        return claim_job(**kwargs)

    def test_requeue_reprioritizes_in_place_and_order_is_correct(self):
        first = queue_transcription(self.artifact_id, 1)
        with db.session() as conn:
            second_art = conn.execute("INSERT INTO artifacts(original_name,mime_type) VALUES ('other.wav','audio/wav')").lastrowid
        queue_transcription(second_art, 5)
        self.assertEqual(first, queue_transcription(self.artifact_id, 10))
        claimed = claim_job()
        self.assertEqual(first, claimed["id"])
        self.assertEqual(10, claimed["priority"])
        self.assertEqual(1, claimed["attempts"])
        self.assertEqual("running", claimed["status"])
        self.assertEqual(first, queue_transcription(self.artifact_id, 3))
        self.assertEqual(3, self.rows("jobs")[0]["priority"])
        actions = [row["action"] for row in self.rows("audit_log")]
        self.assertEqual(2, actions.count("job_reprioritized"))

    def test_simultaneous_enqueue_creates_one_active_job(self):
        barrier = threading.Barrier(2)
        def enqueue():
            barrier.wait()
            return queue_transcription(self.artifact_id, 7)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(enqueue) for _ in range(2)]
            ids = [future.result() for future in futures]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(1, len(self.rows("jobs")))

    def test_bulk_enqueue_preserves_existing_priority_and_explicit_zero_lowers_it(self):
        job_id = queue_transcription(self.artifact_id, 50)
        self.assertEqual(1, queue_all_audio())
        self.assertEqual(50, self.rows("jobs")[0]["priority"])
        self.assertEqual(job_id, queue_transcription(self.artifact_id, 0))
        self.assertEqual(0, self.rows("jobs")[0]["priority"])

    def test_invalid_lease_durations_and_clocks_are_rejected_without_writes(self):
        queue_transcription(self.artifact_id)
        for invalid in [0, -1, float("nan"), float("inf"), 86401]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                claim_job(lease_seconds=invalid)
        self.assertEqual([], self.rows("processing_runs"))
        job = claim_job()
        original_expiry = self.rows("jobs")[0]["lease_expires_at"]
        with self.assertRaises(ValueError):
            renew_lease(job["id"], job["lease_token"], lease_seconds=float("nan"))
        with self.assertRaises(ValueError):
            renew_lease(job["id"], job["lease_token"], now=float("inf"))
        self.assertEqual(original_expiry, self.rows("jobs")[0]["lease_expires_at"])

    def test_two_workers_cannot_claim_one_job(self):
        queue_transcription(self.artifact_id)
        barrier = threading.Barrier(2)
        def claim(owner):
            barrier.wait()
            return claim_job(worker_id=owner)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim, f"worker-{n}") for n in range(2)]
            claims = [future.result() for future in futures]
        self.assertEqual(1, sum(job is not None for job in claims))
        self.assertEqual(1, len(self.rows("processing_runs")))

    def test_crashed_attempt_is_recovered_and_stale_worker_is_fenced(self):
        old = self.queue_claim(now=1000, lease_seconds=10)
        new = claim_job(worker_id="replacement", now=1011, lease_seconds=10)
        self.assertEqual(old["id"], new["id"])
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        self.assertEqual(2, new["attempts"])
        self.assertFalse(renew_lease(old["id"], old["lease_token"], now=1012))
        self.assertFalse(finish_job(old["id"], old["lease_token"], "failed", "stale", now=1012))
        with self.assertRaises(LeaseLost):
            update_run_metadata(old, {"model": "stale"}, now=1012)
        with self.assertRaises(LeaseLost):
            publish_transcript(old, text="stale result", now=1012)
        publish_transcript(new, text="fresh result", now=1012)
        self.assertEqual(["abandoned", "done"], [r["status"] for r in self.rows("processing_runs")])
        self.assertEqual(["fresh result"], [r["text"] for r in self.rows("derived_text")])
        self.assertEqual("done", self.rows("jobs")[0]["status"])

    def test_expired_lease_cannot_publish_before_recovery(self):
        job = self.queue_claim(now=1000, lease_seconds=10)
        with self.assertRaises(LeaseLost):
            publish_transcript(job, text="too late", now=1010)
        self.assertEqual([], self.rows("derived_text"))
        self.assertFalse(renew_lease(job["id"], job["lease_token"], now=1010))
        self.assertEqual(1, recover_expired_jobs(now=1010))

    def test_worker_finishing_after_recovery_does_not_fail_replacement_attempt(self):
        claimed_again = []
        artifact_id = self.artifact_id
        class ReclaimedDuringASR(FakeASR):
            def transcribe(self, path):
                with db.session() as conn:
                    conn.execute("UPDATE jobs SET lease_expires_at=? WHERE artifact_id=? AND status='running'", (time.time() - 1, artifact_id))
                claimed_again.append(claim_job(worker_id="replacement-worker"))
                return super().transcribe(path)
        old = self.queue_claim()
        with self.assertRaises(LeaseLost):
            process_job(old, ReclaimedDuringASR())
        self.assertEqual([], self.rows("derived_text"))
        self.assertEqual(["abandoned", "running"], [r["status"] for r in self.rows("processing_runs")])
        self.assertEqual("running", self.rows("jobs")[0]["status"])
        self.assertIsNone(self.rows("jobs")[0]["error"])
        process_job(claimed_again[0], FakeASR("replacement result"))
        self.assertEqual(["replacement result"], [r["text"] for r in self.rows("derived_text")])

    def test_repeat_publication_after_commit_cannot_duplicate_output(self):
        job = self.queue_claim()
        publish_transcript(job, text="one committed result")
        with self.assertRaises(LeaseLost):
            publish_transcript(job, text="duplicate")
        self.assertEqual(1, len(self.rows("derived_text")))
        self.assertIsNone(claim_job())

    def test_publication_failure_rolls_back_result_fts_audit_and_completion(self):
        job = self.queue_claim()
        from server.app import evidence
        original = evidence.add_derived_text
        def insert_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulate transaction failure")
        with patch.object(evidence, "add_derived_text", insert_then_fail):
            with self.assertRaisesRegex(RuntimeError, "transaction failure"):
                publish_transcript(job, text="must roll back")
        self.assertEqual([], self.rows("derived_text"))
        with db.session() as conn:
            self.assertEqual(0, conn.execute("SELECT count(*) FROM search_fts").fetchone()[0])
        self.assertEqual("running", self.rows("jobs")[0]["status"])
        self.assertEqual("running", self.rows("processing_runs")[0]["status"])
        self.assertNotIn("derive_text", [r["action"] for r in self.rows("audit_log")])

    def test_every_new_completed_processing_has_a_new_version_and_run(self):
        first_job = self.queue_claim()
        first_id = process_job(first_job, FakeASR("first"))
        second_job = self.queue_claim()
        second_id = process_job(second_job, FakeASR("second"))
        self.assertNotEqual(first_job["id"], second_job["id"])
        self.assertNotEqual(first_id, second_id)
        texts = self.rows("derived_text")
        self.assertEqual(["first", "second"], [r["text"] for r in texts])
        self.assertEqual([first_job["run_id"], second_job["run_id"]], [r["run_id"] for r in texts])
        self.assertEqual(2, len(self.rows("processing_runs")))

    def test_model_provenance_and_language_probability_do_not_become_transcript_confidence(self):
        job = self.queue_claim()
        process_job(job, FakeASR())
        run = self.rows("processing_runs")[0]
        self.assertEqual("synthetic-asr", run["tool"])
        self.assertEqual("test-1", run["tool_version"])
        self.assertEqual("local-test", run["provider"])
        self.assertEqual("unknown", run["model_version"])
        self.assertEqual({"word_timestamps": True}, json.loads(run["parameters_json"]))
        self.assertIn("+00:00", run["started_at"])
        self.assertIn("+00:00", run["finished_at"])
        self.assertEqual(hashlib.sha256(self.audio.read_bytes()).hexdigest(), json.loads(run["provenance_json"])["input_sha256"])
        transcript = self.rows("derived_text")[0]
        self.assertIsNone(transcript["confidence"])
        self.assertEqual(0.99, json.loads(transcript["metadata_json"])["language_probability"])
        self.assertEqual("unknown", json.loads(transcript["metadata_json"])["transcript_confidence"])

    def test_unknown_fields_are_explicit(self):
        job = self.queue_claim()
        update_run_metadata(job, {})
        run = self.rows("processing_runs")[0]
        for field in ["tool", "tool_version", "provider", "model", "model_version"]:
            self.assertEqual("unknown", run[field])

    def test_heartbeat_keeps_long_running_adapter_owned(self):
        job = self.queue_claim(lease_seconds=0.3)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(process_job, job, FakeASR(delay=0.6), lease_seconds=0.3, heartbeat_interval=0.04)
            time.sleep(0.4)
            self.assertIsNone(claim_job(worker_id="another-worker"))
            result.result(timeout=3)
        self.assertEqual("done", self.rows("jobs")[0]["status"])

    def test_adapter_failure_has_failed_run_and_no_result(self):
        class FailingASR(FakeASR):
            def transcribe(self, path):
                raise RuntimeError("synthetic ASR failure")
        job = self.queue_claim()
        with self.assertRaisesRegex(RuntimeError, "synthetic ASR failure"):
            process_job(job, FailingASR())
        self.assertEqual("failed", self.rows("jobs")[0]["status"])
        self.assertEqual("failed", self.rows("processing_runs")[0]["status"])
        self.assertEqual("test-1", self.rows("processing_runs")[0]["tool_version"])
        self.assertEqual([], self.rows("derived_text"))

    def test_changed_input_is_refused(self):
        job = self.queue_claim()
        adapter = FakeASR()
        self.audio.write_bytes(b"X" * self.audio.stat().st_size)
        with self.assertRaisesRegex(MediaIntegrityError, "SHA256"):
            process_job(job, adapter)
        self.assertEqual(0, adapter.calls)
        self.assertEqual([], self.rows("derived_text"))
        self.assertEqual("failed", self.rows("processing_runs")[0]["status"])

    def test_legacy_duplicate_jobs_do_not_run_concurrently(self):
        queue_transcription(self.artifact_id)
        with db.session() as conn:
            conn.execute("INSERT INTO jobs(kind,artifact_id) VALUES ('transcribe',?)", (self.artifact_id,))
        first = claim_job()
        self.assertIsNone(claim_job())
        publish_transcript(first, text="first legacy queue")
        second = claim_job()
        self.assertNotEqual(first["id"], second["id"])

    def test_old_database_is_migrated_without_erasing_results_or_stuck_jobs(self):
        old_path = self.path / "old.db"
        with db.connect(old_path) as conn:
            conn.executescript(db.SCHEMA)
            conn.execute("INSERT INTO cases(name) VALUES ('old')")
            artifact = conn.execute("INSERT INTO artifacts(original_name,mime_type) VALUES ('old.wav','audio/wav')").lastrowid
            conn.execute("INSERT INTO derived_text(artifact_id,kind,text,confidence) VALUES (?,'transcript','old text',.99)", (artifact,))
            conn.execute("INSERT INTO jobs(kind,artifact_id,status,attempts) VALUES ('transcribe',?,'running',1)", (artifact,))
        with patch.object(db, "settings", replace(self.settings, db_path=old_path)):
            db.init_db()
            db.init_db()
            job = claim_job()
            self.assertEqual(2, job["attempts"])
            old_text = self.rows("derived_text")[0]
            self.assertEqual("old text", old_text["text"])
            self.assertIsNone(old_text["run_id"])
            self.assertEqual({}, json.loads(old_text["metadata_json"]))
            self.assertEqual(1, len(self.rows("processing_runs")))
            self.assertIn("job_lease_expired", [r["action"] for r in self.rows("audit_log")])

    def test_missing_artifact_is_rejected_before_queueing(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            queue_transcription(99999)
        self.assertEqual([], self.rows("jobs"))


if __name__ == "__main__":
    unittest.main()

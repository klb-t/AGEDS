"""Independent synthetic integration: acquisition, versioned words, inert archive.

These tests do not infer real ASR accuracy, access a phone or open private data.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.app import archive, citations, db, evidence, jobs, packages, worker
from server.app.transcription import queue_transcription
from server.app.worker import TranscriptionResult, process_job


class SyntheticWordAdapter:
    def describe(self):
        return {"tool": "night-integration-synthetic", "tool_version": "1",
                "provider": "synthetic-test", "model": "not-an-ASR-model"}

    def transcribe(self, source):
        # Exercise the real worker boundary and verify the captured input.
        assert source.read() == b"synthetic waveform placeholder\x00\xff"
        return TranscriptionResult(" Zażółć  gęślą.\n jaźń!", "pl", [
            {"start": .125, "end": 1.25, "text": " Zażółć  gęślą.", "words": [
                {"start": .125, "end": .6, "word": " Zażółć"},
                {"start": .7, "end": 1.25, "word": "  gęślą."}]},
            {"start": 1.5, "end": 2.0004, "text": "\n jaźń!", "words": [
                {"start": 1.5004, "end": 2.0004, "word": "\n jaźń!"}]}],
            {"test_only": True, "language_probability": .99,
             "language_probability_meaning": "language identification only"})


class NightIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = replace(db.settings, data_dir=self.root,
                                db_path=self.root / "live.sqlite", store_dir=self.root / "store")
        self.patches = [patch.object(module, "settings", self.settings) for module in (db, evidence, worker)]
        for item in self.patches:
            item.start()
        db.init_db()
        self.input = self.root / "original.wav"
        self.input.write_bytes(b"synthetic waveform placeholder\x00\xff")
        self.original = (self.input.read_bytes(), self.input.stat().st_mtime_ns)
        self.source = evidence.ensure_source("synthetic", "N6 isolated fixture")
        self.artifact = evidence.ingest_file(self.input, source_id=self.source,
                                            source_locator="content://synthetic/night/original")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def completed_version(self):
        queue_transcription(self.artifact)
        claim = jobs.claim_job(worker_id="independent-night-qa")
        return process_job(claim, SyntheticWordAdapter())

    def word_anchor(self, version):
        return citations.create_citation(self.artifact, version, word_refs=[
            {"segment_index": 0, "word_index": 1},
            {"segment_index": 1, "word_index": 0}], quote_text="  gęślą.\n jaźń!")

    @staticmethod
    def rehash(package):
        package["integrity"] = packages._integrity({key: value for key, value in package.items()
                                                    if key != "integrity"})
        return package

    def test_worker_old_word_version_survives_archive_with_running_job_inert(self):
        old = self.completed_version()
        newer = self.completed_version()
        anchor = self.word_anchor(old)
        self.assertNotEqual(old, newer)
        self.assertEqual(anchor["derived_text_id"], old)
        self.assertEqual((anchor["start_ms"], anchor["end_ms"]), (700, 2000))
        self.assertEqual(anchor["quote_sha256"], hashlib.sha256("  gęślą.\n jaźń!".encode()).hexdigest())
        self.assertEqual(anchor["selector"]["precision"], "word_asr")
        self.assertEqual(anchor["audio_verification"], "not_performed")
        queue_transcription(self.artifact)
        active = jobs.claim_job(worker_id="live-owner-remains-live")
        package = packages.export_metadata_package()
        self.assertTrue(packages.validate_metadata_package(package)["valid"])
        output = self.root / "inert.sqlite"
        receipt = archive.import_metadata_archive(package, output)
        self.assertFalse(receipt["jobs_resumed"])
        self.assertFalse(receipt["live_restore_supported"])
        restored = archive.read_metadata_archive(output)
        self.assertEqual(packages.canonical_json(package), packages.canonical_json(restored))
        self.assertEqual(restored["tables"]["evidence_anchors"][0]["derived_text_id"], old)
        self.assertEqual(len(restored["tables"]["processing_runs"]), 3)
        self.assertEqual(len(restored["tables"]["derived_text"]), 2)
        exported = self.root / "again.json"
        archive.export_metadata_archive(output, exported)
        self.assertEqual(archive.load_package(exported), package)
        with sqlite3.connect(output) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"archive_envelope", "archive_records"})
        with db.session() as connection:
            current = dict(connection.execute("SELECT * FROM jobs WHERE id=?", (active["id"],)).fetchone())
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["lease_token"], active["lease_token"])
            self.assertEqual(connection.execute("SELECT count(*) FROM evidence_anchors").fetchone()[0], 1)
        self.assertIsNone(jobs.claim_job(worker_id="archive-must-not-release-live-job"))
        self.assertEqual((self.input.read_bytes(), self.input.stat().st_mtime_ns), self.original)

    def test_word_selector_tampering_fails_even_after_all_digest_recomputation(self):
        self.word_anchor(self.completed_version())
        good = packages.export_metadata_package()
        for field, replacement in (("precision", "word_verified"), ("source_end", 900),
                                   ("word_refs", [{"segment_index": False, "word_index": 1}])):
            with self.subTest(field=field):
                corrupt = deepcopy(good)
                stored = corrupt["tables"]["evidence_anchors"][0]
                selector = json.loads(stored["selector_json"])
                selector[field] = replacement
                stored["selector_json"] = json.dumps(selector)
                self.rehash(corrupt)
                verdict = packages.validate_metadata_package(corrupt)
                self.assertFalse(verdict["valid"])
                self.assertTrue(verdict["digest_valid"])
                self.assertFalse(verdict["anchors_valid"])
                output = self.root / (field + ".sqlite")
                with self.assertRaises(ValueError):
                    archive.import_metadata_archive(corrupt, output)
                self.assertFalse(output.exists())

    def test_unavailable_or_invalid_words_preserve_raw_version_and_segment_fallback(self):
        versions = []
        variants = [None, [], [{"word": " Wrong", "start": 0, "end": 1}],
                    [{"word": " Raw", "start": float("nan"), "end": 1}]]
        for words in variants:
            segment = {"start": 0, "end": 1, "text": " Raw"}
            if words is not None:
                segment["words"] = words
            version = evidence.add_derived_text(self.artifact, "transcript", " Raw", segments=[segment])
            versions.append(version)
            with self.assertRaises(ValueError):
                citations.create_citation(self.artifact, version, word_refs=[{"segment_index": 0, "word_index": 0}])
            fallback = citations.create_citation(self.artifact, version, [0])
            self.assertEqual(fallback["selector"]["precision"], "segment")
        package = packages.export_metadata_package()
        # Nonfinite legacy JSON is a preserved literal field, never a numeric
        # package value or an accepted word timestamp.
        self.assertIn("NaN", package["tables"]["derived_text"][-1]["segments_json"])
        output = self.root / "literal-history.sqlite"
        archive.import_metadata_archive(package, output)
        self.assertEqual(archive.read_metadata_archive(output), package)
        self.assertEqual([row["id"] for row in package["tables"]["derived_text"]], versions)

    def test_archive_refuses_caps_nonfinite_and_existing_targets_without_mutation(self):
        self.word_anchor(self.completed_version())
        package = packages.export_metadata_package()
        sentinel = self.root / "existing.sqlite"
        sentinel.write_bytes(b"never overwrite")
        with self.assertRaises(FileExistsError):
            archive.import_metadata_archive(package, sentinel)
        self.assertEqual(sentinel.read_bytes(), b"never overwrite")
        for field, value in (("source_bytes_included", True), ("signed", True), ("metadata_only", False)):
            with self.subTest(field=field):
                bad = deepcopy(package)
                bad[field] = value
                self.rehash(bad)
                with self.assertRaises(ValueError):
                    archive.import_metadata_archive(bad, self.root / "refused.sqlite")
                self.assertFalse((self.root / "refused.sqlite").exists())
        bad = deepcopy(package)
        bad["unknown_numeric_extension"] = float("nan")
        with self.assertRaises(ValueError):
            archive.import_metadata_archive(bad, self.root / "nan.sqlite")
        self.assertFalse((self.root / "nan.sqlite").exists())
        with self.assertRaises(ValueError):
            archive.import_metadata_archive(package, self.root / "limited.sqlite", limits=archive.ArchiveLimits(max_rows=1))
        self.assertFalse((self.root / "limited.sqlite").exists())
        self.assertEqual((self.input.read_bytes(), self.input.stat().st_mtime_ns), self.original)

    def test_archive_schema_and_row_corruption_refuse_export(self):
        self.word_anchor(self.completed_version())
        package = packages.export_metadata_package()
        for attack in ("view", "row"):
            with self.subTest(attack=attack):
                archived = self.root / (attack + ".sqlite")
                archive.import_metadata_archive(package, archived)
                with sqlite3.connect(archived) as connection:
                    if attack == "view":
                        connection.execute("CREATE VIEW jobs AS SELECT 1 AS id")
                    else:
                        connection.execute("UPDATE archive_records SET row_json='{}' WHERE table_name='evidence_anchors'")
                target = self.root / (attack + ".json")
                with self.assertRaises(ValueError):
                    archive.export_metadata_archive(archived, target)
                self.assertFalse(target.exists())

    def test_http_upload_worker_pinned_words_and_archive_use_one_identity(self):
        from server.app import main
        with patch.object(main, "settings", self.settings), TestClient(main.app) as client:
            uploaded = client.post("/api/artifacts/upload", files={
                "file": ("duplicate-name.wav", self.input.read_bytes(), "audio/wav")},
                data={"source_locator": "content://synthetic/mobile/selected",
                      "metadata_json": json.dumps({"client_relative_path": "folder/duplicate-name.wav"})})
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            artifact = uploaded.json()["artifact_id"]
            for _ in range(2):
                queued = client.post(f"/api/artifacts/{artifact}/transcribe")
                self.assertEqual(queued.status_code, 200, queued.text)
                process_job(jobs.claim_job(worker_id="http-synthetic-qa"), SyntheticWordAdapter())
            versions = client.get(f"/api/artifacts/{artifact}/transcripts").json()
            old = versions[-1]["id"]
            transcript = client.get(f"/api/artifacts/{artifact}/transcript", params={"derived_text_id": old})
            self.assertEqual(transcript.status_code, 200, transcript.text)
            self.assertEqual(transcript.json()["wordTiming"]["status"], "available")
            body = {"derivedTextId": old, "wordRefs": [{"segment_index": 0, "word_index": 1},
                                                      {"segment_index": 1, "word_index": 0}],
                    "quoteText": "  gęślą.\n jaźń!"}
            for bad in [dict(body, segmentIndices=[0]), dict(body, quoteText="gęślą. jaźń!"),
                        dict(body, wordRefs=[{"segment_index": False, "word_index": 0}]),
                        dict(body, wordRefs=[{"segment_index": 0, "word_index": 1, "extra": 0}]),
                        {"derivedTextId": old}]:
                rejected = client.post(f"/api/artifacts/{artifact}/citations", json=bad)
                self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(client.get(f"/api/artifacts/{artifact}/citations").json(), [])
            accepted = client.post(f"/api/artifacts/{artifact}/citations", json=body)
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(accepted.json()["derived_text_id"], old)
            page = client.get(f"/artifact/{artifact}")
            self.assertEqual(page.status_code, 200, page.text)
            self.assertIn(f'transkrypt #{old}', page.text)
            self.assertIn(f'data-artifact-id="{artifact}"', page.text)
            self.assertIn('src="/static/citations.mjs"', page.text)
            self.assertIn('  gęślą.\n jaźń!', page.text)
            self.assertEqual(client.get('/static/citations.mjs').status_code, 200)
            self.assertEqual(client.get(f"/api/artifacts/{artifact}/content").content, self.input.read_bytes())
            package = client.get("/export/manifest.json").json()
            verdict = client.post("/api/packages/validate", json=package)
            self.assertEqual(verdict.status_code, 200, verdict.text)
            self.assertTrue(verdict.json()["valid"])
            archive.import_metadata_archive(package, self.root / "http.sqlite")
            restored = archive.read_metadata_archive(self.root / "http.sqlite")
            self.assertEqual(packages.canonical_json(package), packages.canonical_json(restored))
            anchors = restored["tables"]["evidence_anchors"]
            self.assertEqual([(row["artifact_id"], row["derived_text_id"]) for row in anchors], [(artifact, old)])
        self.assertEqual((self.input.read_bytes(), self.input.stat().st_mtime_ns), self.original)

    def test_http_malformed_historical_json_is_explicit_conflict_and_stays_archivable(self):
        from server.app import main
        raw_fields = [("segments_json", "[{\"start\":NaN,\"end\":1,\"text\":\" Raw\"}]"),
                      ("segments_json", "{bad-json"), ("segments_json", "{}"),
                      ("segments_json", '[{"start":0,"end":1,"text":"\\ud800"}]'),
                      ("metadata_json", "{\"historical\":1e999}"), ("metadata_json", "[]")]
        with patch.object(main, "settings", self.settings), TestClient(main.app) as client:
            for index, (column, raw) in enumerate(raw_fields):
                with self.subTest(column=column, raw=raw):
                    version = evidence.add_derived_text(self.artifact, "transcript", " Raw",
                                                       segments=[{"start": 0, "end": 1, "text": " Raw"}])
                    with db.session() as connection:
                        connection.execute(f"UPDATE derived_text SET {column}=? WHERE id=?", (raw, version))
                    response = client.get(f"/api/artifacts/{self.artifact}/transcript",
                                          params={"derived_text_id": version})
                    self.assertEqual(response.status_code, 409, response.text)
                    with db.session() as connection:
                        self.assertEqual(connection.execute(f"SELECT {column} FROM derived_text WHERE id=?",
                                                            (version,)).fetchone()[0], raw)
                    package = packages.export_metadata_package()
                    target = self.root / f"historical-{index}.sqlite"
                    archive.import_metadata_archive(package, target)
                    self.assertEqual(archive.read_metadata_archive(target), package)


if __name__ == "__main__":
    unittest.main()

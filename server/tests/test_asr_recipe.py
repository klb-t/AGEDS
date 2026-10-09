"""Local recipes through the real worker/store; fake decoder, no network/model."""
from dataclasses import replace
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server.app import asr_recipe, db, jobs, packages, transcription, worker


class AsrRecipeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.profile = self.root / 'recipe.json'
        self.base = asr_recipe.load_recipe().snapshot()
        self.profile.write_text(json.dumps(self.base))
        self.store = self.root / 'store'
        self.store.mkdir()
        settings = replace(db.settings, db_path=self.root / 'db.sqlite',
                           store_dir=self.store, asr_recipe_path=self.profile)
        for module in (db, worker):
            p = patch.object(module, 'settings', settings)
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        self.raw = b'synthetic media; preserve original bytes'
        self.audio = self.store / 'input.wav'
        self.audio.write_bytes(self.raw)
        with db.session() as conn:
            self.aid = conn.execute('INSERT INTO artifacts(original_name,mime_type,stored_path,sha256,size_bytes) VALUES (?,?,?,?,?)',
                ('input.wav', 'audio/wav', str(self.audio), hashlib.sha256(self.raw).hexdigest(), len(self.raw))).lastrowid
        self.calls = []
        calls, raw = self.calls, self.raw
        class Model:
            def __init__(self, model, **kwargs):
                calls.append(('model', model, kwargs))
            def transcribe(self, source, **kwargs):
                calls.append(('decode', kwargs))
                assert source.read() == raw
                words = [SimpleNamespace(start=0., end=1., word=' raw ', probability=None)] if kwargs['word_timestamps'] else []
                return iter([SimpleNamespace(start=0., end=1., text=' raw ', words=words)]), SimpleNamespace(language='en')
        p = patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Model)})
        p.start()
        self.addCleanup(p.stop)

    def rows(self, table):
        with db.session() as conn:
            return [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY id')]

    def run_adapter(self, adapter=None):
        transcription.queue_transcription(self.aid)
        return worker.process_job(jobs.claim_job(), adapter)

    def test_defaults_and_variants_pin_same_snapshot_through_real_worker_and_export(self):
        hashes = []
        for vad, timings in ((True, True), (False, True), (False, False)):
            value = {**self.base, 'options': {'vad_filter': vad, 'word_timestamps': timings}}
            self.profile.write_text(json.dumps(value))
            adapter = worker.FasterWhisperAdapter()
            snapshot = adapter.recipe.snapshot()
            # Later file edits cannot change a described adapter's actual run.
            self.profile.write_text('invalid after snapshot')
            self.run_adapter(adapter)
            self.assertEqual(self.calls[-1], ('decode', snapshot['options']))
            model = self.calls[-2]
            self.assertEqual(model, ('model', worker.settings.whisper_model,
                {'device': worker.settings.whisper_device, 'compute_type': worker.settings.whisper_compute_type}))
            run = self.rows('processing_runs')[-1]
            metadata = json.loads(run['metadata_json'])
            self.assertEqual(metadata['asr_recipe'], snapshot)
            self.assertEqual(metadata['asr_recipe_hash'], adapter.recipe.content_hash)
            hashes.append(metadata['asr_recipe_hash'])
            parameters = json.loads(run['parameters_json'])
            self.assertEqual(parameters['vad_filter'], vad)
            self.assertEqual(parameters['word_timestamps'], timings)
            text_metadata = json.loads(self.rows('derived_text')[-1]['metadata_json'])
            self.assertEqual(text_metadata['asr_recipe_hash'], metadata['asr_recipe_hash'])
            self.assertEqual(text_metadata['word_timing']['status'], 'available' if timings else 'unavailable')
            self.assertEqual(self.audio.read_bytes(), self.raw)
        self.assertEqual(len(set(hashes)), 3)
        package = packages.export_metadata_package()
        self.assertEqual(package['tables']['processing_runs'], self.rows('processing_runs'))
        self.assertTrue(packages.validate_metadata_package(package)['valid'])
        db.init_db()  # Reopen/migration preserves old snapshots, does not backfill.
        self.assertEqual([json.loads(r['metadata_json'])['asr_recipe_hash'] for r in self.rows('processing_runs')], hashes)
        self.assertEqual([r['status'] for r in self.rows('jobs')], ['done'] * 3)

    def test_invalid_recipe_stops_before_decoder_and_repair_creates_separate_run(self):
        self.profile.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'recipe'):
            self.run_adapter()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows('derived_text'), [])
        self.assertEqual(self.rows('processing_runs')[0]['status'], 'failed')
        self.assertEqual(self.profile.read_text(), '{}')
        self.profile.write_text(json.dumps(self.base))
        self.run_adapter()
        self.assertEqual([r['status'] for r in self.rows('processing_runs')], ['failed', 'done'])
        self.assertEqual(len(self.rows('derived_text')), 1)
        self.assertEqual(self.audio.read_bytes(), self.raw)

    def test_production_environment_selects_recipe_defaults_and_explicit_model_overlay(self):
        value = {**self.base, 'model': 'synthetic-profile-model',
                 'options': {'vad_filter': False, 'word_timestamps': True}}
        self.profile.write_text(json.dumps(value))
        env = dict(os.environ)
        for key in ('EW_WHISPER_MODEL', 'EW_WHISPER_DEVICE', 'EW_WHISPER_COMPUTE_TYPE'):
            env.pop(key, None)
        env.update(EW_ASR_RECIPE=str(self.profile), EW_DATA_DIR=str(self.root),
                   EW_DB_PATH=str(self.root / 'subprocess.sqlite'), EW_STORE_DIR=str(self.store))
        script = ('import json; from server.app.worker import FasterWhisperAdapter; '
                  'print(json.dumps(FasterWhisperAdapter().describe()["metadata"]))')
        def describe():
            return json.loads(subprocess.check_output([sys.executable, '-c', script],
                              cwd=Path(__file__).resolve().parents[2], env=env, text=True))
        first = describe()
        self.assertEqual(first['asr_recipe'], value)
        env['EW_WHISPER_MODEL'] = 'synthetic-environment-model'
        second = describe()
        self.assertEqual(second['asr_recipe']['model'], 'synthetic-environment-model')
        self.assertEqual(second['asr_recipe']['options'], value['options'])
        self.assertNotEqual(first['asr_recipe_hash'], second['asr_recipe_hash'])

    def test_validation_has_no_silent_default_or_unknown_switch(self):
        for invalid in ({}, {**self.base, 'revision': True},
                        {**self.base, 'options': {'word_timestamps': True}},
                        {**self.base, 'options': {'word_timestamps': 'false', 'vad_filter': True}},
                        {**self.base, 'options': {**self.base['options'], 'unsupported': True}},
                        {**self.base, 'model': ''}):
            self.profile.write_text(json.dumps(invalid))
            with self.assertRaises(ValueError):
                asr_recipe.load_recipe(self.profile)
        self.profile.write_text('{"schema":1,"schema":2}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            asr_recipe.load_recipe(self.profile)
        with self.assertRaises(FileNotFoundError):
            asr_recipe.load_recipe(self.root / 'absent')
        default = asr_recipe.load_recipe()
        self.assertEqual(default.options(), {'word_timestamps': True, 'vad_filter': True})
        self.assertEqual((default.model, default.device, default.compute_type), ('small', 'cpu', 'int8'))


if __name__ == '__main__':
    unittest.main()

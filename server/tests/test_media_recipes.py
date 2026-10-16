"""ASR recipes as data: canonical hash, job pinning, dedup and run provenance."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

from server.app import jobs, worker
from server.app.media import recipe as recipes
from server.app.media.queue import enqueue_media_job
from server.app.transcription import enqueue_transcription, queue_transcription
from server.tests.test_media_support import MediaCase


class RecipeDataTests(MediaCase):
    def test_every_preset_validates_and_hash_is_canonical(self):
        for recipe_id in recipes.preset_ids():
            preset, _ = recipes.load_preset(recipe_id)
            record = recipes.effective_recipe(recipe_id, kind=preset['kind'])
            self.assertEqual(record['sha256'], recipes.sha256_json(record['effective']))
            shuffled = json.loads(json.dumps(dict(reversed(list(record['effective'].items())))))
            self.assertEqual(record['sha256'], recipes.sha256_json(shuffled))
            self.assertNotIn('description', record['effective'])
        legacy = recipes.effective_recipe('legacy')
        self.assertEqual(('small', 'cpu', 'int8'), (legacy['effective']['model'], legacy['effective']['device'],
                                                    legacy['effective']['compute_type']))
        self.assertEqual('EW_WHISPER_MODEL', legacy['provenance']['resolved_settings']['model']['env'])

    def test_settings_change_changes_effective_hash_but_not_preset_hash(self):
        small = recipes.effective_recipe('legacy')
        self.use_settings(whisper_model='medium')
        medium = recipes.effective_recipe('legacy')
        self.assertNotEqual(small['sha256'], medium['sha256'])
        self.assertEqual(small['provenance']['preset_sha256'], medium['provenance']['preset_sha256'])

    def test_legacy_recipe_reproduces_the_pre_recipe_adapter_call_exactly(self):
        captured = []

        class Model:
            def __init__(self, *args, **kwargs):
                captured.append(('init', args, kwargs))

            def transcribe(self, audio, **kwargs):
                captured.append(('transcribe', kwargs))
                return iter([]), SimpleNamespace(language='en')

        with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Model)}):
            worker.FasterWhisperAdapter().transcribe('synthetic:not-read')
            before = list(captured)
            captured.clear()
            worker.FasterWhisperAdapter(recipes.effective_recipe('legacy')).transcribe('synthetic:not-read')
        self.assertEqual(before, captured)
        self.assertEqual(('init', ('small',), {'device': 'cpu', 'compute_type': 'int8'}), captured[0])
        self.assertEqual(('transcribe', {'word_timestamps': True, 'vad_filter': True}), captured[1])

    def test_invalid_recipes_and_overrides_are_refused(self):
        cases = [
            ('legacy', {'unknown_key': 1}),
            ('legacy', {'id': 'other'}),
            ('legacy', {'strategy': 'whatever'}),
            ('legacy', {'allowed_languages': ['pl']}),
            ('legacy', {'transcribe_options': {'language': 'pl'}}),
            ('legacy', {'transcribe_options': {'task': 'translate'}}),
            ('legacy', {'vad': {'parameters': {'threshold': float('nan')}}}),
            ('per-segment-pl-nl-en', {'allowed_languages': []}),
            ('per-segment-pl-nl-en', {'allowed_languages': ['pl', 'pl']}),
            ('per-segment-pl-nl-en', {'windows': {'max_window_s': 31}}),
            ('per-segment-pl-nl-en', {'language': 'pl'}),
            ('per-segment-pl-nl-en', {'vad': {'enabled': False}}),
            ('per-segment-pl-nl-en', {'model': {'$setting': 'secret_key'}}),
        ]
        for recipe_id, overrides in cases:
            with self.subTest(recipe=recipe_id, overrides=overrides):
                with self.assertRaises(recipes.RecipeError):
                    recipes.effective_recipe(recipe_id, overrides=overrides)
        with self.assertRaises(recipes.RecipeError):
            recipes.effective_recipe('legacy', kind='ocr')
        with self.assertRaises(recipes.RecipeError):
            recipes.effective_recipe('../legacy')

    def test_private_recipe_directory_adds_presets_but_cannot_shadow_builtins(self):
        private = self.root / 'private-recipes'
        private.mkdir()
        preset, _ = recipes.load_preset('per-segment-pl-nl-en')
        custom = dict(preset, id='pl-en-large', allowed_languages=['pl', 'en'], model='large-v3')
        (private / 'pl-en-large.json').write_text(json.dumps(custom), encoding='utf-8')
        with patch.dict(os.environ, {'EW_MEDIA_RECIPE_DIR': str(private)}):
            record = recipes.effective_recipe('pl-en-large')
            self.assertEqual('private', record['provenance']['preset_origin'])
            self.assertEqual('large-v3', record['effective']['model'])
            (private / 'legacy.json').write_text(json.dumps(dict(custom, id='legacy')), encoding='utf-8')
            with self.assertRaisesRegex(recipes.RecipeError, 'collides'):
                recipes.effective_recipe('legacy')


class RecipePinningTests(MediaCase):
    def setUp(self):
        super().setUp()
        self.aid = self.add_audio('audio_single_en')

    def payload(self, job_id):
        row = [r for r in self.rows('jobs') if r['id'] == job_id][0]
        return json.loads(row['payload_json'])

    def test_enqueue_pins_effective_recipe_and_applies_dedup_policy(self):
        first = enqueue_transcription(self.aid)
        self.assertTrue(first['created'])
        payload = self.payload(first['job_id'])
        self.assertEqual('legacy', payload['recipe']['id'])
        self.assertEqual(recipes.sha256_json(payload['recipe']['effective']), payload['recipe']['sha256'])
        self.assertEqual('reuse', payload['dedup_policy'])
        again = enqueue_transcription(self.aid, 9)
        self.assertEqual((first['job_id'], False, 'active'), (again['job_id'], again['created'], again['reused']))
        self.assertEqual(9, self.rows('jobs')[0]['priority'])
        other = enqueue_transcription(self.aid, recipe='per-segment-pl-nl-en')
        self.assertTrue(other['created'])
        self.assertNotEqual(first['recipe']['sha256'], other['recipe']['sha256'])
        larger = enqueue_transcription(self.aid, recipe='per-segment-pl-nl-en', overrides={'model': 'large-v3'})
        self.assertTrue(larger['created'])
        self.assertNotIn(larger['recipe']['sha256'], {first['recipe']['sha256'], other['recipe']['sha256']})
        duplicate = enqueue_transcription(self.aid, dedup='duplicate')
        self.assertTrue(duplicate['created'])
        self.assertEqual(first['recipe']['sha256'], duplicate['recipe']['sha256'])
        self.assertEqual(4, len(self.rows('jobs')))
        enqueued = [json.loads(r['details_json']) for r in self.rows('audit_log') if r['action'] == 'job_enqueued']
        self.assertEqual({'legacy', 'per-segment-pl-nl-en'}, {d['recipe_id'] for d in enqueued})
        with self.assertRaises(recipes.RecipeError):
            enqueue_transcription(self.aid, dedup='sometimes')
        with self.assertRaises(ValueError):
            enqueue_media_job('summarize', self.aid)

    def test_completed_jobs_reuse_only_under_reuse_completed(self):
        self.fake_whisper()
        job_id = queue_transcription(self.aid)
        worker.process_job(jobs.claim_job())
        reused = enqueue_transcription(self.aid, dedup='reuse_completed')
        self.assertEqual((job_id, 'completed', False), (reused['job_id'], reused['reused'], reused['created']))
        fresh = enqueue_transcription(self.aid)
        self.assertTrue(fresh['created'])
        self.assertNotEqual(job_id, fresh['job_id'])

    def test_changed_defaults_never_change_a_pinned_job(self):
        fake = self.fake_whisper()
        pinned = enqueue_transcription(self.aid)
        self.use_settings(whisper_model='medium')
        with patch.dict(os.environ, {'EW_ASR_RECIPE': 'per-segment-pl-nl-en'}):
            later = enqueue_transcription(self.aid)
            self.assertEqual('per-segment-pl-nl-en', later['recipe']['id'])
        self.assertTrue(later['created'])
        job = jobs.claim_job()
        self.assertEqual(pinned['job_id'], job['id'])
        worker.process_job(job)
        self.assertEqual('small', fake.named('WhisperModel')[0]['model'])
        self.assertEqual([{'input': 'stream', 'samples': None, 'word_timestamps': True, 'vad_filter': True}],
                         fake.named('transcribe'))
        run = self.rows('processing_runs')[0]
        provenance = json.loads(run['provenance_json'])
        self.assertEqual(pinned['recipe']['sha256'], provenance['recipe']['sha256'])
        self.assertEqual('pinned_at_enqueue', provenance['recipe']['pin_status'])
        self.assertEqual('adapter_built_from_pinned_recipe', provenance['recipe']['application'])
        self.assertEqual('small', run['model'])

    def test_run_records_full_effective_recipe_and_hash(self):
        self.fake_whisper()
        queued = enqueue_transcription(self.aid, recipe='per-segment-pl-nl-en')
        worker.process_job(jobs.claim_job())
        run = self.rows('processing_runs')[0]
        parameters = json.loads(run['parameters_json'])
        provenance = json.loads(run['provenance_json'])
        effective = self.payload(queued['job_id'])['recipe']['effective']
        self.assertEqual(effective, parameters['recipe'])
        self.assertEqual(queued['recipe']['sha256'], parameters['recipe_sha256'])
        self.assertEqual(effective, provenance['recipe']['effective'])
        self.assertEqual('hash_matches', recipes.verify_run_recipe(provenance)['status'])
        work = json.loads(run['metadata_json'])['work']
        self.assertEqual(('audio_second', 4.0), (work['unit'], work['amount']))

    def test_pre_recipe_job_runs_as_legacy_with_unknown_history_and_is_not_backfilled(self):
        self.fake_whisper()
        from server.app import db
        with db.session() as conn:
            job_id = conn.execute("INSERT INTO jobs(kind,artifact_id) VALUES ('transcribe',?)", (self.aid,)).lastrowid
        reused = enqueue_transcription(self.aid)
        self.assertEqual((job_id, 'active', False), (reused['job_id'], reused['reused'], reused['pinned']))
        worker.process_job(jobs.claim_job())
        recipe = json.loads(self.rows('processing_runs')[0]['provenance_json'])['recipe']
        self.assertEqual(('legacy', 'unpinned_pre_recipe_job', 'unknown'),
                         (recipe['id'], recipe['pin_status'], recipe['historical_recipe']))
        self.assertEqual('{}', self.rows('jobs')[0]['payload_json'])
        self.assertEqual('resolved_at_run_time_from_current_settings', recipe['provenance']['resolution'])

    def test_tampered_pin_fails_the_attempt_without_output(self):
        self.fake_whisper()
        job_id = queue_transcription(self.aid)
        from server.app import db
        payload = self.payload(job_id)
        payload['recipe']['effective']['model'] = 'large-v3'
        with db.session() as conn:
            conn.execute('UPDATE jobs SET payload_json=? WHERE id=?', (json.dumps(payload), job_id))
        with self.assertRaisesRegex(recipes.RecipeError, 'hash mismatch'):
            worker.process_job(jobs.claim_job())
        self.assertEqual('failed', self.rows('jobs')[0]['status'])
        self.assertEqual('failed', self.rows('processing_runs')[0]['status'])
        self.assertEqual([], self.rows('derived_text'))

    def test_injected_adapter_is_recorded_as_not_bound_to_the_recipe(self):
        class Adapter:
            def describe(self):
                return {'tool': 'synthetic', 'parameters': {'word_timestamps': True}}

            def transcribe(self, audio):
                return worker.TranscriptionResult('x', 'en', [{'start': 0.0, 'end': 1.0, 'text': 'x'}], {})
        queue_transcription(self.aid)
        worker.process_job(jobs.claim_job(), Adapter())
        run = self.rows('processing_runs')[0]
        self.assertEqual({'word_timestamps': True}, json.loads(run['parameters_json']))
        recipe = json.loads(run['provenance_json'])['recipe']
        self.assertEqual('injected_adapter_not_bound_to_recipe', recipe['application'])
        work = json.loads(run['metadata_json'])['work']
        self.assertEqual(('wav_header_declared_duration', 4.0), (work['basis'], work['amount']))

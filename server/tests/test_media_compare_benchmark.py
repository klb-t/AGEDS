"""Run comparison determinism; WER/CER known answers; benchmark tool boundaries."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path

from server.app import cli, db, worker
from server.app.jobs import claim_job
from server.app.media import benchmark
from server.app.media.comparison import artifact_versions, compare_versions
from server.app.media.textmetrics import AlignmentLimit, align_counts, error_rates, normalize_text
from server.app.transcription import enqueue_transcription
from server.tests.test_media_support import MediaCase


class TextMetricTests(unittest.TestCase):
    def test_known_answers(self):
        same = error_rates('a b c', 'a b c')
        self.assertEqual((0.0, 0.0), (same['word']['wer'], same['char']['cer']))
        sub = error_rates('a b c', 'a x c')['word']
        self.assertEqual((1, 0, 0, 1 / 3), (sub['substitutions'], sub['deletions'], sub['insertions'], sub['wer']))
        insert = error_rates('a b', 'a x b')['word']
        self.assertEqual((0, 0, 1, 0.5), (insert['substitutions'], insert['deletions'], insert['insertions'], insert['wer']))
        delete = error_rates('a b c', 'a')['word']
        self.assertEqual((2, 2 / 3), (delete['deletions'], delete['wer']))
        empty_hyp = error_rates('a b', '')['word']
        self.assertEqual((2, 1.0), (empty_hyp['deletions'], empty_hyp['wer']))

    def test_empty_reference_has_no_rate_but_counts_insertions(self):
        result = error_rates('', 'a b')
        self.assertIsNone(result['word']['wer'])
        self.assertEqual('undefined_empty_reference', result['word']['status'])
        self.assertEqual(2, result['word']['insertions'])
        self.assertIsNone(result['char']['cer'])
        self.assertEqual(3, result['char']['insertions'])
        both = error_rates('  ', '...')
        self.assertEqual((None, 0), (both['word']['wer'], both['word']['edits']))

    def test_polish_and_dutch_diacritics_are_kept_but_case_and_punctuation_are_not(self):
        stripped = error_rates('Zażółć gęślą jaźń', 'zazolc gesla jazn')
        self.assertEqual(1.0, stripped['word']['wer'])
        self.assertEqual((9, 17), (stripped['char']['substitutions'], stripped['char']['reference_length']))
        self.assertAlmostEqual(9 / 17, stripped['char']['cer'])
        styled = error_rates('Zażółć gęślą jaźń', 'ZAŻÓŁĆ, gęślą... jaźń!')
        self.assertEqual((0.0, 0.0), (styled['word']['wer'], styled['char']['cer']))
        decomposed = error_rates('żółw', 'żółw')  # NFC vs NFD input
        self.assertEqual(0.0, decomposed['word']['wer'])
        self.assertEqual(1.0, error_rates('één', 'een')['word']['wer'])
        self.assertEqual("zo'n s avonds 3,5 km²", normalize_text("Zo’n 'S Avonds 3,5 km²"))
        self.assertEqual('a b', normalize_text('a­b'.replace('­', '­ ')))

    def test_tie_break_is_deterministic_and_limit_is_explicit(self):
        self.assertEqual({'edits': 2, 'substitutions': 2, 'deletions': 0, 'insertions': 0, 'hits': 0,
                          'reference_length': 2, 'hypothesis_length': 2}, align_counts(['a', 'b'], ['b', 'a']))
        self.assertEqual(align_counts(list('kitten'), list('sitting'))['edits'], 3)
        with self.assertRaises(AlignmentLimit):
            align_counts(['a'] * 10, ['b'] * 10, max_cells=99)
        limited = error_rates('a ' * 50, 'b ' * 50, max_cells=100)
        self.assertEqual('not_computed', limited['word']['status'])


class ComparisonTests(MediaCase):
    def setUp(self):
        super().setUp()
        self.fake_whisper()
        self.aid = self.add_audio()
        enqueue_transcription(self.aid)
        enqueue_transcription(self.aid, recipe='per-segment-pl-nl-en')
        self.legacy = worker.process_job(claim_job())
        self.switched = worker.process_job(claim_job())

    def test_comparison_is_deterministic_and_reports_language_and_timing_differences(self):
        first = compare_versions(self.legacy, self.switched)
        second = compare_versions(self.legacy, self.switched)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first['content_sha256'], second['content_sha256'])
        self.assertEqual(('auto_per_file', 'per_segment_language'), (first['a']['strategy'], first['b']['strategy']))
        self.assertEqual(('words', 'words'), (first['a']['token_source'], first['b']['token_source']))
        counts = first['text_diff']['counts']
        self.assertEqual(first['a']['token_count'], counts['equal'] + counts['a_only_deleted'] + counts['replaced_a'])
        self.assertEqual(first['b']['token_count'], counts['equal'] + counts['b_only_inserted'] + counts['replaced_b'])
        self.assertGreater(counts['replaced_a'], 0)
        transitions = first['language']['transitions_seconds']
        self.assertIn('pl->nl', transitions)
        self.assertIn('pl->en', transitions)
        self.assertGreater(first['language']['overlap_seconds']['different'], 0)
        self.assertEqual({'version'}, {p['a_language_scope'] for p in first['language']['pairs']})
        self.assertEqual({'segment'}, {p['b_language_scope'] for p in first['language']['pairs']})
        self.assertGreater(first['timing']['matched_word_start_delta_seconds']['count'], 0)
        normalized = compare_versions(self.legacy, self.switched, normalize=True)
        self.assertEqual('ageds-asr-eval-norm/1', normalized['normalization'])

    def test_invalid_pairs_are_refused(self):
        with self.assertRaises(ValueError):
            compare_versions(self.legacy, self.legacy)
        other = self.add_audio('audio_single_en')
        enqueue_transcription(other)
        foreign = worker.process_job(claim_job())
        with self.assertRaisesRegex(ValueError, 'different artifacts'):
            compare_versions(self.legacy, foreign)
        with self.assertRaises(LookupError):
            compare_versions(self.legacy, 999)
        versions = artifact_versions(self.aid)
        self.assertEqual(['per_segment_language', 'auto_per_file'], [v['strategy'] for v in versions])
        self.assertEqual(['en', 'nl', 'pl'], versions[0]['segment_languages'])
        self.assertEqual({'available'}, {v['word_timing'] for v in versions})


class BenchmarkTests(MediaCase):
    def manifest(self) -> Path:
        audio_path = Path(self.corpus['audio_codeswitch'])
        manifest = {
            'schema': 'ageds.asr_benchmark_manifest', 'schema_version': 1,
            'items': [{'id': 'tones-1', 'audio': audio_path.name, 'language': None,
                       'reference_segments': [
                           {'start': 1.0, 'end': 6.0, 'text': ' '.join(['pl'] * 10), 'language': 'pl'},
                           {'start': 6.0, 'end': 9.0, 'text': ' '.join(['nl'] * 6), 'language': 'nl'},
                           {'start': 10.5, 'end': 14.5, 'text': ' '.join(['en'] * 8), 'language': 'en'},
                           {'start': 15.5, 'end': 17.0, 'text': 'pl pl pl', 'language': 'pl'}]}],
        }
        path = audio_path.parent / 'benchmark.json'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        return path

    def test_recipes_run_through_the_adapter_interface_and_are_scored_per_segment_and_language(self):
        self.fake_whisper()
        source = Path(self.corpus['audio_codeswitch'])
        before = source.read_bytes()
        out = self.root / 'private-results'
        result = benchmark.run_benchmark(self.manifest(), out, recipes=['legacy', 'per-segment-pl-nl-en'])
        self.assertEqual(before, source.read_bytes())
        written = json.loads(Path(result['output']).read_text(encoding='utf-8'))
        self.assertEqual(result['content_sha256'], written['content_sha256'])
        self.assertIn('not a general quality claim', written['scope_statement'])
        runs = {run['label']: run for run in written['runs']}
        segments = runs['per-segment-pl-nl-en']['items'][0]['segments']
        self.assertEqual([0.2, 0.0, 0.0, 1.0], [s['word']['wer'] for s in segments])
        self.assertEqual(['pl', 'nl', 'en', None], [s['hypothesis_language_by_overlap'] for s in segments])
        self.assertEqual(3, segments[3]['word']['deletions'])          # quiet segment dropped by VAD
        legacy = runs['legacy']['aggregates']['by_reference_segment_language']
        switched = runs['per-segment-pl-nl-en']['aggregates']['by_reference_segment_language']
        self.assertEqual(0.0, switched['nl']['word']['wer'])
        self.assertGreater(legacy['nl']['word']['wer'], switched['nl']['word']['wer'])
        agreement = runs['per-segment-pl-nl-en']['aggregates']['segment_language_label_agreement']
        self.assertEqual((3, 3), (agreement['compared'], agreement['agree']))
        self.assertEqual('ageds-asr-eval-norm/1', written['normalization']['id'])
        item = runs['legacy']['items'][0]
        self.assertEqual(18.0, item['audio_seconds'])
        self.assertEqual(64, len(item['audio_sha256']))

    def test_output_inside_repository_or_store_is_refused_and_results_never_overwrite(self):
        manifest = self.manifest()
        with self.assertRaisesRegex(ValueError, 'inside the repository'):
            benchmark.run_benchmark(manifest, benchmark.REPO_ROOT / 'docs' / 'results')
        with self.assertRaisesRegex(ValueError, 'content store'):
            benchmark.run_benchmark(manifest, self.store / 'bench', store_dir=self.store)
        self.assertFalse((benchmark.REPO_ROOT / 'docs' / 'results').exists())
        hypotheses = self.root / 'hyp.json'
        hypotheses.write_text(json.dumps({'schema': 'ageds.asr_benchmark_hypotheses', 'schema_version': 1,
                                          'runs': {'manual': {'tones-1': {'text': 'pl pl'}}, 'empty': {}}}))
        out = self.root / 'out'
        first = benchmark.run_benchmark(manifest, out, hypotheses_path=hypotheses)
        second = benchmark.run_benchmark(manifest, out, hypotheses_path=hypotheses)
        self.assertNotEqual(first['output'], second['output'])
        self.assertEqual(first['content_sha256'], second['content_sha256'])
        runs = {run['label']: run for run in first['result']['runs']}
        self.assertEqual('no hypothesis supplied for this item', runs['empty']['items'][0]['error'])
        self.assertEqual(27, runs['manual']['items'][0]['word']['reference_length'])

    def test_cli_scores_precomputed_hypotheses_and_reports_bad_manifests(self):
        hypotheses = self.root / 'hyp.json'
        hypotheses.write_text(json.dumps({'schema': 'ageds.asr_benchmark_hypotheses', 'schema_version': 1,
                                          'runs': {'manual': {'tones-1': {'text': ' '.join(['pl'] * 10)}}}}))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(['asr-benchmark', '--manifest', str(self.manifest()), '--output', str(self.root / 'cli-out'),
                             '--hypotheses', str(hypotheses)])
        self.assertEqual(0, code)
        emitted = json.loads(stdout.getvalue())
        self.assertEqual(('asr-benchmark', False), (emitted['command'], emitted['source_bytes_written']))
        self.assertAlmostEqual(17 / 27, emitted['aggregates']['manual']['word']['wer'])
        bad = self.root / 'bad.json'
        bad.write_text(json.dumps({'schema': 'something-else'}))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(2, cli.main(['asr-benchmark', '--manifest', str(bad), '--output', str(self.root / 'x')]))
        self.assertIn('ageds.asr_benchmark_manifest', stderr.getvalue())

    def test_cli_compare_and_queue_use_the_database(self):
        self.fake_whisper()
        aid = self.add_audio()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(0, cli.main(['media-enqueue', str(aid)]))
            self.assertEqual(0, cli.main(['media-enqueue', str(aid), '--recipe', 'per-segment-pl-nl-en',
                                          '--override', '{"model": "base"}', '--priority', '3']))
            self.assertEqual(0, cli.main(['media-queue']))
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual('base', json.loads([r for r in self.rows('jobs')][1]['payload_json'])['recipe']['effective']['model'])
        self.assertEqual(2, len(lines[2]['jobs']))
        self.assertEqual(lines[1]['job_id'], lines[2]['jobs'][0]['job_id'])
        a = worker.process_job(claim_job())
        b = worker.process_job(claim_job())
        out = self.root / 'comparison.json'
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(0, cli.main(['transcript-compare', str(b), str(a), '--output', str(out)]))
            self.assertEqual(2, cli.main(['transcript-compare', str(b), str(a), '--output', str(out)]))
            self.assertEqual(0, cli.main(['asr-recipes', '--kind', 'asr']))
        self.assertEqual(json.loads(out.read_text())['content_sha256'], json.loads(stdout.getvalue().splitlines()[0])['content_sha256'])
        recipes = json.loads(stdout.getvalue().splitlines()[1])['recipes']
        self.assertEqual({'legacy', 'per-segment-pl-nl-en', 'asr-vertex-gemini', 'asr-vertex-gemini-aligned', 'asr-vertex-gemini-chirp', 'asr-vertex-gemini-forced', 'asr-vertex-gemini-hybrid'}, {r['id'] for r in recipes})
        with db.session() as conn:
            self.assertEqual(2, conn.execute("SELECT count(*) FROM derived_text").fetchone()[0])

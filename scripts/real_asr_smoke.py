#!/usr/bin/env python3
"""Opt-in real local ASR smoke; generates speech, never opens a source corpus.

Requires repository server requirements, faster-whisper and FFmpeg with flite.
Supply an already downloaded CTranslate2 tiny.en model directory. No fake adapter,
network download, production DB or service is used. The work directory must be new.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback
import wave

TEXT = 'The quick brown fox jumps over the lazy dog. This is a test of local speech recognition.'
MODEL_FILES = ('config.json', 'tokenizer.json', 'vocabulary.txt', 'model.bin')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--model-revision', default='unknown', help='Externally verified acquisition revision; not inferred from folder name')
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    model = args.model_dir.resolve(strict=True)
    for name in MODEL_FILES:
        require((model / name).is_file(), f'Missing local model file: {name}')
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    # Set isolation before the first application import (config creates directories).
    os.environ.update(EW_DATA_DIR=str(root), EW_DB_PATH=str(root / 'live.sqlite'),
                      EW_STORE_DIR=str(root / 'store'), EW_WHISPER_MODEL=str(model),
                      EW_WHISPER_DEVICE='cpu', EW_WHISPER_COMPUTE_TYPE='int8',
                      HF_HUB_OFFLINE='1')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    receipt = {'schema_version': 1, 'task_id': 'AGEDS-20261001-N11',
               'started_at': datetime.now(timezone.utc).isoformat(),
               'status': 'started', 'actual_inference_completed': False,
               'input_kind': 'generated_synthetic_speech', 'expected_text': TEXT,
               'model': {'repository': 'Systran/faster-whisper-tiny.en',
                         'acquisition_revision': args.model_revision,
                         'file_sha256': {name: digest(model / name) for name in MODEL_FILES},
                         'file_bytes': {name: (model / name).stat().st_size for name in MODEL_FILES}},
               'environment': {'python': platform.python_version(), 'platform': platform.platform(),
                               'cpu_count': os.cpu_count()},
               'limitations': ['Single generated English utterance; not human corpus quality evaluation.',
                               'No manual listening or acoustic alignment verification.',
                               'Model revision is recorded by this harness; production adapter reports unknown.',
                               'Metadata archive excludes audio and model bytes; not replay or live restore.']}
    started = time.perf_counter()
    try:
        from server.app import archive, citations, db, evidence, jobs, packages, transcription, worker
        receipt['code_sha256'] = {name: digest(Path(__file__).resolve().parents[1] / name)
                                  for name in ('server/app/worker.py', 'server/app/jobs.py',
                                               'server/app/citations.py', 'server/app/packages.py',
                                               'server/requirements-whisper.txt', 'scripts/real_asr_smoke.py')}
        receipt['environment']['packages'] = {name: importlib.metadata.version(name) for name in
            ('faster-whisper', 'ctranslate2', 'onnxruntime', 'av', 'numpy', 'tokenizers', 'huggingface-hub', 'requests')}
        wav = root / 'synthetic.wav'
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                   f'flite=text={TEXT}:voice=slt', '-ar', '16000', '-ac', '1', str(wav)]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
        before = (digest(wav), wav.stat().st_mtime_ns)
        with wave.open(str(wav)) as audio:
            duration = audio.getnframes() / audio.getframerate()
        receipt['synthesis'] = {'command': command, 'ffmpeg_version': subprocess.check_output(
            ['ffmpeg', '-version'], text=True).splitlines()[0], 'sha256': before[0], 'duration_seconds': duration}
        db.init_db()
        source = evidence.ensure_source('synthetic', 'N11 generated offline flite speech')
        artifact = evidence.ingest_file(wav, source_id=source, source_locator='synthetic://n11/flite-slt')
        transcription.queue_transcription(artifact)
        job = jobs.claim_job(worker_id='real-asr-smoke', lease_seconds=300)
        require(job is not None, 'No job claimed')
        inference_start = time.perf_counter()
        transcript_id = worker.process_job(job, lease_seconds=300)
        receipt['worker_seconds'] = time.perf_counter() - inference_start
        receipt['actual_inference_completed'] = True
        with db.session() as connection:
            row = dict(connection.execute('SELECT * FROM derived_text WHERE id=?', (transcript_id,)).fetchone())
            receipt['processing_runs'] = [dict(r) for r in connection.execute('SELECT * FROM processing_runs')]
            receipt['jobs'] = [dict(r) for r in connection.execute('SELECT * FROM jobs')]
        receipt['transcript'] = row
        segments = json.loads(row['segments_json'])
        require(bool(row['text'].strip()) and bool(segments), 'Empty inference result')
        metadata = json.loads(row['metadata_json'])
        require(metadata['word_timing']['status'] == 'available', 'ASR words are not selectable')
        require(receipt['jobs'][0]['status'] == 'done', 'Worker did not publish atomically with job completion')
        # Select actual saved model words, with whitespace intact, rather than expected words.
        refs = [{'segment_index': 0, 'word_index': i} for i in range(min(3, len(segments[0]['words'])))]
        quote = ''.join(segments[0]['words'][ref['word_index']]['word'] for ref in refs)
        anchor = citations.create_citation(artifact, transcript_id, word_refs=refs, quote_text=quote)
        require(anchor['derived_text_id'] == transcript_id and anchor['quote_text'] == quote, 'Citation lost version or exact text')
        receipt['citation'] = anchor
        package = packages.export_metadata_package()
        verdict = packages.validate_metadata_package(package)
        require(verdict['valid'], f'Invalid metadata export: {verdict}')
        archive_receipt = archive.import_metadata_archive(package, root / 'inert.sqlite')
        restored = archive.read_metadata_archive(root / 'inert.sqlite')
        require(packages.canonical_json(package) == packages.canonical_json(restored), 'Inert archive roundtrip differs')
        (root / 'metadata.json').write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
        receipt['export'] = {'validation': verdict, 'archive_receipt': archive_receipt,
                             'canonical_roundtrip_equal': True, 'metadata_sha256': digest(root / 'metadata.json')}
        require(before == (digest(wav), wav.stat().st_mtime_ns), 'Generated source changed')
        require(digest(model / 'model.bin') == receipt['model']['file_sha256']['model.bin'], 'Model weights changed')
        receipt['source_unchanged'] = True
        receipt['status'] = 'passed'
    except Exception as error:
        receipt.update(status='failed', error_type=type(error).__name__, error=str(error), traceback=traceback.format_exc())
    finally:
        receipt['total_seconds'] = time.perf_counter() - started
        receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
        (root / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': receipt['status'], 'receipt': str(root / 'receipt.json'),
                      'actual_inference_completed': receipt['actual_inference_completed']}))
    return 0 if receipt['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

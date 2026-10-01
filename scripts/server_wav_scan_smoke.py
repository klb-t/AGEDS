#!/usr/bin/env python3
"""Run the real scan CLI/export on a new synthetic WAV tree, never a private corpus."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import time
import traceback


def sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def snapshot(root):
    return [{'path': str(path.relative_to(root)), 'kind': 'directory' if path.is_dir() else 'file',
             'size_bytes': path.stat().st_size if path.is_file() else None,
             'mtime_ns': path.stat().st_mtime_ns,
             'sha256': sha256(path) if path.is_file() else None}
            for path in [root, *sorted(root.rglob('*'))]]


def header(data_bytes, format_code=1):
    fmt = struct.pack('<HHIIHH', format_code, 1, 16000, 32000, 2, 16)
    return b'RIFF' + struct.pack('<I', 36 + data_bytes) + b'WAVEfmt ' + struct.pack('<I', 16) + fmt + b'data' + struct.pack('<I', data_bytes)


def fixtures(root):
    root.mkdir()
    (root / 'valid_pcm.wav').write_bytes(header(16000) + bytes(16000))
    (root / 'truncated_44_declares_16000.wav').write_bytes(header(16000))
    with (root / 'large_declared_sparse.wav').open('wb') as output:
        output.write(header(4 * 1024 * 1024))
        output.seek(44 + 4 * 1024 * 1024 - 1)
        output.write(b'\0')
    fmt = header(16)[12:36]
    payload = b'WAVE' + (b'JUNK' + struct.pack('<I', 0)) * 9000 + fmt + b'data' + struct.pack('<I', 16) + bytes(16)
    (root / 'junk_fanout_over_64k.wav').write_bytes(b'RIFF' + struct.pack('<I', len(payload)) + payload)
    (root / 'unsupported_format.wav').write_bytes(header(32, format_code=6) + bytes(32))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True, help='New isolated directory; must not exist')
    args = parser.parse_args()
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    source, output = root / 'source', root / 'metadata' / 'manifest.json'
    receipt = {'schema_version': 1, 'task_id': 'AGEDS-20261001-N52', 'status': 'started',
               'started_at': datetime.now(timezone.utc).isoformat(), 'input_kind': 'synthetic_generated_wav_tree',
               'python': platform.python_version(), 'actual_cli_subprocess': False,
               'limitations': ['Header declarations are not decoded audio or measured playback duration.',
                               'No ASR, acoustic alignment, device runtime or private corpus was exercised.',
                               'Source access timestamps may change; source bytes, names and modification times are checked.',
                               'Manifest is metadata, not a source copy or replay archive.']}
    started = time.perf_counter()
    try:
        receipt['code_sha256'] = {name: sha256(repo / name) for name in (
            'server/app/cli.py', 'server/app/scanner.py', 'server/app/wav_header.py', 'scripts/server_wav_scan_smoke.py')}
        fixtures(source)
        before = snapshot(source)
        receipt['source_before'] = before
        command = [sys.executable, '-m', 'server.app.cli', 'scan', str(source), '--output', str(output),
                   '--max-hash-bytes', '1048576', '--max-total-hash-bytes', '2097152', '--max-output-bytes', '262144']
        environment = os.environ.copy()
        environment.update(EW_DATA_DIR=str(root / 'unexpected-app-data'), PYTHONDONTWRITEBYTECODE='1')
        completed = subprocess.run(command, cwd=repo, env=environment, capture_output=True, text=True, timeout=60)
        receipt['actual_cli_subprocess'] = True
        receipt['cli'] = {'command': command, 'returncode': completed.returncode, 'stdout': completed.stdout, 'stderr': completed.stderr}
        require(completed.returncode == 0, 'CLI did not successfully export the scan manifest')
        emitted = json.loads(completed.stdout)
        require(emitted['source_bytes_written'] is False, 'CLI output lost read-only declaration')
        manifest = json.loads(output.read_text())
        limits = manifest['policy']['limits']
        require(manifest['policy']['read_only'] is True and manifest['policy']['source_copies'] is False, 'Manifest lost source policy')
        require(limits['max_wav_header_bytes'] == 65536, 'Unexpected WAV prefix budget')
        require(limits['max_total_wav_header_bytes'] == 8 * 1024 * 1024, 'Unexpected aggregate WAV prefix budget')
        require(manifest['coverage']['complete'] is False, 'Header-only WAV scan falsely reports complete coverage')
        files = {entry['relative_path']: entry for entry in manifest['files']}
        require(set(files) == {item['path'] for item in before if item['kind'] == 'file'}, 'CLI inventory differs from synthetic source tree')
        observations = {}
        for name, entry in files.items():
            metadata = entry['audio_metadata']
            probe = metadata['header_probe']
            require(metadata['source'] == 'same_descriptor_riff_header' and metadata['scope'] == 'header_prefix_only', 'Missing bounded WAV header provenance')
            require(metadata['size_basis'] == 'fstat_same_descriptor' and metadata['source_stability'] == 'descriptor_stat_unchanged', 'Missing descriptor size/stability observation')
            require(metadata['body_validated'] is False and probe['body_validated'] is False, 'Header scan claims validated audio body')
            require(0 <= metadata['header_bytes_read'] <= 65536 and 0 <= probe['bytes_inspected'] <= 65536, 'WAV header read exceeds per-file budget')
            observations[name] = {'parse_status': entry['parse_status'], 'hash_status': entry['hash_status'],
                                  'sha256': entry['sha256'], 'audio_metadata': metadata}
        valid = observations['valid_pcm.wav']
        require(valid['parse_status'] == 'metadata_only' and valid['audio_metadata']['header_probe']['status'] == 'observed', 'Valid PCM header not recognized')
        require(valid['audio_metadata']['header_probe']['declared_duration_sec'] == 0.5, 'Valid declared duration differs')
        require(valid['sha256'] == next(item['sha256'] for item in before if item['path'] == 'valid_pcm.wav'), 'Valid WAV full hash differs from source')
        require(valid['audio_metadata']['header_probe']['duration_basis'] == 'declared_data_bytes_divided_by_header_byte_rate', 'Declared duration basis is missing')
        large = observations['large_declared_sparse.wav']
        require(large['sha256'] is None, 'Large WAV unexpectedly receives a whole-file digest despite hash cap')
        require(large['audio_metadata']['header_probe']['declared_duration_sec'] == 131.072, 'Large declared duration differs')
        for name in ['truncated_44_declares_16000.wav', 'junk_fanout_over_64k.wav', 'unsupported_format.wav']:
            probe = observations[name]['audio_metadata']['header_probe']
            require(probe['declared_duration_sec'] is None, f'{name}: invalid/limited header acquired a duration')
            require(observations[name]['parse_status'] != 'metadata_only', f'{name}: invalid/limited header marked complete')
        require(observations['unsupported_format.wav']['audio_metadata']['header_probe']['status'] == 'unsupported', 'Unsupported format was not explicit')
        reads = sum(item['audio_metadata']['header_bytes_read'] for item in observations.values())
        require(manifest['coverage']['wav_header_bytes_read'] == reads <= limits['max_total_wav_header_bytes'], 'Aggregate header byte accounting differs')
        after = snapshot(source)
        receipt['source_after'] = after
        require(before == after, 'Source names, contents, sizes or modification times changed')
        non_source_files = sorted(str(path.relative_to(root)) for path in root.rglob('*') if path.is_file() and source not in path.parents)
        require(non_source_files == ['metadata/manifest.json'], 'Unexpected artifact/cache/source copy written outside synthetic source tree')
        require(not (root / 'unexpected-app-data').exists(), 'Scan CLI initialized a data/cache directory')
        require(not manifest['tables'], 'WAV manifest unexpectedly includes sample/table payloads')
        receipt.update(status='passed', source_unchanged=True, source_files_count=len(files),
                       observations=observations, coverage=manifest['coverage'], limits=limits,
                       manifest={'path': str(output), 'bytes': output.stat().st_size, 'sha256': sha256(output), 'policy': manifest['policy']},
                       observed_non_source_files=non_source_files, no_audio_copy_in_output=True)
        require(all(sha256(repo / name) == value for name, value in receipt['code_sha256'].items()), 'Executed code changed during smoke')
    except Exception as error:
        receipt.update(status='failed', error_type=type(error).__name__, error=str(error), traceback=traceback.format_exc())
    finally:
        receipt['elapsed_seconds'] = time.perf_counter() - started
        receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
        (root / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'status': receipt['status'], 'receipt': str(root / 'receipt.json')}))
    return 0 if receipt['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

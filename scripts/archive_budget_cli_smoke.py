#!/usr/bin/env python3
"""Real CLI acceptance for inert archives and hidden semantic JSON budgets.

All fixtures are compact and synthetic. The harness hashes its own source sentinel;
CLI operations use inert metadata archives and never restore live application state.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(path):
    stat = path.stat()
    return {'path': str(path), 'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': digest(path)}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def nested(depth):
    value = 'literal leaf'
    for _ in range(depth):
        value = [value]
    return value


def wire_depth(value):
    stack, maximum = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        maximum = max(maximum, depth)
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return maximum


def rehash(package):
    payload = {key: value for key, value in package.items() if key != 'integrity'}
    package['integrity'] = {'algorithm': 'sha256', 'canonicalization': 'sorted-keys-compact-utf8-no-nan/v1',
                            'payload_sha256': hashlib.sha256(canonical(payload)).hexdigest(),
                            'table_sha256': {name: hashlib.sha256(canonical(rows)).hexdigest() for name, rows in payload['tables'].items()}}
    return package


def fixture(table_names, source):
    quote = ' Raw ąć\n'
    tables = {name: [] for name in table_names}
    tables['cases'] = [{'id': 1, 'name': 'synthetic case'}]
    tables['sources'] = [{'id': 2, 'case_id': 1, 'label': 'literal source'}]
    tables['artifacts'] = [{'id': 3, 'source_id': 2, 'stored_path': str(source),
                            'source_locator': 'urn:synthetic:never-dereference',
                            'metadata_json': json.dumps(nested(80)), 'sha256': digest(source), 'size_bytes': source.stat().st_size}]
    tables['jobs'] = [{'id': 4, 'artifact_id': 3, 'status': 'running', 'lease_token': 'historical-only-not-a-live-lease'}]
    tables['derived_text'] = [{'id': 5, 'artifact_id': 3, 'kind': 'transcript', 'text': quote,
                              'segments_json': json.dumps([{'start': 0.125, 'end': 1.25, 'text': quote}])}]
    tables['annotations'] = [{'id': 6, 'artifact_id': 3, 'derived_text_id': 5, 'body': 'raw note\r\n'}]
    selector = {'kind': 'segments', 'indices': [0], 'text_join': 'concatenate_exact',
                'time_unit': 'seconds', 'stored_time_unit': 'milliseconds', 'rounding': 'nearest_ms', 'precision': 'segment'}
    tables['evidence_anchors'] = [{'id': 7, 'artifact_id': 3, 'derived_text_id': 5,
                                 'start_ms': 125, 'end_ms': 1250, 'quote_text': quote,
                                 'quote_sha256': hashlib.sha256(quote.encode()).hexdigest(), 'selector_json': json.dumps(selector)}]
    return rehash({'schema': 'ageds.metadata-package/v1', 'metadata_only': True, 'source_bytes_included': False,
                   'replay_supported': False, 'signed': False, 'exported_at': 'synthetic fixed time',
                   'missing_tables': [], 'tables': tables, 'extension': {'literal': ['keep', -0.0, True, None]}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True, help='New isolated directory')
    args = parser.parse_args()
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    environment = os.environ.copy()
    environment.update(EW_DATA_DIR=str(root / 'unexpected-live-data'), EW_DB_PATH=str(root / 'unexpected-live.sqlite'),
                       EW_STORE_DIR=str(root / 'unexpected-live-store'), PYTHONDONTWRITEBYTECODE='1')
    receipt = {'schema_version': 1, 'task_id': 'AGEDS-20261001-N65', 'status': 'started',
               'started_at': datetime.now(timezone.utc).isoformat(), 'commands': [],
               'input_kind': 'compact synthetic metadata and inert archive fixtures',
               'limits': {'cli_max_input_bytes': 65536, 'cli_max_archive_bytes': 1048576, 'cli_max_rows': 100,
                          'default_semantic_max_depth': 64, 'invalid_semantic_fixture_depth': 70,
                          'opaque_metadata_fixture_depth': 80},
               'limitations': ['Inert metadata archive only; no live restore, jobs resumed, replay, signatures or media copies.',
                               'Only generated fixtures; no ASR, downloads, private corpus or device interaction.',
                               'Byte equality and hashes are not proof of authenticity or truth.']}
    started = time.perf_counter()
    try:
        from server.app import packages
        receipt['code_sha256'] = {name: digest(repo / name) for name in (
            'server/app/cli.py', 'server/app/packages.py', 'server/app/archive.py',
            'server/app/verification_budget.py', 'server/app/citations.py', 'scripts/archive_budget_cli_smoke.py')}
        inputs = root / 'inputs'; inputs.mkdir()
        source = inputs / 'literal-source.bin'; source.write_bytes(b'synthetic source; archive must not copy or modify this')
        valid = fixture(packages.TABLES, source)
        hidden_segments = deepcopy(valid)
        segments = json.loads(hidden_segments['tables']['derived_text'][0]['segments_json'])
        segments[0]['extra_semantic_tree'] = nested(70)
        hidden_segments['tables']['derived_text'][0]['segments_json'] = json.dumps(segments)
        rehash(hidden_segments)
        hidden_selector = deepcopy(valid)
        selector = json.loads(hidden_selector['tables']['evidence_anchors'][0]['selector_json'])
        selector['extra_semantic_tree'] = nested(70)
        hidden_selector['tables']['evidence_anchors'][0]['selector_json'] = json.dumps(selector)
        rehash(hidden_selector)
        fixtures = {'valid': valid, 'hidden-segments': hidden_segments, 'hidden-selector': hidden_selector}
        paths = {}
        for name, value in fixtures.items():
            paths[name] = inputs / f'{name}.json'; paths[name].write_bytes(canonical(value))
            require(wire_depth(value) < 64 and paths[name].stat().st_size < 65536, 'Fixture accidentally exceeds outer wire-depth/byte budget')
        receipt['fixture_wire_depths'] = {name: wire_depth(value) for name, value in fixtures.items()}
        before = [snapshot(path) for path in sorted(inputs.iterdir())]
        receipt['inputs_before'] = before
        common = ['--max-input-bytes', '65536', '--max-archive-bytes', '1048576', '--max-rows', '100']

        def run(arguments, expected, readonly=()):
            held = [snapshot(path) for path in readonly]
            command = [sys.executable, '-m', 'server.app.cli', *map(str, arguments)]
            completed = subprocess.run(command, cwd=repo, env=environment, capture_output=True, text=True, timeout=30)
            item = {'command': command, 'returncode': completed.returncode, 'stdout': completed.stdout, 'stderr': completed.stderr}
            receipt['commands'].append(item)
            require(completed.returncode == expected, f'Unexpected CLI status for {arguments[0]}: {completed.returncode}')
            require(held == [snapshot(path) for path in readonly], f'CLI modified a read-only input for {arguments[0]}')
            item['readonly_inputs_unchanged'] = True
            return item

        run(['metadata-verify', paths['valid'], '--max-input-bytes', '65536'], 0, [paths['valid'], source])
        output = root / 'outputs'; archive = output / 'valid.sqlite'; restored = output / 'roundtrip.json'
        imported = run(['metadata-archive-import', paths['valid'], archive, *common], 0, [paths['valid'], source])
        exported = run(['metadata-archive-export', archive, restored, *common], 0, [archive, source])
        restored_value = json.loads(restored.read_bytes())
        require(canonical(valid) == canonical(restored_value), 'Canonical inert roundtrip differs')
        require(restored_value['tables']['artifacts'][0]['metadata_json'] == valid['tables']['artifacts'][0]['metadata_json'], 'Opaque deep metadata changed')
        require(restored_value['tables']['jobs'][0]['status'] == 'running', 'Historical job state changed')
        for command in (imported, exported):
            flags = json.loads(command['stdout'])
            require(flags['inert'] is True and flags['jobs_resumed'] is False and flags['live_restore_supported'] is False,
                    'Archive CLI omitted inert/no-restore/no-resume flags')
        with sqlite3.connect(f'file:{archive}?mode=ro', uri=True) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require(names == {'archive_envelope', 'archive_records'}, 'Archive contains live application tables')
        for name in ('hidden-segments', 'hidden-selector'):
            verified = run(['metadata-verify', paths[name], '--max-input-bytes', '65536'], 1, [paths[name], source])
            require('verification_limit_exceeded' in verified['stdout'], f'{name}: expected semantic verification limit result')
            rejected = root / 'never-created' / f'{name}.sqlite'
            imported_bad = run(['metadata-archive-import', paths[name], rejected, *common], 2, [paths[name], source])
            require('verification_limit_exceeded' in imported_bad['stderr'], f'{name}: expected bounded archive rejection')
            require(not rejected.parent.exists(), 'Rejected import created a partial output/directory')

        # A deliberately malicious inert fixture, with consistent envelope digests, bypasses
        # the importer only during fixture construction. The real CLI must reject its export.
        hostile = inputs / 'hidden-segments.sqlite'; hostile.write_bytes(archive.read_bytes())
        with sqlite3.connect(hostile) as connection:
            envelope = {key: value for key, value in hidden_segments.items() if key != 'tables'}
            connection.execute('UPDATE archive_envelope SET package_sha256=?,envelope_json=?',
                               (hashlib.sha256(canonical(hidden_segments)).hexdigest(), canonical(envelope).decode()))
            connection.execute("UPDATE archive_records SET row_json=? WHERE table_name='derived_text' AND ordinal=0",
                               (canonical(hidden_segments['tables']['derived_text'][0]).decode(),))
        hostile_before = snapshot(hostile)
        rejected_export = root / 'never-exported' / 'rejected.json'
        rejected = run(['metadata-archive-export', hostile, rejected_export, *common], 2, [hostile, source])
        require('verification_limit_exceeded' in rejected['stderr'], 'Malicious archive was not rejected for semantic budget')
        require(not rejected_export.parent.exists(), 'Rejected export created partial output')
        run(['metadata-archive-import', paths['valid'], archive, *common], 2, [archive, source])
        after = [snapshot(path) for path in sorted(inputs.iterdir()) if path != hostile]
        require(before == after, 'Generated input names/bytes/mtime changed')
        require(snapshot(hostile) == hostile_before, 'Hostile inert input changed')
        require(not list(root.rglob('.ageds-*')), 'Partial temporary publication left behind')
        require(not any((root / name).exists() for name in ['unexpected-live-data', 'unexpected-live.sqlite', 'unexpected-live-store']), 'CLI initialized live storage')
        require(all(digest(repo / name) == value for name, value in receipt['code_sha256'].items()), 'Executed code changed during acceptance')
        receipt.update(status='passed', actual_cli_subprocesses=len(receipt['commands']), canonical_roundtrip_equal=True,
                       opaque_deep_metadata_preserved=True, archive_semantic_hidden_depth_rejections=3, metadata_verify_limit_reports=2,
                       no_partial_outputs=True, source_and_inputs_unchanged=True, inputs_after=after,
                       hostile_archive_unchanged=hostile_before,
                       output_files=[snapshot(path) for path in sorted(output.iterdir())], inert_table_names=sorted(names))
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

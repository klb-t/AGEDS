#!/usr/bin/env python3
"""Run Python and real compiled Kotlin probes on identical bounded synthetic bytes.

No downloads, Gradle, source scanning or audio decoding. Existing cached Kotlin
compiler dependencies and a JDK are required; see --toolchain-root / --java.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'scripts/wav_parity/fixtures.json'
FIELDS = {
    'status': 'status', 'coverage': 'coverage', 'bytes_inspected': 'bytesInspected',
    'prefix_limit_bytes': 'prefixLimitBytes', 'end_of_input': 'endOfInput',
    'provider_size_bytes': 'providerSizeBytes', 'riff_declared_bytes': 'riffDeclaredBytes',
    'format_code': 'formatCode', 'channels': 'channels', 'sample_rate_hz': 'sampleRateHz',
    'byte_rate': 'byteRate', 'block_align_bytes': 'blockAlignBytes', 'bits_per_sample': 'bitsPerSample',
    'data_declared_bytes': 'dataDeclaredBytes', 'declared_duration_sec': 'declaredDurationSec',
    'duration_basis': 'durationBasis', 'body_validated': 'bodyValidated',
}
SOURCES = [
    'core/src/commonMain/kotlin/dev/klbt/ageds/core/SourceScanModels.kt',
    'core/src/commonMain/kotlin/dev/klbt/ageds/core/WavHeaderObservation.kt',
    'core/src/commonMain/kotlin/dev/klbt/ageds/core/WavHeaderProbe.kt',
    'scripts/wav_parity/WavHeaderParityRunner.kt',
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_fixtures(path):
    raw = path.read_bytes()
    if len(raw) > 100 * 1024:
        raise ValueError('fixture JSON exceeds 100 KiB')
    document = json.loads(raw)
    if document.get('schema') != 'ageds.synthetic-wav-header-fixtures/v1':
        raise ValueError('unsupported fixture schema')
    records = document['fixtures']
    if not 1 <= len(records) <= 100:
        raise ValueError('fixture count outside bound')
    seen = set()
    generated = []
    for record in records:
        identity = record['id']
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError('invalid/duplicate fixture identity')
        seen.add(identity)
        prefix = bytes.fromhex(record['prefix_hex'])
        repeated = bytes.fromhex(record.get('repeat_hex', ''))
        count = record.get('repeat_count', 0)
        if type(count) is not int or not 0 <= count <= 131072:
            raise ValueError('invalid repeat count')
        suffix = bytes.fromhex(record.get('suffix_hex', ''))
        if len(prefix) + len(repeated) * count + len(suffix) > 131072:
            raise ValueError('generated fixture exceeds 128 KiB')
        data = prefix + repeated * count + suffix
        provider = record['provider_size_bytes']
        if provider is not None and (type(provider) is not int or not 0 <= provider <= 2**63 - 1):
            raise ValueError('invalid shared-domain provider size')
        if type(record['end_of_input']) is not bool:
            raise ValueError('EOF flag must be boolean')
        generated.append((record, data))
    return raw, generated


def jar(cache, group, artifact, version='*'):
    files = sorted(cache.glob(f'{group}/{artifact}/{version}/*/*.jar'))
    if len(files) != 1:
        raise RuntimeError(f'expected one cached {group}:{artifact}:{version}, found {len(files)}; no download attempted')
    return files[0]


def run_kotlin(toolchain_root, java_override, generated):
    cache = toolchain_root / 'gradle-user-home/caches/modules-2/files-2.1'
    if java_override:
        java = java_override
    else:
        candidates = sorted(toolchain_root.glob('jdk21/*/bin/java'))
        if len(candidates) != 1:
            raise RuntimeError('provide --java for an existing JDK')
        java = candidates[0]
    kotlin = jar(cache, 'org.jetbrains.kotlin', 'kotlin-stdlib', '2.4.20')
    compiler = [jar(cache, 'org.jetbrains.kotlin', name, '2.4.20') for name in
                ('kotlin-compiler-embeddable', 'kotlin-build-tools-api', 'kotlin-script-runtime', 'kotlin-daemon-embeddable')]
    compiler += [kotlin, jar(cache, 'org.jetbrains.kotlin', 'kotlin-reflect', '1.6.10'),
                 jar(cache, 'org.jetbrains.kotlinx', 'kotlinx-coroutines-core-jvm', '1.8.0'),
                 jar(cache, 'org.jetbrains', 'annotations', '13.0')]
    runtime = [kotlin, jar(cache, 'org.jetbrains.kotlinx', 'kotlinx-serialization-core-jvm', '1.11.0'),
               jar(cache, 'org.jetbrains.kotlinx', 'kotlinx-serialization-json-jvm', '1.11.0')]
    plugin = jar(cache, 'org.jetbrains.kotlin', 'kotlin-serialization-compiler-plugin-embeddable', '2.4.20')
    join = lambda paths: ':'.join(map(str, paths))
    inputs = '\n'.join(json.dumps({'id': item['id'], 'prefix_base64': base64.b64encode(data).decode('ascii'),
                                 'provider_size_bytes': item['provider_size_bytes'], 'end_of_input': item['end_of_input']},
                                separators=(',', ':')) for item, data in generated) + '\n'
    with tempfile.TemporaryDirectory(prefix='ageds-wav-parity-') as directory:
        output = Path(directory) / 'classes'
        compile_command = [str(java), '-Xmx512m', '-cp', join(compiler), 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
                           '-no-stdlib', '-no-reflect', '-Xplugin=' + str(plugin), '-classpath', join(runtime),
                           '-d', str(output), *[str(ROOT / source) for source in SOURCES]]
        compiled = subprocess.run(compile_command, capture_output=True, text=True, timeout=120)
        if compiled.returncode:
            raise RuntimeError('Kotlin compilation failed:\n' + compiled.stderr[-10000:])
        invoked = subprocess.run([str(java), '-cp', join([*runtime, output]), 'WavHeaderParityRunnerKt'],
                                 input=inputs, capture_output=True, text=True, timeout=30)
        if invoked.returncode:
            raise RuntimeError('Kotlin execution failed:\n' + invoked.stderr[-10000:])
        outputs = [json.loads(line) for line in invoked.stdout.splitlines()]
    if len(outputs) != len(generated) or [r['id'] for r in outputs] != [r['id'] for r, _ in generated]:
        raise RuntimeError('Kotlin output identities/count differ from supplied fixtures')
    version = subprocess.run([str(java), '-version'], capture_output=True, text=True, timeout=10)
    return outputs, {
        'kotlin_compiler': '2.4.20', 'serialization': '1.11.0', 'java_version': version.stderr.strip(),
        'compile_returncode': compiled.returncode, 'run_returncode': invoked.returncode,
        'compile_warning_lines': compiled.stderr.count('warning:'),
        'dependencies_sha256': {p.name: sha(p.read_bytes()) for p in [*compiler, *runtime, plugin]},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain-root', type=Path, default=Path('/tmp/ageds-clean-build-fw_7dz1b'))
    parser.add_argument('--java', type=Path)
    parser.add_argument('--fixtures', type=Path, default=FIXTURES)
    parser.add_argument('--receipt', type=Path, help='optional explicit JSON receipt destination')
    args = parser.parse_args(argv)
    raw, generated = load_fixtures(args.fixtures)
    inputs = {source: sha((ROOT / source).read_bytes()) for source in
              [*SOURCES, 'server/app/wav_header.py', 'scripts/compare_wav_headers.py']}
    spec = importlib.util.spec_from_file_location('ageds_parity_python_probe', ROOT / 'server/app/wav_header.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native, runtime = run_kotlin(args.toolchain_root, args.java, generated)
    cases = []
    for (record, data), native_record in zip(generated, native):
        before = sha(data)
        observed = module.probe_wav_header(data, provider_size_bytes=record['provider_size_bytes'], end_of_input=record['end_of_input'])
        other = native_record['observation']
        python_fields = {key: observed[key] for key in FIELDS}
        kotlin_fields = {key: other[name] for key, name in FIELDS.items()}
        differences = {key: {'python': python_fields[key], 'kotlin': kotlin_fields[key]}
                       for key in FIELDS if type(python_fields[key]) is not type(kotlin_fields[key]) or python_fields[key] != kotlin_fields[key]}
        # Both implementations agreeing is not enough to satisfy the fixture's
        # independently stated status/duration checkpoints.
        expectations = []
        for implementation, fields in (('python', python_fields), ('kotlin', kotlin_fields)):
            if fields['status'] != record['expected_status']:
                expectations.append({'implementation': implementation, 'field': 'status', 'expected': record['expected_status'], 'actual': fields['status']})
            if 'expected_duration_sec' in record and fields['declared_duration_sec'] != record['expected_duration_sec']:
                expectations.append({'implementation': implementation, 'field': 'declared_duration_sec', 'expected': record['expected_duration_sec'], 'actual': fields['declared_duration_sec']})
        cases.append({'id': record['id'], 'supplied_bytes': len(data), 'supplied_sha256': before,
                      'effective_prefix_sha256': sha(data[:65536]), 'provider_size_bytes': record['provider_size_bytes'],
                      'requested_end_of_input': record['end_of_input'], 'input_unchanged_python': sha(data) == before,
                      'supplied_sha256_kotlin': native_record['supplied_sha256'],
                      'input_unchanged_kotlin': native_record['input_unchanged'],
                      'identical_runtime_inputs': before == native_record['supplied_sha256'],
                      'python_projection': python_fields, 'kotlin_projection': kotlin_fields, 'differences': differences,
                      'expectation_failures': expectations, 'python_issue_codes': [v['code'] for v in observed['issues']],
                      'kotlin_issue_codes': [v['code'] for v in other['issues']]})
    after = {source: sha((ROOT / source).read_bytes()) for source in inputs}
    if after != inputs:
        raise RuntimeError('source files changed during comparison; rerun on stable sources')
    failed = [case['id'] for case in cases if case['differences'] or case['expectation_failures'] or not case['input_unchanged_python'] or not case['input_unchanged_kotlin'] or not case['identical_runtime_inputs']]
    receipt = {'schema': 'ageds.wav-header-cross-runtime-receipt/v1', 'task': 'AGEDS-20261001-N53',
               'executed_at_utc': datetime.now(timezone.utc).isoformat(), 'fixture_count': len(cases),
               'failed_cases': failed, 'passed': not failed, 'fixture_json_sha256': sha(raw),
               'source_sha256': inputs, 'runtime': runtime, 'cases': cases,
               'limits': ['Pure prefix probes only; no SAF/POSIX scan-policy equivalence.',
                          'Synthetic bytes only; no real corpus or acoustic validation.',
                          'Issue messages are not compared; codes are recorded only.',
                          'Valid shared argument domain only; Python rejects negative/bool sizes, unlike native negative-size observation.',
                          'Agreement is scoped to these fixtures, not exhaustive format support.']}
    if args.receipt:
        args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'fixture_count': len(cases), 'passed': not failed, 'failed_cases': failed}, ensure_ascii=False))
    if failed:
        for case in cases:
            if case['id'] in failed:
                print(json.dumps({'id': case['id'], 'differences': case['differences'], 'expectation_failures': case['expectation_failures']}, ensure_ascii=False))
    return int(bool(failed))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('WAV parity comparison failed: ' + str(error), file=sys.stderr)
        raise SystemExit(2)

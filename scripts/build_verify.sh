#!/usr/bin/env bash
# Build the current checkout in an isolated directory; keep a reviewable receipt.
set -euo pipefail
AGEDS_REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export AGEDS_REPO_ROOT
exec python3 - "$@" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

repo = Path(os.environ['AGEDS_REPO_ROOT'])
output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='ageds-build-result-'))
output.mkdir(parents=True, exist_ok=True)
# Do not overwrite a prior receipt or APK.
if any(output.iterdir()):
    raise SystemExit('Output directory must be empty: ' + str(output))
env = dict(os.environ)
java = Path(env.get('JAVA_HOME', '')) / 'bin' / 'java'
javac = java.with_name('javac')
sdk = Path(env.get('ANDROID_HOME', env.get('ANDROID_SDK_ROOT', '')))
if not java.is_file() or not javac.is_file() or not (sdk / 'platforms').is_dir():
    raise SystemExit('Set JAVA_HOME to a full JDK and ANDROID_HOME to an installed Android SDK')
env['PATH'] = str(java.parent) + os.pathsep + env.get('PATH', '')
env['ANDROID_HOME'] = str(sdk.resolve())
root_files = {'build.gradle.kts', 'settings.gradle.kts', 'gradle.properties', 'gradlew', 'gradlew.bat'}
def collect_inputs():
    files = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=repo).decode().split('\0')
    return sorted({p for p in files if p and (p in root_files or p.startswith(('androidApp/', 'core/', 'gradle/'))) and (repo / p).is_file()})
inputs = collect_inputs()
if any((repo / p).is_symlink() for p in inputs):
    raise SystemExit('Symlink build inputs require explicit review')
def hashes():
    return {p: hashlib.sha256((repo / p).read_bytes()).hexdigest() for p in collect_inputs()}
source_hashes = hashes()
snapshot = Path(tempfile.mkdtemp(prefix='ageds-build-snapshot-'))
for p in inputs:
    target = snapshot / p
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo / p, target)
snapshot_hashes = {p: hashlib.sha256((snapshot / p).read_bytes()).hexdigest() for p in inputs}
if source_hashes != snapshot_hashes:
    raise SystemExit('Sources changed while copying the snapshot')
args = ['./gradlew', '--no-daemon', '--no-build-cache']
# Read the current proxy for this invocation; do not persist its host or port.
proxy = urlparse(env.get('HTTPS_PROXY', env.get('https_proxy', '')))
if proxy.hostname:
    for protocol in ('http', 'https'):
        args += [f'-D{protocol}.proxyHost={proxy.hostname}', f'-D{protocol}.proxyPort={proxy.port or 80}']
trust = env.get('AGEDS_JAVA_TRUST_STORE')
if not trust and Path('/etc/ssl/certs/java/cacerts').is_file():
    trust = '/etc/ssl/certs/java/cacerts'
if trust:
    args += ['-Djavax.net.ssl.trustStore=' + trust]
tasks = [':androidApp:assembleDebug', ':androidApp:testDebugUnitTest', ':core:desktopTest']
args += tasks + ['--stacktrace']
started = datetime.now(timezone.utc).isoformat()
print('Build snapshot:', snapshot, '\nResults:', output, flush=True)
with (output / 'gradle.log').open('w') as log:
    result = subprocess.run(args, cwd=snapshot, env=env, stdout=log, stderr=subprocess.STDOUT)
receipt = {'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(), 'tasks': tasks,
           'build_exit_code': result.returncode, 'source_sha256': source_hashes,
           'source_unchanged': hashes() == source_hashes, 'tests': [],
           'checkout_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip(),
           'java_version': subprocess.check_output([str(java), '-version'], stderr=subprocess.STDOUT, text=True).strip()}
for suite in snapshot.glob('*/build/test-results/**/TEST-*.xml'):
    document = ET.parse(suite).getroot()
    receipt['tests'].append({'path': str(suite.relative_to(snapshot)), 'suite': document.attrib.get('name'), **{k: int(document.attrib.get(k, 0)) for k in ('tests', 'failures', 'errors', 'skipped')}})
    relative = suite.relative_to(snapshot)
    dest = output / 'test-results' / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(suite, dest)
apk = snapshot / 'androidApp/build/outputs/apk/debug/androidApp-debug.apk'
if result.returncode == 0 and apk.is_file():
    destination = output / 'AGEDS_night_debug.apk'
    shutil.copy2(apk, destination)
    receipt['apk_sha256'] = hashlib.sha256(destination.read_bytes()).hexdigest()
    receipt['apk_bytes'] = destination.stat().st_size
    signers = sorted(sdk.glob('build-tools/*/apksigner'))
    if not signers:
        raise SystemExit('Build succeeded but SDK apksigner is missing')
    with (output / 'apksigner.log').open('w') as log:
        verification = subprocess.run([str(signers[-1]), 'verify', '--verbose', '--print-certs', str(destination)], env=env, stdout=log, stderr=subprocess.STDOUT)
    receipt['apksigner_exit_code'] = verification.returncode
(output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps({k: v for k, v in receipt.items() if k != 'source_sha256'}, indent=2), flush=True)
if result.returncode or not receipt['source_unchanged'] or receipt.get('apksigner_exit_code') != 0:
    raise SystemExit(1)
required_test_roots = ('androidApp/build/test-results/testDebugUnitTest/', 'core/build/test-results/desktopTest/')
missing_test_roots = [root for root in required_test_roots if not any(t['path'].startswith(root) and t['tests'] > 0 for t in receipt['tests'])]
if missing_test_roots or any(t['failures'] or t['errors'] for t in receipt['tests']):
    raise SystemExit('Required tests did not complete successfully')
PY

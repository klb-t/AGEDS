#!/usr/bin/env bash
# Probe first; --run executes only synthetic provider instrumentation on a selected device.
set -euo pipefail
export AGEDS_PROVIDER_REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 - "$@" <<'PY'
import hashlib, json, os, pathlib, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

args = sys.argv[1:]
run = '--run' in args
if any(a != '--run' for a in args):
    raise SystemExit('Usage: verify_saf_provider.sh [--run]')
repo = pathlib.Path(os.environ['AGEDS_PROVIDER_REPO'])
sdk = pathlib.Path(os.environ.get('ANDROID_HOME', os.environ.get('ANDROID_SDK_ROOT', '/nonexistent')))
adb = sdk / 'platform-tools/adb'
output = pathlib.Path(tempfile.mkdtemp(prefix='ageds-saf-receipt-'))
probe = subprocess.run([str(adb), 'devices', '-l'], capture_output=True, text=True) if adb.is_file() else None
devices = [line.split()[0] for line in probe.stdout.splitlines()[1:] if len(line.split()) >= 2 and line.split()[1] == 'device'] if probe and probe.returncode == 0 else []
receipt = {
    'task': 'AGEDS-20261001-N10', 'observed_at': datetime.now(timezone.utc).isoformat(),
    'kvm_device_exists': pathlib.Path('/dev/kvm').exists(),
    'kvm_module_exists': pathlib.Path('/sys/module/kvm').exists(),
    'emulator_binary_exists': (sdk / 'emulator/emulator').is_file(),
    'system_images': sorted(str(p.relative_to(sdk)) for p in sdk.glob('system-images/*/*/*/package.xml')),
    'adb_exit_code': probe.returncode if probe else None,
    'adb_devices_output': probe.stdout.strip() if probe else 'adb unavailable',
    'connected_devices': devices, 'runtime_tests_executed': 0,
    'runtime_status': 'not_requested' if not run else 'blocked_no_device',
    'limitations': ['Synthetic provider only; no picker UI, persisted grant lifecycle, recording selection or real provider acceptance.'],
}
exit_code = 0
if run:
    serial = os.environ.get('ANDROID_SERIAL')
    if not serial and len(devices) == 1: serial = devices[0]
    if serial not in devices:
        receipt['runtime_status'] = 'blocked_select_connected_device'
        exit_code = 2
    else:
        java = pathlib.Path(os.environ.get('JAVA_HOME', '/nonexistent')) / 'bin/java'
        if not java.is_file(): raise SystemExit('Set JAVA_HOME to a full JDK')
        files = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=repo).decode().split('\0')
        files = sorted(set(p for p in files if p and (p in {'build.gradle.kts', 'settings.gradle.kts', 'gradle.properties', 'gradlew', 'gradlew.bat'} or p.startswith(('androidApp/', 'core/', 'gradle/')))))
        def hashes(): return {p: hashlib.sha256((repo/p).read_bytes()).hexdigest() for p in files}
        receipt['source_sha256'] = hashes()
        snapshot = output / 'source'
        for p in files:
            target = snapshot / p; target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(repo/p, target)
        command = ['./gradlew', '--no-daemon', '--no-build-cache']
        proxy = urlparse(os.environ.get('HTTPS_PROXY', os.environ.get('https_proxy', '')))
        if proxy.hostname:
            for protocol in ('http', 'https'): command += [f'-D{protocol}.proxyHost={proxy.hostname}', f'-D{protocol}.proxyPort={proxy.port or 80}']
        trust = os.environ.get('AGEDS_JAVA_TRUST_STORE', '/etc/ssl/certs/java/cacerts')
        if pathlib.Path(trust).is_file(): command += ['-Djavax.net.ssl.trustStore=' + trust]
        command += [':androidApp:connectedDebugAndroidTest', '-Pandroid.testInstrumentationRunnerArguments.class=dev.klbt.ageds.SourceScannerProviderTest', '--stacktrace']
        env = dict(os.environ, ANDROID_SERIAL=serial)
        env['PATH'] = str(java.parent) + os.pathsep + env.get('PATH', '')
        with (output/'gradle.log').open('w') as log: result = subprocess.run(command, cwd=snapshot, env=env, stdout=log, stderr=subprocess.STDOUT)
        receipt['gradle_exit_code'] = result.returncode
        suites = []
        for path in snapshot.glob('androidApp/build/outputs/androidTest-results/connected/**/*.xml'):
            root = ET.parse(path).getroot()
            if root.tag == 'testsuite': suites.append({k: int(root.get(k, '0')) for k in ('tests','failures','errors','skipped')})
        receipt['test_suites'] = suites
        receipt['runtime_tests_executed'] = sum(s['tests'] for s in suites)
        receipt['source_unchanged'] = hashes() == receipt['source_sha256']
        passed = result.returncode == 0 and receipt['runtime_tests_executed'] == 6 and not any(s['failures'] or s['errors'] or s['skipped'] for s in suites) and receipt['source_unchanged']
        receipt['runtime_status'] = 'passed' if passed else 'failed_or_incomplete'
        exit_code = 0 if passed else 1
(output/'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))
print('Receipt:', output/'receipt.json')
raise SystemExit(exit_code)
PY

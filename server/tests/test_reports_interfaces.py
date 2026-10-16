"""Reports through the CLI (real subprocesses), HTTP API, pages, read API, citation
bundles and the declared extension tables of the metadata package."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from server.app import archive, db, main, packages
from server.app.reports.manifest import verify_report
from server.tests.test_reports_support import ReportEnv, load_json

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def env():
    with pytest.MonkeyPatch.context() as monkeypatch:
        environment = ReportEnv(chat=False, catalog=False).start(monkeypatch)
        yield environment
        environment.close()


@pytest.fixture
def client(env, monkeypatch):
    monkeypatch.setenv('EW_REPORTS_DIR', str(env.root / 'http-reports'))
    with TestClient(main.app) as test_client:
        yield test_client


def _cli(env, *args, check_code=None):
    environment = {**os.environ, 'EW_DATA_DIR': str(env.settings.data_dir), 'EW_DB_PATH': str(env.settings.db_path),
                   'EW_STORE_DIR': str(env.settings.store_dir), 'EW_SCAN_ROOTS': str(env.root / 'scan'),
                   'PYTHONPATH': str(REPO)}
    environment.pop('EW_PDF_CHROMIUM', None)
    completed = subprocess.run([sys.executable, '-m', 'server.app.cli', *args], cwd=env.root, env=environment,
                               capture_output=True, text=True, timeout=300)
    if check_code is not None:
        assert completed.returncode == check_code, (completed.stdout, completed.stderr)
    return completed


def test_cli_create_list_verify_and_refusals(env):
    out = env.root / 'cli-out'
    created = _cli(env, 'report-create', '--type', 'timeline', '--params', json.dumps({'counterparty': 'Urząd'}),
                   '--output', str(out), check_code=0)
    result = json.loads(created.stdout)
    assert result['command'] == 'report-create' and result['signed'] is False and result['source_bytes_written'] is False
    assert Path(result['output_dir']).parent == out.resolve()
    listing = json.loads(_cli(env, 'report-list', '--type', 'timeline', check_code=0).stdout)
    row = next(item for item in listing['reports'] if item['id'] == result['report_id'])
    assert row['manifest_sha256'] == result['manifest_sha256'] and row['params']['counterparty'] == 'Urząd'
    verified = json.loads(_cli(env, 'report-verify', result['manifest'], '--expected-manifest-sha256',
                               result['manifest_sha256'], check_code=0).stdout)
    assert verified['valid'] and verified['report_uid'] == result['report_uid']
    (Path(result['output_dir']) / 'report.json').write_text('{}', encoding='utf-8')
    tampered = json.loads(_cli(env, 'report-verify', result['manifest'], check_code=1).stdout)
    assert not tampered['valid'] and {'file_digest_mismatch', 'invalid_computed_document'} <= {e['code'] for e in tampered['errors']}
    refused = _cli(env, 'report-create', '--type', 'timeline', '--output', str(REPO / 'reports-out'), check_code=2)
    assert 'repository' in json.loads(refused.stderr)['message'] and not (REPO / 'reports-out').exists()
    refused = _cli(env, 'report-create', '--type', 'timeline', '--output', str(env.root / 'scan' / 'x'), check_code=2)
    assert 'scan_root' in json.loads(refused.stderr)['message']
    bad = _cli(env, 'report-create', '--type', 'timeline', '--params', '{"nope": 1}', '--output', str(out), check_code=2)
    assert 'unknown parameters' in json.loads(bad.stderr)['message']


def test_report_verify_runs_without_database_or_config(env, tmp_path):
    result = env.report('lawyer_package', {}, env.root / 'isolated')
    copy = tmp_path / 'copied-report'
    shutil.copytree(result['output_dir'], copy)
    code = ('import sys; from server.app.cli import main; rc = main(["report-verify", sys.argv[1]]); '
            'assert "server.app.config" not in sys.modules and "server.app.db" not in sys.modules, sorted(sys.modules); '
            'raise SystemExit(rc)')
    completed = subprocess.run([sys.executable, '-c', code, str(copy / 'manifest.json')], cwd=tmp_path,
                               env={**os.environ, 'PYTHONPATH': str(REPO)}, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)['valid'] is True
    assert not (tmp_path / 'data').exists()


def test_citation_bundle_export_and_independent_inspection(env, tmp_path):
    out = tmp_path / 'bundle-out'
    pairs = [f'{env.artifact_audio}:{env.anchor_segment["id"]}', f'{env.artifact_audio}:{env.anchor_words["id"]}',
             f'{env.artifact_sms}:{env.anchor_segment["id"]}']
    exported = json.loads(_cli(env, 'citation-bundle-export', str(out), *sum((['--citation', p] for p in pairs), []),
                               check_code=0).stdout)
    assert exported['packets'] == 2 and len(exported['excluded']) == 1  # foreign pair is excluded, not dropped
    assert 'PacketNotFound' in exported['excluded'][0]['reason']
    again = _cli(env, 'citation-bundle-export', str(out), '--citation', pairs[0], check_code=2)
    assert 'already exists' in json.loads(again.stderr)['message']
    inspected = json.loads(_cli(env, 'citation-bundle-inspect', str(out), check_code=0).stdout)
    assert inspected['valid'] and {p['anchor_id'] for p in inspected['packets']} == {env.anchor_segment['id'], env.anchor_words['id']}
    single = json.loads(_cli(env, 'citation-export', str(env.artifact_audio), str(env.anchor_segment['id']),
                             str(tmp_path / 'single.json'), check_code=0).stdout)
    packet = out / 'packets' / f'a{env.artifact_audio}-c{env.anchor_segment["id"]}.json'
    assert packet.read_bytes() == (tmp_path / 'single.json').read_bytes()  # byte-identical to the single export
    assert single['payload_sha256'] == json.loads(packet.read_bytes())['integrity']['payload_sha256']
    # The standalone consumer runs as an isolated script (python -I, no AGEDS package import).
    script = REPO / 'server' / 'app' / 'reports' / 'bundle_consumer.py'
    standalone = subprocess.run([sys.executable, '-I', str(script), str(out)], cwd=tmp_path, capture_output=True,
                                text=True, timeout=120)
    assert standalone.returncode == 0, standalone.stderr
    assert json.loads(standalone.stdout)['valid'] is True
    data = json.loads(packet.read_bytes())
    data['payload']['projection']['quote_text'] = 'inny tekst'
    packet.write_text(json.dumps(data), encoding='utf-8')
    broken = subprocess.run([sys.executable, '-I', str(script), str(out)], cwd=tmp_path, capture_output=True, text=True,
                            timeout=120)
    assert broken.returncode == 1 and json.loads(broken.stdout)['errors'][0]['code'] == 'packet_file_mismatch'
    report = env.report('lawyer_package', {}, env.root / 'bundle-report')
    by_report = json.loads(_cli(env, 'citation-bundle-export', str(tmp_path / 'from-report'), '--report',
                                str(report['report_id']), check_code=0).stdout)
    assert by_report['packets'] == 2
    assert json.loads(_cli(env, 'citation-bundle-inspect', str(tmp_path / 'from-report' / 'citation-bundle.json'),
                           check_code=0).stdout)['report_uid'] == report['report_uid']


def test_http_api_pages_and_downloads(env, client, monkeypatch):
    from server.app.web import NAV
    assert ('/reports', 'Raporty') in NAV
    page = client.get('/reports')
    assert page.status_code == 200 and 'href="/reports">Raporty' in page.text and 'data-type-form="timeline"' in page.text
    prefilled = client.get('/reports/new', params={'from': '2026-03-01T00:00:00Z', 'to': '2026-03-02T00:00:00Z'})
    assert prefilled.status_code == 200 and 'value="2026-03-01T00:00:00Z"' in prefilled.text
    created = client.post('/api/reports', json={'type': 'lawyer_package', 'params': {'open_questions': ['Pytanie?']},
                                                'output_subdir': 'case-a'})
    assert created.status_code == 201, created.text
    report = created.json()
    assert Path(report['output_dir']).parent == (env.root / 'http-reports' / 'case-a').resolve()
    assert client.post('/api/reports', json={'type': 'timeline', 'output_subdir': '../escape'}).status_code == 422
    assert client.post('/api/reports', json={'type': 'timeline', 'params': {'bogus': 1}}).status_code == 422
    verify = client.get(f'/api/reports/{report["report_id"]}/verify').json()
    assert verify['valid'] and verify['report_id'] == report['report_id']
    detail = client.get(f'/reports/{report["report_id"]}')
    assert detail.status_code == 200 and 'data-verification="valid"' in detail.text and 'sandbox=""' in detail.text
    view = client.get(f'/reports/{report["report_id"]}/view/report.html')
    assert view.status_code == 200 and view.headers['content-security-policy'].startswith('sandbox;')
    assert view.headers['x-content-type-options'] == 'nosniff'
    download = client.get(f'/reports/{report["report_id"]}/files/report.json')
    assert download.status_code == 200 and 'attachment' in download.headers['content-disposition']
    assert json.loads(download.content)['report_uid'] == report['report_uid']
    assert client.get(f'/reports/{report["report_id"]}/files/../../etc/passwd').status_code in (404, 422)
    assert client.get(f'/reports/{report["report_id"]}/files/unlisted.txt').status_code == 404
    manifest = client.get(f'/reports/{report["report_id"]}/manifest')
    assert manifest.status_code == 200 and json.loads(manifest.content)['report_uid'] == report['report_uid']
    (Path(report['output_dir']) / 'report.json').write_text('{}', encoding='utf-8')
    assert client.get(f'/reports/{report["report_id"]}/files/report.json').status_code == 409
    assert client.get(f'/api/reports/{report["report_id"]}/verify').json()['valid'] is False
    tampered = client.get(f'/reports/{report["report_id"]}')
    assert 'data-verification="invalid"' in tampered.text and 'data-preview-blocked' in tampered.text
    assert client.get('/api/reports/999999').status_code == 404
    form = client.post('/reports/create', data={'type': 'timeline', 'from': '2026-03-01', 'to': '2026-03-03',
                                                'formats': ['csv'], 'title': 'Marzec'}, follow_redirects=False)
    assert form.status_code == 303 and form.headers['location'].startswith('/reports/')
    bad_form = client.post('/reports/create', data={'type': 'timeline', 'from': '2026-03-01T10:00'})
    assert bad_form.status_code == 422 and 'data-report-error' in bad_form.text
    listing = client.get('/api/reports').json()
    assert listing['output_enabled'] and len(listing['reports']) >= 2
    monkeypatch.delenv('EW_REPORTS_DIR')
    disabled = client.post('/api/reports', json={'type': 'timeline'})
    assert disabled.status_code == 403 and 'EW_REPORTS_DIR' in disabled.json()['detail']
    assert 'data-output-disabled' in client.get('/reports').text


def test_read_api_lists_reports_without_output_paths(env, client):
    from server.app.integrations import tokens
    token = tokens.create_token('reports-read', ('read',), client_id='reports-read')
    env.report('timeline', {}, env.root / 'read-api')
    response = client.get('/api/read/v1/reports', headers={'Authorization': f'Bearer {token["token"]}'})
    body = response.json()
    assert response.status_code == 200 and body['available'] is True and body['items']
    assert all('output_dir' not in item for item in body['items']) and body['output_location_included'] is False
    assert client.get('/api/read/v1/reports').status_code == 401


def test_extension_tables_in_metadata_export(env, tmp_path):
    env.report('timeline', {}, env.root / 'package-report')
    package = packages.export_metadata_package()
    section = package['extension_tables']
    assert section['schema'] == 'ageds.metadata-extension-tables/v1'
    assert section['undeclared_tables'] == [] and 'api_tokens' not in section['tables']
    assert section['excluded_tables'] == {'api_tokens': packages.EXCLUDED_EXTENSION_TABLES['api_tokens']}
    assert section['tables']['reports'] and section['tables']['event_times']
    assert all('token_id' not in row for row in section['tables']['annotation_proposals'])
    for name, rows in section['tables'].items():
        assert section['table_sha256'][name] == packages._sha(rows)
    raw = packages.canonical_json(package).decode('utf-8')
    with db.session() as conn:
        secrets = conn.execute('SELECT token_key, salt, secret_sha256 FROM api_tokens').fetchall()
    assert secrets
    for token_key, salt, digest in secrets:
        # token_key is a public identifier (also in the core audit_log); salt and digest are credentials.
        assert salt not in raw and digest not in raw
    assert env.token['token'] not in raw and env.token['token'].split('.', 1)[1] not in raw
    assert packages.validate_metadata_package(package)['valid']
    legacy = packages.export_metadata_package(include_extension_tables=False)
    assert 'extension_tables' not in legacy
    assert legacy['integrity']['table_sha256'] == package['integrity']['table_sha256']  # core format unchanged
    assert packages.validate_metadata_package(legacy)['valid']
    tampered = json.loads(raw)
    tampered['extension_tables']['tables']['reports'][0]['params_json'] = '{}'
    codes = {e['code'] for e in packages.validate_metadata_package(tampered)['errors']}
    assert {'extension_table_digest_mismatch', 'payload_digest_mismatch'} <= codes
    injected = json.loads(raw)
    injected['extension_tables']['tables']['api_tokens'] = []
    injected['extension_tables']['declared'].append('api_tokens')
    injected['extension_tables']['table_sha256']['api_tokens'] = packages._sha([])
    assert 'excluded_extension_table_present' in {e['code'] for e in packages.validate_metadata_package(injected)['errors']}
    leaked = json.loads(raw)
    leaked['extension_tables']['tables']['collections'] = [{'id': 1, 'salt': 'x'}]
    leaked['extension_tables']['table_sha256']['collections'] = packages._sha(leaked['extension_tables']['tables']['collections'])
    assert 'excluded_extension_column_present' in {e['code'] for e in packages.validate_metadata_package(leaked)['errors']}
    # The inert archive keeps the section byte-exactly (envelope), core tables as rows.
    archive_path, exported = tmp_path / 'meta.sqlite', tmp_path / 'roundtrip.json'
    archive.import_metadata_archive(package, archive_path)
    archive.export_metadata_archive(archive_path, exported)
    assert exported.read_bytes() == packages.canonical_json(package)


def test_undeclared_table_is_named_not_exported(env):
    with db.session() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS zz_new_area_table (id INTEGER PRIMARY KEY, secret_note TEXT)')
        conn.execute("INSERT INTO zz_new_area_table(secret_note) VALUES ('never exported silently')")
    try:
        package = packages.export_metadata_package()
        assert package['extension_tables']['undeclared_tables'] == ['zz_new_area_table']
        assert 'never exported silently' not in packages.canonical_json(package).decode('utf-8')
    finally:
        with db.session() as conn:
            conn.execute('DROP TABLE zz_new_area_table')


def test_every_migrated_table_is_classified():
    """A new table must be declared, excluded with a reason, or be internal (tripwire for integration)."""
    import sqlite3
    from server.app.db import ADDITIVE_SCHEMA, SCHEMA
    from server.app.schema import extension_migrations
    connection = sqlite3.connect(':memory:')
    connection.executescript(SCHEMA)
    connection.executescript(ADDITIVE_SCHEMA)
    for _, _, sql in extension_migrations():
        try:
            connection.executescript(sql)
        except sqlite3.OperationalError as exc:  # ALTER of a column added by another statement order
            if 'duplicate column' not in str(exc):
                raise
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    known = set(packages.TABLES) | set(packages.EXTENSION_TABLES) | set(packages.EXCLUDED_EXTENSION_TABLES)
    unclassified = sorted(t for t in tables if t not in known and not t.startswith(packages.INTERNAL_TABLE_PREFIXES))
    assert unclassified == [], ('declare these tables in packages.EXTENSION_TABLES or exclude them with a reason in '
                                f'packages.EXCLUDED_EXTENSION_TABLES: {unclassified}')

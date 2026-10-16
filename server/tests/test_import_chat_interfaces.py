"""CLI and HTTP entry points of the chat importers (synthetic corpus only)."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import cli
from server.app import db as dbmod, evidence
from server.app.config import Settings
from server.tests.corpus import chat_exports


@pytest.fixture(scope='module')
def corpus(tmp_path_factory):
    return chat_exports.build(tmp_path_factory.mktemp('chat_corpus_interfaces'))


@pytest.fixture
def store(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, db_path=tmp_path / 'evidence.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', settings)
    monkeypatch.setattr(evidence, 'settings', settings)
    return tmp_path


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def count(sql):
    with dbmod.session() as db:
        return db.execute(sql).fetchone()[0]


def test_cli_import_and_dedup_report(corpus, store, capsys):
    for label in ('e1', 'e2', 'e3'):
        code, out, err = run(capsys, 'import-chatgpt', corpus['chatgpt_exports'][label], '--export-label', label)
        assert code == 0, err
        result = json.loads(out)
        assert result['command'] == 'import-chatgpt' and result['source_bytes_written'] is False
        assert result['export']['label'] == label and result['coverage'] == 'complete'
    code, out, _ = run(capsys, 'import-claude', corpus['claude']['path'], '--export-label', 'k1')
    assert code == 0 and json.loads(out)['namespace'] == 'claude'
    code, out, _ = run(capsys, 'chat-dedup-report', '--namespace', 'chatgpt', '--changes-only')
    report = json.loads(out)
    assert code == 0 and report['command'] == 'chat-dedup-report'
    [interpretation] = report['namespaces'][0]['interpretations']
    assert interpretation['summary']['records_content_variants'] == 1
    target = store / 'reports' / 'dedup.json'
    code, out, _ = run(capsys, 'chat-dedup-report', '--output', str(target))
    assert code == 0 and json.loads(target.read_text())['schema'] == 'ageds.logical_dedup_report.v1'
    assert sorted(json.loads(out)['namespaces']) == ['chatgpt', 'claude']
    code, _, err = run(capsys, 'chat-dedup-report', '--output', str(target))
    assert code == 2 and json.loads(err)['error'] == 'FileExistsError'


def test_cli_partial_coverage_and_errors(corpus, store, capsys):
    code, out, err = run(capsys, 'import-chatgpt', corpus['malformed']['path'], '--max-conversation-bytes', '100000')
    assert code == 0 and json.loads(out)['coverage'] == 'partial'
    assert json.loads(err)['warning'] == 'partial_coverage'
    code, _, err = run(capsys, 'import-chatgpt', str(store / 'does-not-exist'))
    assert code == 2 and json.loads(err)['error'] == 'FileNotFoundError'
    empty = store / 'empty-dir'
    empty.mkdir()
    code, _, err = run(capsys, 'import-claude', str(empty))
    assert code == 2 and 'no claude conversation files' in json.loads(err)['message']


def test_http_upload_is_idempotent_and_reports(corpus, store):
    from server.app.main import app
    with TestClient(app) as client:
        payload = (Path(corpus['chatgpt_exports']['e1']) / 'conversations.json').read_bytes()
        first = client.post('/api/imports/chatgpt', files=[('files', ('conversations.json', payload, 'application/json'))],
                            data={'export_label': 'upload-e1'})
        assert first.status_code == 200, first.text
        assert first.json()['files'][0]['locator'] == 'upload:chatgpt:conversations.json'
        events = count('SELECT COUNT(*) FROM events')
        again = client.post('/api/imports/chatgpt', files=[('files', ('conversations.json', payload, 'application/json'))],
                            data={'export_label': 'upload-e1'})
        assert again.json() == first.json() and count('SELECT COUNT(*) FROM events') == events
        edge = Path(corpus['edge']['path'])
        uploads = [('files', (name, (edge / name).read_bytes(), 'application/octet-stream'))
                   for name in ('conversations.json', 'file-ATTACH1-notes.txt')]
        response = client.post('/api/imports/chatgpt', files=uploads, data={'export_label': 'upload-edge'})
        assert response.status_code == 200 and response.json()['attachment_candidates'] == 1
        assert count("SELECT COUNT(*) FROM chat_attachment_refs WHERE file_id='file-ATTACH1' AND resolution='resolved'") == 1
        claude = (Path(corpus['claude']['path']) / 'conversations.json').read_bytes()
        response = client.post('/api/imports/claude', files=[('files', ('conversations.json', claude, 'application/json'))])
        assert response.status_code == 200 and response.json()['export']['label_origin'] == 'derived_from_file_hashes'
        report = client.get('/api/imports/chat/dedup-report', params={'namespace': 'chatgpt', 'changes_only': 'true'})
        assert report.status_code == 200 and report.json()['namespaces'][0]['namespace'] == 'chatgpt'
        assert client.get('/api/imports/chat/dedup-report', params={'limit': 0}).status_code == 422
        bad = client.post('/api/imports/chatgpt', files=[('files', ('notes.txt', b'x', 'text/plain'))])
        assert bad.status_code == 422 and 'no chatgpt conversation files' in bad.json()['detail']
        duplicate = client.post('/api/imports/chatgpt', files=[('files', ('conversations.json', payload, 'application/json'))] * 2)
        assert duplicate.status_code == 422

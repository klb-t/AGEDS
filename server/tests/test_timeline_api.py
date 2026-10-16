"""HTTP and CLI contracts of the timeline area (synthetic temporary databases only)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import db, evidence, main, timeinfo

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(tmp_path):
    settings = replace(db.settings, data_dir=tmp_path, db_path=tmp_path / 'db.sqlite', store_dir=tmp_path / 'store')
    patches = [patch.object(module, 'settings', settings) for module in (db, evidence, main)]
    for p in patches:
        p.start()
    try:
        with TestClient(main.app) as test_client:
            test_client.source = evidence.ensure_source('synthetic', 'api tests <b>label</b>')
            yield test_client
    finally:
        for p in reversed(patches):
            p.stop()


def add(client, event_type='sms', **kwargs):
    kwargs.setdefault('external_id', f"api:{kwargs.get('ts_start')}:{kwargs.get('body')}:{event_type}")
    return evidence.add_event(source_id=client.source, artifact_id=None, event_type=event_type, **kwargs)


def seed(client):
    ids = {
        'sms': add(client, ts_start='2024-01-01T10:00:00+00:00', direction='incoming', phone_or_address='+48 000 000 101',
                   body='<script>window.x=1</script>', metadata={'timestamp': {'raw': '1704103200000'}}),
        'wa': add(client, 'whatsapp_message', ts_start='2024-01-01T11:00:00', thread_id='czat', body='lokalnie'),
        'undated': add(client, body='bez czasu'),
    }
    chat = add(client, 'chat_message', thread_id='conv-1', body='pytanie', metadata={'chatbot': 'claude'})
    with db.session() as connection:
        timeinfo.record_event_times(connection, chat, [timeinfo.from_epoch('created', 1704105000, 'synthetic:ct')])
    ids['chat'] = chat
    return ids


def test_legacy_endpoint_keeps_shape_and_documents_order(client):
    ids = seed(client)
    response = client.get('/api/timeline', params={'limit': 10})
    assert response.status_code == 200
    rows = response.json()
    assert [r['id'] for r in rows] == [ids['wa'], ids['chat'], ids['sms'], ids['undated']]
    assert rows[0]['ts_start'] == '2024-01-01T11:00:00'
    assert response.headers['x-ageds-timeline-rule'] == 'display_time@1'
    filtered = client.get('/api/timeline', params={'phone': '+48 000 000 101', 'event_type': 'sms'}).json()
    assert [r['id'] for r in filtered] == [ids['sms']]
    assert client.get('/api/timeline', params={'limit': 0}).status_code == 422


def test_window_density_undated_items_and_errors(client):
    ids = seed(client)
    page = client.get('/api/timeline/window', params={'from': '2024-01-01', 'to': '2024-01-01', 'group': 'chatbot', 'limit': 2}).json()
    assert [i['event_id'] for i in page['items']] == [ids['sms'], ids['chat']]
    assert page['has_more'] and page['next_cursor'] and page['complete'] is False
    assert page['rule']['rule_id'] == 'display_time' and page['undated_count'] == 1
    assert {lane['key'] for lane in page['lanes']['lanes']} == {'claude', None}
    assert page['items'][0]['candidates'][0]['selected'] in (True, False)
    second = client.get('/api/timeline/window', params={'from': '2024-01-01', 'to': '2024-01-01', 'group': 'chatbot', 'limit': 2,
                                                        'cursor': page['next_cursor']}).json()
    assert [i['event_id'] for i in second['items']] == [ids['wa']] and 'lanes' not in second
    multi = client.get('/api/timeline/window', params=[('event_type', 'sms'), ('event_type', 'chat_message'), ('candidates', 'false')]).json()
    assert [i['event_id'] for i in multi['items']] == [ids['sms'], ids['chat']] and 'candidates' not in multi['items'][0]
    zoned = client.get('/api/timeline/window', params={'assume_zone': 'Europe/Warsaw', 'assumption_rule': 'api-1',
                                                       'event_type': 'whatsapp_message'}).json()
    assert zoned['items'][0]['time']['display'] == '2024-01-01T10:00:00Z' and zoned['params']['assumption_rule'] == 'api-1'
    density = client.get('/api/timeline/density', params={'bucket': 'hour', 'group': 'type'}).json()
    assert density['bucket'] == 'hour' and density['total'] == 3 and density['undated_count'] == 1
    assert client.get('/api/timeline/density', params={'bucket': 'minute', 'from': '2000', 'to': '2024'}).status_code == 413
    undated = client.get('/api/timeline/undated').json()
    assert [i['event_id'] for i in undated['items']] == [ids['undated']]
    detail = client.get(f"/api/timeline/items/event:{ids['sms']}").json()
    assert detail['text'] == '<script>window.x=1</script>' and detail['time']['basis'] == 'utc_epoch'
    assert client.get('/api/timeline/items/event:999999').status_code == 404
    assert client.get('/api/timeline/items/bogus').status_code == 422
    for params in ({'from': 'yesterday'}, {'from': '2024-02', 'to': '2024-01'}, {'cursor': 'xyz'}, {'kind': 'other'},
                   {'group': 'nope'}, {'limit': 5000}, {'assume_zone': 'Europe/Warsaw'}, {'q': '"open'},
                   {'rule': 'display_time@7'}):
        assert client.get('/api/timeline/window', params=params).status_code == 422, params
    rules = client.get('/api/timeline/rules').json()
    assert {r['rule_id'] for r in rules} >= {'display_time', 'recording_call_log', 'email_chat', 'same_conversation'}
    facets = client.get('/api/timeline/facets').json()
    assert facets['undated_items'] == 1 and facets['sources'][0]['label'].startswith('api tests')


def test_export_endpoint(client):
    seed(client)
    csv_response = client.get('/api/timeline/export', params={'format': 'csv', 'from': '2024-01-01', 'to': '2024-01-02'})
    assert csv_response.status_code == 200
    assert csv_response.headers['content-disposition'] == 'attachment; filename="ageds-timeline-20240101-20240102.csv"'
    assert csv_response.headers['x-ageds-timeline-rows'] == '3' and csv_response.headers['x-ageds-timeline-complete'] == 'true'
    assert csv_response.text.splitlines()[0].startswith('key,kind,display_utc,basis')
    json_response = client.get('/api/timeline/export', params={'format': 'json', 'max_rows': 1})
    body = json_response.json()
    assert body['row_count'] == 1 and body['complete'] is False and 'not a signed record' in body['scope']
    assert client.get('/api/timeline/export', params={'format': 'xlsx'}).status_code == 422


def test_correlation_endpoints(client, tmp_path):
    call = add(client, 'call', ts_start='2024-06-03T08:15:00+00:00', ts_end='2024-06-03T08:16:35+00:00',
               phone_or_address='+48 000 000 123', metadata={'timestamp': {'raw': '1717402500000'}, 'duration': {'seconds': 95}})
    recording_source = evidence.ensure_source('synthetic', 'recordings')
    path = tmp_path / 'r.bin'
    path.write_bytes(b'not a wav')
    artifact = evidence.ingest_file(path, source_id=recording_source, source_locator='synthetic:r', mime_type='audio/mpeg')
    event = evidence.add_event(source_id=recording_source, artifact_id=artifact, event_type='recording', external_id='r1',
                               metadata={'filename_number': '000000123', 'duration_seconds': 94.5})
    with db.session() as connection:
        timeinfo.record_event_times(connection, event, [timeinfo.from_iso('filename_derived', '2024-06-03 10:15:02', 'synthetic:name')])
    run = client.post('/api/timeline/correlations/runs', json={'rule': 'recording_call_log', 'actor': 'api-test'})
    assert run.status_code == 200, run.text
    run = run.json()
    assert run['hypothesis_count'] == 1 and run['actor'] == 'api-test'
    assert client.post('/api/timeline/correlations/runs', json={'rule': 'recording_call_log', 'params': {'bogus': 1}}).status_code == 422
    assert client.post('/api/timeline/correlations/runs', json={'rule': 'unknown_rule'}).status_code == 422
    listing = client.get('/api/timeline/correlations', params={'run_id': run['id'], 'status': 'proposed'}).json()
    [h] = listing['hypotheses']
    assert h['right_id'] == call and h['criteria']['duration']['recording_duration_source'].endswith('metadata.duration_seconds')
    assert client.get('/api/timeline/correlations', params={'status': 'maybe'}).status_code == 422
    accepted = client.post(f"/api/timeline/correlations/{h['id']}/review", json={'action': 'accept', 'reason': 'ok'})
    assert accepted.status_code == 200 and accepted.json()['status'] == 'accepted'
    again = client.post(f"/api/timeline/correlations/{h['id']}/review", json={'action': 'accept', 'reason': 'ok'})
    assert again.status_code == 409
    assert client.post('/api/timeline/correlations/999999/review', json={'action': 'accept', 'reason': 'x'}).status_code == 404
    assert client.post(f"/api/timeline/correlations/{h['id']}/review", json={'action': 'accept', 'reason': ''}).status_code == 422
    detail = client.get(f"/api/timeline/correlations/{h['id']}").json()
    assert [s['status'] for s in detail['history']] == ['proposed', 'accepted'] and detail['probability'] is None
    item = client.get(f'/api/timeline/items/event:{call}').json()
    assert item['hypotheses'][0]['status'] == 'accepted'
    runs = client.get('/api/timeline/correlations/runs').json()
    assert runs['runs'][0]['id'] == run['id']
    assert client.get('/api/timeline/correlations/runs/999').status_code == 404


def test_timeline_page_and_navigation(client):
    response = client.get('/timeline')
    assert response.status_code == 200
    html = response.text
    assert 'data-timeline' in html and 'data-reports-available="false"' in html
    assert '/static/timeline.mjs' in html and '<a href="/timeline">Oś czasu</a>' in html
    assert client.get('/static/timeline-math.mjs').status_code == 200
    assert '<a href="/timeline">Oś czasu</a>' in client.get('/').text


def cli(env, *args, cwd):
    return subprocess.run([sys.executable, '-m', 'server.app.cli', *map(str, args)], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def test_cli_window_correlate_and_review(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONDONTWRITEBYTECODE='1', EW_DATA_DIR=str(tmp_path / 'data'),
               EW_DB_PATH=str(tmp_path / 'data' / 'db.sqlite'), EW_STORE_DIR=str(tmp_path / 'data' / 'store'))
    work = tmp_path / 'work'
    work.mkdir()
    missing = cli(env, 'timeline-rebuild', cwd=work)
    assert missing.returncode == 2 and 'database not found' in missing.stderr
    setup = ('from server.app import db, evidence\n'
             'db.init_db()\n'
             's = evidence.ensure_source("synthetic", "cli")\n'
             'evidence.add_event(source_id=s, artifact_id=None, event_type="sms", ts_start="2024-01-01T10:00:00+00:00", '
             'body="=1+1", external_id="1")\n'
             'evidence.add_event(source_id=s, artifact_id=None, event_type="email", ts_start="2024-01-01T09:00:00+00:00", '
             'body="alpha beta gamma delta epsilon zeta eta", external_id="2")\n'
             'evidence.add_event(source_id=s, artifact_id=None, event_type="chat_message", ts_start="2024-01-01T09:30:00+00:00", '
             'body="quoted: alpha beta gamma delta epsilon zeta eta", external_id="3")\n')
    subprocess.run([sys.executable, '-c', setup], cwd=work, env=env, check=True, timeout=60)
    output = tmp_path / 'out' / 'window.json'
    result = cli(env, 'timeline-window', output, '--from', '2024-01-01', '--to', '2024-01-01', '--event-type', 'sms,email', cwd=work)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status['rows'] == 2 and status['format'] == 'json' and status['source_bytes_written'] is False
    assert [i['event_type'] for i in json.loads(output.read_text())['items']] == ['email', 'sms']
    assert cli(env, 'timeline-window', output, cwd=work).returncode == 2  # never overwrites
    csv_output = tmp_path / 'out' / 'window.csv'
    assert cli(env, 'timeline-window', csv_output, '--q', 'alpha', cwd=work).returncode == 0
    assert csv_output.read_text().count('\n') == 3
    rebuild = json.loads(cli(env, 'timeline-rebuild', '--full', cwd=work).stdout)
    assert rebuild['build']['mode'] == 'full'
    bad_zone = cli(env, 'timeline-rebuild', '--assume-zone', 'Europe/Warsaw', cwd=work)
    assert bad_zone.returncode == 2 and 'assumption_rule' in bad_zone.stderr
    run = cli(env, 'correlate', '--rule', 'email_chat', '--params', '{"ngram_words": 4}', cwd=work)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)['hypotheses'] == 1
    assert cli(env, 'correlate', '--rule', 'email_chat', '--params', '[1]', cwd=work).returncode == 2
    review = cli(env, 'correlation-review', '1', 'reject', '--reason', 'synthetic', cwd=work)
    assert review.returncode == 0, review.stderr
    assert [h['status'] for h in json.loads(review.stdout)['history']] == ['proposed', 'rejected']
    assert cli(env, 'correlation-review', '1', 'reject', '--reason', 'again', cwd=work).returncode == 2
    assert cli(env, 'correlation-review', '99', 'accept', '--reason', 'x', cwd=work).returncode == 2
    assert list(work.iterdir()) == []

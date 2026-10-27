"""Review 2 (AGEDS-TASK-20261016-R2): Host allowlist against DNS rebinding (#5), token coverage of HTML
pages and report files under ``EW_REQUIRE_TOKEN`` (#6) and rejected-token bookkeeping without a write
lock per request (P2). Temporary synthetic databases only."""
from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from server.app import main
from server.app.api import integrations
from server.tests.test_review_fixes_support import env  # noqa: F401 (fixture)

REBIND = 'http://rebind.attacker.example:8080'


# --------------------------------------------------------------------- finding 5

def test_review2_5_dns_rebinding_host_is_refused_for_reads_and_writes(env):  # noqa: F811
    with TestClient(main.app, base_url=REBIND) as http:
        same_origin = {'Origin': REBIND, 'Sec-Fetch-Site': 'same-origin'}
        write = http.post('/api/workbench/folders', json={'name': 'planted'}, headers=same_origin)
        assert write.status_code == 400 and write.json()['detail']['code'] == 'untrusted_host'  # pre-fix: 201
        read = http.get('/api/timeline')
        assert read.status_code == 400  # a rebinding page could otherwise read every GET response
        assert http.get('/').status_code == 400
    assert env.one('SELECT COUNT(*) FROM workbench_folders') == 0


@pytest.mark.parametrize('base', ['http://localhost:8080', 'http://127.0.0.1:8080', 'http://10.0.2.2:8080',
                                  'http://192.168.1.20', 'http://localhost'])
def test_review2_5_loopback_and_ip_literal_hosts_work_by_default(env, monkeypatch, base):  # noqa: F811
    monkeypatch.delenv('EW_TRUSTED_HOSTS', raising=False)
    with TestClient(main.app, base_url=base) as http:
        assert http.get('/api/timeline').status_code == 200
        created = http.post('/api/workbench/folders', json={'name': 'same origin'},
                            headers={'Origin': base, 'Sec-Fetch-Site': 'same-origin'})
        assert created.status_code == 201, created.text
        assert http.post('/api/workbench/folders', json={'name': 'cross'},
                         headers={'Origin': 'http://evil.example'}).status_code == 403


def test_review2_5_named_lan_hosts_need_explicit_configuration(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv('EW_TRUSTED_HOSTS', 'evidence.lan, *.example.lan')
    for base, expected in (('http://evidence.lan:8080', 200), ('http://pc.example.lan', 200),
                           ('http://example.lan.evil.example', 400), ('http://other.lan', 400)):
        with TestClient(main.app, base_url=base) as http:
            assert http.get('/api/timeline').status_code == expected, base
    monkeypatch.setenv('EW_TRUSTED_HOSTS', '')
    monkeypatch.setenv('EW_TRUSTED_ORIGINS', 'https://evidence.example.org')
    with TestClient(main.app, base_url='https://evidence.example.org') as http:  # reverse proxy keeps the public Host
        assert http.get('/api/timeline').status_code == 200
    with TestClient(main.app, base_url='http://testserver') as http:
        assert http.get('/api/timeline').status_code == 400  # the test default is only the conftest's setting


@pytest.mark.parametrize('host', ['a b', 'x@y', '[::1', 'host:port', 'evil.example/', '1.2.3.4:'])
def test_review2_5_malformed_host_headers_are_refused(host):
    assert integrations.host_allowed(host) is False


@pytest.mark.parametrize('host', ['[::1]:8080', '[::1]', '127.0.0.1', 'LOCALHOST:8080', '[fe80::1]:80'])
def test_review2_5_ip_literals_are_allowed_without_configuration(monkeypatch, host):
    monkeypatch.delenv('EW_TRUSTED_HOSTS', raising=False)
    assert integrations.host_allowed(host) is True


def test_review2_5_origin_is_checked_against_the_allowlist_not_against_host(monkeypatch):
    monkeypatch.delenv('EW_TRUSTED_HOSTS', raising=False)
    monkeypatch.delenv('EW_TRUSTED_ORIGINS', raising=False)
    assert integrations._trusted('http://rebind.attacker.example', 'rebind.attacker.example') is False  # pre-fix True
    assert integrations._trusted('http://127.0.0.1:8080', '127.0.0.1:8080') is True
    assert integrations._trusted('http://127.0.0.1:9090', '127.0.0.1:8080') is False
    refusal = integrations.csrf_refusal('POST', {'host': 'rebind.attacker.example', 'origin': 'http://rebind.attacker.example'})
    assert refusal is not None


# --------------------------------------------------------------------- finding 6

def _report(env) -> int:
    from server.app.reports.service import create_report
    return create_report('timeline', {}, env.root / 'reports')['report_id']


def test_review2_6_require_token_protects_pages_and_report_files(env, monkeypatch):  # noqa: F811
    from server.app import db as dbmod
    from server.app.integrations import tokens
    with dbmod.session() as db:
        db.execute("INSERT INTO events(event_type, body, ts_start) VALUES ('sms', 'secret body text zebra', "
                   "'2024-01-01T10:00:00+00:00')")
    report_id = _report(env)
    token = tokens.create_token('operator browser', ['read'])['token']
    monkeypatch.setenv('EW_REQUIRE_TOKEN', '1')
    protected = ['/workbench?q=zebra', '/', '/integrations', '/timeline', '/hypotheses', '/analysis/budget',
                 '/reports', f'/reports/{report_id}', f'/reports/{report_id}/files/report.json',
                 f'/reports/{report_id}/view/report.html', f'/reports/{report_id}/manifest', '/workbench/folders',
                 '/docs', '/openapi.json', '/export/events.csv', '/api/timeline']
    with TestClient(main.app) as http:
        for path in protected:
            response = http.get(path)
            assert response.status_code == 401, (path, response.status_code)  # pre-fix: 200 for every HTML page
            assert 'secret body text' not in response.text, path
        login = http.get('/workbench?q=zebra')
        assert 'action="/integrations/session"' in login.text and 'value="/workbench?q=zebra"' in login.text
        assert http.get('/static/app.css').status_code == 200  # assets stay open for the login form
        assert http.get('/integrations/session').status_code == 200
        bearer = {'Authorization': f'Bearer {token}'}
        assert http.get(f'/reports/{report_id}/manifest', headers=bearer).status_code == 200
        session = http.post('/integrations/session', data={'token': token, 'next': '/workbench?q=zebra'},
                            follow_redirects=False)
        assert session.status_code == 303 and session.headers['location'] == '/workbench?q=zebra'
        page = http.get('/workbench?q=zebra')
        assert page.status_code == 200 and 'secret body text' in page.text
        assert http.get(f'/reports/{report_id}/files/report.json').status_code == 200


@pytest.mark.parametrize('target', ['https://evil.example/x', '//evil.example/x', '/\\evil.example', 'javascript:alert(1)'])
def test_review2_6_login_redirect_stays_on_this_server(env, monkeypatch, target):  # noqa: F811
    from server.app.integrations import tokens
    token = tokens.create_token('operator browser', ['read'])['token']
    monkeypatch.setenv('EW_REQUIRE_TOKEN', '1')
    with TestClient(main.app) as http:
        response = http.post('/integrations/session', data={'token': token, 'next': target}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/integrations'


# --------------------------------------------------------------------- P2

def test_review2_p2_repeated_rejections_do_not_take_the_write_lock(env, monkeypatch):  # noqa: F811
    from server.app.integrations import tokens
    monkeypatch.setenv('EW_REQUIRE_TOKEN', '1')
    bogus = {'Authorization': 'Bearer ageds_aaaaaaaaaaaa.junk'}
    with TestClient(main.app) as http:
        assert http.get('/api/timeline', headers=bogus).status_code == 401  # first one per hour is written + audited
        blocker = sqlite3.connect(str(env.settings.db_path), timeout=0.1)
        blocker.execute('BEGIN IMMEDIATE')  # a long write elsewhere
        try:
            started = time.monotonic()
            for _ in range(3):
                response = http.get('/api/timeline', headers=bogus)
                assert response.status_code == 401, response.text  # pre-fix: waited 5 s each, then 503
            assert time.monotonic() - started < 3
        finally:
            blocker.rollback()
            blocker.close()
    tokens.flush_rejection_counts()
    assert env.one('SELECT SUM(count) FROM api_token_rejections') == 4
    assert env.one("SELECT COUNT(*) FROM audit_log WHERE action='token_rejected'") == 1

"""Read-only Gmail connector on a fake transport (generated messages; no network, no account)."""
from __future__ import annotations

import base64
import json
import os
import stat
import urllib.parse

import pytest

from server.app import db as dbmod
from server.app.connectors import gmail
from server.tests.test_import_comm import env, rows  # noqa: F401  (fixture)

RAW = (b'From: sender@example.org\r\nTo: owner@example.net\r\nSubject: Afspraak\r\nDate: Tue, 14 Oct 2026 10:42:00 +0200\r\n'
       b'Message-ID: <api-1@example.org>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
       b'--b\r\nContent-Type: text/plain\r\n\r\nUw afspraak.\r\n--b\r\nContent-Type: application/pdf\r\n'
       b'Content-Disposition: attachment; filename="brief.pdf"\r\nContent-Transfer-Encoding: base64\r\n\r\n'
       + base64.b64encode(b'%PDF-1.4 synthetic') + b'\r\n--b--\r\n')


class FakeGoogle:
    def __init__(self, scope=gmail.SCOPE):
        self.scope, self.calls = scope, []

    def __call__(self, method, url, data):
        self.calls.append((method, url))
        if url == gmail.TOKEN_URL:
            if data.get('grant_type') == 'authorization_code':
                return {'access_token': 'a', 'refresh_token': 'r', 'scope': self.scope}
            return {'access_token': 'a'}
        assert method == 'GET', 'the connector must only read'
        path = urllib.parse.urlparse(url).path.rsplit('/me/', 1)[1]
        if path == 'profile':
            return {'emailAddress': 'owner@example.net'}
        if path == 'messages':
            spam = 'includeSpamTrash=true' in url
            return {'messages': [{'id': 'm1'}, {'id': 'm2'}] + ([{'id': 'spam1'}] if spam else [])}
        if path.startswith('threads/'):
            return {'messages': [{'id': 'm1'}]}
        message_id = path.split('/')[1]
        raw = RAW if message_id == 'm1' else RAW.replace(b'api-1', b'api-2').replace(b'Afspraak', b'Tweede')
        return {'id': message_id, 'threadId': 't1', 'labelIds': ['INBOX', 'IMPORTANT'], 'internalDate': '1781419320000',
                'historyId': '99', 'sizeEstimate': len(raw), 'raw': base64.urlsafe_b64encode(raw).decode().rstrip('=')}


@pytest.fixture()
def config(tmp_path, monkeypatch, env):  # noqa: F811
    monkeypatch.setattr(gmail, 'CONFIG_DIR', tmp_path / 'cfg')
    monkeypatch.setenv('AGEDS_GMAIL_CLIENT', str(tmp_path / 'cfg' / 'gmail_client.json'))
    monkeypatch.setenv('AGEDS_GMAIL_TOKEN', str(tmp_path / 'cfg' / 'gmail_token.json'))
    (tmp_path / 'cfg').mkdir()
    (tmp_path / 'cfg' / 'gmail_client.json').write_text(json.dumps({'installed': {'client_id': 'cid', 'client_secret': 'cs'}}))
    return tmp_path / 'cfg'


def authorise(fake):
    url = gmail.auth_url()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url['url']).query)
    assert query['scope'] == [gmail.SCOPE] and query['code_challenge_method'] == ['S256']
    return gmail.auth_complete(f'http://127.0.0.1:8765/?state={query["state"][0]}&code=c', transport=fake)


def test_nothing_runs_before_authorisation(config):
    with pytest.raises(gmail.GmailError, match='not authorised'):
        gmail.import_selection(query='x', transport=FakeGoogle())


def test_wider_grant_is_refused_and_not_stored(config):
    with pytest.raises(gmail.GmailError, match='read-only'):
        authorise(FakeGoogle(scope=gmail.SCOPE + ' https://mail.google.com/'))
    assert not (config / 'gmail_token.json').exists()


def test_state_mismatch_is_refused_and_the_pending_verifier_is_gone(config):
    gmail.auth_url()
    with pytest.raises(gmail.GmailError, match='state'):
        gmail.auth_complete('http://127.0.0.1:8765/?state=forged&code=c', transport=FakeGoogle())
    assert not (config / 'gmail_auth_pending.json').exists()
    with pytest.raises(gmail.GmailError, match='no pending'):
        gmail.auth_complete('http://127.0.0.1:8765/?state=x&code=c', transport=FakeGoogle())


def test_dry_run_then_apply_is_idempotent_and_read_only(config):
    fake = FakeGoogle()
    stored = authorise(fake)
    assert stat.S_IMODE(os.stat(stored['stored']).st_mode) == 0o600
    dry = gmail.import_selection(query='from:example.org', transport=fake)
    assert (dry['selected'], dry['imported'], rows('SELECT * FROM artifacts')) == (2, [], [])
    assert dry['spam_trash_not_selected'] == 1  # left out by default, and said so
    wide = gmail.import_selection(query='from:example.org', transport=fake, include_spam_trash=True)
    assert wide['selected'] == 3 and 'spam_trash_not_selected' not in wide
    assert not any('format=raw' in url for _, url in fake.calls)  # no content fetched in a dry run
    first = gmail.import_selection(query='from:example.org', apply=True, transport=fake)
    assert [i['rfc822_message_id'] for i in first['imported']] == ['<api-1@example.org>', '<api-2@example.org>']
    emails = rows("SELECT subject FROM events WHERE event_type='email' ORDER BY id")
    assert [e['subject'] for e in emails] == ['Afspraak', 'Tweede']
    assert len(rows("SELECT * FROM artifacts WHERE original_name='brief.pdf'")) == 2
    fetch = rows('SELECT * FROM gmail_api_fetches ORDER BY id')[0]
    assert (json.loads(fetch['label_ids_json']), fetch['gmail_thread_id']) == (['INBOX', 'IMPORTANT'], 't1')
    locator = rows('SELECT source_locator FROM artifacts WHERE id=?', (first['imported'][0]['artifact_id'],))[0]
    assert locator['source_locator'] == 'gmail-api:owner@example.net/messages/m1'
    again = gmail.import_selection(thread_id='t1', apply=True, transport=fake)
    assert (again['imported'], [s['reason'] for s in again['skipped']]) == ([], ['already fetched with the same bytes'])
    assert {method for method, url in fake.calls if url != gmail.TOKEN_URL} == {'GET'}
    with dbmod.session() as conn, pytest.raises(Exception):
        conn.execute('DELETE FROM gmail_api_fetches')

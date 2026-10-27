"""Read-only Gmail API connector: the owner's OAuth, scope gmail.readonly, raw RFC 822 in.

Nothing here runs until the owner authorises it (docs/GMAIL_API.md). The OAuth flow is the
installed-app flow with PKCE and a loopback redirect: the owner opens the printed URL, consents,
and pastes the address the browser was redirected to (the page itself need not load, so this
works when AGEDS runs on a remote machine). The token file lives outside the repository with
mode 0600 and is never exported.

Only GET requests are made. A selected query or thread is listed first (dry run); with apply,
each message is fetched with ``format=raw`` and acquired through the EML importer as a
temporary copy, so parsing, attachments and dates follow the same rules as an exported .eml.
The API's labels, thread id, internalDate and historyId are recorded separately as what the API
said at fetch time. A Gmail message already fetched with the same raw SHA-256 is not fetched
into a new acquisition again. Gmail's raw form is Gmail's serialisation of the message: it can
differ byte-wise from the copy in a Takeout mbox, and both stay as separate acquisitions.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..db import session

SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
ALLOWED_SCOPES = {SCOPE, 'openid', 'https://www.googleapis.com/auth/userinfo.email', 'email'}
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
API = 'https://gmail.googleapis.com/gmail/v1/users/me'
CONFIG_DIR = Path(os.environ.get('AGEDS_CONFIG_DIR', Path.home() / '.config' / 'ageds'))
MAX_MESSAGES = 500


class GmailError(RuntimeError):
    """The connector cannot proceed; nothing was acquired for the failing message."""


def _paths():
    return {'client': Path(os.environ.get('AGEDS_GMAIL_CLIENT', CONFIG_DIR / 'gmail_client.json')),
            'token': Path(os.environ.get('AGEDS_GMAIL_TOKEN', CONFIG_DIR / 'gmail_token.json')),
            'pending': CONFIG_DIR / 'gmail_auth_pending.json'}


def _write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle)
    os.chmod(path, 0o600)


def _client() -> dict:
    path = _paths()['client']
    if not path.is_file():
        raise GmailError(f'OAuth client file missing: {path} (docs/GMAIL_API.md, step 2)')
    data = json.loads(path.read_text())
    client = data.get('installed') or {}
    if not client.get('client_id') or not client.get('client_secret'):
        raise GmailError('the OAuth client must be of type "Desktop app" (an "installed" client)')
    return client


def _post_form(url: str, fields: dict, transport=None) -> dict:
    if transport is not None:
        return transport('POST', url, fields)
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method='POST',
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise GmailError(f'OAuth HTTP {exc.code}: {exc.read()[:300].decode("utf-8", "replace")}') from exc


def auth_url(port: int = 8765) -> dict:
    """Step 1: a consent URL for gmail.readonly with PKCE; the verifier stays in a private file."""
    client = _client()
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(24)
    redirect = f'http://127.0.0.1:{port}/'
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    _write_private(_paths()['pending'], {'verifier': verifier, 'state': state, 'redirect_uri': redirect,
                                         'created': time.time()})
    query = {'client_id': client['client_id'], 'redirect_uri': redirect, 'response_type': 'code', 'scope': SCOPE,
             'access_type': 'offline', 'prompt': 'consent', 'state': state, 'code_challenge': challenge,
             'code_challenge_method': 'S256'}
    return {'url': f'{AUTH_URL}?{urllib.parse.urlencode(query)}', 'scope': SCOPE, 'redirect_uri': redirect,
            'next': 'open the URL, consent, then run gmail-auth-complete --redirect-url "<address the browser shows>"'}


def auth_complete(redirect_url: str, transport=None) -> dict:
    """Step 2: exchange the code; refuse a grant wider than read-only."""
    pending_path = _paths()['pending']
    if not pending_path.is_file():
        raise GmailError('no pending authorisation; run gmail-auth-url first')
    pending = json.loads(pending_path.read_text())
    if time.time() - pending['created'] > 900:
        raise GmailError('the pending authorisation is older than 15 minutes; run gmail-auth-url again')
    params = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)
    if params.get('error'):
        raise GmailError(f'consent refused: {params["error"][0]}')
    if params.get('state', [None])[0] != pending['state']:
        raise GmailError('state does not match the pending authorisation')
    code = params.get('code', [None])[0]
    if not code:
        raise GmailError('the redirect address has no code')
    client = _client()
    token = _post_form(TOKEN_URL, {'code': code, 'client_id': client['client_id'], 'client_secret': client['client_secret'],
                                   'redirect_uri': pending['redirect_uri'], 'grant_type': 'authorization_code',
                                   'code_verifier': pending['verifier']}, transport)
    granted = set((token.get('scope') or '').split())
    if SCOPE not in granted or granted - ALLOWED_SCOPES:
        raise GmailError(f'granted scopes {sorted(granted)} are not exactly read-only Gmail; nothing was stored')
    if not token.get('refresh_token'):
        raise GmailError('no refresh token returned; revoke the app at myaccount.google.com and authorise again')
    _write_private(_paths()['token'], {'refresh_token': token['refresh_token'], 'scope': token['scope'],
                                       'obtained_at': time.time()})
    pending_path.unlink()
    return {'stored': str(_paths()['token']), 'mode': '0600', 'scope': sorted(granted)}


def _access_token(transport=None) -> str:
    path = _paths()['token']
    if not path.is_file():
        raise GmailError('not authorised: run gmail-auth-url and gmail-auth-complete (docs/GMAIL_API.md)')
    stored = json.loads(path.read_text())
    if set(stored.get('scope', '').split()) - ALLOWED_SCOPES:
        raise GmailError('stored token is not read-only; refusing to use it')
    client = _client()
    token = _post_form(TOKEN_URL, {'refresh_token': stored['refresh_token'], 'client_id': client['client_id'],
                                   'client_secret': client['client_secret'], 'grant_type': 'refresh_token'}, transport)
    if not token.get('access_token'):
        raise GmailError('token refresh failed')
    return token['access_token']


class Api:
    """GET-only client; ``transport(method, url, headers)`` can replace the network in tests."""

    def __init__(self, transport=None):
        self.transport = transport
        self.token = _access_token(transport)

    def get(self, path: str, params: dict | None = None) -> dict:
        url = f'{API}/{path}' + (f'?{urllib.parse.urlencode(params, doseq=True)}' if params else '')
        headers = {'Authorization': f'Bearer {self.token}'}
        if self.transport is not None:
            return self.transport('GET', url, headers)
        request = urllib.request.Request(url, headers=headers, method='GET')
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 503) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GmailError(f'Gmail HTTP {exc.code}: {exc.read()[:300].decode("utf-8", "replace")}') from exc
        raise GmailError('Gmail request failed')


def select(api: Api, *, query: str | None = None, thread_id: str | None = None,
           max_messages: int = MAX_MESSAGES) -> tuple[list[str], bool]:
    """Message ids of a query (paged) or one thread; second value says whether the list is complete."""
    if (query is None) == (thread_id is None):
        raise GmailError('give exactly one of --query or --thread-id')
    if thread_id is not None:
        thread = api.get(f'threads/{urllib.parse.quote(thread_id)}', {'format': 'minimal'})
        ids = [m['id'] for m in thread.get('messages', [])]
        return ids[:max_messages], len(ids) <= max_messages
    ids, page = [], None
    while True:
        params = {'q': query, 'maxResults': min(500, max_messages - len(ids)), 'includeSpamTrash': 'true'}
        if page:
            params['pageToken'] = page
        listing = api.get('messages', params)
        ids += [m['id'] for m in listing.get('messages', [])]
        page = listing.get('nextPageToken')
        if not page:
            return ids, True
        if len(ids) >= max_messages:
            return ids[:max_messages], False


def import_selection(*, query: str | None = None, thread_id: str | None = None, apply: bool = False,
                     max_messages: int = MAX_MESSAGES, collection_id: int | None = None, transport=None) -> dict:
    from ..evidence import temporary_copy_acquisitions
    from ..importers.email_mbox import import_eml
    from ..importers.email_mbox import scan_ids, DEFAULT_LIMITS
    api = Api(transport)
    profile = api.get('profile')
    account = profile.get('emailAddress') or 'unknown'
    ids, complete = select(api, query=query, thread_id=thread_id, max_messages=max_messages)
    request = {'query': query, 'thread_id': thread_id, 'max_messages': max_messages, 'include_spam_trash': True}
    report = {'account': account, 'selected': len(ids), 'selection_complete': complete, 'applied': apply,
              'imported': [], 'skipped': [], 'failed': []}
    if not apply:
        report['note'] = 'dry run: message ids listed only, no message content fetched'
        return report
    for message_id in ids:
        message = api.get(f'messages/{urllib.parse.quote(message_id)}', {'format': 'raw'})
        raw = base64.urlsafe_b64decode(message['raw'] + '=' * (-len(message['raw']) % 4))
        digest = hashlib.sha256(raw).hexdigest()
        with session() as db:
            seen = db.execute('SELECT artifact_id FROM gmail_api_fetches WHERE account=? AND gmail_message_id=? AND raw_sha256=?',
                              (account, message_id, digest)).fetchone()
        if seen is not None:
            report['skipped'].append({'gmail_message_id': message_id, 'reason': 'already fetched with the same bytes',
                                      'artifact_id': seen['artifact_id']})
            continue
        rfc_id = (scan_ids(raw, DEFAULT_LIMITS) or {}).get('message_id')
        fd, temp = tempfile.mkstemp(suffix='.eml')
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
            with temporary_copy_acquisitions():
                result = import_eml(Path(temp), source_locator=f'gmail-api:{account}/messages/{message_id}',
                                    original_name=f'gmail-{message_id}.eml', collection_id=collection_id,
                                    label=f'Gmail API ({account})')
        except Exception as exc:
            report['failed'].append({'gmail_message_id': message_id, 'error': f'{type(exc).__name__}: {str(exc)[:200]}'})
            continue
        finally:
            os.unlink(temp)
        with session() as db:
            db.execute('''INSERT INTO gmail_api_fetches(account,gmail_message_id,gmail_thread_id,label_ids_json,internal_date_ms,
                          history_id,size_estimate,raw_sha256,rfc822_message_id,artifact_id,request_json)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                       (account, message_id, message.get('threadId'), json.dumps(message.get('labelIds', [])),
                        message.get('internalDate'), message.get('historyId'), message.get('sizeEstimate'), digest,
                        rfc_id, result['artifact_id'], json.dumps(request)))
        report['imported'].append({'gmail_message_id': message_id, 'artifact_id': result['artifact_id'],
                                   'rfc822_message_id': rfc_id, 'attachments': result.get('attachments')})
    with session() as db:
        db.execute("INSERT INTO audit_log(actor,action,object_kind,details_json) VALUES ('gmail-connector','gmail_api_import','connector',?)",
                   (json.dumps({**request, 'account': account, 'selected': len(ids), 'complete': complete,
                                'imported': len(report['imported']), 'skipped': len(report['skipped']),
                                'failed': len(report['failed'])}),))
    return report

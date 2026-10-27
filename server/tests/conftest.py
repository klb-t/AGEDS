"""Process-wide test identities.

Starlette's TestClient sends Host 'testserver' from client 'testclient'. Production
defaults trust neither: both Host allow-lists (EW_TRUSTED_HOSTS from the integrations
middleware, EW_ALLOWED_HOSTS from http_authz) and the legacy_open client list name them
here instead of weakening the defaults. Tests of the allow-lists override with monkeypatch.
"""
import os

os.environ.setdefault('EW_TRUSTED_HOSTS', 'testserver')
os.environ['EW_LEGACY_OPEN_CLIENTS'] = ','.join(filter(None, [os.environ.get('EW_LEGACY_OPEN_CLIENTS', ''), 'testclient']))

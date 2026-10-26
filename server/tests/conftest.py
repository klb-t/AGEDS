"""Test-only network identities: Starlette's TestClient uses Host 'testserver' and client 'testclient'.

Production defaults allow neither; the tests name them explicitly here.
"""
import os

os.environ['EW_ALLOWED_HOSTS'] = ','.join(filter(None, [os.environ.get('EW_ALLOWED_HOSTS', ''), 'testserver']))
os.environ['EW_LEGACY_OPEN_CLIENTS'] = ','.join(filter(None, [os.environ.get('EW_LEGACY_OPEN_CLIENTS', ''), 'testclient']))

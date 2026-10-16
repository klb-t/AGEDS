"""Process-wide test defaults.

FastAPI's ``TestClient`` sends ``Host: testserver``. The Host allowlist against DNS
rebinding (review 2 finding 5) accepts localhost and IP literals only by default, so the
test process lists ``testserver`` explicitly instead of weakening the production default.
Tests that check the allowlist itself override ``EW_TRUSTED_HOSTS`` with ``monkeypatch``.
"""
import os

os.environ.setdefault('EW_TRUSTED_HOSTS', 'testserver')

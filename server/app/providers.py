"""Model providers for the advocate (package P8). None is enabled by default.

Until a provider is configured and the owner's consent covers the principal's
scope, the advocate runs retrieval_only and makes no network call.
"""
from __future__ import annotations


def configured_provider(db, principal_id: int):
    return None

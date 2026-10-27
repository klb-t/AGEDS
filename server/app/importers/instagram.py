"""Instagram data download — traces area.

Messages (``your_instagram_activity/messages/inbox/<thread>/message_N.json``) use
the Facebook legacy layout and its mojibake rule: ``facebook.import_messages``
with namespace ``instagram`` (platform lane ``instagram``). Login, logout and
password-change activity (``security_and_login_information/...``) is read by
``account_activity`` (``import_instagram_activity`` below delegates).
"""
from __future__ import annotations

from pathlib import Path

from .facebook import import_messages


def import_instagram_messages(path: Path, **kwargs) -> dict:
    return import_messages(path, namespace='instagram', **kwargs)


def import_instagram_activity(path: Path, **kwargs) -> dict:
    from .account_activity import import_account_activity
    return import_account_activity(path, provider_hint='instagram', **kwargs)

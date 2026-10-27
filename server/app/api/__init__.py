"""Feature routers. Each module exposes ``router`` (APIRouter) and may expose
``NAV`` — a list of ``(path, label)`` pairs for the shared navigation.

Modules are owned by separate areas (see coordination/state.json); adding a
module here is the only shared edit needed to publish a new router.
"""
from __future__ import annotations

import importlib

from ..web import NAV

ROUTER_MODULES = (
    'catalog',
    'timeline',
    'media',
    'analysis',
    'reports',
    'integrations',
    'workbench',
    'imports_chat',
    'imports_comm',
    'hypotheses',
    'importplan',
    'location',
    'traces',
    'ledger',
    'owner',
)


def include_routers(app) -> None:
    for name in ROUTER_MODULES:
        module = importlib.import_module(f'{__name__}.{name}')
        # Every feature router sits behind installation scope (advocate authorization, 2026-10-27).
        from ..http_authz import installation_by_method
        app.include_router(module.router, dependencies=[installation_by_method()])
        for item in getattr(module, 'NAV', ()):
            if item not in NAV:
                NAV.append(item)
        install = getattr(module, 'install_app', None)
        if install is not None:
            install(app)

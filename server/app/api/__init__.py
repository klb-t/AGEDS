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
    'importplan',
)


def include_routers(app) -> None:
    for name in ROUTER_MODULES:
        module = importlib.import_module(f'{__name__}.{name}')
        app.include_router(module.router)
        for item in getattr(module, 'NAV', ()):
            if item not in NAV:
                NAV.append(item)
        install = getattr(module, 'install_app', None)
        if install is not None:
            install(app)

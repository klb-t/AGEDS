"""CLI subcommands owned by feature areas.

Each module exposes ``register(subparsers)`` and ``handle(args) -> int | None``.
``handle`` returns None for commands it does not own. Command output follows
``cli._emit`` conventions: one JSON object on stdout, errors on stderr.
"""
from __future__ import annotations

import importlib

COMMAND_MODULES = (
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
)


def _modules():
    return [importlib.import_module(f'{__name__}.{name}') for name in COMMAND_MODULES]


def register_all(subparsers) -> None:
    for module in _modules():
        module.register(subparsers)


def dispatch(args) -> int | None:
    for module in _modules():
        result = module.handle(args)
        if result is not None:
            return result
    return None

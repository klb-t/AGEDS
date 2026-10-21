"""Hard synthetic corpus, built from code at test time and never committed as data.

Each part module exposes ``build(root: Path) -> dict`` and owns its subdirectory
``root/<part>/``. The returned dict describes the planted hard cases so tests can
assert against declared expectations instead of re-deriving them. No part may
contain private names, numbers, addresses, recordings or records; use reserved
example domains (example.org/.com/.net), the +31 6 0000 xxxx / +48 000 000 xxx
fictional ranges declared in each part, and generated audio.
"""
from __future__ import annotations

import importlib
from pathlib import Path

PARTS = ('catalog_files', 'chat_exports', 'communications', 'audio', 'activity', 'hypotheses', 'location', 'traces',
         'ledger')


def build_corpus(root: Path, parts: tuple[str, ...] = PARTS) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name in parts:
        try:
            module = importlib.import_module(f'{__name__}.{name}')
        except ModuleNotFoundError as exc:
            if exc.name != f'{__name__}.{name}':
                raise
            manifest[name] = {'status': 'not_built_yet'}
            continue
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        manifest[name] = module.build(target)
    return manifest

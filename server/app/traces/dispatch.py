"""One table of trace importers for the CLI and the HTTP routes.

Each kind names its importer function and the options it accepts; options a
kind does not accept are refused (never silently dropped). Opt-in flags are
passed through unchanged; each importer records them on its run.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Kind:
    module: str
    function: str
    accepts: frozenset
    directory: bool  # the importer also takes a directory (export folder)
    file: bool = True
    label: str = ''


_COMMON = frozenset({'label', 'collection_id', 'source_locator', 'limits'})
_FILE = _COMMON | {'original_name'}
_LOCATION = frozenset({'enable_location', 'coordinate_decimals'})

KINDS: dict[str, Kind] = {
    'photos-takeout': Kind('photos_takeout', 'import_photos_takeout', _COMMON | _LOCATION, True,
                           label='Google Photos Takeout (sidecars, EXIF GPS)'),
    'photo-sidecar': Kind('photos_takeout', 'import_sidecar', _FILE | _LOCATION, False, label='Google Photos sidecar JSON'),
    'photo': Kind('photos_takeout', 'import_photo', _FILE | _LOCATION, False, label='Image EXIF + GPS'),
}


def run(kind: str, path: Path, **options) -> dict:
    if kind not in KINDS:
        raise ValueError(f'unknown trace import kind {kind!r}')
    spec = KINDS[kind]
    given = {k: v for k, v in options.items() if v is not None and v is not False}
    refused = sorted(set(given) - spec.accepts)
    if refused:
        raise ValueError(f'{kind} does not accept: {", ".join(refused)}')
    if path.is_dir() and not spec.directory:
        raise ValueError(f'{kind} expects a file')
    if path.is_file() and not spec.file:
        raise ValueError(f'{kind} expects a directory')
    function = getattr(importlib.import_module(f'..importers.{spec.module}', __package__), spec.function)
    return function(path, **{k: v for k, v in options.items() if k in spec.accepts and v is not None})


def describe() -> list[dict]:
    return [{'kind': name, 'label': spec.label, 'directory': spec.directory, 'file': spec.file,
             'options': sorted(spec.accepts)} for name, spec in KINDS.items()]

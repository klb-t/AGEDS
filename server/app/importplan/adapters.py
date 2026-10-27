"""Importer registry for plan execution: opt-ins, parameters and call shims.

Each adapter calls a real importer from ``server.app.importers`` on bytes that
were materialized from the catalog and verified by SHA-256. Provenance is
passed through the importers' own arguments: ``collection_id`` and a
``source_locator`` of the form ``catalog:collection/<id>/<virtual locator>``
(the same form as catalog ingest, so the same entry maps to one artifact row).

Where an importer has no ``collection_id`` argument (``sms_backup``) or no
locator argument (``exif.import_images``, ``recording_names.observe_paths``),
the adapter first acquires the verified file itself (``acquire_entry``, the
catalog-ingest contract) and then calls the importer's documented entry point
for an already acquired artifact or with the same locator. No importer file is
edited.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import db as dbmod
from .. import evidence
from ..catalog.ingest import LOCATOR_PREFIX, source_locator


@dataclass(frozen=True)
class Param:
    name: str
    required: bool
    description: str
    kind: str = 'text'  # text | int | choice
    minimum: int | None = None
    maximum: int | None = None


@dataclass
class MaterializedFile:
    entry_id: int
    path: Path
    locator: str
    source_locator: str
    name: str
    role: str
    relative_path: str
    mime: str | None = None


@dataclass
class RunContext:
    item: dict
    collection_id: int
    root_locator: str
    root_source_locator: str
    root_path: Path
    files: list[MaterializedFile]
    params: dict
    opt_ins: dict
    plan_options: dict
    export_label: str
    actor: str = 'importplan'

    def single(self) -> MaterializedFile:
        if len(self.files) != 1:
            raise ValueError('this importer expects exactly one file')
        return self.files[0]


@dataclass(frozen=True)
class Importer:
    key: str
    label: str
    unit_kinds: tuple[str, ...]
    run: Callable[[RunContext], dict]
    required_opt_ins: tuple[str, ...] = ()
    optional_opt_ins: tuple[str, ...] = ()
    params: tuple[Param, ...] = ()
    notes: str = ''
    namespaces: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> dict:
        return {'key': self.key, 'label': self.label, 'unit_kinds': list(self.unit_kinds),
                'required_opt_ins': list(self.required_opt_ins), 'optional_opt_ins': list(self.optional_opt_ins),
                'params': [{'name': p.name, 'required': p.required, 'description': p.description, 'kind': p.kind,
                            'minimum': p.minimum, 'maximum': p.maximum} for p in self.params],
                'notes': self.notes}


def acquire_entry(ctx: RunContext, item: MaterializedFile, *, importer: str) -> int:
    """Catalog-ingest contract for an already verified file; links the entry to the artifact."""
    with dbmod.session() as db:
        case_id = db.execute('SELECT case_id FROM collections WHERE id=?', (ctx.collection_id,)).fetchone()['case_id']
    source_id = evidence.ensure_source('catalog_collection', f'catalog collection {ctx.collection_id}',
                                       f'{LOCATOR_PREFIX}/{ctx.collection_id}', case_id=case_id)
    metadata = {'catalog_entry_id': item.entry_id, 'virtual_locator': item.locator, 'import_plan_item_id': ctx.item['id'],
                'importer': importer,
                'acquisition': 'copied from the catalogued location via a temporary file outside the source root',
                'catalog_sha256_verified_before_ingest': True}
    from ..catalog.ingest import declared_mtime
    with dbmod.session() as db:
        mtime, basis = declared_mtime(db, item.entry_id)
    # The path is a temporary copy; the catalogue knows the original's mtime (review finding 1).
    return evidence.ingest_file(item.path, source_id=source_id, source_locator=item.source_locator,
                                original_name=item.name, mime_type=item.mime, metadata=metadata,
                                collection_id=ctx.collection_id, source_stat='declared',
                                declared_source_modified_at=mtime, declared_time_basis=basis)


# ------------------------------------------------------------------- shims

def _chat(namespace: str):
    def run(ctx: RunContext) -> dict:
        from ..importers.chatgpt import import_chatgpt_export
        from ..importers.claude_ai import import_claude_export
        locators = {os.path.abspath(f.path): f.source_locator for f in ctx.files}

        def locate(path) -> str:
            key = os.path.abspath(path)
            if key not in locators:
                raise ValueError('a file outside the materialized plan item was offered to the chat importer')
            return locators[key]
        call = import_chatgpt_export if namespace == 'chatgpt' else import_claude_export
        return call([ctx.root_path], export_label=ctx.export_label, collection_id=ctx.collection_id, locator_for=locate)
    return run


def _file_importer(module: str, function: str, **fixed):
    def run(ctx: RunContext) -> dict:
        import importlib
        target = getattr(importlib.import_module(f'..importers.{module}', __package__), function)
        item = ctx.single()
        return target(item.path, source_locator=item.source_locator, original_name=item.name,
                      collection_id=ctx.collection_id, **fixed)
    return run


def _sms_backup(ctx: RunContext) -> dict:
    from ..importers.sms_backup import import_sms_backup
    item = ctx.single()
    # import_sms_backup has no collection argument: acquire first under the source it
    # will use, so the acquisition observation carries the collection; it then reuses it.
    source_id = evidence.ensure_source('android_backup', 'SMS Backup & Restore', item.source_locator)
    evidence.ingest_file(item.path, source_id=source_id, source_locator=item.source_locator, original_name=item.name,
                         metadata={'importer': 'sms_backup', 'import_plan_item_id': ctx.item['id'],
                                   'catalog_entry_id': item.entry_id, 'pre_acquired_for_collection': True},
                         collection_id=ctx.collection_id)
    return import_sms_backup(item.path, source_locator=item.source_locator, original_name=item.name)


def _takeout_activity(ctx: RunContext) -> dict:
    from ..importers.takeout_activity import import_my_activity
    item = ctx.single()
    return import_my_activity(item.path, enable_browser_history=bool(ctx.opt_ins.get('enable_browser_history')),
                              source_locator=item.source_locator, original_name=item.name,
                              collection_id=ctx.collection_id)


def _browser_history(ctx: RunContext) -> dict:
    from ..importers.browser_history import import_browser_history
    item = ctx.single()
    return import_browser_history(item.path, enable_browser_history=bool(ctx.opt_ins.get('enable_browser_history')),
                                  source_locator=item.source_locator, original_name=item.name,
                                  collection_id=ctx.collection_id)


def _location(ctx: RunContext) -> dict:
    # Since 2026-10-17 (location area L1): parser version 2 (raw samples, provider inferences, streaming).
    # An item that still carries the version-1 parameter ``coarse_decimals`` runs version 1 unchanged.
    from ..importers.location import import_location_history, import_location_history_v2
    item = ctx.single()
    enable = bool(ctx.opt_ins.get('enable_location'))
    if ctx.params.get('coarse_decimals') is not None:
        return import_location_history(item.path, enable_location=enable,
                                       coarse_decimals=int(ctx.params['coarse_decimals']),
                                       source_locator=item.source_locator, original_name=item.name,
                                       collection_id=ctx.collection_id)
    decimals = ctx.params.get('store_decimals')
    return import_location_history_v2(item.path, enable_location=enable,
                                      store_decimals=int(decimals) if decimals is not None else None,
                                      stream=ctx.params.get('stream') or None, source_locator=item.source_locator,
                                      original_name=item.name, collection_id=ctx.collection_id)


def _billing(ctx: RunContext) -> dict:
    from ..importers.billing import import_billing
    item = ctx.single()
    profiles = ctx.plan_options.get('billing_profiles_path')
    return import_billing(item.path, profile_id=ctx.params['profile_id'],
                          profiles_path=Path(profiles) if profiles else None,
                          source_locator=item.source_locator, original_name=item.name,
                          collection_id=ctx.collection_id)


def _whatsapp(ctx: RunContext) -> dict:
    from ..importers.whatsapp_media import import_whatsapp_export
    return import_whatsapp_export(ctx.root_path, chat_member=ctx.params.get('chat_member') or None,
                                  source_locator=ctx.root_source_locator, collection_id=ctx.collection_id)


def _recording_names(ctx: RunContext) -> dict:
    from ..importers.recording_names import observe_artifacts
    item = ctx.single()
    artifact_id = acquire_entry(ctx, item, importer='recording_names')
    result = observe_artifacts([artifact_id])
    return {'artifact_id': artifact_id, **result}


def _exif(ctx: RunContext) -> dict:
    from ..importers.exif import observe_artifact
    item = ctx.single()
    artifact_id = acquire_entry(ctx, item, importer='exif')
    return observe_artifact(artifact_id)


IMPORTERS: dict[str, Importer] = {i.key: i for i in (
    Importer('chatgpt_export', 'Eksport ChatGPT', ('archive', 'directory'), _chat('chatgpt'), namespaces=('chatgpt',),
             notes='conversations.json / conversations-NNN.json with assets; export label is the unit locator'),
    Importer('claude_export', 'Eksport Claude.ai', ('archive', 'directory'), _chat('claude'), namespaces=('claude',)),
    Importer('email_mbox', 'E-mail (MBOX)', ('file',), _file_importer('email_mbox', 'import_mbox')),
    Importer('email_eml', 'E-mail (EML)', ('file',), _file_importer('email_mbox', 'import_eml')),
    Importer('ics', 'Kalendarz (ICS)', ('file',), _file_importer('ics', 'import_ics')),
    Importer('vcf', 'Kontakty (VCF)', ('file',), _file_importer('vcf', 'import_vcf')),
    Importer('calls_xml', 'Rejestr połączeń (calls.xml)', ('file',), _file_importer('call_log', 'import_calls_xml')),
    Importer('sms_backup', 'SMS Backup & Restore (XML)', ('file',), _sms_backup,
             notes='older importer; also reads <calls>; acquired first so the observation carries the collection'),
    Importer('takeout_activity', 'Google My Activity', ('file',), _takeout_activity,
             optional_opt_ins=('enable_browser_history',),
             notes='browsing-like products (Chrome, YouTube) are skipped unless enable_browser_history'),
    Importer('browser_history', 'Historia przeglądania (opt-in)', ('file',), _browser_history,
             required_opt_ins=('enable_browser_history',)),
    Importer('location_history', 'Historia lokalizacji (opt-in)', ('file',), _location,
             required_opt_ins=('enable_location',),
             params=(Param('store_decimals', False, 'parser v2: stored coordinate precision (0-9 decimals; default: '
                           'as the source states it)', 'int', 0, 9),
                     Param('stream', False, 'parser v2: device/account stream key for deduplication'),
                     Param('coarse_decimals', False, 'parser v1 compatibility: when set, the coarse v1 importer runs '
                           '(0-4 decimals)', 'int', 0, 4)),
             notes='parser v2 (2026-10-17): raw samples + provider inferences; coarse_decimals selects v1'),
    Importer('billing', 'Billing operatora', ('file',), _billing,
             params=(Param('profile_id', True, 'explicit billing profile id', 'choice'),)),
    Importer('whatsapp_export', 'Eksport WhatsApp', ('archive', 'directory'), _whatsapp,
             params=(Param('chat_member', False, 'chat .txt to use when the export holds several', 'choice'),)),
    Importer('recording_names', 'Nazwy nagrań (hipotezy)', ('file',), _recording_names,
             notes='acquires the recording and observes its file name; nothing in the name is a fact'),
    Importer('exif', 'EXIF zdjęć', ('file',), _exif,
             notes='acquires the image and records EXIF time claims (zone unknown unless stated)'),
)}


def _load_extension_importers() -> None:
    """Area-owned importers: ``importplan/adapters_ext/<area>.py`` exposing ``IMPORTERS``."""
    import importlib
    import pkgutil
    from . import adapters_ext
    for info in sorted(pkgutil.iter_modules(adapters_ext.__path__), key=lambda m: m.name):
        module = importlib.import_module(f'{adapters_ext.__name__}.{info.name}')
        for importer in getattr(module, 'IMPORTERS', ()):
            if importer.key in IMPORTERS:
                raise RuntimeError(f'duplicate importer key {importer.key} in adapters_ext.{info.name}')
            IMPORTERS[importer.key] = importer


_load_extension_importers()


def catalog_source_locator(collection_id: int, locator: str) -> str:
    return source_locator(collection_id, locator)

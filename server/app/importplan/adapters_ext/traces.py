"""Import-plan adapters of the traces area (AGEDS-TASK-20261017-L2).

Every adapter runs on the verified, materialized copy and passes the catalog
locator (``item.source_locator``) and collection, so the plan footprint and the
importer's own acquisition name the same artifact. Opt-ins come only from the
item decision (``ctx.opt_ins``); location and browser history stay off unless a
person enables them for that item.
"""
from __future__ import annotations

from ..adapters import Importer, Param, RunContext


def _sidecar(ctx: RunContext) -> dict:
    from ...importers.photos_takeout import catalog_scope, import_sidecar
    item = ctx.single()
    return import_sidecar(item.path, enable_location=bool(ctx.opt_ins.get('enable_location')),
                          coordinate_decimals=ctx.params.get('coordinate_decimals'),
                          source_locator=item.source_locator, original_name=item.name, collection_id=ctx.collection_id,
                          scope_kind='catalog_directory', scope_files=catalog_scope(item.entry_id))


def _photo(ctx: RunContext) -> dict:
    from ...importers.photos_takeout import import_photo
    item = ctx.single()
    return import_photo(item.path, enable_location=bool(ctx.opt_ins.get('enable_location')),
                        coordinate_decimals=ctx.params.get('coordinate_decimals'),
                        source_locator=item.source_locator, original_name=item.name, collection_id=ctx.collection_id)


_DECIMALS = Param('coordinate_decimals', False, 'round stored coordinates (0-8 decimals); empty = source precision',
                  'int', 0, 8)

IMPORTERS: tuple[Importer, ...] = (
    Importer('google_photos_sidecar', 'Google Photos: metadane (sidecar JSON)', ('file',), _sidecar,
             optional_opt_ins=('enable_location',), params=(_DECIMALS,),
             notes='times, title and geo presence; coordinates only with enable_location; matched to catalogued '
                   'media of the same directory as a hypothesis'),
    Importer('photo_geotags', 'Zdjęcie: EXIF + GPS (geotag)', ('file',), _photo,
             optional_opt_ins=('enable_location',), params=(_DECIMALS,),
             notes='EXIF time claims (exif_minimal v1) plus the GPS IFD; coordinates only with enable_location'),
)

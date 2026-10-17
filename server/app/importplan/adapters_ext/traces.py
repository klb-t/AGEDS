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


def _messages(module: str, function: str, *, owner_param: str | None, resolver: bool = True):
    def run(ctx: RunContext) -> dict:
        import importlib
        from ...traces.messaging import catalog_resolver
        target = getattr(importlib.import_module(f'...importers.{module}', __package__), function)
        item = ctx.single()
        kwargs = {'source_locator': item.source_locator, 'original_name': item.name, 'collection_id': ctx.collection_id,
                  'export_label': ctx.params.get('export_label') or None}
        if owner_param:
            kwargs[owner_param] = ctx.params.get(owner_param) or None
        if resolver:
            kwargs['resolver'] = catalog_resolver(item.entry_id)
        return target(item.path, **kwargs)
    return run


def _account_activity(ctx: RunContext) -> dict:
    from ...importers.account_activity import import_account_activity
    item = ctx.single()
    return import_account_activity(item.path, enable_location=bool(ctx.opt_ins.get('enable_location')),
                                   source_locator=item.source_locator, original_name=item.name,
                                   collection_id=ctx.collection_id)


_EXPORT_LABEL = Param('export_label', False, 'logical export label (groups files of one download for cross-export identity)')
_OWNER_NAME = Param('owner_name', False, 'account owner display name as exported (sets direction; not stored)')
_OWNER_ID = Param('owner_id', False, 'account owner id/e-mail as exported (sets direction)')

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
    Importer('facebook_messages', 'Facebook/Messenger: wiadomości (JSON)', ('file',),
             _messages('facebook', 'import_messages', owner_param='owner_name'), params=(_OWNER_NAME, _EXPORT_LABEL),
             notes='mojibake decoded as a separate layer (fb_latin1_utf8_v1); media are references to catalogued files'),
    Importer('instagram_messages', 'Instagram: wiadomości (JSON)', ('file',),
             _messages('instagram', 'import_instagram_messages', owner_param='owner_name'),
             params=(_OWNER_NAME, _EXPORT_LABEL)),
    Importer('telegram_desktop', 'Telegram Desktop (result.json)', ('file',),
             _messages('telegram', 'import_result', owner_param='owner_id'), params=(_OWNER_ID, _EXPORT_LABEL),
             notes='date (exporting computer wall clock, zone unknown) and date_unixtime kept as separate candidates'),
    Importer('google_chat', 'Google Chat (Takeout messages.json)', ('file',),
             _messages('google_chat', 'import_chat', owner_param='owner_id'), params=(_OWNER_ID, _EXPORT_LABEL)),
    Importer('twitter_x_archive', 'X/Twitter: archiwum (tweets.js, direct-messages.js)', ('file',),
             _messages('twitter_x', 'import_archive', owner_param='owner_id'), params=(_OWNER_ID, _EXPORT_LABEL)),
    Importer('linkedin_messages', 'LinkedIn: messages.csv', ('file',),
             _messages('linkedin', 'import_messages_csv', owner_param='owner_name', resolver=False),
             params=(_OWNER_NAME, _EXPORT_LABEL)),
    Importer('account_activity', 'Aktywność logowania / bezpieczeństwo konta', ('file',), _account_activity,
             optional_opt_ins=('enable_location',),
             notes='time, IP raw, user agent, device; cookies never stored; the provider\'s location labels only with '
                   'enable_location; no IP geolocation'),
)

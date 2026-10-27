"""Import-plan adapters of the location area (task L1): GPX, KML/KMZ, GeoJSON and FIT.

Every adapter requires the per-item opt-in ``enable_location`` (recorded by the
importer on every run and event) and passes the catalog provenance through the
importer's own ``source_locator`` / ``collection_id`` arguments. Google location
exports keep the existing ``location_history`` importer key (base detector rules);
since 2026-10-17 it runs parser version 2 unless a plan item still carries the
version-1 parameter ``coarse_decimals`` (see ``importplan.adapters._location``).
"""
from __future__ import annotations

from ..adapters import Importer, Param, RunContext

STORE_DECIMALS = Param('store_decimals', False, 'stored coordinate precision in decimal places (0-9; default: as the '
                       'source states it)', 'int', 0, 9)
STREAM = Param('stream', False, 'device/account stream key for deduplication (default: collection:<id>)')


def _common(ctx: RunContext) -> dict:
    item = ctx.single()
    decimals = ctx.params.get('store_decimals')
    return {'enable_location': bool(ctx.opt_ins.get('enable_location')),
            'store_decimals': int(decimals) if decimals is not None else None,
            'stream': ctx.params.get('stream') or None, 'source_locator': item.source_locator,
            'original_name': item.name, 'collection_id': ctx.collection_id}


def _gpx(ctx: RunContext) -> dict:
    from ...importers.gpx import import_gpx
    return import_gpx(ctx.single().path, gpx_naive_time=ctx.params.get('gpx_naive_time') or 'unknown', **_common(ctx))


def _kml(ctx: RunContext) -> dict:
    from ...importers.kml import import_kml
    return import_kml(ctx.single().path, **_common(ctx))


def _geojson(ctx: RunContext) -> dict:
    import json
    from ...importers.geojson import import_geojson
    raw = ctx.params.get('geojson_options')
    options = json.loads(raw) if raw else None
    return import_geojson(ctx.single().path, options=options, **_common(ctx))


def _fit(ctx: RunContext) -> dict:
    from ...importers.fit import import_fit
    return import_fit(ctx.single().path, **_common(ctx))


IMPORTERS = (
    Importer('gpx_track', 'Ślady GPX (opt-in lokalizacji)', ('file',), _gpx, required_opt_ins=('enable_location',),
             params=(STORE_DECIMALS, STREAM,
                     Param('gpx_naive_time', False, 'times without offset: unknown (default) or utc_per_schema', 'choice')),
             notes='tracks -> raw samples + location_track; routes/waypoints are declared, not observed'),
    Importer('kml_track', 'KML/KMZ (opt-in lokalizacji)', ('file',), _kml, required_opt_ins=('enable_location',),
             params=(STORE_DECIMALS, STREAM),
             notes='gx:Track -> raw samples; LineString/Point -> declared paths/points; KMZ by zip signature'),
    Importer('geojson_track', 'GeoJSON (opt-in lokalizacji)', ('file',), _geojson, required_opt_ins=('enable_location',),
             params=(STORE_DECIMALS, STREAM,
                     Param('geojson_options', False, 'JSON object: time/start/end/coord_times/accuracy property names, '
                           'epoch_unit')),
             notes='time property names are parameters; numeric times need epoch_unit'),
    Importer('fit_track', 'FIT (opcjonalny adapter fitparse)', ('file',), _fit, required_opt_ins=('enable_location',),
             params=(STORE_DECIMALS, STREAM),
             notes='needs the optional python package fitparse; refused as unavailable otherwise (nothing acquired)'),
)

"""Ledger-area import-plan adapters: tabular profiles, CAMT.053, MT940 and health exports.

Health importers require the per-item opt-in ``enable_health`` (special
category data); without it the item stays blocked (``opt_in_disabled``) and the
importer itself refuses. The tabular importer needs an explicit ``profile_id``
(detector reasons name the example profile whose header markers matched).
A private profile file is passed as the plan option ``ledger_profiles_path``.
"""
from __future__ import annotations

from pathlib import Path

from ..adapters import Importer, Param, RunContext


def _common(ctx: RunContext) -> tuple:
    item = ctx.single()
    return item, {'source_locator': item.source_locator, 'original_name': item.name, 'collection_id': ctx.collection_id}


def _tabular(ctx: RunContext) -> dict:
    from ...importers.tabular_profiles import import_tabular
    item, common = _common(ctx)
    path = ctx.plan_options.get('ledger_profiles_path')
    return import_tabular(item.path, profile_id=ctx.params['profile_id'], profiles_path=Path(path) if path else None,
                          encoding=ctx.params.get('encoding') or None, **common)


def _camt(ctx: RunContext) -> dict:
    from ...importers.camt053 import import_camt053
    item, common = _common(ctx)
    return import_camt053(item.path, **common)


def _mt940(ctx: RunContext) -> dict:
    from ...importers.mt940 import import_mt940
    item, common = _common(ctx)
    base = ctx.params.get('two_digit_year_base')
    return import_mt940(item.path, two_digit_year_base=int(base) if base is not None else 2000,
                        encoding=ctx.params.get('encoding') or None, **common)


def _health(module: str, function: str):
    def run(ctx: RunContext) -> dict:
        import importlib
        target = getattr(importlib.import_module(f'...importers.{module}', __package__), function)
        item, common = _common(ctx)
        return target(item.path, enable_health=ctx.opt_ins.get('enable_health') is True, **common)
    return run


_HEALTH_NOTE = 'special-category data: needs the per-item opt-in enable_health; processed locally only'

IMPORTERS = (
    Importer('ledger_tabular', 'Rejestr tabelaryczny (profil)', ('file',), _tabular,
             params=(Param('profile_id', True, 'tabular profile id (shipped examples or ledger_profiles_path)', 'choice'),
                     Param('encoding', False, 'explicit encoding (utf-8, utf-8-sig, cp1250, iso-8859-2, cp1252)')),
             notes='bank CSV, transit card history or ride trips through an explicit (example or private) profile'),
    Importer('camt053', 'Wyciąg bankowy (CAMT.053)', ('file',), _camt,
             notes='ISO 20022 statements; balance checks computed by code, mismatches reported'),
    Importer('mt940', 'Wyciąg bankowy (MT940)', ('file',), _mt940,
             params=(Param('two_digit_year_base', False, 'century of YYMMDD dates (default 2000)', 'int', 1900, 2100),
                     Param('encoding', False, 'explicit encoding of the :86: text')),
             notes='entry-date years inferred relative to the value date; ambiguity kept'),
    Importer('health_apple', 'Apple Health (opt-in)', ('file',), _health('health_apple', 'import_apple_health'),
             required_opt_ins=('enable_health',), notes=_HEALTH_NOTE),
    Importer('health_google_fit', 'Google Fit (opt-in)', ('file',), _health('health_google_fit', 'import_google_fit'),
             required_opt_ins=('enable_health',), notes=_HEALTH_NOTE),
    Importer('health_samsung', 'Samsung Health (opt-in)', ('file',), _health('health_samsung', 'import_samsung_health'),
             required_opt_ins=('enable_health',), notes=_HEALTH_NOTE),
    Importer('health_fitbit', 'Fitbit (opt-in)', ('file',), _health('health_fitbit', 'import_fitbit'),
             required_opt_ins=('enable_health',), notes=_HEALTH_NOTE),
)

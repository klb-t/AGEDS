"""Section providers: one module per section type, loaded lazily.

Contract ``ageds.report-section/v1`` — ``provide(ctx) -> dict`` with keys:

=================  ==========================================================
``section``        section type (registry key)
``provider``       module that produced it; ``provider_version`` its version
``status``         ``ok`` | ``partial`` (limit hit / some input unreadable) |
                   ``empty`` (no rows in scope) | ``unavailable`` (module or
                   data not present, ``reason`` says why) | ``error`` (an
                   upstream area's code raised; ``reason`` has the message)
``reason``         text or None
``title_key``      label key of the section heading
``blocks``         render blocks (``reports.blocks``); computed values only
``inputs``         ``{table: [{id, row_sha256}]}`` rows read (``SELECT *``)
``model_runs``     processing/description runs referenced, every field
                   given or ``unknown``
``cited``          ``{artifacts, derived_text, anchors}`` with their hashes
``citations``      ``[[artifact_id, anchor_id], ...]`` for the bundle
``unknowns``       names of fields that are unknown in this section
``exclusions``     ``{name: count|bool|text}`` (untimed, unknown zone,
                   unsupported, limit hit, filtered)
``data_range``     ``{from, to, basis}`` observed in this section (UTC text)
``snapshot``       ``report_read_transaction`` or ``separate_read_transaction``
                   (an upstream function that opens its own connection)
``rule``           rule/projection id used for time placement, or None
=================  ==========================================================

A provider never writes. ``ctx.db`` is a read-only SQLite connection inside one
read transaction shared by all sections of the report. ``ctx.scope`` carries
ids found by earlier sections (``event_ids``, ``artifact_ids``) so later
sections can restrict themselves to the same material; when they do, the
section says so in its exclusions.

Sections fed by other areas (catalog A, timeline C, time budget E1, hypothesis
dossier E2) look up their upstream function lazily. A missing module is an
explicit ``unavailable`` section, never an empty success. ``register`` lets an
integrator wire a different callable without editing this package.
"""
from __future__ import annotations

import importlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from ..blocks import BLOCK_KINDS, notice
from ..common import row_sha256
from ..labels import label

CONTRACT = 'ageds.report-section/v1'
STATUSES = ('ok', 'partial', 'empty', 'unavailable', 'error')
REGISTRY = {
    'catalog': 'catalog', 'sources': 'sources', 'documents': 'documents',
    'communications': 'communications', 'chronology': 'chronology', 'timeline': 'timeline',
    'chat_records': 'chat_records', 'media': 'media', 'citations': 'citations',
    'annotations': 'annotations', 'integrations': 'integrations', 'counterparties': 'counterparties',
    'open_questions': 'open_questions', 'time_budget': 'time_budget',
    'hypothesis_dossier': 'hypothesis_dossier',
}
_OVERRIDES: dict[str, Callable[['SectionContext'], dict]] = {}
REQUIRED_KEYS = ('section', 'provider', 'provider_version', 'status', 'reason', 'title_key', 'blocks', 'inputs',
                 'model_runs', 'cited', 'citations', 'unknowns', 'exclusions', 'data_range', 'snapshot', 'rule')


@dataclass
class SectionContext:
    db: sqlite3.Connection
    params: dict
    lang: str
    options: dict = field(default_factory=dict)
    scope: dict = field(default_factory=dict)
    bundle_enabled: bool = False


def register(section: str, provider: Callable[[SectionContext], dict]) -> None:
    """Wire a provider callable for ``section`` (integration hook, process-local)."""
    if section not in REGISTRY:
        raise ValueError(f'unknown section type {section}')
    _OVERRIDES[section] = provider


def unregister(section: str) -> None:
    _OVERRIDES.pop(section, None)


class Section:
    """Builder for one section result; collects inputs/runs/hashes with dedup."""

    def __init__(self, ctx: SectionContext, section: str, provider: str, version: str, title_key: str):
        self.ctx = ctx
        self.data = {'section': section, 'provider': provider, 'provider_version': version, 'status': 'ok',
                     'reason': None, 'title_key': title_key, 'blocks': [], 'inputs': {}, 'model_runs': [],
                     'cited': {'artifacts': [], 'derived_text': [], 'anchors': []}, 'citations': [],
                     'unknowns': [], 'exclusions': {}, 'data_range': {'from': None, 'to': None, 'basis': None},
                     'snapshot': 'report_read_transaction', 'rule': None}
        self._seen: set[tuple] = set()

    @property
    def lang(self) -> str:
        return self.ctx.lang

    def block(self, block: dict) -> None:
        self.data['blocks'].append(block)

    def input_row(self, table: str, row: Any) -> None:
        row = dict(row)
        key = ('input', table, row.get('id'))
        if row.get('id') is None or key in self._seen:
            return
        self._seen.add(key)
        self.data['inputs'].setdefault(table, []).append({'id': row['id'], 'row_sha256': row_sha256(row)})

    def input_rows(self, table: str, rows) -> None:
        for row in rows:
            self.input_row(table, row)

    def model_run(self, run: dict) -> None:
        key = ('run', run.get('table', 'processing_runs'), run.get('run_id'))
        if key in self._seen:
            return
        self._seen.add(key)
        self.data['model_runs'].append(run)

    def cite(self, kind: str, entry: dict) -> None:
        key = ('cited', kind, entry.get('id'))
        if key in self._seen:
            return
        self._seen.add(key)
        self.data['cited'][kind].append(entry)

    def citation(self, artifact_id: int, anchor_id: int) -> None:
        key = ('citation', artifact_id, anchor_id)
        if key not in self._seen:
            self._seen.add(key)
            self.data['citations'].append([artifact_id, anchor_id])

    def unknown(self, name: str) -> None:
        if name not in self.data['unknowns']:
            self.data['unknowns'].append(name)

    def exclude(self, name: str, value: Any) -> None:
        self.data['exclusions'][name] = value

    def observe_time(self, utc_text: str | None, basis: str) -> None:
        if not utc_text:
            return
        span = self.data['data_range']
        span['basis'] = basis if span['basis'] in (None, basis) else 'mixed'
        span['from'] = utc_text if span['from'] is None or utc_text < span['from'] else span['from']
        span['to'] = utc_text if span['to'] is None or utc_text > span['to'] else span['to']

    def status(self, status: str, reason: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(status)
        self.data['status'] = status
        self.data['reason'] = reason

    def unavailable(self, reason: str) -> dict:
        self.status('unavailable', reason)
        self.block(notice(f'{self.data["section"]}.unavailable', 'unavailable',
                          f'{label(self.lang, "status_unavailable")}: {reason}'))
        return self.result()

    def result(self) -> dict:
        return self.data


def load_upstream(candidates: tuple[tuple[str, str], ...]) -> tuple[Callable | None, str]:
    """First importable ``(module, function)``; otherwise (None, reason)."""
    reasons = []
    for module_name, function_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            reasons.append(f'module {module_name} not present ({exc.name})')
            continue
        function = getattr(module, function_name, None)
        if callable(function):
            return function, f'{module_name}.{function_name}'
        reasons.append(f'{module_name} has no {function_name}()')
    return None, '; '.join(reasons)


def validate_section(result: Any, name: str) -> dict:
    if not isinstance(result, dict):
        raise RuntimeError(f'provider {name} returned {type(result).__name__}, not a dict')
    missing = [key for key in REQUIRED_KEYS if key not in result]
    if missing:
        raise RuntimeError(f'provider {name} result misses {missing}')
    if result['status'] not in STATUSES:
        raise RuntimeError(f'provider {name} returned unknown status {result["status"]!r}')
    for block in result['blocks']:
        if not isinstance(block, dict) or block.get('kind') not in BLOCK_KINDS:
            raise RuntimeError(f'provider {name} returned an unknown block')
    return result


def run_section(name: str, ctx: SectionContext) -> dict:
    if name not in REGISTRY:
        raise ValueError(f'unknown section type {name}')
    override = _OVERRIDES.get(name)
    if override is not None:
        return validate_section(override(ctx), name)
    module_name = f'{__name__}.{REGISTRY[name]}'
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        return Section(ctx, name, module_name, 'unknown', f'section_{name}').unavailable(
            f'provider module {module_name} not present')
    return validate_section(module.provide(ctx), name)

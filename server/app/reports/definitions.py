"""Report types, their sections and the parameter contract (defaults are presets).

Templates are generic (``templates/reports/base.html`` plus a short per-type
file); no case data lives in them. Every parameter is recorded verbatim in the
method section with its SHA-256; unknown parameters are refused rather than
ignored, so a typo cannot silently produce a different report.
"""
from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import ReportInputError, parse_utc_bound
from .labels import LANGUAGES
from .providers import REGISTRY

FORMATS = ('html', 'json', 'csv', 'xlsx', 'pdf')
REQUIRED_FORMATS = ('html', 'json')
GROUP_BY = ('counterparty', 'domain', 'collection')

TYPES: dict[str, dict] = {
    'catalog': {'template': 'catalog.html', 'version': '1', 'title_key': 'type_catalog',
                'sections': (('catalog', {}), ('sources', {}), ('chat_records', {}), ('media', {}),
                             ('integrations', {}))},
    'timeline': {'template': 'timeline.html', 'version': '1', 'title_key': 'type_timeline',
                 'sections': (('timeline', {}), ('communications', {}), ('citations', {'scope': 'scope'}),
                              ('annotations', {}))},
    'time_budget': {'template': 'time_budget.html', 'version': '1', 'title_key': 'type_time_budget',
                    'sections': (('time_budget', {}),)},
    'hypothesis_dossier': {'template': 'hypothesis_dossier.html', 'version': '1',
                           'title_key': 'type_hypothesis_dossier',
                           'sections': (('hypothesis_dossier', {}), ('citations', {'scope': 'scope'}))},
    'lawyer_package': {'template': 'lawyer_package.html', 'version': '1', 'title_key': 'type_lawyer_package',
                       'sections': (('counterparties', {}), ('citations', {'title_key': 'key_quotes'}),
                                    ('documents', {}), ('annotations', {}), ('open_questions', {}))},
}

DEFAULTS: dict[str, Any] = {
    'from': None, 'to': None, 'language': 'pl', 'assume_zone': None, 'event_types': [],
    'counterparty': None, 'topic': None, 'include_hidden_chat_parts': False,
    'max_rows': 2000, 'max_events': 200_000, 'quote_chars': 1000, 'per_group_rows': 200,
    'formats': ['html', 'json', 'csv', 'xlsx'], 'include_citation_bundle': True,
    'anchor_ids': [], 'open_questions': [], 'group_by': 'counterparty', 'collection_id': None,
    'min_gap_days': 30, 'budget_run_id': None, 'hypothesis_id': None,
    'workbench_base_url': None, 'title': None, 'sections': None,
}
_INT_LIMITS = {'max_rows': (1, 1_000_000), 'max_events': (1, 10_000_000), 'quote_chars': (0, 1_000_000),
               'per_group_rows': (1, 1_000_000), 'min_gap_days': (1, 100_000)}
_OPTIONAL_IDS = ('collection_id', 'budget_run_id', 'hypothesis_id')
_URL = re.compile(r'https?://[^\s"\'<>`]+')
MAX_SQL_INTEGER = 2**63 - 1


def _int(name: str, value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ReportInputError(f'{name} must be an integer between {low} and {high}')
    return value


def _text(name: str, value: Any, limit: int, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ReportInputError(f'{name} must be text of at most {limit} characters')
    value = value.strip()
    return value or None


def _list(name: str, value: Any, limit: int) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise ReportInputError(f'{name} must be a list of at most {limit} items')
    return value


def normalize_params(report_type: str, raw: dict | None) -> dict:
    """Full canonical parameter dict (defaults filled) or ReportInputError."""
    if report_type not in TYPES:
        raise ReportInputError(f'unknown report type {report_type!r}; known: {sorted(TYPES)}')
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ReportInputError('params must be a JSON object')
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ReportInputError(f'unknown parameters: {unknown}')
    params = {**DEFAULTS, **raw}
    out: dict[str, Any] = {}
    out['from'] = parse_utc_bound(params['from'], 'from')
    out['to'] = parse_utc_bound(params['to'], 'to')
    if out['from'] and out['to'] and out['from'] >= out['to']:
        raise ReportInputError('from must be earlier than to')
    if params['language'] not in LANGUAGES:
        raise ReportInputError(f'language must be one of {list(LANGUAGES)}')
    out['language'] = params['language']
    zone = _text('assume_zone', params['assume_zone'], 64)
    if zone is not None:
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ReportInputError(f'assume_zone is not an IANA zone: {zone!r}') from exc
    out['assume_zone'] = zone
    types = _list('event_types', params['event_types'] or [], 100)
    if any(not isinstance(item, str) or not item or len(item) > 100 for item in types):
        raise ReportInputError('event_types must be non-empty strings')
    out['event_types'] = sorted(set(types))
    out['counterparty'] = _text('counterparty', params['counterparty'], 500)
    out['topic'] = _text('topic', params['topic'], 500)
    for name in ('include_hidden_chat_parts', 'include_citation_bundle'):
        if type(params[name]) is not bool:
            raise ReportInputError(f'{name} must be true or false')
        out[name] = params[name]
    for name, (low, high) in _INT_LIMITS.items():
        out[name] = _int(name, params[name], low, high)
    formats = _list('formats', params['formats'], len(FORMATS))
    if any(item not in FORMATS for item in formats) or len(set(formats)) != len(formats):
        raise ReportInputError(f'formats must be distinct values of {list(FORMATS)}')
    missing = [item for item in REQUIRED_FORMATS if item not in formats]
    if missing:
        raise ReportInputError(f'formats must include {list(REQUIRED_FORMATS)} (HTML primary, JSON for machines)')
    out['formats'] = [item for item in FORMATS if item in formats]
    anchors = _list('anchor_ids', params['anchor_ids'] or [], 10_000)
    out['anchor_ids'] = [_int('anchor_ids[]', item, 1, MAX_SQL_INTEGER) for item in anchors]
    questions = _list('open_questions', params['open_questions'] or [], 200)
    out['open_questions'] = [_text('open_questions[]', item, 10_000, optional=False) for item in questions]
    out['open_questions'] = [item for item in out['open_questions'] if item]
    if params['group_by'] not in GROUP_BY:
        raise ReportInputError(f'group_by must be one of {list(GROUP_BY)}')
    out['group_by'] = params['group_by']
    for name in _OPTIONAL_IDS:
        out[name] = None if params[name] is None else _int(name, params[name], 1, MAX_SQL_INTEGER)
    url = _text('workbench_base_url', params['workbench_base_url'], 2000)
    if url is not None:
        url = url.rstrip('/')
        if not _URL.fullmatch(url):
            raise ReportInputError('workbench_base_url must be an http(s) URL without spaces or quotes')
    out['workbench_base_url'] = url
    out['title'] = _text('title', params['title'], 300)
    sections = params['sections']
    if sections is not None:
        sections = _list('sections', sections, len(REGISTRY))
        if not sections or any(item not in REGISTRY for item in sections) or len(set(sections)) != len(sections):
            raise ReportInputError(f'sections must be distinct values of {sorted(REGISTRY)}')
    out['sections'] = sections
    return out


def section_plan(report_type: str, params: dict) -> list[tuple[str, dict]]:
    default = TYPES[report_type]['sections']
    if params['sections'] is None:
        return [(name, dict(options)) for name, options in default]
    options = dict(default)
    return [(name, dict(options.get(name, {}))) for name in params['sections']]


def describe_types() -> dict:
    return {'types': {name: {'template': f'reports/{spec["template"]}', 'version': spec['version'],
                             'sections': [name for name, _ in spec['sections']]} for name, spec in TYPES.items()},
            'defaults': DEFAULTS, 'formats': list(FORMATS), 'required_formats': list(REQUIRED_FORMATS),
            'group_by': list(GROUP_BY), 'sections': sorted(REGISTRY)}

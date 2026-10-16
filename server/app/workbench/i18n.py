"""Interface translations: PL (default), NL, EN from JSON catalogs.

Catalogs live in ``server/app/static/i18n/<lang>.json`` so the browser helper
(``/static/workbench-i18n.mjs``) reads the same data. A value is a string with
``{name}`` placeholders, or an object of CLDR plural categories
(``one``/``few``/``many``/``other``) selected by the ``n`` parameter.

Language precedence for a request: ``?lang=`` (that request only), then the
``ageds_lang`` cookie, then ``Accept-Language`` (q-values, primary subtag),
then ``pl``. Unsupported values are ignored, not errors.

A key missing from the chosen catalog falls back to Polish and is logged once;
a key missing from Polish renders as ``⟦key⟧`` and is logged as an error. A
translation is never empty. Only interface text is translated: source text,
stored data, model output and user notes are shown as stored.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

log = logging.getLogger(__name__)

LANGUAGES = ('pl', 'nl', 'en')
DEFAULT_LANGUAGE = 'pl'
COOKIE = 'ageds_lang'
THEME_COOKIE = 'ageds_theme'
THEMES = ('auto', 'dark', 'light')
CATALOG_DIR = Path(__file__).resolve().parents[1] / 'static' / 'i18n'
PLURAL_CATEGORIES = ('zero', 'one', 'two', 'few', 'many', 'other')
MAX_ACCEPT_LANGUAGE = 1024
MAX_ACCEPT_ENTRIES = 32
_PLACEHOLDER = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}')
_LANG_TAG = re.compile(r'^[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*$|^\*$')
_reported: set[tuple[str, str]] = set()
_MAX_REPORTED = 4096


class CatalogError(ValueError):
    """A catalog file is malformed."""


def _validate_catalog(lang: str, data) -> dict:
    if not isinstance(data, dict):
        raise CatalogError(f'{lang}: catalog must be an object')
    for key, value in data.items():
        if key == '_meta':
            continue
        if isinstance(value, str):
            if not value:
                raise CatalogError(f'{lang}: empty value for {key}')
        elif isinstance(value, dict):
            if 'other' not in value or set(value) - set(PLURAL_CATEGORIES):
                raise CatalogError(f'{lang}: plural value for {key} needs "other" and CLDR categories only')
            if not all(isinstance(v, str) and v for v in value.values()):
                raise CatalogError(f'{lang}: plural forms of {key} must be non-empty strings')
        else:
            raise CatalogError(f'{lang}: value for {key} must be a string or plural object')
    return data


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict:
    if lang not in LANGUAGES:
        raise CatalogError(f'unsupported language: {lang}')
    with (CATALOG_DIR / f'{lang}.json').open('r', encoding='utf-8') as handle:
        return _validate_catalog(lang, json.load(handle))


def plural_category(lang: str, n) -> str:
    """CLDR cardinal rules for integers in pl/nl/en; non-integers use 'other'."""
    if type(n) is not int:
        return 'other'
    n = abs(n)
    if lang == 'pl':
        if n == 1:
            return 'one'
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return 'few'
        return 'many'
    return 'one' if n == 1 else 'other'


def _report(lang: str, key: str, level: int, message: str) -> None:
    marker = (lang, key)
    if marker in _reported:
        return
    if len(_reported) < _MAX_REPORTED:
        _reported.add(marker)
    log.log(level, message, key, lang)


def _format(template: str, params: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), template)


def _resolve(value, lang: str, params: dict) -> str:
    if isinstance(value, dict):
        category = plural_category(lang, params.get('n'))
        value = value.get(category) or value['other']
    return _format(value, params)


def translate(lang: str, key: str, **params) -> str:
    if lang not in LANGUAGES:
        lang = DEFAULT_LANGUAGE
    value = catalog(lang).get(key)
    if value is not None:
        return _resolve(value, lang, params)
    fallback = catalog(DEFAULT_LANGUAGE).get(key)
    if fallback is not None:
        _report(lang, key, logging.WARNING, 'i18n: missing key %s in %s; showing the Polish text')
        return _resolve(fallback, DEFAULT_LANGUAGE, params)
    _report(lang, key, logging.ERROR, 'i18n: missing key %s in every catalog (requested %s)')
    return f'⟦{key}⟧'


def has_key(lang: str, key: str) -> bool:
    return key in catalog(lang)


def parse_accept_language(header: str | None) -> list[tuple[str, float]]:
    """Bounded RFC 9110 parsing: [(primary subtag, q)] sorted by q, header order for ties."""
    if not header or len(header) > MAX_ACCEPT_LANGUAGE:
        return []
    entries = []
    for index, part in enumerate(header.split(',')[:MAX_ACCEPT_ENTRIES]):
        pieces = [p.strip() for p in part.split(';')]
        tag = pieces[0]
        if not tag or not _LANG_TAG.match(tag):
            continue
        q = 1.0
        for param in pieces[1:]:
            if param[:2].lower() == 'q=':
                try:
                    q = float(param[2:])
                except ValueError:
                    q = -1.0
        if not 0.0 < q <= 1.0:
            continue
        entries.append((tag.split('-')[0].lower(), q, index))
    entries.sort(key=lambda e: (-e[1], e[2]))
    return [(tag, q) for tag, q, _ in entries]


def negotiate(query_lang: str | None, cookie_lang: str | None, accept_language: str | None) -> tuple[str, str]:
    """Return (language, source) with source in query|cookie|accept-language|default."""
    if query_lang in LANGUAGES:
        return query_lang, 'query'
    if cookie_lang in LANGUAGES:
        return cookie_lang, 'cookie'
    for tag, _ in parse_accept_language(accept_language):
        if tag in LANGUAGES:
            return tag, 'accept-language'
    return DEFAULT_LANGUAGE, 'default'


def request_language(request) -> tuple[str, str]:
    if request is None:
        return DEFAULT_LANGUAGE, 'default'
    state = getattr(request, 'state', None)
    cached = getattr(state, 'ageds_lang', None) if state is not None else None
    if cached:
        return cached
    result = negotiate(request.query_params.get('lang'), request.cookies.get(COOKIE), request.headers.get('accept-language'))
    if state is not None:
        state.ageds_lang = result
    return result


def request_theme(request) -> str:
    value = request.cookies.get(THEME_COOKIE) if request is not None else None
    return value if value in THEMES else 'auto'


# --- Jinja integration ------------------------------------------------------------
def _ctx_request(ctx):
    return ctx.get('request') if ctx is not None else None


def install(env) -> None:
    """Register ``t``, ``t_nav``, ``ui_lang``, ``ui_html_attrs`` and ``ui_shell_config`` on a Jinja env."""
    from jinja2 import pass_context

    @pass_context
    def t(ctx, key, **params):
        return translate(request_language(_ctx_request(ctx))[0], key, **params)

    @pass_context
    def t_nav(ctx, path, label):
        lang = request_language(_ctx_request(ctx))[0]
        key = f'nav.{path}'
        if has_key(lang, key):
            return translate(lang, key)
        if lang != DEFAULT_LANGUAGE:
            _report(lang, key, logging.INFO, 'i18n: navigation key %s missing in %s; showing the area label')
        return label

    @pass_context
    def ui_lang(ctx):
        return request_language(_ctx_request(ctx))[0]

    @pass_context
    def ui_html_attrs(ctx, theme_support: bool = True):
        request = _ctx_request(ctx)
        lang = request_language(request)[0]
        attrs = f'lang="{lang}"'
        if theme_support:
            attrs += ' data-theme-support="light dark"'
            theme = request_theme(request)
            if theme in ('light', 'dark'):
                attrs += f' data-theme="{theme}"'
        return Markup(attrs)

    @pass_context
    def ui_shell_config(ctx):
        request = _ctx_request(ctx)
        lang, source = request_language(request)
        config = {'lang': lang, 'langSource': source, 'languages': list(LANGUAGES), 'defaultLanguage': DEFAULT_LANGUAGE,
                  'langCookie': COOKIE, 'themeCookie': THEME_COOKIE, 'theme': request_theme(request),
                  'catalogUrl': '/static/i18n/{lang}.json', 'keysUrl': '/static/workbench-keys.json'}
        return Markup(json.dumps(config, ensure_ascii=True).replace('<', '\\u003c'))

    env.globals.update(t=t, t_nav=t_nav, ui_lang=ui_lang, ui_html_attrs=ui_html_attrs, ui_shell_config=ui_shell_config,
                       ui_languages=LANGUAGES)


def _placeholders(value) -> set[str]:
    values = value.values() if isinstance(value, dict) else [value]
    return {name for text in values for name in _PLACEHOLDER.findall(text)}


def catalog_report() -> dict:
    """Completeness against Polish: missing/extra keys and placeholder mismatches (``nav.<path>`` keys are optional)."""
    base = {k: v for k, v in catalog(DEFAULT_LANGUAGE).items() if k != '_meta'}
    report = {'default': DEFAULT_LANGUAGE, 'keys': len(base), 'languages': {}}
    for lang in LANGUAGES:
        data = {k: v for k, v in catalog(lang).items() if k != '_meta'}
        optional = lambda key: key.startswith('nav./')
        report['languages'][lang] = {
            'keys': len(data),
            'missing': sorted(k for k in base if k not in data and not optional(k)),
            'extra': sorted(k for k in data if k not in base and not optional(k)),
            'placeholder_mismatch': sorted(k for k in base if k in data and _placeholders(base[k]) != _placeholders(data[k])),
        }
    report['complete'] = all(not (v['missing'] or v['extra'] or v['placeholder_mismatch']) for v in report['languages'].values())
    return report

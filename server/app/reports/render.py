"""HTML rendering of a computed report document (HTML is the primary format).

Templates live in ``server/app/templates/reports/`` and receive only the
document. Every dynamic value is emitted by ``v(value, pointer)`` as
``<span data-k="/json/pointer">escaped text</span>``; source text is escaped
data, never markup. The page is self-contained (inline CSS, no script, no
external asset) and carries a restrictive Content-Security-Policy.
"""
from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, pass_context, select_autoescape
from markupsafe import Markup, escape

from .common import pointer_token, sha256_bytes
from .labels import label

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / 'templates' / 'reports'
BASE_TEMPLATE = 'base.html'


def fmt(value, lang: str) -> str:
    """Deterministic text of a JSON value (tests compare spans with it)."""
    if value is None:
        return label(lang, 'null')
    if value is True or value is False:
        return label(lang, 'true' if value else 'false')
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value, allow_nan=False)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


@pass_context
def _value(context, value, pointer: str, css: str | None = None) -> Markup:
    lang = context['doc']['language']
    classes = f' class="v-{escape(css)}"' if css else ''
    return Markup(f'<span data-k="{escape(pointer)}"{classes}>{escape(fmt(value, lang))}</span>')


def environment() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(['html']),
                      undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    env.globals['v'] = _value
    env.filters['pt'] = pointer_token
    return env


def template_files(report_template: str) -> list[dict]:
    """Template files used by a report type, with their SHA-256 (for the method)."""
    names = [BASE_TEMPLATE, report_template] if report_template != BASE_TEMPLATE else [BASE_TEMPLATE]
    return [{'name': f'reports/{name}', 'sha256': sha256_bytes((TEMPLATE_DIR / name).read_bytes())} for name in names]


def render_html(document: dict, report_template: str) -> bytes:
    return environment().get_template(report_template).render(doc=document).encode('utf-8')

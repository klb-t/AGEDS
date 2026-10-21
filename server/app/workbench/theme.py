"""WCAG 2.x contrast of the theme text tokens, computed from ``static/app.css``.

The CSS file is the single source of the colours; this module only parses the
two token blocks (dark default and explicit light) and computes ratios for the
foreground/background pairs that carry text. Alpha colours are not text tokens
and are not evaluated.
"""
from __future__ import annotations

import re
from pathlib import Path

CSS_PATH = Path(__file__).resolve().parents[1] / 'static' / 'app.css'
AA_NORMAL = 4.5
_DARK_BLOCK = re.compile(r'^:root\{(.*?)\}', re.S | re.M)
_LIGHT_BLOCK = re.compile(r':root\[data-theme-support~="light"\]\[data-theme="light"\]\{(.*?)\}', re.S)
_MEDIA_LIGHT = re.compile(r':root\[data-theme-support~="light"\]:not\(\[data-theme="dark"\]\)\{(.*?)\}', re.S)
_TOKEN = re.compile(r'--([a-z0-9-]+):\s*(#[0-9a-fA-F]{3,8})')

TEXT_PAIRS = (
    ('text', 'bg'), ('text', 'surface'), ('text', 'input-bg'), ('text', 'neutral-button'),
    ('muted', 'bg'), ('muted', 'surface'), ('muted', 'input-bg'),
    ('link', 'bg'), ('link', 'surface'),
    ('code', 'bg'), ('code', 'surface'),
    ('on-accent', 'accent'), ('on-accent', 'accent-hover'),
    ('mark-text', 'mark-bg'),
    ('danger', 'surface'), ('warning', 'surface'), ('ok', 'surface'),
    ('badge-raw', 'surface'), ('badge-parser', 'surface'), ('badge-model', 'surface'),
    ('badge-human', 'surface'), ('badge-unclassified', 'surface'),
)


def _tokens(block: str) -> dict[str, str]:
    return {name: value.lower() for name, value in _TOKEN.findall(block)}


def theme_tokens(css: str | None = None) -> dict[str, dict[str, str]]:
    css = CSS_PATH.read_text(encoding='utf-8') if css is None else css
    dark, light, media = _DARK_BLOCK.search(css), _LIGHT_BLOCK.search(css), _MEDIA_LIGHT.search(css)
    if not (dark and light and media):
        raise ValueError('app.css lacks the dark, explicit-light or prefers-color-scheme token block')
    result = {'dark': _tokens(dark.group(1)), 'light': _tokens(light.group(1))}
    if _tokens(media.group(1)) != result['light']:
        raise ValueError('prefers-color-scheme light tokens differ from the explicit light tokens')
    return result


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    digits = hex_colour.lstrip('#')
    if len(digits) == 3:
        digits = ''.join(ch * 2 for ch in digits)
    if len(digits) != 6:
        raise ValueError(f'opaque #rgb or #rrggbb colour required, got {hex_colour}')
    r, g, b = (int(digits[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(foreground: str, background: str) -> float:
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def contrast_report(css: str | None = None) -> dict:
    themes = theme_tokens(css)
    report = {'standard': 'WCAG 2.x relative luminance contrast', 'threshold_normal_text': AA_NORMAL, 'themes': {}}
    for name, tokens in themes.items():
        rows = []
        for fg, bg in TEXT_PAIRS:
            ratio = contrast(tokens[fg], tokens[bg])
            rows.append({'foreground': fg, 'background': bg, 'fg': tokens[fg], 'bg': tokens[bg],
                         'ratio': round(ratio, 2), 'aa_normal_text': ratio >= AA_NORMAL})
        report['themes'][name] = {'pairs': rows, 'all_aa': all(r['aa_normal_text'] for r in rows),
                                  'minimum': min(rows, key=lambda r: r['ratio'])}
    return report

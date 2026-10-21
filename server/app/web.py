"""Shared HTML rendering: one template environment and a data-driven navigation.

Feature routers append ``(path, label)`` pairs to their module-level ``NAV``;
``api.include_routers`` collects them, so adding a page never edits another
area's template.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=Path(__file__).parent / 'templates')
NAV: list[tuple[str, str]] = [('/', 'Start')]
templates.env.globals['ageds_nav'] = NAV

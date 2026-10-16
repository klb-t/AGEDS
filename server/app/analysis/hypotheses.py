"""Hypothesis analyzer core: fixed disclaimer label, hashing, event selectors.

This layer is interpretive. Every section it produces carries ``DISCLAIMER``
(Polish sentence fixed by the task, English variant) together with the SHA-256
of each sentence, so a report reader can check that the label was not edited.
Selectors only filter observed events; they never create or reinterpret them.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from ..exchange import CANONICALIZATION, canonical_json

DISCLAIMER_ID = 'ageds.analysis-disclaimer/v1'
DISCLAIMER_PL = 'Wynik analizatora jest materiałem pomocniczym dla człowieka/biegłego, nie wnioskiem przyczynowym.'
DISCLAIMER_EN = "The analyzer's result is auxiliary material for a human/expert, not a causal conclusion."


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sha256_canonical(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


DISCLAIMER = {
    'id': DISCLAIMER_ID,
    'pl': DISCLAIMER_PL,
    'en': DISCLAIMER_EN,
    'sha256_pl': sha256_text(DISCLAIMER_PL),
    'sha256_en': sha256_text(DISCLAIMER_EN),
}


def disclaimer() -> dict:
    """A fresh copy so callers cannot mutate the shared label."""
    return dict(DISCLAIMER)


class HypothesisError(ValueError):
    """Invalid hypothesis input or a refused analysis (reported to the caller)."""


class HypothesisNotFound(LookupError):
    """A referenced hypothesis, version, run or dossier record does not exist."""


MAX_ACTOR_CHARS = 100
MAX_TEXT_CHARS = 20_000


def utc_now() -> str:
    """Fixed-width UTC timestamp (always microseconds) so text order equals time order."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def clean_actor(actor) -> str:
    """Self-declared actor label; the service has no authentication (see HANDOFF)."""
    if actor is None:
        return 'unknown'
    if not isinstance(actor, str) or not actor.strip() or len(actor) > MAX_ACTOR_CHARS or '\x00' in actor:
        raise HypothesisError(f'actor must be nonblank text up to {MAX_ACTOR_CHARS} characters')
    return actor.strip()


def clean_text(value, name: str, *, max_chars: int = MAX_TEXT_CHARS, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > max_chars or '\x00' in value:
        raise HypothesisError(f'{name} must be {"nonblank " if required else ""}text up to {max_chars} characters')
    return value


@lru_cache(maxsize=1)
def code_version() -> str:
    """``git describe --always --dirty --abbrev=40`` of this checkout, else ``unknown``."""
    root = Path(__file__).resolve().parents[3]
    try:
        out = subprocess.run(['git', '-C', str(root), 'describe', '--always', '--dirty', '--abbrev=40'],
                             capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    value = out.stdout.strip()
    return value if out.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}(-dirty)?', value) else 'unknown'


# ---------------------------------------------------------------- selectors

SELECTOR_TEXT_LISTS = ('event_types', 'exclude_event_types', 'tags_any', 'tags_all', 'counterparties',
                       'chatbots', 'directions', 'categories')
SELECTOR_INT_LISTS = ('collection_ids',)
SELECTOR_KEYS = SELECTOR_TEXT_LISTS + SELECTOR_INT_LISTS + ('text_query',)
MAX_SELECTOR_VALUES = 500
MAX_SELECTOR_VALUE_CHARS = 500
MAX_SQL_INTEGER = 2**63 - 1


def normalize_selector(value, name: str, *, require_criterion: bool) -> dict:
    """Validate an event filter. Lists are sets (sorted, unique) so order never changes a hash.

    Criteria combine with AND; values inside one list combine with OR. ``{}``
    selects every timed event (useful for documentation density).
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise HypothesisError(f'{name} must be an object')
    unknown = sorted(set(value) - set(SELECTOR_KEYS))
    if unknown:
        raise HypothesisError(f'{name} has unknown keys: {", ".join(unknown)}')
    result: dict = {}
    for key in SELECTOR_TEXT_LISTS:
        if key not in value:
            continue
        items = value[key]
        if (not isinstance(items, list) or not items or len(items) > MAX_SELECTOR_VALUES
                or any(not isinstance(v, str) or not v or len(v) > MAX_SELECTOR_VALUE_CHARS or '\x00' in v for v in items)):
            raise HypothesisError(f'{name}.{key} must be a nonempty list of up to {MAX_SELECTOR_VALUES} nonempty strings')
        result[key] = sorted(set(items))
    for key in SELECTOR_INT_LISTS:
        if key not in value:
            continue
        items = value[key]
        if (not isinstance(items, list) or not items or len(items) > MAX_SELECTOR_VALUES
                or any(type(v) is not int or not 1 <= v <= MAX_SQL_INTEGER for v in items)):
            raise HypothesisError(f'{name}.{key} must be a nonempty list of positive integers')
        result[key] = sorted(set(items))
    if 'text_query' in value:
        query = value['text_query']
        if not isinstance(query, str) or not query.strip():
            raise HypothesisError(f'{name}.text_query must be nonblank FTS5 text')
        from ..search import SearchQueryError, validate_query
        try:
            validate_query(query, 1)
        except SearchQueryError as exc:
            raise HypothesisError(f'{name}.text_query: {exc}') from exc
        result['text_query'] = query
    if require_criterion and not result:
        raise HypothesisError(f'{name} needs at least one criterion')
    return result


def selector_matches(item: dict, selector: dict, *, fts_ids: set | None = None, category_ids: set | None = None) -> bool:
    if 'event_types' in selector and item['event_type'] not in selector['event_types']:
        return False
    if 'exclude_event_types' in selector and item['event_type'] in selector['exclude_event_types']:
        return False
    tags = set(item.get('tags') or ())
    if 'tags_any' in selector and not tags.intersection(selector['tags_any']):
        return False
    if 'tags_all' in selector and not set(selector['tags_all']) <= tags:
        return False
    if 'counterparties' in selector and item.get('counterparty') not in selector['counterparties']:
        return False
    if 'chatbots' in selector and item.get('chatbot') not in selector['chatbots']:
        return False
    if 'directions' in selector and item.get('direction') not in selector['directions']:
        return False
    if 'collection_ids' in selector and item.get('collection_id') not in selector['collection_ids']:
        return False
    if 'text_query' in selector and item['event_id'] not in (fts_ids or set()):
        return False
    if 'categories' in selector and item['event_id'] not in (category_ids or set()):
        return False
    return True


FTS_MAX_VM_STEPS = 20_000_000


def fts_event_ids(db: sqlite3.Connection, query: str, *, max_ids: int) -> tuple[set[int], bool]:
    """Event ids whose indexed title/body match ``query``; ``truncated`` when over ``max_ids``."""
    from ..search import SearchQueryError, validate_query
    try:
        validate_query(query, 1)
    except SearchQueryError as exc:
        raise HypothesisError(f'text_query: {exc}') from exc
    steps = [0]

    def progress():
        steps[0] += 1000
        return int(steps[0] >= FTS_MAX_VM_STEPS)
    db.set_progress_handler(progress, 1000)
    try:
        rows = db.execute("SELECT object_id FROM search_fts WHERE search_fts MATCH ? AND object_kind='event' LIMIT ?",
                          (query, max_ids + 1)).fetchall()
    except sqlite3.OperationalError as exc:
        if steps[0] >= FTS_MAX_VM_STEPS:
            raise HypothesisError('text_query exceeded the SQLite work budget; narrow the query') from exc
        raise
    finally:
        db.set_progress_handler(None, 0)
    ids = {int(r[0]) for r in rows[:max_ids]}
    return ids, len(rows) > max_ids


def category_adapter_status() -> dict:
    """Whether category selectors can be evaluated through the time-budget track's labeller."""
    try:
        from . import categories
    except ImportError as exc:
        return {'status': 'unavailable', 'reason': f'analysis.categories is not importable: {exc}'}
    missing = [name for name in ('label_items', 'load_effective_annotations', 'load_config', 'resolve_config_path',
                                 'parse_config', 'labels_sha256', 'needs_fields') if not hasattr(categories, name)]
    if missing:
        return {'status': 'unavailable', 'reason': 'analysis.categories lacks ' + ', '.join(missing)}
    return {'status': 'available', 'module': 'analysis.categories', 'labeller': getattr(categories, 'LABELLER_ID', 'unknown')}


def resolve_category_config(registered_sha256: str | None, supplied=None):
    """Return the configuration whose canonical hash equals the registered one, or refuse.

    ``supplied`` is a loaded ``CategoryConfig``, an inline dict or None (then the
    ``AGEDS_ACTIVITY_CATEGORIES`` file). The configuration is private user data;
    only its hash is part of the registered definition.
    """
    if registered_sha256 is None:
        return None
    status = category_adapter_status()
    if status['status'] != 'available':
        raise HypothesisError('category selector cannot be evaluated: ' + status['reason'])
    from . import categories
    try:
        if supplied is None:
            path = categories.resolve_config_path(None)
            if path is None:
                raise HypothesisError('category selectors need the private category configuration '
                                      f'({categories.ENV_VAR} or an explicit configuration); none was given')
            config = categories.load_config(path)
        elif isinstance(supplied, dict):
            config = categories.parse_config(supplied, origin='inline')
        elif isinstance(supplied, (str, Path)):
            config = categories.load_config(supplied)
        else:
            config = supplied
    except (ValueError, OSError) as exc:
        raise HypothesisError(f'category configuration: {exc}') from exc
    if config.sha256 != registered_sha256:
        raise HypothesisError(f'category configuration hash {config.sha256} differs from the registered '
                              f'{registered_sha256}; register a new hypothesis version to use another configuration')
    return config


def category_event_ids(db: sqlite3.Connection, items: list[dict], names: list[str], config) -> tuple[set[int], dict]:
    """Events whose effective label (human correction > rule > default) includes any of ``names``.

    Model labels are never used here: no model-label run is opted into by a hypothesis run.
    """
    from . import categories
    annotations = categories.load_effective_annotations(db, [i['event_id'] for i in items])
    labels = categories.label_items(items, config, annotations=annotations)
    wanted = set(names)
    ids = {eid for eid, label in labels.items() if wanted & set(label['categories'])}
    paths: dict[str, int] = {}
    for eid in ids:
        paths[labels[eid]['path']] = paths.get(labels[eid]['path'], 0) + 1
    return ids, {'labels_sha256': categories.labels_sha256(labels), 'selected_by_path': dict(sorted(paths.items())),
                 'model_labels': 'not used'}


__all__ = ['CANONICALIZATION', 'DISCLAIMER', 'HypothesisError', 'HypothesisNotFound', 'canonical_json',
           'clean_actor', 'clean_text', 'code_version', 'disclaimer', 'normalize_selector', 'selector_matches',
           'sha256_canonical', 'sha256_text', 'utc_now']

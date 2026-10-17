"""Activity categories from a private configuration and labelled events (stable API).

The category configuration is user data. It lives outside the repository
(``--config PATH``, the ``AGEDS_ACTIVITY_CATEGORIES`` environment variable, or
an inline JSON object over HTTP); ``data/categories.example.json`` is only a
generic example. Each result records the configuration's canonical hash and
declared version.

Labelling paths, in precedence order (each label states its path):

1. ``annotation`` — the latest human correction for the event (append-only
   table ``analysis_label_annotations``); an empty correction withdraws the
   earlier one and lets the automatic paths apply again;
2. ``rule`` — deterministic rules (counterparty dictionary, e-mail domains,
   keywords, chatbot, tags, thread/conversation ids, event types) with
   rule-given weights; rule id and rule version are kept per label;
3. ``model`` — a model-label hypothesis, used only when a run explicitly opts
   in to one stored model-label run and only for events no rule matched;
4. ``default`` — the configured default category, or ``bez_kategorii``.

Weights are shares of an event's time in ``(0, 1]``. They are not normalized:
``{"a": 1, "b": 1}`` means the event counts fully in both, and the budget's
overlap rule then decides how simultaneous categories share time.

``nieobserwowane`` (time without any observation) and ``bez_kategorii``
(observed but unlabelled) are reserved and always present; no rule or
correction can assign an observed event to ``nieobserwowane``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .provenance import canonical_json, sha256_json

CONFIG_SCHEMA = 'ageds.activity_categories.v1'
ENV_VAR = 'AGEDS_ACTIVITY_CATEGORIES'
LABELLER_ID = 'category_rules.v1'
UNOBSERVED = 'nieobserwowane'
UNLABELLED = 'bez_kategorii'
RESERVED = (UNOBSERVED, UNLABELLED)
RESERVED_COLORS = {UNOBSERVED: '#8b949e', UNLABELLED: '#6e7681'}
# Validated categorical order (dataviz reference palette, dark steps; checked against the #161b22 panel).
DEFAULT_COLORS = ('#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767')
OVERFLOW_COLOR = '#9da7b3'
RULE_KINDS = ('counterparty', 'email_domain', 'keyword', 'chatbot', 'tag', 'thread', 'event_type', 'signal_basis')
COMBINE_MODES = ('max', 'first')
PATHS = ('annotation', 'rule', 'model', 'default')
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_RULE_VALUES = 100_000
_CATEGORY_ID = re.compile(r'^[a-z0-9][a-z0-9_]{0,63}$')
_RULE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
_ADDRESS = re.compile(r'[^\s<>,;"\'()\[\]]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')
_PHONEISH = re.compile(r'^[+\d\s().\-/]{5,}$')
_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')
DEFAULT_KEYWORD_FIELDS = ('subject', 'body')
KEYWORD_FIELDS = ('subject', 'body', 'sender', 'recipients', 'contact_label', 'phone_or_address')


def fold(text: str) -> str:
    """Case-fold and NFKC-normalize; diacritics are kept (``ł`` != ``l``)."""
    return unicodedata.normalize('NFKC', text).casefold()


def normalize_counterparty(value: str) -> str:
    """Compare phone-like values by digits (leading ``+`` kept), others case-folded."""
    text = fold(value).strip()
    if _PHONEISH.match(text):
        digits = re.sub(r'\D', '', text)
        return ('+' if text.startswith('+') else '') + digits
    return text


@dataclass(frozen=True)
class Rule:
    id: str
    version: int
    kind: str
    values: tuple[str, ...]
    weights: dict[str, float]
    fields: tuple[str, ...] = DEFAULT_KEYWORD_FIELDS
    whole_word: bool = False
    event_types: tuple[str, ...] | None = None
    _matchers: tuple = field(default=(), compare=False, repr=False)

    def provenance(self) -> dict:
        return {'rule_id': self.id, 'rule_version': self.version, 'kind': self.kind, 'weights': dict(self.weights)}


@dataclass(frozen=True)
class CategoryConfig:
    data: dict
    sha256: str
    file_sha256: str | None
    version: str
    origin: str
    categories: tuple[dict, ...]
    rules: tuple[Rule, ...]
    combine: str
    default_category: str
    official_categories: tuple[str, ...]

    @property
    def category_ids(self) -> tuple[str, ...]:
        return tuple(c['id'] for c in self.categories)

    def describe(self) -> dict:
        """What a result records about its configuration (no file path)."""
        return {'schema': CONFIG_SCHEMA, 'sha256': self.sha256, 'file_sha256': self.file_sha256,
                'version': self.version, 'origin': self.origin, 'labeller': LABELLER_ID,
                'categories': [dict(c) for c in self.categories], 'combine': self.combine,
                'default_category': self.default_category, 'official_categories': list(self.official_categories),
                'rules': [{**r.provenance(), 'values_count': len(r.values),
                           **({'fields': list(r.fields), 'whole_word': r.whole_word} if r.kind == 'keyword' else {}),
                           **({'event_types': list(r.event_types)} if r.event_types else {})} for r in self.rules]}


def _weights(raw, where: str, known: set[str]) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f'{where}: weights must be a non-empty object')
    out = {}
    for category, weight in raw.items():
        if category in RESERVED:
            raise ValueError(f'{where}: reserved category {category!r} cannot be assigned')
        if category not in known:
            raise ValueError(f'{where}: unknown category {category!r}')
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < weight <= 1:
            raise ValueError(f'{where}: weight for {category!r} must be a number in (0, 1]')
        out[category] = float(weight)
    return dict(sorted(out.items()))


def _string_list(raw, where: str, *, limit: int = MAX_RULE_VALUES) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > limit:
        raise ValueError(f'{where}: must be a non-empty list of at most {limit} strings')
    if not all(isinstance(v, str) and v.strip() for v in raw):
        raise ValueError(f'{where}: every value must be a non-blank string')
    return tuple(raw)


def _compile(kind: str, values: tuple[str, ...], whole_word: bool) -> tuple:
    if kind == 'counterparty':
        return (frozenset(normalize_counterparty(v) for v in values),)
    if kind == 'email_domain':
        return (tuple(sorted({fold(v).strip().lstrip('@').rstrip('.') for v in values})),)
    if kind == 'keyword':
        folded = sorted({fold(v) for v in values})
        if whole_word:
            return (re.compile('|'.join(r'(?<!\w)' + re.escape(v) + r'(?!\w)' for v in folded)),)
        return (tuple(folded),)
    if kind in ('chatbot', 'tag', 'event_type', 'signal_basis'):
        return (frozenset(fold(v) for v in values),)
    return (frozenset(values),)  # thread ids compare exactly


def parse_config(data: dict, *, origin: str = 'inline', file_sha256: str | None = None) -> CategoryConfig:
    if not isinstance(data, dict):
        raise ValueError('category configuration must be a JSON object')
    if data.get('schema') != CONFIG_SCHEMA:
        raise ValueError(f'category configuration schema must be {CONFIG_SCHEMA!r}')
    version = data.get('version', 'unversioned')
    if not isinstance(version, str) or not version.strip():
        raise ValueError('version must be a non-blank string')
    raw_categories = data.get('categories')
    if not isinstance(raw_categories, list) or not raw_categories:
        raise ValueError('categories must be a non-empty list')
    categories, known = [], set()
    for index, c in enumerate(raw_categories):
        where = f'categories[{index}]'
        if not isinstance(c, dict) or not isinstance(c.get('id'), str) or not _CATEGORY_ID.match(c['id']):
            raise ValueError(f'{where}: id must match {_CATEGORY_ID.pattern}')
        if c['id'] in RESERVED:
            raise ValueError(f'{where}: {c["id"]!r} is reserved and always present')
        if c['id'] in known:
            raise ValueError(f'{where}: duplicate category {c["id"]!r}')
        color = c.get('color')
        if color is not None and (not isinstance(color, str) or not _COLOR.match(color)):
            raise ValueError(f'{where}: color must be #rrggbb')
        label = c.get('label', c['id'])
        if not isinstance(label, str):
            raise ValueError(f'{where}: label must be a string')
        known.add(c['id'])
        categories.append({'id': c['id'], 'label': label, 'color': color})
    combine = data.get('combine', 'max')
    if combine not in COMBINE_MODES:
        raise ValueError(f'combine must be one of {COMBINE_MODES}')
    default = data.get('default_category')
    if default is None:
        default = UNLABELLED
    elif default not in known:
        raise ValueError('default_category must be a declared category or null')
    official = data.get('official_categories', [])
    if not isinstance(official, list) or any(o not in known for o in official):
        raise ValueError('official_categories must list declared categories')
    rules, rule_ids = [], set()
    raw_rules = data.get('rules', [])
    if not isinstance(raw_rules, list):
        raise ValueError('rules must be a list')
    for index, r in enumerate(raw_rules):
        where = f'rules[{index}]'
        if not isinstance(r, dict):
            raise ValueError(f'{where}: must be an object')
        rid = r.get('id')
        if not isinstance(rid, str) or not _RULE_ID.match(rid):
            raise ValueError(f'{where}: id must match {_RULE_ID.pattern}')
        if rid in rule_ids:
            raise ValueError(f'{where}: duplicate rule id {rid!r}')
        rule_ids.add(rid)
        rversion = r.get('version', 1)
        if isinstance(rversion, bool) or not isinstance(rversion, int) or rversion < 1:
            raise ValueError(f'{where}: version must be a positive integer')
        kind = r.get('kind')
        if kind not in RULE_KINDS:
            raise ValueError(f'{where}: kind must be one of {RULE_KINDS}')
        values = _string_list(r.get('values'), f'{where}.values')
        weights = _weights(r.get('weights'), f'{where}.weights', known)
        fields = tuple(r.get('fields', DEFAULT_KEYWORD_FIELDS))
        if kind == 'keyword' and (not fields or any(f not in KEYWORD_FIELDS for f in fields)):
            raise ValueError(f'{where}: fields must be a subset of {KEYWORD_FIELDS}')
        whole_word = r.get('whole_word', False)
        if not isinstance(whole_word, bool):
            raise ValueError(f'{where}: whole_word must be true/false')
        event_types = r.get('event_types')
        if event_types is not None:
            event_types = _string_list(event_types, f'{where}.event_types', limit=1000)
        rules.append(Rule(rid, rversion, kind, values, weights, fields, whole_word, event_types,
                          _compile(kind, values, whole_word)))
    canonical = json.loads(canonical_json(data))
    return CategoryConfig(canonical, sha256_json(canonical), file_sha256, version, origin, tuple(categories),
                          tuple(rules), combine, default, tuple(official))


def resolve_config_path(explicit: str | os.PathLike | None = None) -> Path | None:
    """CLI/API argument first, then ``AGEDS_ACTIVITY_CATEGORIES``; None when neither is set."""
    if explicit:
        return Path(explicit)
    env = os.environ.get(ENV_VAR)
    return Path(env) if env else None


def load_config(path: str | os.PathLike) -> CategoryConfig:
    p = Path(path)
    size = p.stat().st_size
    if size > MAX_CONFIG_BYTES:
        raise ValueError(f'category configuration exceeds {MAX_CONFIG_BYTES} bytes')
    raw = p.read_bytes()
    try:
        data = json.loads(raw.decode('utf-8'), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f'category configuration is not valid UTF-8 JSON: {exc}') from exc
    return parse_config(data, origin='file', file_sha256=hashlib.sha256(raw).hexdigest())


# ---------------------------------------------------------------- matching

def _addresses(item: dict) -> list[str]:
    f = item.get('fields') or {}
    found = []
    for key in ('phone_or_address', 'sender', 'recipients', 'contact_label'):
        value = f.get(key)
        if isinstance(value, str):
            found.extend(m.group(0) for m in _ADDRESS.finditer(value))
    return found


def _rule_matches(rule: Rule, item: dict) -> bool:
    if rule.event_types and item.get('event_type') not in rule.event_types:
        return False
    f = item.get('fields') or {}
    matcher = rule._matchers[0]
    if rule.kind == 'counterparty':
        candidates = [item.get('counterparty'), f.get('contact_label'), f.get('phone_or_address')]
        candidates += _addresses(item)
        return any(isinstance(c, str) and normalize_counterparty(c) in matcher for c in candidates)
    if rule.kind == 'email_domain':
        for address in _addresses(item):
            domain = fold(address.rsplit('@', 1)[1]).rstrip('.')
            if any(domain == d or domain.endswith('.' + d) for d in matcher):
                return True
        return False
    if rule.kind == 'keyword':
        for name in rule.fields:
            text = f.get(name)
            if not isinstance(text, str) or not text:
                continue
            folded = fold(text)
            if rule.whole_word:
                if matcher.search(folded):
                    return True
            elif any(k in folded for k in matcher):
                return True
        return False
    if rule.kind == 'chatbot':
        return isinstance(item.get('chatbot'), str) and fold(item['chatbot']) in matcher
    if rule.kind == 'tag':
        return any(fold(t) in matcher for t in item.get('tags') or ())
    if rule.kind == 'thread':
        return item.get('thread_id') in matcher
    if rule.kind == 'event_type':
        return isinstance(item.get('event_type'), str) and fold(item['event_type']) in matcher
    if rule.kind == 'signal_basis':  # metadata.signal.basis (digital-traces contract, 2026-10-17)
        basis = (item.get('signal') or {}).get('basis')
        return isinstance(basis, str) and fold(basis) in matcher
    return False


def needs_fields(config: CategoryConfig) -> bool:
    return any(r.kind in ('keyword', 'email_domain') for r in config.rules)


def rule_label(item: dict, config: CategoryConfig) -> dict | None:
    """Combine matching rules; None when no rule matched."""
    hits = [r for r in config.rules if _rule_matches(r, item)]
    if not hits:
        return None
    if config.combine == 'first':
        hits = hits[:1]
    weights: dict[str, float] = {}
    for r in hits:
        for category, w in r.weights.items():
            weights[category] = max(weights.get(category, 0.0), w)
    return {'categories': dict(sorted(weights.items())), 'path': 'rule',
            'provenance': {'rules': [r.provenance() for r in hits], 'combine': config.combine}}


def _validated_categories(raw, config: CategoryConfig) -> tuple[dict[str, float], list[str]]:
    issues, out = [], {}
    for category, w in (raw or {}).items():
        if category == UNOBSERVED:
            issues.append('annotation_cannot_mark_observed_event_unobserved')
            continue
        if category not in config.category_ids and category != UNLABELLED:
            issues.append(f'category_not_in_config:{category}')
        out[category] = float(w)
    return dict(sorted(out.items())), issues


def label_items(items: list[dict], config: CategoryConfig, *, annotations: dict[int, dict] | None = None,
                model_labels: dict[int, dict] | None = None) -> dict[int, dict]:
    """Label every item; returns ``{event_id: label}``.

    ``annotations``: effective human corrections ``{event_id: {'annotation_id', 'categories'}}``
    (see ``load_effective_annotations``). ``model_labels``: ``{event_id: {'run_id', 'categories',
    'rationale'}}`` from one opted-in model-label run. Lower-precedence candidates stay visible in
    ``shadowed`` so a reader sees what a correction replaced.
    """
    if items and needs_fields(config) and any('fields' not in it for it in items):
        raise ValueError('these rules need text fields; load items with fields=True')
    annotations = annotations or {}
    model_labels = model_labels or {}
    labels = {}
    for it in items:
        eid = it['event_id']
        candidates = []
        note = annotations.get(eid)
        if note and note.get('categories'):
            cats, issues = _validated_categories(note['categories'], config)
            if cats:
                candidates.append({'categories': cats, 'path': 'annotation',
                                   'provenance': {'annotation_id': note['annotation_id']}, 'issues': issues})
        by_rule = rule_label(it, config)
        if by_rule:
            candidates.append(by_rule)
        model = model_labels.get(eid)
        if model and model.get('categories') and not by_rule:
            cats, issues = _validated_categories(model['categories'], config)
            if cats:
                candidates.append({'categories': cats, 'path': 'model',
                                   'provenance': {'model_run_id': model['run_id']}, 'issues': issues})
        candidates.append({'categories': {config.default_category: 1.0}, 'path': 'default',
                           'provenance': {'default_category': config.default_category}})
        chosen = dict(candidates[0])
        chosen['event_id'] = eid
        chosen.setdefault('issues', [])
        chosen['shadowed'] = [{'path': c['path'], 'categories': c['categories'], 'provenance': c['provenance']}
                              for c in candidates[1:] if c['path'] != 'default']
        labels[eid] = chosen
    return labels


def labels_sha256(labels: dict[int, dict]) -> str:
    return sha256_json([[eid, labels[eid]['path'], labels[eid]['categories'], labels[eid]['provenance']]
                        for eid in sorted(labels)])


def category_palette(config: CategoryConfig, extra: list[str] = ()) -> list[dict]:
    """Declared categories, then reserved ones, then any extra ids found in labels."""
    out = []
    for index, c in enumerate(config.categories):
        entry = dict(c)
        if not entry.get('color'):
            entry['color'] = DEFAULT_COLORS[index] if index < len(DEFAULT_COLORS) else OVERFLOW_COLOR
        out.append(entry)
    seen = {c['id'] for c in out}
    for rid in (UNLABELLED, UNOBSERVED):
        out.append({'id': rid, 'label': rid, 'color': RESERVED_COLORS[rid], 'reserved': True})
        seen.add(rid)
    for e in extra:
        if e not in seen:
            out.append({'id': e, 'label': e, 'color': OVERFLOW_COLOR, 'not_in_config': True})
            seen.add(e)
    return out


# ---------------------------------------------------------------- storage

def add_annotation(db: sqlite3.Connection, event_id: int, categories: dict | None, *, note: str = '',
                   author: str = 'unknown') -> int:
    """Append a human label correction; ``None``/``{}`` withdraws the previous one."""
    if not db.execute('SELECT 1 FROM events WHERE id=?', (event_id,)).fetchone():
        raise LookupError(f'event {event_id} not found')
    cats = {}
    for category, w in (categories or {}).items():
        if not isinstance(category, str) or not _CATEGORY_ID.match(category) or category == UNOBSERVED:
            raise ValueError(f'invalid category for an observed event: {category!r}')
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not 0 < w <= 1:
            raise ValueError(f'weight for {category!r} must be a number in (0, 1]')
        cats[category] = float(w)
    if not isinstance(note, str) or len(note) > 10_000 or not isinstance(author, str) or len(author) > 200:
        raise ValueError('note must be a string up to 10000 characters; author up to 200')
    cur = db.execute('INSERT INTO analysis_label_annotations(event_id,categories_json,note,author) VALUES(?,?,?,?)',
                     (event_id, canonical_json(dict(sorted(cats.items()))), note, author or 'unknown'))
    return int(cur.lastrowid)


def load_effective_annotations(db: sqlite3.Connection, event_ids=None) -> dict[int, dict]:
    """Latest correction per event (the newest id wins); withdrawals yield empty categories."""
    rows = db.execute('''SELECT a.* FROM analysis_label_annotations a
                         JOIN (SELECT event_id, MAX(id) AS id FROM analysis_label_annotations GROUP BY event_id) m
                         ON m.id=a.id ORDER BY a.event_id''').fetchall()
    wanted = None if event_ids is None else set(event_ids)
    out = {}
    for r in rows:
        if wanted is not None and r['event_id'] not in wanted:
            continue
        out[r['event_id']] = {'annotation_id': r['id'], 'categories': json.loads(r['categories_json']),
                              'note': r['note'], 'author': r['author'], 'created_at': r['created_at']}
    return out


def annotation_history(db: sqlite3.Connection, event_id: int) -> list[dict]:
    return [{'annotation_id': r['id'], 'categories': json.loads(r['categories_json']), 'note': r['note'],
             'author': r['author'], 'created_at': r['created_at']}
            for r in db.execute('SELECT * FROM analysis_label_annotations WHERE event_id=? ORDER BY id', (event_id,))]


def record_label_run(db: sqlite3.Connection, labels: dict[int, dict], config: CategoryConfig, *,
                     input_sha256: str, code_version: str, parameters: dict | None = None) -> dict:
    """Store labels append-only under a labelling run; an identical run is reused."""
    digest = labels_sha256(labels)
    params = canonical_json(parameters or {})
    existing = db.execute('''SELECT id FROM analysis_label_runs WHERE labels_sha256=? AND config_sha256=?
                             AND input_sha256=? AND code_version=? AND parameters_json=? ORDER BY id LIMIT 1''',
                          (digest, config.sha256, input_sha256, code_version, params)).fetchone()
    if existing:
        return {'label_run_id': existing[0], 'labels_sha256': digest, 'reused': True}
    cur = db.execute('''INSERT INTO analysis_label_runs(labeller,config_sha256,config_version,code_version,
                        parameters_json,input_sha256,labels_sha256,event_count) VALUES(?,?,?,?,?,?,?,?)''',
                     (LABELLER_ID, config.sha256, config.version, code_version, params, input_sha256, digest, len(labels)))
    run_id = int(cur.lastrowid)
    rows = []
    for eid in sorted(labels):
        lab = labels[eid]
        prov = lab['provenance']
        rule_ids = ','.join(f"{r['rule_id']}@{r['rule_version']}" for r in prov.get('rules', [])) or None
        for category, weight in lab['categories'].items():
            rows.append((run_id, eid, category, weight, lab['path'], rule_ids, prov.get('annotation_id'),
                         prov.get('model_run_id')))
    db.executemany('''INSERT INTO analysis_event_labels(label_run_id,event_id,category,weight,path,rules,
                      annotation_id,model_run_id) VALUES(?,?,?,?,?,?,?,?)''', rows)
    return {'label_run_id': run_id, 'labels_sha256': digest, 'reused': False}

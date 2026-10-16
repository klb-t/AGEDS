"""Versioned timeline rules stored as data files.

A rule is identified by ``rule_id``, ``version`` and the SHA-256 of its file
bytes. Released files are pinned in ``RELEASED_SHA256``: editing a released
file is refused at load time (add ``<rule_id>.v<N+1>.json`` instead), so a
materialized projection or a correlation run can always name the exact rule it
applied. Experimental rules (for tests or operator experiments) are loaded from
explicit data and must not reuse a released ``(rule_id, version)`` pair.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RULES_DIR = Path(__file__).parent / 'rules'
_FILE = re.compile(r'^(?P<rule_id>[a-z][a-z0-9_]{0,63})\.v(?P<version>[1-9][0-9]{0,5})\.json$')
_REF = re.compile(r'^(?P<rule_id>[a-z][a-z0-9_]{0,63})(?:@(?P<version>[1-9][0-9]{0,5}))?$')

# Pinned hashes of released rule files. A mismatch means a released rule was
# edited in place; that is refused rather than silently re-labelled.
RELEASED_SHA256: dict[tuple[str, int], str] = {
    ('display_time', 1): 'b977c01deff300a8db32b5a767a3902f7c652d45c832bcaf2aa2b52c47810978',
    ('email_chat', 1): 'ee6a9fe20e557b5726fe46133eda219c34e1e62095f8087370b1497847cc141c',
    ('recording_call_log', 1): 'f7ef7fee08419216263a25803681268748a858f17d005f8605eef0bf3f351d37',
    ('same_conversation', 1): '0323cfe6de7e719f96406d91633f19e61027832e2d533b5ff3d1919ac67887c5',
}


class RuleError(ValueError):
    """Unknown rule, invalid rule data or invalid parameters (HTTP 422)."""


class RuleIntegrityError(RuntimeError):
    """A released rule file no longer matches its pinned hash."""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: int
    kind: str
    sha256: str
    data: dict = field(compare=False, hash=False, repr=False)
    origin: str = 'released'

    @property
    def ref(self) -> str:
        return f'{self.rule_id}@{self.version}'

    def describe(self) -> dict:
        return {'rule_id': self.rule_id, 'version': self.version, 'kind': self.kind,
                'sha256': self.sha256, 'origin': self.origin}


def _validate(data: dict, *, sha256: str, origin: str) -> Rule:
    if not isinstance(data, dict):
        raise RuleError('rule must be a JSON object')
    rule_id, version, kind = data.get('rule_id'), data.get('version'), data.get('kind')
    if not isinstance(rule_id, str) or not _REF.match(rule_id) or '@' in rule_id:
        raise RuleError('rule_id must be a lowercase identifier')
    if type(version) is not int or not 1 <= version <= 999_999:
        raise RuleError('rule version must be a positive integer')
    if kind not in ('display_rule', 'correlation_rule'):
        raise RuleError('rule kind must be display_rule or correlation_rule')
    if kind == 'display_rule':
        for key in ('kind_priority', 'origin_priority', 'tz_status_priority', 'precision_priority',
                    'unknown_zone', 'legacy_derivation', 'lanes', 'recording'):
            if key not in data:
                raise RuleError(f'display rule lacks {key}')
        if 'default' not in data['kind_priority']:
            raise RuleError('display rule kind_priority needs a default list')
        zone = data['unknown_zone']
        low, high = zone.get('offset_hours_min'), zone.get('offset_hours_max')
        if not all(isinstance(v, (int, float)) for v in (low, high)) or not -24 <= low <= high <= 24:
            raise RuleError('unknown_zone offsets must satisfy -24 <= min <= max <= 24')
    else:
        if not isinstance(data.get('parameters'), dict) or not isinstance(data.get('relation'), str):
            raise RuleError('correlation rule needs parameters and relation')
    return Rule(rule_id, version, kind, sha256, data, origin)


def _canonical_sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def released_rules() -> list[Rule]:
    rules = []
    for path in sorted(RULES_DIR.glob('*.json')):
        match = _FILE.match(path.name)
        if not match:
            continue
        raw = path.read_bytes()
        digest = _canonical_sha(raw)
        rule = _validate(json.loads(raw), sha256=digest, origin='released')
        if (rule.rule_id, rule.version) != (match['rule_id'], int(match['version'])):
            raise RuleIntegrityError(f'{path.name} declares {rule.ref}')
        pinned = RELEASED_SHA256.get((rule.rule_id, rule.version))
        if pinned is not None and pinned != digest:
            raise RuleIntegrityError(f'released rule {rule.ref} was edited in place; add a new version file instead')
        rules.append(rule)
    return rules


def load_rule(ref: str | None = None, *, kind: str | None = None) -> Rule:
    """``display_time`` (latest released version) or ``display_time@1``."""
    if ref is None:
        if kind != 'display_rule':
            raise RuleError('rule reference is required')
        ref = 'display_time'
    if not isinstance(ref, str):
        raise RuleError('rule reference must be text')
    match = _REF.match(ref.strip())
    if not match:
        raise RuleError('rule reference must look like rule_id or rule_id@version')
    candidates = [r for r in released_rules() if r.rule_id == match['rule_id']]
    if match['version']:
        candidates = [r for r in candidates if r.version == int(match['version'])]
    if kind:
        candidates = [r for r in candidates if r.kind == kind]
    if not candidates:
        raise RuleError(f'unknown rule: {ref}')
    return max(candidates, key=lambda r: r.version)


def experimental_rule(data: dict) -> Rule:
    """A rule supplied as data (tests, operator experiments); never a released id/version."""
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    rule = _validate(json.loads(raw), sha256=_canonical_sha(raw), origin='experimental')
    if any((r.rule_id, r.version) == (rule.rule_id, rule.version) for r in released_rules()):
        raise RuleError(f'{rule.ref} is a released rule; experimental data must use a new version')
    return rule


def load_rule_file(path: Path) -> Rule:
    try:
        data = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise RuleError(f'cannot read rule file: {exc}') from exc
    return experimental_rule(data)


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _zone(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 64:
        raise RuleError('zone must be an IANA zone name')
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuleError(f'unknown zone: {name}') from exc
    return name


def display_params(assume_zone: str | None = None, assumption_rule: str | None = None,
                   zone_by_source: dict | None = None) -> dict:
    """Canonical operator parameters of a display projection.

    An assumed zone is never applied without a named assumption, so every
    projected instant that depends on it can be traced to that decision.
    """
    params: dict = {}
    if assume_zone or zone_by_source:
        if not isinstance(assumption_rule, str) or not assumption_rule.strip() or len(assumption_rule) > 200:
            raise RuleError('an assumed zone requires a non-empty assumption_rule id (at most 200 characters)')
        params['assumption_rule'] = assumption_rule.strip()
    elif assumption_rule:
        raise RuleError('assumption_rule given without assume_zone or zone_by_source')
    if assume_zone:
        params['assume_zone'] = _zone(assume_zone)
    if zone_by_source:
        if not isinstance(zone_by_source, dict) or len(zone_by_source) > 1000:
            raise RuleError('zone_by_source must map at most 1000 source ids to zones')
        mapping = {}
        for key, value in zone_by_source.items():
            try:
                source_id = int(key)
            except (TypeError, ValueError) as exc:
                raise RuleError('zone_by_source keys must be source ids') from exc
            if source_id < 1:
                raise RuleError('zone_by_source keys must be positive source ids')
            mapping[str(source_id)] = _zone(value)
        params['zone_by_source'] = dict(sorted(mapping.items(), key=lambda kv: int(kv[0])))
    return params


def params_sha256(params: dict) -> str:
    return hashlib.sha256(canonical_json(params).encode('utf-8')).hexdigest()

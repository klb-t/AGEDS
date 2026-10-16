"""Time candidates of timeline items and the versioned display selection.

Stored candidates come from ``event_times`` (append-only importer output).
Derived candidates are computed on the fly and labelled with ``origin`` and
``derived_from``; they are never written back as stored truth. The selection
applies one display rule version and reports the chosen candidate, its basis
and an uncertainty interval. Unknown zones stay unknown: they are sorted "as if
UTC" only together with an interval covering every real UTC offset.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from .. import timeinfo
from .ruleset import Rule
from .timeutil import US, iso, local_text_to_us, precision_span, utc_text_to_us

FLAG_KEYS = ('dst', 'utc_alternatives', 'date_order_ambiguous', 'ampm_ambiguous', 'hour_12h_ambiguous',
             'fraction_truncated_to_microseconds', 'two_digit_year_policy', 'assumption_rule', 'assumed_zone')
BASIS_RANK = {'explicit_offset': 0, 'utc_epoch': 1, 'assumed_zone': 2, 'assumed_zone_operator': 2,
              'local_as_utc_unknown_zone': 3}
_PHONE_LIKE = re.compile(r'^\+?[0-9 ().\-]{3,40}$')
_SEPARATORS = re.compile(r'[ ().\-]')


@dataclass
class Candidate:
    ref: str
    origin: str
    kind: str
    raw: str | None
    source: str
    utc: str | None = None
    local: str | None = None
    tz_status: str = 'unknown'
    tz: str | None = None
    precision: str = 'unknown'
    metadata: dict = field(default_factory=dict)
    derived_from: list | None = None
    usable: bool = True
    unusable_reason: str | None = None
    recorded_at: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Placement:
    sort_us: int
    lo_us: int
    hi_us: int
    basis: str
    precision: str
    flags: dict

    def as_dict(self) -> dict:
        return {'display': iso(self.sort_us), 'sort_us': self.sort_us, 'basis': self.basis,
                'precision': self.precision, 'interval': {'from': iso(self.lo_us), 'to': iso(self.hi_us)},
                'uncertainty_seconds': round((self.hi_us - self.lo_us) / US, 6), 'flags': self.flags}


def _from_timecandidate(tc: timeinfo.TimeCandidate, *, ref: str, origin: str, derived_from: list | None = None,
                        extra: dict | None = None) -> Candidate:
    meta = dict(tc.metadata)
    if extra:
        meta.update(extra)
    return Candidate(ref, origin, tc.kind, tc.raw, tc.source, tc.utc, tc.local, tc.tz_status, tc.tz,
                     tc.precision, meta, derived_from)


def stored_candidate(row, *, origin: str = 'stored', ref_prefix: str = 'stored') -> Candidate:
    try:
        metadata = json.loads(row['metadata_json'] or '{}')
        if not isinstance(metadata, dict):
            metadata = {'unparsed_metadata': row['metadata_json']}
    except (TypeError, ValueError):
        metadata = {'unparsed_metadata': row['metadata_json']}
    c = Candidate(f"{ref_prefix}:{row['id']}", origin, row['kind'], row['raw'], row['source'], row['utc'], row['local'],
                  row['tz_status'], row['tz'], row['precision'] or 'unknown', metadata,
                  recorded_at=row['created_at'])
    if not c.utc and not c.local:
        c.usable, c.unusable_reason = False, 'no_utc_or_local_value'
    return c


def _path(value, dotted: str):
    for part in dotted.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _epoch_ms(kind: str, raw, source: str, derived_from: list, ref: str) -> Candidate | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    if not isinstance(raw, str) or not re.fullmatch(r'-?\d{1,16}', raw.strip()):
        return None
    try:
        tc = timeinfo.from_epoch(kind, raw.strip(), source, unit='ms')
    except (ValueError, OverflowError):
        return None
    return _from_timecandidate(tc, ref=ref, origin='derived_legacy', derived_from=derived_from)


def _iso_candidate(kind: str, raw: str, source: str, ref: str, origin: str, derived_from: list) -> Candidate:
    try:
        tc = timeinfo.from_iso(kind, raw, source)
    except (ValueError, OverflowError):
        return Candidate(ref, origin, kind, raw, source, metadata={}, derived_from=derived_from,
                         usable=False, unusable_reason='unparseable')
    return _from_timecandidate(tc, ref=ref, origin=origin, derived_from=derived_from)


def legacy_event_candidates(event, metadata: dict, rule: Rule, *, origin: str = 'derived_legacy',
                            ref_prefix: str = 'derived') -> list[Candidate]:
    """Candidates computed from ``events`` columns and parser metadata of one event."""
    derivation = rule.data['legacy_derivation']
    spec = derivation.get(event['event_type']) or derivation['default']
    by_direction = spec.get('ts_start_kind_by_direction')
    if by_direction:
        start_kind = by_direction.get(event['direction'] or '', by_direction.get('*', 'other'))
    else:
        start_kind = spec.get('ts_start_kind', 'start')
    out: list[Candidate] = []
    ts_start, ts_end = event['ts_start'], event['ts_end']
    start_covered = False
    epoch_path = spec.get('epoch_ms_metadata')
    if epoch_path:
        raw = _path(metadata, epoch_path)
        c = _epoch_ms(start_kind, raw, f'derived:events.metadata_json:$.{epoch_path}',
                      [f'events.metadata_json:$.{epoch_path}'], f'{ref_prefix}:metadata.{epoch_path}')
        if c is not None:
            c.origin = origin
            if ts_start:
                try:
                    if utc_text_to_us(ts_start) == utc_text_to_us(c.utc):
                        c.derived_from.append('events.ts_start')
                        start_covered = True
                except ValueError:
                    pass
            out.append(c)
    for attribute, kind in (spec.get('epoch_ms_raw_attributes') or {}).items():
        raw = _path(metadata, f'raw_attributes.{attribute}')
        if isinstance(raw, str) and raw.strip() not in ('', '0'):
            c = _epoch_ms(kind, raw, f'derived:events.metadata_json:$.raw_attributes.{attribute}',
                          [f'events.metadata_json:$.raw_attributes.{attribute}'],
                          f'{ref_prefix}:metadata.raw_attributes.{attribute}')
            if c is not None:
                c.origin = origin
                c.metadata['note'] = 'value as recorded by the source application; device clock not verified'
                out.append(c)
    if ts_start and not start_covered:
        c = _iso_candidate(start_kind, ts_start, 'derived:events.ts_start', f'{ref_prefix}:ts_start', origin,
                           ['events.ts_start'])
        raw_time_path = spec.get('precision_from_raw_time')
        if raw_time_path and c.usable:
            raw_time = _path(metadata, raw_time_path)
            if isinstance(raw_time, str) and raw_time.count(':') == 1:
                c.precision = 'minute'
                c.metadata['precision_from'] = f'events.metadata_json:$.{raw_time_path}'
        timestamp = metadata.get('timestamp') if isinstance(metadata, dict) else None
        if isinstance(timestamp, dict):
            for key in ('date_order_ambiguous', 'timezone_status', 'raw_date', 'raw_time', 'raw_ampm'):
                if key in timestamp:
                    c.metadata.setdefault(key, timestamp[key])
        out.append(c)
    if ts_end:
        out.append(_iso_candidate(spec.get('ts_end_kind', 'end'), ts_end, 'derived:events.ts_end',
                                  f'{ref_prefix}:ts_end', origin, ['events.ts_end']))
    return out


def zone_for(params: dict, source_id) -> str | None:
    by_source = params.get('zone_by_source') or {}
    if source_id is not None and str(source_id) in by_source:
        return by_source[str(source_id)]
    return params.get('assume_zone')


def place(c: Candidate, rule: Rule, params: dict, source_id) -> tuple[Placement | None, str | None]:
    """Instant, interval and basis of one candidate under the rule and operator params."""
    flags = {k: c.metadata[k] for k in FLAG_KEYS if k in c.metadata}
    if not c.usable:
        return None, c.unusable_reason
    try:
        if c.tz_status in ('explicit_offset', 'utc_epoch', 'assumed_zone') and c.utc:
            base = utc_text_to_us(c.utc)
            lo = hi = base
            basis = c.tz_status
            alternatives = c.metadata.get('utc_alternatives')
            if isinstance(alternatives, list) and alternatives:
                points = [utc_text_to_us(a) for a in alternatives if isinstance(a, str)]
                if points:
                    lo, hi = min(points + [base]), max(points + [base])
        elif c.local:
            zone = zone_for(params, source_id)
            assumed = None
            if zone:
                kind = c.kind if c.kind in timeinfo.TIME_KINDS else 'other'
                precision = c.precision if c.precision in timeinfo.PRECISIONS else 'unknown'
                tc = timeinfo.TimeCandidate(kind, c.raw, c.source, local=c.local, precision=precision)
                assumed = timeinfo.assume_zone(tc, zone, rule=params['assumption_rule'])
                flags['assumption_rule'] = params['assumption_rule']
                flags['assumed_zone'] = zone
                if assumed.utc is None:
                    flags['dst'] = assumed.metadata.get('dst', 'nonexistent_local_time')
                    assumed = None
            if assumed is not None:
                base = utc_text_to_us(assumed.utc)
                lo = hi = base
                basis = 'assumed_zone_operator'
                if assumed.metadata.get('utc_alternatives'):
                    flags['dst'] = 'ambiguous_fold'
                    flags['utc_alternatives'] = assumed.metadata['utc_alternatives']
                    points = [utc_text_to_us(a) for a in assumed.metadata['utc_alternatives']]
                    lo, hi = min(points), max(points)
            else:
                zone_rule = rule.data['unknown_zone']
                base = local_text_to_us(c.local)
                lo = base - int(zone_rule['offset_hours_max'] * 3600 * US)
                hi = base - int(zone_rule['offset_hours_min'] * 3600 * US)
                basis = 'local_as_utc_unknown_zone'
        else:
            return None, 'no_utc_or_local_value'
    except (ValueError, OverflowError):
        return None, 'unparseable'
    span = precision_span(base, c.precision)
    if span:
        hi += span - 1
    return Placement(base, lo, hi, basis, c.precision, flags), None


def _rank(order: list, value) -> int:
    try:
        return order.index(value)
    except ValueError:
        return len(order)


def select(candidates: list[Candidate], item_type: str, rule: Rule, params: dict, source_id) -> dict:
    """Apply the display rule; returns the selection and a per-candidate evaluation.

    The returned evaluation lists every candidate (including unusable and
    ineligible ones) so the UI can show why a time was chosen.
    """
    data = rule.data
    kinds = data['kind_priority'].get(item_type) or data['kind_priority']['default']
    origins = data['origin_priority']
    precisions = data['precision_priority']
    end_rules = data.get('end_kinds') or {}
    end_kinds = end_rules.get(item_type) or end_rules.get('default') or ['end']
    stored_usable = any(c.origin == 'stored' and c.usable and (c.utc or c.local) for c in candidates)
    evaluations = []
    ranked = []
    ends = []
    for c in candidates:
        placement, reason = place(c, rule, params, source_id)
        eligible = placement is not None
        if eligible and c.origin == 'derived_legacy' and stored_usable:
            eligible, reason = False, 'stored_candidate_present'
        entry = {'ref': c.ref, 'eligible': eligible, 'reason': reason, 'placement': placement}
        evaluations.append(entry)
        if not eligible:
            continue
        if c.kind in end_kinds:
            ends.append((end_kinds.index(c.kind), _rank(origins, c.origin), c.ref, placement))
        key = (_rank(kinds, c.kind), _rank(origins, c.origin), BASIS_RANK.get(placement.basis, 9),
               _rank(precisions, c.precision), c.ref)
        ranked.append((key, c, placement))
    if not ranked:
        return {'candidate': None, 'placement': None, 'end_us': None, 'evaluations': evaluations}
    ranked.sort(key=lambda item: item[0])
    _, chosen, placement = ranked[0]
    end_us = None
    for _, _, ref, end in sorted(ends, key=lambda item: item[:3]):
        if ref == chosen.ref:
            continue
        if end.sort_us >= placement.sort_us:
            end_us = end.sort_us
        else:
            placement.flags['end_before_start'] = ref
        break
    for entry in evaluations:
        entry['selected'] = entry['ref'] == chosen.ref
    return {'candidate': chosen, 'placement': placement, 'end_us': end_us, 'evaluations': evaluations}


def lane_key(value) -> str | None:
    """Grouping key: phone-like values lose separators; others are kept verbatim."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _PHONE_LIKE.match(text):
        compact = _SEPARATORS.sub('', text)
        if len(compact.lstrip('+')) >= 3:
            return compact
    return text[:500]


def counterparty(event, rule: Rule) -> str | None:
    fields = rule.data['lanes']['counterparty_fields']
    for name in fields.get(event['event_type']) or fields['default']:
        value = event[name] if name in event.keys() else None
        key = lane_key(value)
        if key:
            return key
    return None

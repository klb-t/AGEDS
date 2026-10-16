"""Feature series over time (rule ``feature_series.v1``); an interpretive artifact.

Deterministic features are functions of observed events, their labels and the
calendar (zone parameter); each has an id and version. Model-estimated features
(e.g. tone) are a separate, labelled series bound to an ``analysis_model_runs``
row; with the default provider ``none`` they are recorded as not run.

Events are bucketed by the local date of their start time in ``zone``. Items
are prepared exactly like the time budget (same filters, labels and exclusion
report), so ``category_time_share`` is the probable allocation of that budget.
"""
from __future__ import annotations

import bisect
import json
import re
import statistics
from datetime import datetime
from pathlib import Path

from . import budget as bud
from . import calendar as cal
from . import categories as cat
from .provenance import code_sha256, code_version

RULE = 'feature_series.v1'
RESULT_SCHEMA = 'ageds.feature_series_result.v1'
DEFAULTS = json.loads((Path(__file__).parent / 'data' / 'features_defaults.json').read_text('utf-8'))['params']
_TIME = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')

FEATURES = {
    'message_count': (1, 'count', 'events whose event_type is in message_event_types'),
    'messages_outgoing': (1, 'count', 'message events with direction outgoing'),
    'messages_incoming': (1, 'count', 'message events with direction incoming'),
    'official_counterparties_distinct': (1, 'count', 'distinct normalized counterparties/addresses of events labelled '
                                         'with an official category by a counterparty/e-mail-domain rule or a human '
                                         'correction'),
    'night_activity_share': (1, 'share', 'events with local start time in [night_start, night_end) divided by all '
                             'events of the bucket (count-based)'),
    'assistant_response_chars': (1, 'characters', 'mean and median text length of chat messages whose role is in '
                                 'response_roles'),
    'category_time_share': (1, 'share', 'probable seconds of a category (time budget at budget_params) divided by '
                            'bucket length, and by observed probable time'),
}


def effective_params(overrides: dict | None) -> dict:
    overrides = overrides or {}
    if not isinstance(overrides, dict):
        raise ValueError('params must be a JSON object')
    unknown = sorted(set(overrides) - set(DEFAULTS))
    if unknown:
        raise ValueError(f'unknown parameters: {unknown}')
    p = {**json.loads(json.dumps(DEFAULTS)), **overrides}
    for key in ('night_start', 'night_end'):
        if not isinstance(p[key], str) or not _TIME.match(p[key]):
            raise ValueError(f'{key} must be HH:MM')
    for key in ('message_event_types', 'response_roles', 'official_rule_kinds'):
        if not isinstance(p[key], list) or not all(isinstance(v, str) for v in p[key]):
            raise ValueError(f'{key} must be a list of strings')
    if not isinstance(p['budget_params'], dict):
        raise ValueError('budget_params must be an object')
    model = p['model_features']
    if not isinstance(model, dict) or set(model) - {'tone'}:
        raise ValueError('model_features supports only "tone" in this increment')
    for name, spec in model.items():
        if not isinstance(spec, dict) or set(spec) - {'provider', 'opt_in'}:
            raise ValueError(f'model_features.{name} must be {{"provider", "opt_in"}}')
    p['budget_params'] = bud.effective_params(p['budget_params'])
    return dict(sorted(p.items()))


def _minutes(text: str) -> int:
    h, m = text.split(':')
    return int(h) * 60 + int(m)


def _in_night(minute: int, start: int, end: int) -> bool:
    return start <= minute < end if start <= end else minute >= start or minute < end


def _official_counterparty(item: dict, label: dict, config: cat.CategoryConfig, rule_kinds: list[str]) -> str | None:
    if not any(c in config.official_categories for c in label['categories']):
        return None
    if label['path'] == 'rule':
        if not any(r['kind'] in rule_kinds for r in label['provenance']['rules']):
            return None
    elif label['path'] != 'annotation':
        return None
    f = item.get('fields') or {}
    for value in (item.get('counterparty'), f.get('phone_or_address'), f.get('sender')):
        if isinstance(value, str) and value.strip():
            found = cat._ADDRESS.search(value)
            return cat.normalize_counterparty(found.group(0) if found else value)
    return None


def run_features(db, config: cat.CategoryConfig, *, window_from: str, window_to: str, zone: str,
                 params: dict | None = None, assume_zone: str | None = None, assumption_rule: str | None = None,
                 persist: bool = True) -> dict:
    from . import model_runs, runs
    p = effective_params(params)
    bp = p['budget_params']
    prepared = bud.prepare_inputs(db, config, window_from=window_from, window_to=window_to, zone=zone, params=bp,
                                  assume_zone=assume_zone, assumption_rule=assumption_rule, persist=persist,
                                  need_fields=True)
    lo, hi = prepared['lo'], prepared['hi']
    z = cal.zone_info(zone)
    day_list = cal.days(lo, hi, z)
    starts = [d['start'] for d in day_list]
    items = [it for it in prepared['items'] if lo <= it['t_start'] < hi]
    labels = prepared['labels']
    night_start, night_end = _minutes(p['night_start']), _minutes(p['night_end'])
    per_day = [{'messages': 0, 'out': 0, 'in': 0, 'all': 0, 'night': 0, 'official': set(), 'resp': []} for _ in day_list]
    for it in items:
        d = bisect.bisect_right(starts, it['t_start']) - 1
        row = per_day[d]
        row['all'] += 1
        local = datetime.fromtimestamp(it['t_start'], z)
        if _in_night(local.hour * 60 + local.minute, night_start, night_end):
            row['night'] += 1
        if it['event_type'] in p['message_event_types']:
            row['messages'] += 1
            direction = bud._role_direction(it)
            row['out'] += direction == 'outgoing'
            row['in'] += direction == 'incoming'
        official = _official_counterparty(it, labels[it['event_id']], config, p['official_rule_kinds'])
        if official:
            row['official'].add(official)
        if (it.get('chat') or {}).get('role') in p['response_roles'] and it.get('text_chars') is not None:
            row['resp'].append(it['text_chars'])
    computed = bud.compute_budget(prepared['items'], labels, config, lo=lo, hi=hi, zone_name=zone,
                                  params={**bp, 'grid_mode': 'one_at_a_time', 'grid': {}})
    tone = {}
    tone_spec = p['model_features'].get('tone', {'provider': 'none', 'opt_in': False})
    tone_run = None
    if persist:
        texts = [it for it in items if it.get('text_chars')]
        tone_run = model_runs.run_model(db, texts, purpose='feature:tone', provider=tone_spec.get('provider', 'none'),
                                        opt_in=bool(tone_spec.get('opt_in', False)), parameters=tone_spec)
        if tone_run['status'] == 'done':
            tone = {eid: out['value'] for eid, out in model_runs.load_outputs(db, tone_run['model_run_id'],
                                                                             purpose='feature:tone').items()}
    tone_by_day = [[] for _ in day_list]
    for it in items:
        if it['event_id'] in tone:
            tone_by_day[bisect.bisect_right(starts, it['t_start']) - 1].append(tone[it['event_id']])

    series = {name: {'id': name, 'version': FEATURES[name][0], 'unit': FEATURES[name][1], 'kind': 'deterministic',
                     'definition': FEATURES[name][2], 'values': {}} for name in FEATURES}
    category_ids = [c['id'] for c in computed['palette'] if c['id'] != cat.UNOBSERVED]
    for g in bp['granularities']:
        rows = {name: [] for name in FEATURES}
        tone_rows = []
        for b, candle in zip(cal.buckets(day_list, g), computed['candles'][g]):
            agg = {'messages': 0, 'out': 0, 'in': 0, 'all': 0, 'night': 0, 'official': set(), 'resp': []}
            tones = []
            for d in b['days']:
                for k in ('messages', 'out', 'in', 'all', 'night'):
                    agg[k] += per_day[d][k]
                agg['official'] |= per_day[d]['official']
                agg['resp'] += per_day[d]['resp']
                tones += tone_by_day[d]
            key = {'bucket': b['key'], 'partial': b['partial']}
            rows['message_count'].append({**key, 'value': agg['messages']})
            rows['messages_outgoing'].append({**key, 'value': agg['out']})
            rows['messages_incoming'].append({**key, 'value': agg['in']})
            rows['official_counterparties_distinct'].append({**key, 'value': len(agg['official'])})
            rows['night_activity_share'].append({**key, 'value': bud._r(agg['night'] / agg['all']) if agg['all'] else None,
                                                 'n': agg['all']})
            rows['assistant_response_chars'].append({**key, 'value': bud._r(statistics.fmean(agg['resp'])) if agg['resp'] else None,
                                                     'median': statistics.median(agg['resp']) if agg['resp'] else None,
                                                     'n': len(agg['resp'])})
            observed_p = candle['observed']['probable']
            shares = {}
            for c in category_ids:
                pv = candle['categories'].get(c, {}).get('probable', 0.0)
                shares[c] = {'share_of_bucket': bud._r(pv / b['length_s']),
                             'share_of_observed': bud._r(pv / observed_p) if observed_p else None}
            rows['category_time_share'].append({**key, 'value': shares})
            tone_rows.append({**key, 'value': bud._r(statistics.fmean(tones)) if tones else None, 'n': len(tones)})
        for name in FEATURES:
            series[name]['values'][g] = rows[name]
        series.setdefault('tone', {'id': 'tone', 'version': 1, 'unit': '[-1, 1] (model scale)', 'kind': 'model_estimated',
                                   'estimated_by_model': True, 'model_run': tone_run,
                                   'status': tone_run['status'] if tone_run else 'not_run',
                                   'definition': 'mean model output per bucket; a hypothesis, not an observation',
                                   'values': {}})['values'][g] = tone_rows
    result = {
        'schema': RESULT_SCHEMA, 'artifact_class': 'interpretive', 'disclaimer': bud.DISCLAIMER,
        'method': {'rule': RULE, 'budget_rule': bud.RULE, 'source_rule': prepared['input']['source_rule'],
                   'source_rule_sha256': prepared['input']['source_rule_sha256'], 'seam': prepared['input']['seam'],
                   'bucketing': 'local date of the start time in the stated zone',
                   'features': {k: {'version': v[0], 'unit': v[1], 'definition': v[2]} for k, v in FEATURES.items()},
                   'model_series': 'separate, labelled, bound to a model run; provider none = not run'},
        'parameters': p, 'code': {'version': code_version(), 'sha256': code_sha256()},
        'window': prepared['window'], 'config': config.describe(), 'categories': computed['palette'],
        'labels': prepared['labels_record'], 'input': prepared['input'], 'exclusions': prepared['exclusions'],
        'series': [series[k] for k in sorted(series)],
    }
    stored = None
    if persist:
        stored = runs.store_run(db, kind='feature_series', parameters={'params': p, 'window': result['window']},
                                config_sha256=config.sha256, config_version=config.version, source_rule=RULE,
                                input_sha256=prepared['input_sha256'],
                                label_run_id=prepared['labels_record']['label_run_id'], result=result)
    return {'run': stored, 'result': result}

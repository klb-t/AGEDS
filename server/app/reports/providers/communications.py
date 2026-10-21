"""Events/communication summary: counts by type, direction, counterparty, chatbot.

Counts are over events placed on the UTC axis by the analysis source seam
(rule shown); untimed events are counted separately by reason.
"""
from __future__ import annotations

from ..blocks import kv, table
from ..common import epoch_to_utc
from . import Section
from ._events import record_event_inputs, scoped_events

VERSION = 'communications.v1'


def _group(items, key, max_rows):
    groups: dict = {}
    for item in items:
        value = key(item)
        entry = groups.setdefault(value, {'events': 0, 'first': item['t_start'], 'last': item['t_start']})
        entry['events'] += 1
        entry['first'] = min(entry['first'], item['t_start'])
        entry['last'] = max(entry['last'], item['t_start'])
    ordered = sorted(groups.items(), key=lambda pair: (-pair[1]['events'], str(pair[0])))
    return ordered[:max_rows], len(ordered)


def provide(ctx) -> dict:
    section = Section(ctx, 'communications', __name__, VERSION, 'section_communications')
    events = scoped_events(ctx)
    section.data['rule'] = events['rule']
    items, max_rows = events['items'], ctx.params['max_rows']
    record_event_inputs(section, events, [item['event_id'] for item in items] + [item['event_id'] for item in events['untimed']])
    for name, value in events['exclusions'].items():
        section.exclude(name, value)
    if events['truncated']:
        section.status('partial', 'event scan limit reached (max_events)')
    if not items and not events['untimed']:
        section.status('empty', 'no events in scope')
    for item in items:
        section.observe_time(epoch_to_utc(item['t_start']), 'event_utc')
    section.block(kv('communications.summary', [
        ('events', len(items)), ('untimed', len(events['untimed'])), ('rule', events['rule']),
        ('assumed_zone', events['assume_zone']),
    ], ctx.lang))
    specs = (
        ('by_type', 'event_type', lambda i: i['event_type']),
        ('by_direction', 'direction', lambda i: i['direction']),
        ('by_counterparty', 'counterparty', lambda i: i['counterparty']),
        ('by_chatbot', 'chatbot', lambda i: i['chatbot']),
    )
    for block_id, column, key in specs:
        groups, total = _group(items, key, max_rows)
        rows = [{column: value, 'events': data['events'], 'first_utc': epoch_to_utc(data['first']),
                 'last_utc': epoch_to_utc(data['last'])} for value, data in groups]
        section.block(table(f'communications.{block_id}', [(column, 'text'), ('events', 'number'),
                                                           ('first_utc', 'time'), ('last_utc', 'time')],
                            rows, ctx.lang, title_key=block_id, total_rows=total, truncated=total > max_rows))
    reasons: dict[str, int] = {}
    for item in events['untimed']:
        reasons[item['reason']] = reasons.get(item['reason'], 0) + 1
    section.block(table('communications.untimed', [('untimed_reason', 'text'), ('events', 'number')],
                        [{'untimed_reason': reason, 'events': count} for reason, count in sorted(reasons.items())],
                        ctx.lang, title_key='untimed'))
    if any(item['time_basis'].get('assumed_zone') for item in items):
        section.unknown('original_zone_of_assumed_zone_items')
    return section.result()

"""Events organized by counterparty / institution (lawyer package).

``group_by``: ``counterparty`` (contact label, else address/number as
recorded), ``domain`` (the part after ``@`` of an e-mail address, else the
counterparty) or ``collection``. Grouping is literal: two spellings of one
institution stay two groups; merging them is a human decision.
"""
from __future__ import annotations

from ..blocks import table
from ..common import epoch_to_utc
from . import Section
from ._events import record_event_inputs, scoped_events
from .chronology import event_row

VERSION = 'counterparties.v1'
UNKNOWN = 'unknown'
EVENT_COLUMNS = [('time_utc', 'time'), ('tz_status', 'text'), ('event_type', 'text'), ('direction', 'text'),
                 ('quote', 'quote'), ('quote_truncated', 'bool'), ('body_sha256', 'hash'), ('event_id', 'id'),
                 ('artifact_id', 'id'), ('artifact_sha256', 'hash')]


def _key(group_by: str, row: dict, item: dict) -> str:
    if group_by == 'collection':
        return str(item.get('collection_id')) if item.get('collection_id') is not None else UNKNOWN
    counterparty = row.get('contact_label') or row.get('phone_or_address') or row.get('sender')
    if group_by == 'domain':
        for value in (row.get('phone_or_address'), row.get('sender')):
            if isinstance(value, str) and '@' in value:
                return value.rsplit('@', 1)[1].strip().strip('>').casefold() or UNKNOWN
    return counterparty if counterparty else UNKNOWN


def provide(ctx) -> dict:
    section = Section(ctx, 'counterparties', __name__, VERSION, 'section_counterparties')
    events = scoped_events(ctx)
    section.data['rule'] = events['rule']
    group_by, max_rows, per_group = ctx.params['group_by'], ctx.params['max_rows'], ctx.params['per_group_rows']
    groups: dict[str, dict] = {}
    for item in events['items']:
        row = events['rows'][item['event_id']]
        entry = groups.setdefault(_key(group_by, row, item), {'items': [], 'untimed': 0, 'types': set(), 'artifacts': set()})
        entry['items'].append(item)
        entry['types'].add(row['event_type'])
        if row['artifact_id'] is not None:
            entry['artifacts'].add(row['artifact_id'])
    for item in events['untimed']:
        row = events['rows'][item['event_id']]
        entry = groups.setdefault(_key(group_by, row, {}), {'items': [], 'untimed': 0, 'types': set(), 'artifacts': set()})
        entry['untimed'] += 1
        entry['types'].add(row['event_type'])
    for name, value in events['exclusions'].items():
        section.exclude(name, value)
    section.exclude('group_by', group_by)
    ordered = sorted(groups.items(), key=lambda pair: (-len(pair[1]['items']) - pair[1]['untimed'], pair[0]))
    if not ordered:
        section.status('empty', 'no events in scope')
    summary = []
    for key, entry in ordered[:max_rows]:
        times = [item['t_start'] for item in entry['items']]
        summary.append({'group_key': key, 'events': len(entry['items']), 'untimed': entry['untimed'],
                        'first_utc': epoch_to_utc(min(times)) if times else None,
                        'last_utc': epoch_to_utc(max(times)) if times else None,
                        'event_type': ','.join(sorted(str(t) for t in entry['types'])),
                        'artifacts': len(entry['artifacts'])})
    section.block(table('counterparties.summary', [('group_key', 'text'), ('events', 'number'), ('untimed', 'number'),
                                                   ('first_utc', 'time'), ('last_utc', 'time'), ('event_type', 'text'),
                                                   ('artifacts', 'number')],
                        summary, ctx.lang, total_rows=len(ordered), truncated=len(ordered) > max_rows))
    listed_ids = []
    for index, (key, entry) in enumerate(ordered[:max_rows]):
        rows = []
        for item in entry['items'][:per_group]:
            row = event_row(ctx, events, item)
            section.observe_time(row['time_utc'], 'event_utc')
            if row['artifact_id'] is not None:
                section.cite('artifacts', {'id': row['artifact_id'], 'sha256': row['artifact_sha256'],
                                           'size_bytes': (events['artifacts'].get(row['artifact_id']) or {}).get('size_bytes')})
            rows.append(row)
            listed_ids.append(item['event_id'])
        block = table(f'counterparties.group.{index}', EVENT_COLUMNS, rows, ctx.lang, total_rows=len(entry['items']),
                      truncated=len(entry['items']) > per_group, href_columns={'artifact_id': 'artifact_href'})
        block['title'] = key
        section.block(block)
    record_event_inputs(section, events, listed_ids)
    if len(ordered) > max_rows or any(len(entry['items']) > per_group for _, entry in ordered[:max_rows]):
        section.status('partial', 'group or per-group row limit reached')
    return section.result()

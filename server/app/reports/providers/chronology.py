"""Chronology with quotes (interim projection over the analysis source seam).

Each row is one event placed on the UTC axis by rule ``analysis_source.v1``
with its time basis (kind, zone status, assumed zone), the counterparty, a
bounded quote of the stored text (source text is data, escaped on render) and
the SHA-256 of the full text, plus the artifact and its SHA-256.
"""
from __future__ import annotations

from ..blocks import kv, prose, table
from ..common import epoch_to_utc, sha256_text
from . import Section
from ._events import record_event_inputs, scoped_events

VERSION = 'chronology.v1'
SUMMARY = {
    'pl': 'Chronologia obejmuje {events} zdarzeń umieszczonych na osi UTC według reguły {rule}; '
          '{untimed} zdarzeń bez ustalonego czasu UTC wymieniono osobno.',
    'en': 'The chronology lists {events} events placed on the UTC axis by rule {rule}; '
          '{untimed} events without an established UTC time are listed separately.',
}
COLUMNS = [('time_utc', 'time'), ('time_kind', 'text'), ('tz_status', 'text'), ('assumed_zone', 'text'),
           ('event_type', 'text'), ('direction', 'text'), ('counterparty', 'text'), ('chatbot', 'text'),
           ('quote', 'quote'), ('quote_truncated', 'bool'), ('body_sha256', 'hash'), ('event_id', 'id'),
           ('artifact_id', 'id'), ('artifact_sha256', 'hash')]


def event_row(ctx, events: dict, item: dict) -> dict:
    row = events['rows'][item['event_id']]
    body = row.get('body')
    limit = ctx.params['quote_chars']
    artifact = events['artifacts'].get(row['artifact_id']) or {}
    base = ctx.params['workbench_base_url']
    return {
        'time_utc': epoch_to_utc(item['t_start']), 'time_kind': item['time_basis']['kind'],
        'tz_status': item['time_basis']['tz_status'], 'assumed_zone': item['time_basis'].get('assumed_zone'),
        'event_type': row['event_type'], 'direction': row['direction'], 'counterparty': item['counterparty'],
        'chatbot': item['chatbot'],
        'quote': body[:limit] if isinstance(body, str) else None,
        'quote_truncated': bool(isinstance(body, str) and len(body) > limit),
        'body_sha256': sha256_text(body) if isinstance(body, str) else None,
        'event_id': row['id'], 'artifact_id': row['artifact_id'], 'artifact_sha256': artifact.get('sha256'),
        'artifact_href': f'{base}/artifact/{row["artifact_id"]}' if base and row['artifact_id'] else None,
    }


def provide(ctx, *, section_name: str = 'chronology', title_key: str = 'section_chronology') -> dict:
    section = Section(ctx, section_name, __name__, VERSION, title_key)
    events = scoped_events(ctx)
    section.data['rule'] = events['rule']
    max_rows = ctx.params['max_rows']
    items, untimed = events['items'], events['untimed']
    listed = items[:max_rows]
    record_event_inputs(section, events, [item['event_id'] for item in listed] +
                        [item['event_id'] for item in untimed[:max_rows]])
    for name, value in events['exclusions'].items():
        section.exclude(name, value)
    section.exclude('rows_not_listed_over_max_rows', max(0, len(items) - max_rows))
    if events['truncated'] or len(items) > max_rows:
        section.status('partial', 'row or event scan limit reached')
    if not items and not untimed:
        section.status('empty', 'no events in scope')
    rows = []
    for item in listed:
        row = event_row(ctx, events, item)
        section.observe_time(row['time_utc'], 'event_utc')
        if row['artifact_id'] is not None:
            section.cite('artifacts', {'id': row['artifact_id'], 'sha256': row['artifact_sha256'],
                                       'size_bytes': (events['artifacts'].get(row['artifact_id']) or {}).get('size_bytes')})
            if row['artifact_sha256'] is None:
                section.unknown(f'artifact.{row["artifact_id"]}.sha256')
        rows.append(row)
    section.block(prose('chronology.summary', f'{VERSION}.summary', SUMMARY.get(ctx.lang, SUMMARY['pl']),
                        {'events': len(items), 'rule': events['rule'], 'untimed': len(untimed)}))
    section.block(kv('chronology.rule', [('rule', events['rule']), ('assumed_zone', events['assume_zone'])], ctx.lang))
    section.block(table('chronology.events', COLUMNS, rows, ctx.lang, total_rows=len(items),
                        truncated=len(items) > max_rows, href_columns={'artifact_id': 'artifact_href'}))
    untimed_rows = []
    for item in untimed[:max_rows]:
        row = events['rows'][item['event_id']]
        body = row.get('body')
        untimed_rows.append({'event_id': row['id'], 'event_type': row['event_type'], 'untimed_reason': item['reason'],
                             'counterparty': row['contact_label'] or row['phone_or_address'],
                             'quote': body[:ctx.params['quote_chars']] if isinstance(body, str) else None,
                             'body_sha256': sha256_text(body) if isinstance(body, str) else None})
    section.block(table('chronology.untimed', [('event_id', 'id'), ('event_type', 'text'), ('untimed_reason', 'text'),
                                               ('counterparty', 'text'), ('quote', 'quote'), ('body_sha256', 'hash')],
                        untimed_rows, ctx.lang, title_key='untimed', total_rows=len(untimed),
                        truncated=len(untimed) > max_rows))
    return section.result()

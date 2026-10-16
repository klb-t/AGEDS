"""Bounded reads over a materialized timeline projection.

Windows use keyset pagination on ``(sort_us, item_kind, ref_id, sub_index)``,
which is unique per projection, so ties never duplicate or skip items. Text
from sources is returned as plain data; clients must not interpret it as HTML.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass, field

from ..search import SearchQueryError, validate_query
from .candidates import lane_key
from .projection import event_selection, label_flags, recording_selection
from .ruleset import Rule, canonical_json
from .timeutil import US, add_unit, floor_unit, iso

MAX_LIMIT = 1000
MAX_FILTER_VALUES = 200
MAX_FILTER_CHARS = 500
MAX_FTS_MATCHES = 50_000
MAX_FTS_STEPS = 5_000_000
MAX_TEXT_CHARS = 2000
MAX_LANES = 200
MAX_BUCKETS = 5000
MAX_EXPORT_ROWS = 200_000
KINDS = ('event', 'transcript_segment')
GROUPS = {'source': 'source_id', 'collection': 'collection_id', 'counterparty': 'counterparty', 'chatbot': 'chatbot',
          'thread': 'thread_id', 'type': 'event_type', 'kind': 'item_kind'}
BUCKETS = ('year', 'month', 'week', 'day', 'hour', 'minute')
APPROX_BUCKET_US = {'year': 365 * 86400 * US, 'month': 30 * 86400 * US, 'week': 7 * 86400 * US,
                    'day': 86400 * US, 'hour': 3600 * US, 'minute': 60 * US}


class QueryInputError(ValueError):
    """Invalid filter, bound, cursor or limit (HTTP 422)."""


class QueryLimitError(ValueError):
    """The request would exceed an explicit work bound (HTTP 413/422)."""


@dataclass
class Filters:
    event_types: list = field(default_factory=list)
    kinds: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    collections: list = field(default_factory=list)
    counterparties: list = field(default_factory=list)
    chatbots: list = field(default_factory=list)
    threads: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    q: str | None = None
    transcript_versions: str = 'latest'
    include_hidden: bool = False

    def canonical(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], '', False)}


def _texts(values, name: str) -> list[str]:
    out = []
    for value in values or []:
        for part in str(value).split(','):
            part = part.strip()
            if part:
                if len(part) > MAX_FILTER_CHARS:
                    raise QueryInputError(f'{name} value exceeds {MAX_FILTER_CHARS} characters')
                out.append(part)
    if len(out) > MAX_FILTER_VALUES:
        raise QueryInputError(f'{name} accepts at most {MAX_FILTER_VALUES} values')
    return sorted(set(out))


def _ints(values, name: str) -> list[int]:
    out = []
    for text in _texts(values, name):
        try:
            number = int(text)
        except ValueError as exc:
            raise QueryInputError(f'{name} values must be integers') from exc
        if not 1 <= number <= 2**63 - 1:
            raise QueryInputError(f'{name} values must be positive integers')
        out.append(number)
    return sorted(set(out))


def make_filters(*, event_types=None, kinds=None, sources=None, collections=None, counterparties=None, chatbots=None,
                 threads=None, tags=None, q=None, transcript_versions='latest', include_hidden=False) -> Filters:
    kinds_list = _texts(kinds, 'kind')
    if any(k not in KINDS for k in kinds_list):
        raise QueryInputError(f'kind must be one of {", ".join(KINDS)}')
    versions = (transcript_versions or 'latest').strip()
    if versions not in ('latest', 'all'):
        versions = ','.join(str(v) for v in _ints([versions], 'transcript_versions'))
    if q is not None and q.strip():
        try:
            validate_query(q, 1)
        except SearchQueryError as exc:
            raise QueryInputError(str(exc)) from exc
    else:
        q = None
    keys = [lane_key(v) for v in _texts(counterparties, 'counterparty')]
    return Filters(_texts(event_types, 'event_type'), kinds_list, _ints(sources, 'source'),
                   _ints(collections, 'collection'), sorted({k for k in keys if k}), _texts(chatbots, 'chatbot'),
                   _texts(threads, 'thread'), _texts(tags, 'tag'), q, versions, bool(include_hidden))


def _fts_matches(db: sqlite3.Connection, q: str) -> tuple[list[int], list[int], bool]:
    steps = 0

    def progress():
        nonlocal steps
        steps += 1000
        return int(steps >= MAX_FTS_STEPS)
    db.set_progress_handler(progress, 1000)
    try:
        rows = db.execute("""SELECT object_kind, object_id FROM search_fts WHERE search_fts MATCH ?
                             AND object_kind IN ('event','derived_text') LIMIT ?""", (q, MAX_FTS_MATCHES + 1)).fetchall()
    except sqlite3.OperationalError as exc:
        if steps >= MAX_FTS_STEPS:
            raise QueryLimitError('text query exceeded its work budget; narrow the query') from exc
        raise
    finally:
        db.set_progress_handler(None, 0)
    truncated = len(rows) > MAX_FTS_MATCHES
    rows = rows[:MAX_FTS_MATCHES]
    events = sorted({int(r[1]) for r in rows if r[0] == 'event'})
    texts = sorted({int(r[1]) for r in rows if r[0] == 'derived_text'})
    return events, texts, truncated


def filter_sql(db, pid: int, f: Filters, alias: str = 'ti') -> tuple[str, list, dict]:
    """WHERE clause (without the keyword) for a projection and filters; values are bound parameters."""
    a = alias
    clauses, args = [f'{a}.projection_id=?'], [pid]
    notes: dict = {}

    def values_in(column: str, values: list):
        clauses.append(f'{a}.{column} IN (SELECT value FROM json_each(?))')
        args.append(json.dumps(values))
    if not f.include_hidden:
        clauses.append(f'{a}.hidden=0')
    if f.kinds:
        values_in('item_kind', f.kinds)
    if f.event_types:
        values_in('event_type', f.event_types)
    if f.sources:
        values_in('source_id', f.sources)
    if f.counterparties:
        values_in('counterparty', f.counterparties)
    if f.chatbots:
        values_in('chatbot', f.chatbots)
    if f.threads:
        values_in('thread_id', f.threads)
    if f.collections:
        payload = json.dumps(f.collections)
        clauses.append(f'''({a}.collection_id IN (SELECT value FROM json_each(?)) OR EXISTS (SELECT 1 FROM source_observations o
            WHERE o.artifact_id={a}.artifact_id AND o.collection_id IN (SELECT value FROM json_each(?))))''')
        args += [payload, payload]
    if f.tags:
        payload = json.dumps(f.tags)
        clauses.append(f'''(({a}.item_kind='event' AND EXISTS (SELECT 1 FROM event_tags et JOIN tags t ON t.id=et.tag_id
              WHERE et.event_id={a}.ref_id AND t.name IN (SELECT value FROM json_each(?))))
            OR ({a}.item_kind='transcript_segment' AND EXISTS (SELECT 1 FROM artifact_tags at JOIN tags t ON t.id=at.tag_id
              WHERE at.artifact_id={a}.artifact_id AND t.name IN (SELECT value FROM json_each(?)))))''')
        args += [payload, payload]
    if f.transcript_versions == 'latest':
        clauses.append(f"({a}.item_kind='event' OR {a}.is_latest=1)")
    elif f.transcript_versions != 'all':
        pinned = json.dumps([int(v) for v in f.transcript_versions.split(',')])
        clauses.append(f'''({a}.item_kind='event' OR {a}.ref_id IN (SELECT value FROM json_each(?))
            OR ({a}.is_latest=1 AND {a}.artifact_id NOT IN (SELECT artifact_id FROM derived_text
                WHERE id IN (SELECT value FROM json_each(?)))))''')
        args += [pinned, pinned]
    if f.q:
        events, texts, truncated = _fts_matches(db, f.q)
        notes['text_query'] = {'matched_events': len(events), 'matched_transcript_versions': len(texts),
                               'truncated': truncated, 'transcript_granularity': 'version'}
        clauses.append(f'''(({a}.item_kind='event' AND {a}.ref_id IN (SELECT value FROM json_each(?)))
            OR ({a}.item_kind='transcript_segment' AND {a}.ref_id IN (SELECT value FROM json_each(?))))''')
        args += [json.dumps(events), json.dumps(texts)]
    return ' AND '.join(clauses), args, notes


SCAN_THRESHOLD = 20_000


def _range_sql(start_us, end_us, match: str, halfwidth: int, a: str = 'ti', *, scan: bool = False) -> tuple[str, list]:
    """Time-range predicate; ``scan`` disables the sort index (``+col``) for aggregates over most rows."""
    col = f'+{a}.sort_us' if scan else f'{a}.sort_us'
    clauses, args = [f'{col} IS NOT NULL'], []
    if match == 'overlap':
        if start_us is not None:
            clauses.append(f'{a}.sort_us>=? AND {a}.hi_us>=?')
            args += [start_us - halfwidth, start_us]
        if end_us is not None:
            clauses.append(f'{a}.sort_us<? AND {a}.lo_us<?')
            args += [end_us + halfwidth, end_us]
    else:
        if start_us is not None:
            clauses.append(f'{col}>=?')
            args.append(start_us)
        if end_us is not None:
            clauses.append(f'{col}<?')
            args.append(end_us)
    return ' AND '.join(clauses), args


def _prefer_scan(db, pid: int, start_us, end_us) -> bool:
    """Aggregates over many rows are cheaper as a primary-key scan than as sort-index lookups."""
    where, args = _range_sql(start_us, end_us, 'display', 0)
    count = db.execute(f'SELECT COUNT(*) FROM (SELECT 1 FROM timeline_items ti WHERE ti.projection_id=? AND {where} LIMIT ?)',
                       (pid, *args, SCAN_THRESHOLD + 1)).fetchone()[0]
    return count > SCAN_THRESHOLD


def _query_hash(*parts) -> str:
    return hashlib.sha256(canonical_json(list(parts)).encode('utf-8')).hexdigest()[:16]


def encode_cursor(pid: int, build_id: int, qhash: str, key: tuple) -> str:
    raw = canonical_json({'p': pid, 'b': build_id, 'h': qhash, 'k': list(key)}).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_cursor(text: str, pid: int, qhash: str) -> tuple[tuple, int]:
    if not isinstance(text, str) or len(text) > 512:
        raise QueryInputError('invalid cursor')
    try:
        data = json.loads(base64.urlsafe_b64decode(text + '=' * (-len(text) % 4)))
        key = data['k']
        if data['p'] != pid or data['h'] != qhash:
            raise QueryInputError('cursor belongs to another projection or query; restart without cursor')
        if not (isinstance(key, list) and len(key) == 4 and type(key[0]) is int and key[1] in KINDS
                and type(key[2]) is int and type(key[3]) is int):
            raise ValueError
        return tuple(key), int(data['b'])
    except QueryInputError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise QueryInputError('invalid cursor') from exc


def _clip(text, limit: int = MAX_TEXT_CHARS):
    if text is None:
        return None, False
    text = str(text)
    return (text[:limit], True) if len(text) > limit else (text, False)


def _time(row) -> dict:
    flags = json.loads(row['flags_json'] or '{}')
    if row['sort_us'] is None:
        return {'display': None, 'basis': 'none', 'precision': 'unknown', 'interval': None, 'end': None,
                'candidate_ref': None, 'candidate_kind': None, 'flags': flags}
    return {'display': iso(row['sort_us']), 'sort_us': row['sort_us'], 'basis': row['basis'],
            'precision': row['precision'], 'interval': {'from': iso(row['lo_us']), 'to': iso(row['hi_us']),
                                                        'from_us': row['lo_us'], 'to_us': row['hi_us']},
            'uncertainty_seconds': round((row['hi_us'] - row['lo_us']) / US, 6),
            'end': iso(row['end_us']), 'end_us': row['end_us'],
            'candidate_ref': row['candidate_ref'], 'candidate_kind': row['candidate_kind'], 'flags': flags}


def _candidate_payload(selection: dict) -> list[dict]:
    evaluations = {e['ref']: e for e in selection['evaluations']}
    out = []
    for c in selection['candidates']:
        entry = c.as_dict()
        evaluation = evaluations.get(c.ref, {})
        entry['eligible'] = evaluation.get('eligible', False)
        entry['not_eligible_reason'] = evaluation.get('reason')
        entry['selected'] = evaluation.get('selected', False)
        placement = evaluation.get('placement')
        entry['placement'] = placement.as_dict() if placement is not None else None
        out.append(entry)
    return out


def hydrate(db, rows, rule: Rule, params: dict, *, candidates: bool, group: str | None) -> list[dict]:
    """Turn cache rows into API items, reading only the page's events/transcripts."""
    event_ids = [r['ref_id'] for r in rows if r['item_kind'] == 'event']
    version_ids = sorted({r['ref_id'] for r in rows if r['item_kind'] == 'transcript_segment'})
    events = {}
    if event_ids:
        for e in db.execute('''SELECT id,event_type,ts_start,ts_end,direction,sender,recipients,contact_label,phone_or_address,
                                 subject,body,thread_id,artifact_id,source_id,external_id FROM events
                               WHERE id IN (SELECT value FROM json_each(?))''', (json.dumps(event_ids),)):
            events[int(e['id'])] = e
    versions = {}
    for v in db.execute('''SELECT d.id,d.artifact_id,d.language,d.model,d.run_id,d.created_at,d.segments_json,a.original_name
                           FROM derived_text d JOIN artifacts a ON a.id=d.artifact_id
                           WHERE d.id IN (SELECT value FROM json_each(?))''', (json.dumps(version_ids),)):
        try:
            segments = json.loads(v['segments_json'] or '[]')
        except ValueError:
            segments = []
        versions[int(v['id'])] = (v, segments if isinstance(segments, list) else [])
    recording_cache: dict = {}
    items = []
    for r in rows:
        item = {'key': f"{'event' if r['item_kind'] == 'event' else 'segment'}:{r['ref_id']}"
                       + (f":{r['sub_index']}" if r['item_kind'] == 'transcript_segment' else ''),
                'kind': r['item_kind'], 'event_type': r['event_type'], 'source_id': r['source_id'],
                'artifact_id': r['artifact_id'], 'collection_id': r['collection_id'], 'counterparty': r['counterparty'],
                'chatbot': r['chatbot'], 'thread_id': r['thread_id'], 'hidden': bool(r['hidden']), 'time': _time(r)}
        label_flags(item['time']['flags'], params)
        if group:
            item['lane'] = r[GROUPS[group]]
        if r['item_kind'] == 'event':
            e = events.get(int(r['ref_id']))
            item['event_id'] = int(r['ref_id'])
            if e is not None:
                text, clipped = _clip(e['body'] if e['body'] is not None else e['subject'])
                item.update({'direction': e['direction'], 'sender': e['sender'], 'contact_label': e['contact_label'],
                             'phone_or_address': e['phone_or_address'], 'subject': _clip(e['subject'], 300)[0],
                             'text': text, 'text_truncated': clipped, 'external_id': e['external_id'],
                             'title': ' · '.join(x for x in (e['event_type'], e['contact_label'] or e['phone_or_address']
                                                             or e['sender'] or e['thread_id']) if x)})
            item['links'] = {'artifact': f"/artifact/{r['artifact_id']}" if r['artifact_id'] else None,
                             'detail': f"/api/timeline/items/{item['key']}"}
            if candidates:
                selection = event_selection(db, int(r['ref_id']), rule, params)
                item['candidates'] = _candidate_payload(selection) if selection else []
        else:
            version, segments = versions.get(int(r['ref_id']), (None, []))
            segment = segments[r['sub_index']] if r['sub_index'] < len(segments) and isinstance(segments[r['sub_index']], dict) else {}
            text, clipped = _clip(segment.get('text'))
            language = segment.get('language')
            item.update({'derived_text_id': int(r['ref_id']), 'segment_index': r['sub_index'],
                         'transcript_version': {'id': int(r['ref_id']), 'is_latest': bool(r['is_latest']),
                                                'model': version['model'] if version else None,
                                                'run_id': version['run_id'] if version else None},
                         'segment': {'start_seconds': segment.get('start'), 'end_seconds': segment.get('end')},
                         'language': language if isinstance(language, str) else (version['language'] if version else None),
                         'language_basis': 'segment' if isinstance(language, str) else 'transcript_version',
                         'text': text, 'text_truncated': clipped,
                         'title': f"Transkrypcja #{r['ref_id']} · segment {r['sub_index']}"
                                  + (f" · {version['original_name']}" if version else '')})
            item['links'] = {'artifact': f"/artifact/{r['artifact_id']}",
                             'transcript': f"/api/artifacts/{r['artifact_id']}/transcript?derived_text_id={r['ref_id']}",
                             'citation_workspace': f"/artifact/{r['artifact_id']}",
                             'detail': f"/api/timeline/items/{item['key']}"}
            if candidates:
                aid = int(r['artifact_id'])
                if aid not in recording_cache:
                    recording_cache[aid] = _candidate_payload(recording_selection(db, aid, rule, params))
                item['candidates'] = recording_cache[aid]
        items.append(item)
    return items


def lane_labels(db, group: str, keys: list) -> dict:
    if group == 'source':
        rows = db.execute('SELECT id,kind,label FROM sources WHERE id IN (SELECT value FROM json_each(?))',
                          (json.dumps([k for k in keys if k is not None]),))
        return {int(r['id']): f"{r['label']} ({r['kind']})" for r in rows}
    if group == 'collection':
        rows = db.execute('SELECT id,label FROM collections WHERE id IN (SELECT value FROM json_each(?))',
                          (json.dumps([k for k in keys if k is not None]),))
        return {int(r['id']): r['label'] for r in rows}
    if group == 'kind':
        return {'event': 'zdarzenia', 'transcript_segment': 'segmenty transkrypcji'}
    return {k: str(k) for k in keys if k is not None}


def lanes(db, where: str, args: list, group: str, max_lanes: int) -> dict:
    column = GROUPS[group]
    rows = db.execute(f'''SELECT ti.{column} AS lane, COUNT(*) AS n, MIN(ti.sort_us) AS first_us, MAX(ti.sort_us) AS last_us
                          FROM timeline_items ti WHERE {where} GROUP BY ti.{column}
                          ORDER BY n DESC, lane LIMIT ?''', (*args, max_lanes + 1)).fetchall()
    more = len(rows) > max_lanes
    rows = rows[:max_lanes]
    labels = lane_labels(db, group, [r['lane'] for r in rows])
    out = [{'key': r['lane'], 'label': labels.get(r['lane'], '(brak)') if r['lane'] is not None else '(brak)',
            'count': int(r['n']), 'first': iso(r['first_us']), 'last': iso(r['last_us'])} for r in rows]
    out.sort(key=lambda lane: (lane['key'] is None, str(lane['label']).casefold(), str(lane['key'])))
    return {'group': group, 'lanes': out, 'lanes_truncated': more, 'max_lanes': max_lanes}


def window(db, projection: dict, rule: Rule, *, start_us=None, end_us=None, filters: Filters | None = None,
           cursor: str | None = None, limit: int = 200, order: str = 'asc', match: str = 'display',
           group: str | None = 'source', candidates: bool = True, include_lanes: bool | None = None,
           max_lanes: int = 50) -> dict:
    filters = filters or Filters()
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise QueryInputError(f'limit must be an integer from 1 to {MAX_LIMIT}')
    if order not in ('asc', 'desc') or match not in ('display', 'overlap'):
        raise QueryInputError('order must be asc|desc and match display|overlap')
    if group is not None and group not in GROUPS:
        raise QueryInputError(f'group must be one of {", ".join(GROUPS)}')
    if not 1 <= max_lanes <= MAX_LANES:
        raise QueryInputError(f'max_lanes must be from 1 to {MAX_LANES}')
    if start_us is not None and end_us is not None and end_us <= start_us:
        raise QueryInputError('to must be later than from')
    pid, params = projection['id'], projection['params']
    where, args, notes = filter_sql(db, pid, filters)
    halfwidth = int(projection['build']['counts'].get('max_halfwidth_us', 0))
    range_where, range_args = _range_sql(start_us, end_us, match, halfwidth)
    qhash = _query_hash(start_us, end_us, match, order, filters.canonical())
    # Selective filters over a long range: a primary-key scan plus a top-N sort beats walking the
    # sort index with a row lookup per candidate. Without filters the sort index pages directly.
    filtered = any(v for k, v in filters.canonical().items() if k not in ('transcript_versions', 'include_hidden'))
    scan = filtered and _prefer_scan(db, pid, start_us, end_us)
    if scan:
        range_where, range_args = _range_sql(start_us, end_us, match, halfwidth, scan=True)
    sort_col = '+ti.sort_us' if scan else 'ti.sort_us'
    page_where, page_args = f'{where} AND {range_where}', [*args, *range_args]
    cursor_note = None
    if cursor:
        key, build_id = decode_cursor(cursor, pid, qhash)
        comparator = '>' if order == 'asc' else '<'
        page_where += f' AND ({sort_col},ti.item_kind,ti.ref_id,ti.sub_index) {comparator} (?,?,?,?)'
        page_args += list(key)
        if build_id != projection['build']['id']:
            cursor_note = 'projection_refreshed_since_previous_page'
    direction = 'ASC' if order == 'asc' else 'DESC'
    rows = db.execute(f'''SELECT * FROM timeline_items ti WHERE {page_where}
                          ORDER BY {sort_col} {direction}, ti.item_kind {direction}, ti.ref_id {direction}, ti.sub_index {direction}
                          LIMIT ?''', (*page_args, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(pid, projection['build']['id'], qhash,
                                (rows[-1]['sort_us'], rows[-1]['item_kind'], rows[-1]['ref_id'], rows[-1]['sub_index'])) if has_more else None
    result = {
        'rule': projection['rule'], 'params': params,
        'projection': {'id': pid, 'status': projection['status'], 'build_id': projection['build']['id'],
                       'build_mode': projection['build']['mode']},
        'window': {'from': iso(start_us), 'to': iso(end_us), 'match': match, 'order': order,
                   'to_exclusive': True, 'axis': 'UTC'},
        'filters': filters.canonical(), 'limit': limit, 'has_more': has_more, 'next_cursor': next_cursor,
        'complete': not has_more and not notes.get('text_query', {}).get('truncated', False),
        'items': hydrate(db, rows, rule, params, candidates=candidates, group=group),
        'undated_count': int(db.execute(f'SELECT COUNT(*) FROM timeline_items ti WHERE {where} AND ti.sort_us IS NULL',
                                        args).fetchone()[0]),
        'notes': {**notes, **({'cursor': cursor_note} if cursor_note else {}), 'plan': 'primary_key_scan' if scan else 'sort_index'},
    }
    if include_lanes is None:
        include_lanes = cursor is None
    if include_lanes and group:
        scan_where, scan_args = _range_sql(start_us, end_us, match, halfwidth, scan=scan or _prefer_scan(db, pid, start_us, end_us))
        result['lanes'] = lanes(db, f'{where} AND {scan_where}', [*args, *scan_args], group, max_lanes)
    return result


def iter_window(db, projection: dict, rule: Rule, *, batch: int = 1000, max_rows: int | None = None, **kwargs):
    """Yield every item of a window page by page (bounded by ``max_rows`` when given)."""
    cursor, produced = None, 0
    while True:
        page = window(db, projection, rule, cursor=cursor, limit=batch, include_lanes=False, **kwargs)
        for item in page['items']:
            if max_rows is not None and produced >= max_rows:
                return
            produced += 1
            yield item
        if not page['has_more']:
            return
        cursor = page['next_cursor']


def _bucket_expr(unit: str, a: str = 'ti') -> str:
    """Integer bucket start in SQL; calendar months/years are folded from UTC days in Python."""
    if unit == 'week':
        width, monday = 7 * 86400 * US, 4 * 86400 * US
        return f'({a}.sort_us - (((({a}.sort_us - {monday}) % {width}) + {width}) % {width}))'
    width = APPROX_BUCKET_US[unit if unit in ('minute', 'hour', 'day') else 'day']
    return f'({a}.sort_us - ((({a}.sort_us % {width}) + {width}) % {width}))'


def _bucket_start(unit: str, value) -> int:
    return floor_unit(int(value), unit) if unit in ('month', 'year') else int(value)


def bucket_count(start_us: int, end_us: int, unit: str) -> int:
    first = floor_unit(start_us, unit)
    if unit in ('month', 'year'):
        from .timeutil import from_us
        a, b = from_us(first), from_us(end_us - 1)
        months = (b.year - a.year) * 12 + (b.month - a.month)
        return (months if unit == 'month' else b.year - a.year) + 1
    return (end_us - 1 - first) // APPROX_BUCKET_US[unit] + 1


def choose_bucket(start_us: int, end_us: int, max_buckets: int) -> str:
    for unit in ('minute', 'hour', 'day', 'week', 'month', 'year'):
        if bucket_count(start_us, end_us, unit) <= max_buckets:
            return unit
    return 'year'


def density(db, projection: dict, *, start_us=None, end_us=None, bucket: str = 'auto', filters: Filters | None = None,
            group: str | None = 'source', max_lanes: int = 50, max_buckets: int = 400) -> dict:
    filters = filters or Filters()
    if group is not None and group not in GROUPS:
        raise QueryInputError(f'group must be one of {", ".join(GROUPS)}')
    if bucket != 'auto' and bucket not in BUCKETS:
        raise QueryInputError(f'bucket must be auto or one of {", ".join(BUCKETS)}')
    if not 1 <= max_buckets <= MAX_BUCKETS or not 1 <= max_lanes <= MAX_LANES:
        raise QueryInputError(f'max_buckets must be 1..{MAX_BUCKETS} and max_lanes 1..{MAX_LANES}')
    pid = projection['id']
    where, args, notes = filter_sql(db, pid, filters)
    if start_us is None or end_us is None:
        extent = db.execute(f'SELECT MIN(ti.sort_us), MAX(ti.sort_us) FROM timeline_items ti WHERE {where} AND ti.sort_us IS NOT NULL',
                            args).fetchone()
        if extent[0] is None:
            start_us = start_us if start_us is not None else 0
            end_us = end_us if end_us is not None else start_us + 1
        else:
            start_us = extent[0] if start_us is None else start_us
            end_us = extent[1] + 1 if end_us is None else end_us
    if end_us <= start_us:
        raise QueryInputError('to must be later than from')
    unit = choose_bucket(start_us, end_us, max_buckets) if bucket == 'auto' else bucket
    count = bucket_count(start_us, end_us, unit)
    if count > max_buckets:
        raise QueryLimitError(f'{count} {unit} buckets exceed max_buckets={max_buckets}; use a coarser bucket or a shorter window')
    range_where, range_args = _range_sql(start_us, end_us, 'display', 0, scan=_prefer_scan(db, pid, start_us, end_us))
    column = f'ti.{GROUPS[group]}' if group else "''"
    rows = db.execute(f'''SELECT {column} AS lane, {_bucket_expr(unit)} AS bucket, COUNT(*) AS n,
                            SUM(ti.basis='local_as_utc_unknown_zone') AS unknown_zone
                          FROM timeline_items ti WHERE {where} AND {range_where}
                          GROUP BY lane, bucket''', (*args, *range_args)).fetchall()
    totals: dict = {}
    for r in rows:
        totals[r['lane']] = totals.get(r['lane'], 0) + int(r['n'])
    ranked = sorted(totals, key=lambda k: (-totals[k], str(k)))
    kept = set(ranked[:max_lanes])
    labels = lane_labels(db, group, list(kept)) if group else {}
    series: dict = {}
    for r in rows:
        key = r['lane'] if r['lane'] in kept else '__other__'
        bucket_key = _bucket_start(unit, r['bucket'])
        lane = series.setdefault(key, {})
        n, unknown = lane.get(bucket_key, (0, 0))
        lane[bucket_key] = (n + int(r['n']), unknown + int(r['unknown_zone'] or 0))
    out_lanes = []
    for key, buckets in series.items():
        label = 'inne (poza limitem pasów)' if key == '__other__' else (labels.get(key, '(brak)') if key is not None else '(brak)')
        out_lanes.append({'key': key, 'label': label, 'total': sum(n for n, _ in buckets.values()),
                          'buckets': [[iso(b), b, n, u] for b, (n, u) in sorted(buckets.items())]})
    out_lanes.sort(key=lambda lane: (lane['key'] == '__other__', lane['key'] is None, str(lane['label']).casefold()))
    max_count = max((b[2] for lane in out_lanes for b in lane['buckets']), default=0)
    return {'rule': projection['rule'], 'params': projection['params'],
            'projection': {'id': pid, 'build_id': projection['build']['id'], 'status': projection['status']},
            'window': {'from': iso(start_us), 'to': iso(end_us), 'from_us': start_us, 'to_us': end_us, 'axis': 'UTC',
                       'placement': 'sort time (display); unknown-zone items are placed local-as-UTC'},
            'bucket': unit, 'bucket_count': count, 'bucket_columns': ['start', 'start_us', 'count', 'unknown_zone_count'],
            'group': group, 'lanes': out_lanes, 'lanes_truncated': len(ranked) > max_lanes,
            'total': sum(totals.values()), 'max_count': max_count, 'filters': filters.canonical(), 'notes': notes,
            'undated_count': int(db.execute(f'SELECT COUNT(*) FROM timeline_items ti WHERE {where} AND ti.sort_us IS NULL',
                                            args).fetchone()[0])}


def undated(db, projection: dict, rule: Rule, *, filters: Filters | None = None, after: str | None = None,
            limit: int = 100) -> dict:
    """Items without a usable time, ordered by identity (never by import time)."""
    filters = filters or Filters()
    if not 1 <= limit <= MAX_LIMIT:
        raise QueryInputError(f'limit must be 1..{MAX_LIMIT}')
    where, args, notes = filter_sql(db, projection['id'], filters)
    qhash = _query_hash('undated', filters.canonical())
    page_where, page_args = f'{where} AND ti.sort_us IS NULL', list(args)
    if after:
        key, _ = decode_cursor(after, projection['id'], qhash)
        page_where += ' AND (ti.item_kind,ti.ref_id,ti.sub_index) > (?,?,?)'
        page_args += list(key[1:])
    rows = db.execute(f'''SELECT * FROM timeline_items ti WHERE {page_where}
                          ORDER BY ti.item_kind, ti.ref_id, ti.sub_index LIMIT ?''', (*page_args, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    cursor = encode_cursor(projection['id'], projection['build']['id'], qhash,
                           (0, rows[-1]['item_kind'], rows[-1]['ref_id'], rows[-1]['sub_index'])) if has_more else None
    return {'rule': projection['rule'], 'order': 'item identity (kind, id, segment); not a time order',
            'items': hydrate(db, rows, rule, projection['params'], candidates=True, group=None),
            'has_more': has_more, 'next_cursor': cursor, 'limit': limit, 'notes': notes}


def parse_key(key: str) -> tuple[str, int, int]:
    parts = key.split(':') if isinstance(key, str) else []
    try:
        if len(parts) == 2 and parts[0] == 'event':
            return 'event', int(parts[1]), 0
        if len(parts) == 3 and parts[0] == 'segment':
            return 'transcript_segment', int(parts[1]), int(parts[2])
    except ValueError:
        pass
    raise QueryInputError('item key must be event:<id> or segment:<version>:<index>')


def item_detail(db, projection: dict, rule: Rule, key: str) -> dict | None:
    kind, ref, sub = parse_key(key)
    row = db.execute('''SELECT * FROM timeline_items ti WHERE projection_id=? AND item_kind=? AND ref_id=? AND sub_index=?''',
                     (projection['id'], kind, ref, sub)).fetchone()
    if row is None:
        return None
    item = hydrate(db, [row], rule, projection['params'], candidates=True, group=None)[0]
    if kind == 'event':
        tags = [r[0] for r in db.execute('''SELECT t.name FROM event_tags et JOIN tags t ON t.id=et.tag_id
                                             WHERE et.event_id=? ORDER BY t.name LIMIT 200''', (ref,))]
        left, left_id = 'event', ref
    else:
        tags = [r[0] for r in db.execute('''SELECT t.name FROM artifact_tags at JOIN tags t ON t.id=at.tag_id
                                             WHERE at.artifact_id=? ORDER BY t.name LIMIT 200''', (row['artifact_id'],))]
        left, left_id = 'artifact', row['artifact_id']
    hypotheses = [dict(r) for r in db.execute('''SELECT h.id,h.rule_id,h.rule_version,h.relation,h.left_kind,h.left_id,
          h.right_kind,h.right_id,h.ambiguity_group,h.candidate_count,
          (SELECT s.status FROM timeline_hypothesis_status s WHERE s.hypothesis_id=h.id ORDER BY s.id DESC LIMIT 1) AS status
        FROM timeline_hypotheses h WHERE (h.left_kind=? AND h.left_id=?) OR (h.right_kind=? AND h.right_id=?)
        ORDER BY h.id DESC LIMIT 100''', (left, left_id, left, left_id))]
    item['tags'] = tags
    item['hypotheses'] = hypotheses
    item['rule'] = projection['rule']
    item['params'] = projection['params']
    return item


def legacy_events(db, projection: dict, *, limit: int, phone: str | None, event_type: str | None) -> list[dict]:
    """Old ``/api/timeline`` shape: event rows, newest projected time first, undated last (by id)."""
    clauses, args = ["ti.projection_id=?", "ti.item_kind='event'"], [projection['id']]
    if phone:
        clauses.append('e.phone_or_address=?')
        args.append(phone)
    if event_type:
        clauses.append('e.event_type=?')
        args.append(event_type)
    where = ' AND '.join(clauses)
    rows = [dict(r) for r in db.execute(f'''SELECT e.* FROM timeline_items ti JOIN events e ON e.id=ti.ref_id
        WHERE {where} AND ti.sort_us IS NOT NULL ORDER BY ti.sort_us DESC, ti.ref_id DESC LIMIT ?''', (*args, limit))]
    if len(rows) < limit:
        rows += [dict(r) for r in db.execute(f'''SELECT e.* FROM timeline_items ti JOIN events e ON e.id=ti.ref_id
            WHERE {where} AND ti.sort_us IS NULL ORDER BY ti.ref_id DESC LIMIT ?''', (*args, limit - len(rows)))]
    return rows


EXPORT_COLUMNS = ('key', 'kind', 'display_utc', 'basis', 'precision', 'interval_from', 'interval_to', 'end_utc',
                  'candidate_ref', 'candidate_kind', 'flags', 'event_type', 'event_id', 'derived_text_id', 'segment_index',
                  'artifact_id', 'source_id', 'collection_id', 'counterparty', 'chatbot', 'thread_id', 'direction',
                  'language', 'title', 'text', 'text_truncated')
_FORMULA = ('=', '+', '-', '@', '\t', '\r')


def _export_row(item: dict) -> dict:
    t = item['time']
    return {'key': item['key'], 'kind': item['kind'], 'display_utc': t['display'], 'basis': t['basis'],
            'precision': t['precision'], 'interval_from': (t['interval'] or {}).get('from'),
            'interval_to': (t['interval'] or {}).get('to'), 'end_utc': t.get('end'), 'candidate_ref': t['candidate_ref'],
            'candidate_kind': t['candidate_kind'], 'flags': json.dumps(t['flags'], ensure_ascii=False, sort_keys=True),
            'event_type': item.get('event_type'), 'event_id': item.get('event_id'),
            'derived_text_id': item.get('derived_text_id'), 'segment_index': item.get('segment_index'),
            'artifact_id': item.get('artifact_id'), 'source_id': item.get('source_id'),
            'collection_id': item.get('collection_id'), 'counterparty': item.get('counterparty'),
            'chatbot': item.get('chatbot'), 'thread_id': item.get('thread_id'), 'direction': item.get('direction'),
            'language': item.get('language'), 'title': item.get('title'), 'text': item.get('text'),
            'text_truncated': item.get('text_truncated')}


def export_window(db, projection: dict, rule: Rule, *, fmt: str, max_rows: int = 50_000, **kwargs) -> tuple[bytes, dict]:
    """CSV or JSON bytes of every item in a window (bounded); coverage stays explicit."""
    if fmt not in ('csv', 'json'):
        raise QueryInputError('format must be csv or json')
    if not 1 <= max_rows <= MAX_EXPORT_ROWS:
        raise QueryInputError(f'max_rows must be 1..{MAX_EXPORT_ROWS}')
    kwargs.setdefault('candidates', False)
    items = list(iter_window(db, projection, rule, max_rows=max_rows + 1, **kwargs))
    complete = len(items) <= max_rows
    items = items[:max_rows]
    meta = {'schema': 'ageds.timeline.window.v1', 'rule': projection['rule'], 'params': projection['params'],
            'projection': {'id': projection['id'], 'build_id': projection['build']['id']},
            'window': {k: kwargs.get(k) for k in ('match', 'order')} | {
                'from': iso(kwargs.get('start_us')), 'to': iso(kwargs.get('end_us'))},
            'filters': (kwargs.get('filters') or Filters()).canonical(), 'row_count': len(items), 'complete': complete,
            'max_rows': max_rows,
            'scope': 'timeline projection metadata and bounded source text; not source bytes, not a signed record'}
    if fmt == 'json':
        meta['items'] = items
        return (json.dumps(meta, ensure_ascii=False, allow_nan=False, indent=1) + '\n').encode('utf-8'), meta
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS, lineterminator='\n')
    writer.writeheader()
    for item in items:
        # Every text cell is guarded: parsers keep raw source values (e.g. a billing
        # direction cell) in columns that are not free text.
        writer.writerow({name: "'" + value if isinstance(value, str) and value.startswith(_FORMULA) else value
                         for name, value in _export_row(item).items()})
    meta['csv_note'] = "text cells beginning with = + - @ tab or CR are prefixed with ' (spreadsheet formula guard); JSON export is verbatim"
    return buffer.getvalue().encode('utf-8'), meta


def facets(db, projection: dict, limit: int = 500) -> dict:
    pid = projection['id']
    def grouped(column):
        return [{'value': r[0], 'count': int(r[1])} for r in db.execute(
            f'''SELECT {column}, COUNT(*) FROM timeline_items WHERE projection_id=? AND {column} IS NOT NULL
                GROUP BY {column} ORDER BY COUNT(*) DESC, {column} LIMIT ?''', (pid, limit))]
    sources = grouped('source_id')
    collections = grouped('collection_id')
    source_labels = lane_labels(db, 'source', [s['value'] for s in sources])
    collection_labels = lane_labels(db, 'collection', [c['value'] for c in collections])
    for s in sources:
        s['label'] = source_labels.get(s['value'])
    for c in collections:
        c['label'] = collection_labels.get(c['value'])
    extent = db.execute('SELECT MIN(sort_us), MAX(sort_us), COUNT(*) FROM timeline_items WHERE projection_id=? AND sort_us IS NOT NULL',
                        (pid,)).fetchone()
    return {'event_types': grouped('event_type'), 'kinds': grouped('item_kind'), 'sources': sources,
            'collections': collections, 'chatbots': grouped('chatbot'),
            'tags': [r[0] for r in db.execute('SELECT name FROM tags ORDER BY name LIMIT ?', (limit,))],
            'extent': {'from': iso(extent[0]), 'to': iso(extent[1]), 'from_us': extent[0], 'to_us': extent[1],
                       'dated_items': int(extent[2])},
            'undated_items': int(db.execute('SELECT COUNT(*) FROM timeline_items WHERE projection_id=? AND sort_us IS NULL',
                                            (pid,)).fetchone()[0]),
            'groups': list(GROUPS), 'buckets': list(BUCKETS), 'rule': projection['rule'], 'params': projection['params']}

"""Typed intents for conversational clients ("Function 1.5").

A client (for example ChatADHD) lets a model parse the user's question into one
of the intents below. AGEDS validates the intent against ``INTENT_SCHEMA`` and
executes it deterministically. The result is a list of **anchored items** —
exact stored text with its artifact, transcript version, selector and quote
hash — and never synthesized prose (``prose`` is always ``null`` in anchored
mode). Deterministic data (IDs, hashes, selectors, times) comes from code.

Interpretive answers are a separate, explicitly labelled contract:
:func:`label_interpretive_answer` records provider/model/run (``unknown`` when
absent) and marks every claim that references no known item id as
``unsupported``. It performs no model call and verifies references, not truth.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from ..citations import MAX_MILLISECONDS, loads_transcript_segments, projection_from_segments
from . import read_api

RESULT_SCHEMA = 'ageds.intent-result/v1'
INTERPRETIVE_SCHEMA = 'ageds.interpretive-answer/v1'
INTENT_KINDS = ('search', 'timeline_window', 'artifact', 'citation', 'quote')
MAX_ITEMS = 200
MAX_SEGMENT_ITEMS_PER_HIT = 20
MAX_SEGMENTS_SCANNED = 10_000
MAX_TERMS = 32
MAX_CLAIMS = 500
MAX_CLAIM_CHARS = 4096
MAX_PROSE_CHARS = 65_536
MAX_ITEM_IDS = 10_000
MATCH_RULE = 'nfkd_strip_marks_strokes_casefold_substring/v1'
_ITEM_ID = re.compile(r'^(segment|event|artifact|citation|derived_text|quote):[0-9:\-]{1,64}$')
_FTS_OPERATORS = {'AND', 'OR', 'NOT', 'NEAR'}

_POSITIVE_INT = {'type': 'integer', 'minimum': 1, 'maximum': MAX_MILLISECONDS}
INTENT_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    '$id': 'https://ageds.invalid/schemas/intent/v1',
    'title': 'AGEDS read intent v1',
    'type': 'object',
    'required': ['kind'],
    'properties': {'kind': {'enum': list(INTENT_KINDS)}},
    'oneOf': [
        {'properties': {'kind': {'const': 'search'},
                        'q': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': read_api.LIMITS['search']['max_limit']}},
         'required': ['kind', 'q'], 'additionalProperties': False},
        {'properties': {'kind': {'const': 'timeline_window'},
                        'from': {'type': 'string', 'maxLength': read_api.TIMELINE_MAX_TIME_CHARS},
                        'to': {'type': 'string', 'maxLength': read_api.TIMELINE_MAX_TIME_CHARS},
                        'event_type': {'type': 'string', 'maxLength': read_api.TIMELINE_MAX_FILTER_CHARS},
                        'phone': {'type': 'string', 'maxLength': read_api.TIMELINE_MAX_FILTER_CHARS},
                        'direction': {'type': 'string', 'maxLength': read_api.TIMELINE_MAX_FILTER_CHARS},
                        'thread_id': {'type': 'string', 'maxLength': read_api.TIMELINE_MAX_FILTER_CHARS},
                        'artifact_id': _POSITIVE_INT, 'source_id': _POSITIVE_INT,
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': read_api.TIMELINE_MAX_LIMIT},
                        'cursor': {'type': 'string', 'maxLength': 512}},
         'required': ['kind'], 'additionalProperties': False},
        {'properties': {'kind': {'const': 'artifact'}, 'artifact_id': _POSITIVE_INT},
         'required': ['kind', 'artifact_id'], 'additionalProperties': False},
        {'properties': {'kind': {'const': 'citation'}, 'artifact_id': _POSITIVE_INT, 'anchor_id': _POSITIVE_INT},
         'required': ['kind', 'artifact_id', 'anchor_id'], 'additionalProperties': False},
        {'properties': {'kind': {'const': 'quote'}, 'artifact_id': _POSITIVE_INT, 'derived_text_id': _POSITIVE_INT,
                        'segment_indices': {'type': 'array', 'minItems': 1, 'maxItems': 10_000,
                                            'items': {'type': 'integer', 'minimum': 0}}},
         'required': ['kind', 'artifact_id', 'derived_text_id', 'segment_indices'], 'additionalProperties': False},
    ],
}

_FIELD_TYPES = {
    'search': {'q': str, 'limit': int},
    'timeline_window': {'from': str, 'to': str, 'event_type': str, 'phone': str, 'direction': str,
                        'thread_id': str, 'artifact_id': int, 'source_id': int, 'limit': int, 'cursor': str},
    'artifact': {'artifact_id': int},
    'citation': {'artifact_id': int, 'anchor_id': int},
    'quote': {'artifact_id': int, 'derived_text_id': int, 'segment_indices': list},
}
_REQUIRED = {'search': ('q',), 'timeline_window': (), 'artifact': ('artifact_id',),
             'citation': ('artifact_id', 'anchor_id'), 'quote': ('artifact_id', 'derived_text_id', 'segment_indices')}


class IntentError(ValueError):
    """The intent does not satisfy ``INTENT_SCHEMA`` (HTTP 422)."""


def validate_intent(intent) -> dict:
    """Validate without a JSON Schema library; the published schema is the contract."""
    if not isinstance(intent, dict):
        raise IntentError('intent must be a JSON object')
    kind = intent.get('kind')
    if kind not in INTENT_KINDS:
        raise IntentError('kind must be one of: ' + ', '.join(INTENT_KINDS))
    allowed = _FIELD_TYPES[kind]
    extra = set(intent) - set(allowed) - {'kind'}
    if extra:
        raise IntentError(f'{kind} intent has unknown fields: ' + ', '.join(sorted(extra)))
    for name in _REQUIRED[kind]:
        if name not in intent:
            raise IntentError(f'{kind} intent requires {name}')
    for name, expected in allowed.items():
        if name not in intent:
            continue
        value = intent[name]
        if type(value) is not expected:
            raise IntentError(f'{name} must be {expected.__name__}')
        if expected is int and not 1 <= value <= MAX_MILLISECONDS and name != 'limit':
            raise IntentError(f'{name} must be a positive SQLite integer')
        if expected is str and ('\x00' in value or not value and name == 'q'):
            raise IntentError(f'{name} must be nonempty text without NUL')
    if kind == 'search':
        if len(intent['q'].encode('utf-8', 'surrogatepass')) > 4096:
            raise IntentError('q exceeds 4096 UTF-8 bytes')
        limit = intent.get('limit')
        if limit is not None and not 1 <= limit <= read_api.LIMITS['search']['max_limit']:
            raise IntentError('limit outside the search limit range')
    if kind == 'timeline_window':
        limit = intent.get('limit')
        if limit is not None and not 1 <= limit <= read_api.TIMELINE_MAX_LIMIT:
            raise IntentError('limit outside the timeline limit range')
    if kind == 'quote':
        indices = intent['segment_indices']
        if not indices or len(indices) > 10_000 or any(type(i) is not int or i < 0 for i in indices):
            raise IntentError('segment_indices must be nonnegative integers (1..10000 entries)')
    return dict(intent)


_STROKES = str.maketrans({'ł': 'l', 'Ł': 'L', 'ø': 'o', 'Ø': 'O', 'đ': 'd', 'Đ': 'D', 'ħ': 'h', 'Ħ': 'H', 'ı': 'i', 'ŧ': 't', 'Ŧ': 'T', 'ß': 'ss', 'æ': 'ae', 'Æ': 'AE', 'œ': 'oe', 'Œ': 'OE'})


def _normalize(text: str) -> str:
    """Fold at least as permissively as FTS5 unicode61: marks stripped, stroke letters mapped, casefold."""
    decomposed = unicodedata.normalize('NFKD', text.translate(_STROKES))
    return ''.join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def query_terms(q: str) -> list[str]:
    """Literal phrases and barewords of an FTS5 query, for locating segments.

    Operators, column filters and parentheses are dropped; a trailing ``*`` is
    removed (substring matching already covers prefixes). This is a locator
    heuristic for anchoring, not a reimplementation of FTS5 matching.
    """
    terms = []
    for phrase, bare in re.findall(r'"((?:[^"]|"")*)"|([^\s"(){}+^,]+)', q):
        if phrase:
            term = phrase.replace('""', '"')
        else:
            term = bare
            if term in _FTS_OPERATORS or term.startswith('-'):
                continue
            if ':' in term:  # column filter such as title:word; keep the word
                term = term.rsplit(':', 1)[1]
            term = term.rstrip('*')
        term = term.strip()
        if term and term not in terms:
            terms.append(term)
        if len(terms) >= MAX_TERMS:
            break
    return terms


def _item_segment(artifact_id, derived_text_id, segments, index, *, match=None) -> dict:
    projection = projection_from_segments(segments, [index])
    return {
        'item_id': f'segment:{derived_text_id}:{index}', 'kind': 'transcript_segment',
        'artifact_id': artifact_id, 'derived_text_id': derived_text_id, 'selector': projection['selector'],
        'quote_text': projection['quote_text'], 'quote_sha256': projection['quote_sha256'],
        'start_ms': projection['start_ms'], 'end_ms': projection['end_ms'], 'match': match,
        'verification': 'projection_from_stored_segments', 'audio_verification': 'not_performed',
    }


def _event_item(row) -> dict:
    body = row['body'] if isinstance(row['body'], str) else ''
    return {
        'item_id': f'event:{row["id"]}', 'kind': 'event', 'event_id': row['id'], 'artifact_id': row['artifact_id'],
        'source_id': row['source_id'], 'event_type': row['event_type'], 'ts_start': row['ts_start'],
        'direction': row['direction'], 'contact_label': row['contact_label'], 'phone_or_address': row['phone_or_address'],
        'subject': row['subject'], 'quote_text': body, 'quote_field': 'body',
        'quote_sha256': hashlib.sha256(body.encode('utf-8', 'surrogatepass')).hexdigest(),
        'external_id': row['external_id'], 'thread_id': row['thread_id'], 'time_status': 'stored_ts_start_text' if row['ts_start'] else 'untimed',
        'verification': 'exact_stored_event_text',
    }


def _search_items(result: dict) -> tuple[list, bool]:
    terms = query_terms(result['query'])
    normalized = [_normalize(t) for t in terms if _normalize(t)]
    items, truncated = [], False

    def reader(db):
        nonlocal truncated
        for hit in result['items']:
            if len(items) >= MAX_ITEMS:
                truncated = True
                break
            kind, object_id = hit['object_kind'], hit['object_id']
            if kind == 'derived_text':
                row = db.execute('SELECT id,artifact_id,kind,segments_json FROM derived_text WHERE id=?', (object_id,)).fetchone()
                if row is None:
                    continue
                reference = {'item_id': f'derived_text:{object_id}', 'kind': 'derived_text_reference', 'artifact_id': row['artifact_id'],
                             'derived_text_id': object_id, 'derived_kind': row['kind'], 'title': hit['title'], 'snippet': hit['snippet'],
                             'anchor': None, 'reason': None, 'verification': 'fts_hit_without_segment_anchor'}
                if row['kind'] != 'transcript':
                    reference['reason'] = 'not_a_transcript_version'
                    items.append(reference)
                    continue
                try:
                    segments = loads_transcript_segments(row['segments_json'])
                except ValueError:
                    reference['reason'] = 'stored_segments_invalid'
                    items.append(reference)
                    continue
                if len(segments) > MAX_SEGMENTS_SCANNED:
                    reference['reason'] = 'segment_limit_exceeded'
                    items.append(reference)
                    continue
                found = 0
                for index, segment in enumerate(segments):
                    text = segment.get('text') if isinstance(segment, dict) else None
                    if not isinstance(text, str):
                        continue
                    haystack = _normalize(text)
                    matched = [term for term, norm in zip(terms, normalized) if norm in haystack]
                    if not matched:
                        continue
                    if found >= MAX_SEGMENT_ITEMS_PER_HIT or len(items) >= MAX_ITEMS:
                        truncated = True
                        break
                    try:
                        items.append(_item_segment(row['artifact_id'], object_id, segments, index,
                                                   match={'rule': MATCH_RULE, 'terms': matched}))
                        found += 1
                    except ValueError as error:
                        items.append({**reference, 'item_id': f'derived_text:{object_id}:{index}', 'reason': 'segment_not_citable: ' + str(error)})
                if found == 0 and (not items or items[-1].get('derived_text_id') != object_id):
                    reference['reason'] = 'no_segment_contains_query_terms'
                    items.append(reference)
            elif kind == 'event':
                row = db.execute('SELECT * FROM events WHERE id=?', (object_id,)).fetchone()
                if row is not None:
                    items.append(_event_item(row))
            elif kind == 'artifact':
                row = db.execute('SELECT id,sha256,original_name,mime_type,size_bytes,source_locator FROM artifacts WHERE id=?', (object_id,)).fetchone()
                if row is not None:
                    items.append({'item_id': f'artifact:{object_id}', 'kind': 'artifact_reference', **dict(row), 'anchor': None,
                                  'verification': 'artifact_metadata_row'})
        return items
    read_api.run_read(reader)
    return items, truncated


def _quote_items(artifact_id: int, derived_text_id: int, indices: list[int]) -> list:
    def reader(db):
        row = db.execute("SELECT segments_json FROM derived_text WHERE id=? AND artifact_id=? AND kind='transcript'",
                         (derived_text_id, artifact_id)).fetchone()
        if row is None:
            raise read_api.ReadNotFound('transcript version does not belong to artifact')
        segments = loads_transcript_segments(row['segments_json'])
        projection = projection_from_segments(segments, indices)
        return [{
            'item_id': f'quote:{derived_text_id}:{indices[0]}-{indices[-1]}', 'kind': 'transcript_selection',
            'artifact_id': artifact_id, 'derived_text_id': derived_text_id, 'selector': projection['selector'],
            'quote_text': projection['quote_text'], 'quote_sha256': projection['quote_sha256'],
            'start_ms': projection['start_ms'], 'end_ms': projection['end_ms'],
            'verification': 'projection_from_stored_segments', 'audio_verification': 'not_performed',
        }]
    try:
        return read_api.run_read(reader)
    except ValueError as error:
        if isinstance(error, read_api.ReadNotFound):
            raise
        raise read_api.ReadInputError(str(error)) from error


def _citation_item(artifact_id: int, anchor_id: int) -> dict:
    packet = json.loads(read_api.read_citation_packet(artifact_id, anchor_id).decode('utf-8'))
    payload = packet['payload']
    anchor = payload['anchor']
    return {
        'item_id': f'citation:{artifact_id}:{anchor_id}', 'kind': 'citation', 'artifact_id': artifact_id,
        'anchor_id': anchor_id, 'derived_text_id': anchor['derived_text_id'], 'selector': payload['projection']['selector'],
        'quote_text': anchor['quote_text'], 'quote_sha256': anchor['quote_sha256'], 'start_ms': anchor['start_ms'],
        'end_ms': anchor['end_ms'], 'packet_schema': packet['schema'], 'packet_payload_sha256': packet['integrity']['payload_sha256'],
        'unknowns': payload['unknowns'], 'verification': 'citation_packet_projection_recomputed', 'audio_verification': 'not_performed',
    }


def execute_intent(intent) -> dict:
    """Validate and run one intent; the result never contains model prose."""
    intent = validate_intent(intent)
    kind = intent['kind']
    has_more = complete = None
    truncated = False
    extra = {}
    if kind == 'search':
        page = read_api.read_search(intent['q'], intent.get('limit'))
        items, truncated = _search_items(page)
        has_more, complete = page['has_more'], page['complete']
        extra = {'search_limit': page['limit'], 'match_rule': MATCH_RULE, 'coverage_note': page['coverage_note']}
    elif kind == 'timeline_window':
        filters = {name: intent[name] for name in ('event_type', 'phone', 'direction', 'thread_id', 'artifact_id', 'source_id') if name in intent}
        window = read_api.read_timeline(from_=intent.get('from'), to=intent.get('to'), filters=filters,
                                        limit=intent.get('limit'), cursor=intent.get('cursor'))
        items = [{'item_id': f'event:{event["id"]}', 'kind': 'event', 'event_id': event['id'], 'quote_field': 'body', 'quote_text': event['body'],
                  'quote_sha256': event['body_sha256'], 'verification': 'exact_stored_event_text', **event} for event in window['items']]
        has_more, complete = window['has_more'], window['complete']
        extra = {'next_cursor': window['next_cursor'], 'display_rule': window['display_rule'],
                 'window_semantics': window['window_semantics'],
                 'untimed_events_excluded_by_window': window['untimed_events_excluded_by_window']}
    elif kind == 'artifact':
        artifact = read_api.read_artifact(intent['artifact_id'])
        items = [{'item_id': f'artifact:{intent["artifact_id"]}', 'kind': 'artifact', 'anchor': None,
                  'verification': 'artifact_metadata_row', **{k: v for k, v in artifact.items() if k not in ('schema', 'kind')}}]
        has_more, complete = False, True
    elif kind == 'citation':
        items = [_citation_item(intent['artifact_id'], intent['anchor_id'])]
        has_more, complete = False, True
    else:
        items = _quote_items(intent['artifact_id'], intent['derived_text_id'], intent['segment_indices'])
        has_more, complete = False, True
    return {'schema': RESULT_SCHEMA, 'mode': 'anchored', 'intent': intent, 'items': items, 'count': len(items),
            'has_more': has_more, 'complete': complete, 'truncated': truncated, 'max_items': MAX_ITEMS,
            'prose': None, 'note': 'anchored mode returns stored text with anchors; no synthesized prose', **extra}


class InterpretiveError(ValueError):
    """The interpretive answer envelope is malformed (HTTP 422)."""


def label_interpretive_answer(answer, known_item_ids=None) -> dict:
    """Attach the mandatory interpretive label and flag unreferenced claims.

    ``known_item_ids`` (or ``answer['item_ids_available']``) is the set of item
    ids the client obtained from anchored results. A claim is ``referenced``
    only when every id it cites is in that set; otherwise ``unsupported``.
    """
    if not isinstance(answer, dict):
        raise InterpretiveError('answer must be a JSON object')
    if answer.get('mode') != 'interpretive':
        raise InterpretiveError("mode must be 'interpretive'; anchored results carry no claims")
    prose = answer.get('prose', '')
    if not isinstance(prose, str) or len(prose) > MAX_PROSE_CHARS:
        raise InterpretiveError(f'prose must be text up to {MAX_PROSE_CHARS} characters')
    claims = answer.get('claims', [])
    if not isinstance(claims, list) or len(claims) > MAX_CLAIMS:
        raise InterpretiveError(f'claims must be an array of up to {MAX_CLAIMS} entries')
    available = known_item_ids if known_item_ids is not None else answer.get('item_ids_available', [])
    if not isinstance(available, (list, set, tuple)) or len(available) > MAX_ITEM_IDS:
        raise InterpretiveError('item_ids_available must be an array of item ids')
    known = set()
    for item_id in available:
        if not isinstance(item_id, str) or not _ITEM_ID.match(item_id):
            raise InterpretiveError('item ids must look like segment:<version>:<index>, event:<id>, artifact:<id>, citation:<artifact>:<anchor>')
        known.add(item_id)
    labelled, unsupported = [], 0
    for position, claim in enumerate(claims):
        if not isinstance(claim, dict) or not isinstance(claim.get('text'), str) or not claim['text'].strip() \
                or len(claim['text']) > MAX_CLAIM_CHARS:
            raise InterpretiveError(f'claim {position} must have nonblank text up to {MAX_CLAIM_CHARS} characters')
        refs = claim.get('item_ids', [])
        if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
            raise InterpretiveError(f'claim {position} item_ids must be an array of strings')
        missing = [r for r in refs if r not in known]
        supported = bool(refs) and not missing
        if not supported:
            unsupported += 1
        labelled.append({'index': position, 'text': claim['text'], 'item_ids': refs, 'unknown_item_ids': missing,
                         'support': 'referenced' if supported else 'unsupported'})

    def recorded(name):
        value = answer.get(name)
        return value if isinstance(value, str) and value.strip() else 'unknown'
    return {
        'schema': INTERPRETIVE_SCHEMA, 'mode': 'interpretive',
        'label': {'mode': 'interpretive', 'provider': recorded('provider'), 'model': recorded('model'), 'run': recorded('run'),
                  'generated_by': 'external_client', 'ageds_verified': 'item references only, not truth',
                  'parameters': answer.get('parameters') if isinstance(answer.get('parameters'), dict) else {}},
        'prose': prose, 'claims': labelled, 'claim_count': len(labelled), 'unsupported_count': unsupported,
        'supported_count': len(labelled) - unsupported, 'known_item_count': len(known),
        'note': 'every claim without known item references is unsupported; AGEDS performed no model call',
    }

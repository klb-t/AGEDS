"""Claude.ai data export importer (``conversations.json``).

Each ``chat_messages`` entry becomes one ``chat_message`` event whose body is the
visible text (``text`` content blocks; the message ``text`` field only when no
blocks exist). ``thinking``, ``tool_use``, ``tool_result`` and unknown block
types each become a separate ``chat_message_part`` event with its own
``content_kind`` (``reasoning``, ``tool_call``, ``tool_output``, ``unknown``) and
are never merged into the answer text. Attachment ``extracted_content`` is a
part with ``content_kind='attachment_extract'``; the attachment file itself is
not in the export and its reference stays ``unresolved``.

``parent_message_uuid`` (when present) gives the branch tree; the export does
not declare a current leaf, so ``on_current_path`` stays unknown. ISO times with
``Z``/offset are ``explicit_offset`` candidates; without an offset they stay
local with ``tz_status='unknown'``. Key paths live in
``data/claude_mapping.v1.json``.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

from .chat_common import (
    DATA_DIR, CHILDREN_LISTED_MAX, ChatImportLimits, ConversationPlan, Element, FileContext, PlannedEvent,
    PlannedRecord, PlannedRef, bounded_body, bounded_json, bounded_list, canonical_sha256, collect_text,
    duplicate_keys, expand, first_of, load_mapping, run_import, scalar_or_none, text_sha256, time_candidate,
    tree_depths, utc_of,
)

PARSER_NAME = 'claude_export'
PARSER_VERSION = '1'
NORMALIZATION_VERSION = 'claude-content-v1'
DEFAULT_MAPPING = DATA_DIR / 'claude_mapping.v1.json'
REQUIRED_SECTIONS = ('conversation', 'message', 'block', 'block_types', 'unknown_block_type', 'direction_by_role',
                     'attachment', 'file')
VISIBLE_JOIN = '\n\n'


def load_claude_mapping(path: Path | str | None = None):
    return load_mapping(path or DEFAULT_MAPPING, namespace='claude', required=REQUIRED_SECTIONS)


def _last_token(pointer: str | None, fallback: str) -> str:
    return pointer.rsplit('/', 1)[-1] if pointer else fallback


def plan_conversation(conv, element: Element, ctx: FileContext) -> ConversationPlan | None:
    m = ctx.mapping.data
    issues = ctx.issues
    file = ctx.file.name

    def loc(rel: str) -> str:
        return f'{file}#/{element.index}{rel}'

    if not isinstance(conv, dict):
        issues.add('conversation_not_object', loc(''))
        return None
    for key in duplicate_keys(conv):
        issues.add('duplicate_key', loc(''), key=key)
    cm, mm, bm = m['conversation'], m['message'], m['block']
    time_cfg = m['time']
    formats, zero_placeholder = time_cfg['formats'], bool(time_cfg.get('zero_is_placeholder'))
    direction_by_role = m['direction_by_role']
    sentinels = set(m.get('root_parent_sentinels') or [])
    parser_meta = ctx.parser_metadata()
    stats = Counter()

    conv_id, _ = first_of(conv, cm['id'])
    if not isinstance(conv_id, str) or not conv_id:
        issues.add('conversation_id_missing', loc(''))
        conv_id = None
    container = conv_id or f'#index:{element.index}'
    occurrence = ctx.seen_containers[container]
    ctx.seen_containers[container] += 1
    if occurrence:
        issues.add('conversation_repeated_in_file', loc(''), conversation_id=container, occurrence=occurrence)
    conv_key = container + (f'~occ{occurrence}' if occurrence else '')
    title, _ = first_of(conv, cm['title'])
    if title is not None and not isinstance(title, str):
        issues.add('title_not_string', loc(''))
    title_text = scalar_or_none(title)
    summary, _ = first_of(conv, cm.get('summary', []))
    account, _ = first_of(conv, cm.get('account', []))
    default_model, _ = first_of(conv, cm.get('default_model', []))
    current_leaf, _ = first_of(conv, cm.get('current_leaf', []))

    def times_for(obj, section: dict, path: str, item_issues: list):
        out = []
        for kind, mapping_key in (('created', 'create_time'), ('updated', 'update_time')):
            value, where = first_of(obj, section[mapping_key])
            candidate = time_candidate(kind, value, ctx.time_source(f'{path}.{_last_token(where, mapping_key)}'),
                                       formats=formats, zero_is_placeholder=zero_placeholder)
            if candidate is None:
                if kind == 'created':
                    item_issues.append(f'{mapping_key}_missing_or_null')
                continue
            status = candidate.metadata.get('parse_status')
            if status != 'parsed':
                item_issues.append(f'{mapping_key}_{status}')
            elif candidate.tz_status == 'unknown':
                item_issues.append(f'{mapping_key}_without_offset')
            out.append(candidate)
        return out

    messages, messages_rel = first_of(conv, cm['messages'])
    if not isinstance(messages, list):
        issues.add('messages_missing' if messages is None else 'messages_not_list', loc(''))
        messages, messages_rel = [], messages_rel or '/chat_messages'
    node_count = len(messages)
    if node_count > ctx.limits.max_nodes_per_conversation:
        issues.add('node_limit_reached', loc(messages_rel), nodes=node_count, limit=ctx.limits.max_nodes_per_conversation)
        messages = messages[:ctx.limits.max_nodes_per_conversation]

    entries = []
    seen_ids = Counter()
    parent_of: dict[str, str | None] = {}
    parent_declared: dict[str, bool] = {}
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            issues.add('message_not_object', loc(f'{messages_rel}/{i}'))
            continue
        msg_id, _ = first_of(message, mm['id'])
        if not isinstance(msg_id, str) or not msg_id:
            issues.add('message_id_missing', loc(f'{messages_rel}/{i}'))
            msg_id = None
        dup = seen_ids[msg_id] if msg_id else 0
        if msg_id:
            seen_ids[msg_id] += 1
        if dup:
            issues.add('duplicate_message_id', loc(f'{messages_rel}/{i}'), message_id=msg_id, duplicate_index=dup)
        declared = any(True for _ in (hit for p in mm['parent'] for hit in expand(message, p)))
        parent_raw, _ = first_of(message, mm['parent'])
        parent = None if (parent_raw is None or parent_raw in sentinels or not isinstance(parent_raw, str)) else parent_raw
        if msg_id and not dup:
            parent_of[msg_id] = parent
            parent_declared[msg_id] = declared
        entries.append((i, message, msg_id, dup, parent, declared, parent_raw))
    children_of: dict[str, list[str]] = defaultdict(list)
    for msg_id, parent in parent_of.items():
        if parent is not None:
            children_of[parent].append(msg_id)
    depth, cycle_members = tree_depths(parent_of)
    if cycle_members:
        issues.add('cycle_in_tree', loc(messages_rel), nodes=sorted(cycle_members)[:50], count=len(cycle_members))
    order = [e[2] for e in entries]

    events: list[PlannedEvent] = []
    parts: list[PlannedEvent] = []
    for position, (i, message, msg_id, dup, parent, declared, parent_raw) in enumerate(entries):
        msg_rel = f'{messages_rel}/{i}'
        record_id = msg_id or f'#index:{i}'
        record_path = f'{conv_key}/{record_id}' + (f'~dup{dup}' if dup else '')
        occurrence_locator = loc(msg_rel)
        item_issues: list[str] = ['duplicate_message_id'] if dup else []
        if parent_raw is not None and not isinstance(parent_raw, str):
            item_issues.append('parent_not_string')
        if parent is not None and parent not in parent_of:
            item_issues.append('missing_parent')
        if msg_id in cycle_members:
            item_issues.append('in_cycle')
        elif msg_id and not dup and depth.get(msg_id, 0) is None:
            item_issues.append('descends_from_cycle')
        role, _ = first_of(message, mm['role'])
        if role is not None and not isinstance(role, str):
            item_issues.append('role_not_string')
        role_text = scalar_or_none(role)
        direction = direction_by_role.get(role_text) if role_text else None
        model, _ = first_of(message, mm.get('model', []))
        text_field, _ = first_of(message, mm['text'])
        content, content_rel = first_of(message, mm['content'])
        blocks: list = []
        if isinstance(content, list):
            blocks = content
        elif content is not None:
            item_issues.append('content_not_list')
        content_rel = content_rel or '/content'
        visible: list[tuple[str, str]] = []
        visible_times = []
        block_projection = []
        kinds = Counter()
        for j, block in enumerate(blocks):
            block_rel = f'{msg_rel}{content_rel}/{j}'
            if not isinstance(block, dict):
                item_issues.append('block_not_object')
                block_projection.append({'type': None, 'raw_sha256': canonical_sha256(block)})
                continue
            block_type, _ = first_of(block, bm['type'])
            spec = m['block_types'].get(block_type) if isinstance(block_type, str) else None
            block_issues: list[str] = []
            if spec is None:
                spec = m['unknown_block_type']
                block_issues.append('unknown_block_type' if block_type is not None else 'block_type_missing')
            texts = collect_text(block, spec.get('text', []))
            entry = {'type': block_type if isinstance(block_type, str) else None, 'text': [t.replace('\r\n', '\n') for t, _ in texts]}
            if spec['kind'] == 'unknown':
                entry['raw_sha256'] = canonical_sha256(block)
            block_projection.append(entry)
            block_times = []
            for kind, key in (('start', 'start_time'), ('end', 'stop_time')):
                value, where = first_of(block, bm[key])
                candidate = time_candidate(kind, value, ctx.time_source(f'{record_path}/content/{j}.{_last_token(where, key)}'),
                                           formats=formats, zero_is_placeholder=zero_placeholder)
                if candidate is not None:
                    if candidate.metadata.get('parse_status') != 'parsed':
                        block_issues.append(f'{key}_{candidate.metadata.get("parse_status")}')
                    block_times.append(candidate)
            kinds[spec['kind']] += 1
            if spec.get('visible'):
                visible.extend((t, f'{content_rel}/{j}{w}') for t, w in texts)
                visible_times.extend(block_times)
                item_issues.extend(block_issues)
                continue
            body, body_info = bounded_body([t for t, _ in texts], spec.get('join', '\n'))
            raw_block = {k: v for k, v in block.items() if k not in ('thinking', 'text')}
            for code in block_issues:
                issues.add(code, loc(block_rel))
            parts.append(PlannedEvent(
                key=f'{record_path}/content/{j}', event_type='chat_message_part',
                metadata={
                    'chat': {'chatbot': ctx.chatbot, 'namespace': ctx.namespace, 'conversation_id': conv_id,
                             'message_id': msg_id, 'part_of_message_key': record_path, 'block_index': j,
                             'block_type': entry['type'], 'content_kind': spec['kind'], 'role': role_text,
                             'conversation_title': title_text},
                    'locator': {'artifact_file': file, 'element_index': element.index,
                                'element_byte_range': [element.start, element.end], 'element_pointer': block_rel,
                                'json_pointer': f'/{element.index}{block_rel}', 'duplicate_index': 0},
                    'parser': parser_meta, 'body': body_info,
                    'raw': {'block_fields': bounded_json(raw_block)},
                    'issues': sorted(set(block_issues)),
                },
                times=block_times, ts_start=utc_of(block_times, 'start'), ts_end=utc_of(block_times, 'end'),
                direction=direction, sender=role_text, contact_label=ctx.chatbot, subject=title_text, body=body,
                thread_id=conv_id))
        if blocks:
            body, body_info = bounded_body([t for t, _ in visible], VISIBLE_JOIN)
            body_info['source'] = 'content_blocks'
            if isinstance(text_field, str):
                body_info['text_field_matches_visible_blocks'] = text_field == VISIBLE_JOIN.join(t for t, _ in visible)
        else:
            fallback = collect_text(message, m.get('visible_text_fallback', []))
            body, body_info = bounded_body([t for t, _ in fallback], VISIBLE_JOIN)
            body_info['source'] = 'text_field' if fallback else 'none'
        if body_info.get('lone_surrogates_replaced'):
            item_issues.append('lone_surrogate_replaced_in_body')
        refs: list[PlannedRef] = []
        attachment_projection = []
        attachments, attachments_rel = first_of(message, mm['attachments'])
        am = m['attachment']
        if isinstance(attachments, list):
            for k, attachment in enumerate(attachments):
                att_rel = f'{msg_rel}{attachments_rel}/{k}'
                if not isinstance(attachment, dict):
                    item_issues.append('attachment_not_object')
                    continue
                name = scalar_or_none(first_of(attachment, am['name'])[0])
                size, _ = first_of(attachment, am['size'])
                mime = scalar_or_none(first_of(attachment, am['mime_type'])[0])
                extracted, extracted_where = first_of(attachment, am['extracted_text'])
                extracted = extracted if isinstance(extracted, str) else None
                attachment_projection.append({'name': name, 'size': size if isinstance(size, int) else None, 'type': mime,
                                              'extracted_sha256': text_sha256(extracted) if extracted is not None else None})
                refs.append(PlannedRef('claude_attachment', name or f'<unnamed:{k}>', None, name, att_rel,
                                       {'size': size if isinstance(size, int) else None, 'mime_type': mime,
                                        'extracted_content_in_export': extracted is not None}))
                if extracted:
                    body_a, info_a = bounded_body([extracted], '\n')
                    parts.append(PlannedEvent(
                        key=f'{record_path}/attachments/{k}', event_type='chat_message_part',
                        metadata={
                            'chat': {'chatbot': ctx.chatbot, 'namespace': ctx.namespace, 'conversation_id': conv_id,
                                     'message_id': msg_id, 'part_of_message_key': record_path, 'attachment_index': k,
                                     'content_kind': 'attachment_extract', 'attachment_name': name, 'role': role_text,
                                     'conversation_title': title_text,
                                     'note': 'text extracted by the chat service, not the attachment bytes'},
                            'locator': {'artifact_file': file, 'element_index': element.index,
                                        'element_byte_range': [element.start, element.end],
                                        'element_pointer': f'{att_rel}{extracted_where}',
                                        'json_pointer': f'/{element.index}{att_rel}{extracted_where}', 'duplicate_index': 0},
                            'parser': parser_meta, 'body': info_a, 'issues': [],
                        },
                        direction=direction, sender=role_text, contact_label=ctx.chatbot, subject=title_text,
                        body=body_a, thread_id=conv_id))
        elif attachments is not None:
            item_issues.append('attachments_not_list')
        file_projection = []
        fm = m['file']
        for pointer in mm['files']:
            for where, files in expand(message, pointer):
                if not isinstance(files, list):
                    item_issues.append('files_not_list')
                    continue
                for k, entry in enumerate(files):
                    if not isinstance(entry, dict):
                        item_issues.append('file_not_object')
                        continue
                    file_id = scalar_or_none(first_of(entry, fm['id'])[0])
                    name = scalar_or_none(first_of(entry, fm['name'])[0])
                    file_projection.append({'id': file_id, 'name': name})
                    refs.append(PlannedRef('claude_file', file_id or name or f'<unnamed:{k}>', file_id, name,
                                           f'{msg_rel}{where}/{k}'))
        times = times_for(message, mm, record_path, item_issues)
        times.extend(visible_times)
        children = children_of.get(msg_id, []) if msg_id and not dup else []
        structure = {
            'parent': parent, 'parent_declared': declared, 'parent_is_root_sentinel': parent_raw in sentinels,
            'children': children[:CHILDREN_LISTED_MAX], 'children_count': len(children),
            'children_truncated': len(children) > CHILDREN_LISTED_MAX,
            'depth': depth.get(msg_id) if msg_id and not dup else None,
            'in_cycle': bool(msg_id) and msg_id in cycle_members,
            'on_current_path': None, 'is_current_node': None, 'duplicate_index': dup,
        }
        if not declared:
            structure['list_predecessor'] = order[position - 1] if position else None
        projection = {'role': role_text,
                      'text_field': text_field.replace('\r\n', '\n') if isinstance(text_field, str) else None,
                      'blocks': block_projection, 'attachments': attachment_projection, 'files': file_projection}
        content_sha256 = canonical_sha256(projection)
        exclude = set(mm.get('raw_exclude', []))
        metadata = {
            'chat': {'chatbot': ctx.chatbot, 'namespace': ctx.namespace, 'conversation_id': conv_id, 'node_id': msg_id,
                     'message_id': msg_id, 'role': role_text, 'model': scalar_or_none(model),
                     'model_status': 'from_export' if isinstance(model, str) else 'not_in_export',
                     'conversation_default_model': scalar_or_none(default_model),
                     'content_kind': 'text' if visible or not blocks else ('reasoning_only' if set(kinds) == {'reasoning'} else 'no_visible_text'),
                     'block_kinds': dict(sorted(kinds.items())), 'hidden': False, 'hidden_reasons': [],
                     'conversation_title': title_text, 'list_index': i, 'tree': structure},
            'locator': {'artifact_file': file, 'element_index': element.index,
                        'element_byte_range': [element.start, element.end], 'element_pointer': msg_rel,
                        'json_pointer': f'/{element.index}{msg_rel}', 'duplicate_index': 0,
                        'text_pointers': bounded_list([f'{msg_rel}{w}' for _, w in visible])},
            'parser': parser_meta,
            'normalization': {'version': ctx.normalization_version, 'content_sha256': content_sha256},
            'body': body_info,
            'attachments': bounded_list([{'ref_kind': r.ref_kind, 'ref': r.ref, 'file_id': r.file_id, 'name': r.name} for r in refs]),
            'raw': {'message_fields': bounded_json({k: v for k, v in message.items() if k not in exclude})},
            'issues': sorted(set(item_issues)),
        }
        for code in sorted(set(item_issues)):
            issues.add(code, occurrence_locator)
        for kind, count in kinds.items():
            stats[f'block_{kind}'] += count
        # A list position is not an identity: messages without an id get no logical record.
        record = PlannedRecord('message', conv_id, record_id, occurrence_locator, content_sha256, structure) if conv_id and msg_id else None
        events.append(PlannedEvent(
            key=record_path, event_type='chat_message', metadata=metadata, times=times,
            ts_start=utc_of(times, 'created'), direction=direction, sender=role_text, contact_label=ctx.chatbot,
            subject=title_text, body=body, thread_id=conv_id, record=record, refs=refs))

    conv_issues: list[str] = []
    conv_times = times_for(conv, cm, conv_key, conv_issues)
    for code in conv_issues:
        issues.add(f'conversation_{code}', loc(''))
    exclude = set(cm.get('raw_exclude', []))
    conv_structure = {
        'title': title_text, 'current_node': current_leaf if isinstance(current_leaf, str) else None,
        'node_count': node_count, 'node_ids_sha256': canonical_sha256(sorted({e[2] for e in entries if e[2]})),
        'current_path_length': None, 'current_path_sha256': None,
        'current_path_status': 'not_declared_by_export' if current_leaf is None else 'declared',
    }
    conv_metadata = {
        'chat': {'chatbot': ctx.chatbot, 'namespace': ctx.namespace, 'conversation_id': conv_id, 'title': title_text,
                 'account_id': scalar_or_none(account), 'default_model': scalar_or_none(default_model),
                 'summary': bounded_json(summary) if summary is not None else None,
                 'message_events': len(events), 'part_events': len(parts), 'cycle_nodes': len(cycle_members),
                 **conv_structure},
        'locator': {'artifact_file': file, 'element_index': element.index, 'element_byte_range': [element.start, element.end],
                    'element_pointer': '', 'json_pointer': f'/{element.index}'},
        'parser': parser_meta,
        'raw': {'conversation_fields': bounded_json({k: v for k, v in conv.items() if k not in exclude})},
        'issues': sorted(set(conv_issues)),
    }
    conv_record = None
    if conv_id is not None:
        conv_record = PlannedRecord('conversation', conv_id, conv_id, loc(''), canonical_sha256({'title': title_text}), conv_structure)
    conversation_event = PlannedEvent(
        key=conv_key, event_type='chat_conversation', metadata=conv_metadata, times=conv_times,
        ts_start=utc_of(conv_times, 'created'), contact_label=ctx.chatbot, subject=title_text, thread_id=conv_id,
        record=conv_record)
    stats['nodes'] += node_count
    return ConversationPlan([conversation_event, *events, *parts], [], stats)


def import_claude_export(paths: list[Path], *, export_label: str | None = None, collection_id: int | None = None,
                         mapping_path: Path | str | None = None, limits: ChatImportLimits | None = None,
                         locator_for: Callable[[Path], str] | None = None) -> dict:
    mapping = load_claude_mapping(mapping_path)
    return run_import([Path(p) for p in paths], mapping=mapping, planner=plan_conversation, parser=PARSER_NAME,
                      parser_version=PARSER_VERSION, normalization_version=NORMALIZATION_VERSION,
                      export_label=export_label, collection_id=collection_id, limits=limits, locator_for=locator_for)

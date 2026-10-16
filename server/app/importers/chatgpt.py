"""ChatGPT data export importer (``conversations.json`` and ``conversations-NNN.json``).

Every node of every conversation ``mapping`` is kept, not only the
``current_node`` path: a node with a message becomes one ``chat_message`` event,
a node without one (e.g. the synthetic root) is a structural logical record.
Branches, the current path, hidden/system flags, the per-message model and
attachment references stay visible in event metadata and in the logical
occurrence structure. ``thoughts``/``reasoning_recap`` nodes keep their own
event with ``content_kind='reasoning'``; they are never merged into an answer.

Key paths come from ``data/chatgpt_mapping.v1.json`` (or ``--mapping``); the
mapping file's version and SHA-256 are part of each event identity, so another
mapping is a separate interpretation and never overwrites earlier events.
Epoch ``create_time``/``update_time`` values are ``utc_epoch`` candidates; null
and the 0 placeholder give no UTC projection. The local machine zone is never used.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

from .chat_common import (
    DATA_DIR, CHILDREN_LISTED_MAX, ChatImportLimits, ConversationPlan, Element, FileContext, PlannedEvent,
    PlannedRecord, PlannedRef, bounded_body, bounded_json, bounded_list, canonical_sha256, collect_text,
    duplicate_keys, escape_token, expand, first_of, iter_key_values, load_mapping, run_import, scalar_or_none,
    storable_text, time_candidate, tree_depths, utc_of,
)

PARSER_NAME = 'chatgpt_export'
PARSER_VERSION = '1'
NORMALIZATION_VERSION = 'chatgpt-content-v1'
DEFAULT_MAPPING = DATA_DIR / 'chatgpt_mapping.v1.json'
REQUIRED_SECTIONS = ('conversation', 'node', 'message', 'content_types', 'unknown_content_type',
                     'direction_by_role', 'hidden_rules', 'asset_pointer', 'attachment')


def load_chatgpt_mapping(path: Path | str | None = None):
    return load_mapping(path or DEFAULT_MAPPING, namespace='chatgpt', required=REQUIRED_SECTIONS)


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _last_token(pointer: str | None, fallback: str) -> str:
    return pointer.rsplit('/', 1)[-1] if pointer else fallback


def _tree(nodes, node_map: dict, current_node, issues, loc: Callable[[str], str], mapping_rel: str):
    """Parent/children/current-path/depth analysis that terminates on any cycle."""
    parent_of: dict[str, str | None] = {}
    children_of: dict[str, list[str]] = {}
    node_issues: dict[str, list[str]] = defaultdict(list)
    for key, node, dup in nodes:
        if dup or not isinstance(node, dict):
            continue
        parent, _ = first_of(node, node_map['parent'])
        if parent is not None and not isinstance(parent, str):
            node_issues[key].append('parent_not_string')
            parent = None
        children, _ = first_of(node, node_map['children'])
        if children is None:
            children = []
        elif not isinstance(children, list):
            node_issues[key].append('children_not_list')
            children = []
        elif any(not isinstance(c, str) for c in children):
            node_issues[key].append('child_not_string')
            children = [c for c in children if isinstance(c, str)]
        if len(set(children)) != len(children):
            node_issues[key].append('duplicate_child_reference')
        parent_of[key] = parent
        children_of[key] = children
    for key, parent in parent_of.items():
        if parent is not None and parent not in parent_of:
            node_issues[key].append('missing_parent')
        if parent is not None and parent in children_of and key not in children_of[parent]:
            node_issues[key].append('not_listed_in_parent_children')
        for child in children_of[key]:
            if child not in parent_of:
                node_issues[key].append('missing_child')
            elif parent_of[child] != key:
                node_issues[key].append('parent_child_mismatch')
    on_path: set[str] | None = None
    path: list[str] = []
    if isinstance(current_node, str) and current_node in parent_of:
        seen: set[str] = set()
        cursor = current_node
        while cursor is not None and cursor in parent_of and cursor not in seen:
            seen.add(cursor)
            path.append(cursor)
            cursor = parent_of[cursor]
        if cursor is not None and cursor in seen:
            issues.add('cycle_on_current_path', loc(mapping_rel), node=cursor)
        elif cursor is not None:
            issues.add('current_path_reaches_missing_parent', loc(mapping_rel), node=cursor)
        on_path = set(path)
    elif current_node is None:
        issues.add('current_node_missing', loc(''))
    else:
        issues.add('current_node_not_in_mapping', loc(''), current_node=current_node if isinstance(current_node, str) else None)
    depth, cycle_members = tree_depths(parent_of)
    if cycle_members:
        issues.add('cycle_in_tree', loc(mapping_rel), nodes=sorted(cycle_members)[:50], count=len(cycle_members))
        for member in cycle_members:
            node_issues[member].append('in_cycle')
    for key, value in depth.items():
        if value is None and key not in cycle_members:
            node_issues[key].append('descends_from_cycle')
    roots = [k for k, p in parent_of.items() if p is None]
    if len(roots) > 1:
        issues.add('multiple_roots', loc(mapping_rel), count=len(roots))
    return parent_of, children_of, node_issues, on_path, list(reversed(path)), depth, cycle_members


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
    cm, nm, mm = m['conversation'], m['node'], m['message']
    stats = Counter()
    conv_id, _ = first_of(conv, cm['id'])
    if conv_id is None and first_of(conv, cm['mapping'])[0] is None:
        # Neither an id nor a mapping: another format (e.g. a Claude export), not a damaged conversation.
        issues.add('conversation_shape_not_recognized', loc(''), keys=sorted(str(k) for k in conv)[:20])
        return None
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
    current_node, _ = first_of(conv, cm['current_node'])
    default_model, _ = first_of(conv, cm['default_model'])
    mapping_obj, mapping_rel = first_of(conv, cm['mapping'])
    nodes: list[tuple[str, object, int]] = []
    if isinstance(mapping_obj, dict):
        nodes = [(k, v, 0) for k, v in mapping_obj.items()]
        dup_counter = Counter()
        for k, v in getattr(mapping_obj, 'duplicates', None) or []:
            dup_counter[k] += 1
            nodes.append((k, v, dup_counter[k]))
    else:
        issues.add('mapping_missing' if mapping_obj is None else 'mapping_not_object', loc(''))
        mapping_rel = mapping_rel or '/mapping'
    node_count = len(nodes)
    if node_count > ctx.limits.max_nodes_per_conversation:
        issues.add('node_limit_reached', loc(mapping_rel), nodes=node_count, limit=ctx.limits.max_nodes_per_conversation)
        nodes = nodes[:ctx.limits.max_nodes_per_conversation]
    parent_of, children_of, node_issues, on_path, current_path, depth, cycle_members = _tree(
        nodes, nm, current_node, issues, loc, mapping_rel)

    time_cfg = m['time']
    formats, zero_placeholder = time_cfg['formats'], bool(time_cfg.get('zero_is_placeholder'))
    direction_by_role = m['direction_by_role']
    asset_cfg = m['asset_pointer']
    asset_pattern = re.compile(asset_cfg['id_pattern'])
    att_map = m['attachment']
    parser_meta = ctx.parser_metadata()
    events: list[PlannedEvent] = []
    structural: list[PlannedRecord] = []

    def times_for(obj, section: dict, path: str, item_issues: list):
        out = []
        for kind, mapping_key in (('created', 'create_time'), ('updated', 'update_time')):
            value, where = first_of(obj, section[mapping_key])
            candidate = time_candidate(kind, value, ctx.time_source(f'{path}.{_last_token(where, mapping_key)}'),
                                       formats=formats, zero_is_placeholder=zero_placeholder)
            if candidate is None:
                if kind == 'created':  # a missing update time is routine, not a defect
                    item_issues.append(f'{mapping_key}_missing_or_null')
                continue
            status = candidate.metadata.get('parse_status')
            if status != 'parsed':
                item_issues.append(f'{mapping_key}_{status}')
            out.append(candidate)
        return out

    for key, node, dup in nodes:
        node_rel = f'{mapping_rel}/{escape_token(key)}'
        record_path = f'{conv_key}/{key}' + (f'~dup{dup}' if dup else '')
        occurrence_locator = loc(node_rel) + (f'#dup{dup}' if dup else '')
        if not isinstance(node, dict):
            issues.add('node_not_object', occurrence_locator)
            continue
        item_issues = list(node_issues.get(key, [])) if not dup else ['duplicate_node_id']
        node_id, _ = first_of(node, nm['id'])
        if node_id is not None and node_id != key:
            item_issues.append('node_id_differs_from_mapping_key')
        if dup:
            parent, _ = first_of(node, nm['parent'])
            children, _ = first_of(node, nm['children'])
            parent = parent if isinstance(parent, str) else None
            children = [c for c in children if isinstance(c, str)] if isinstance(children, list) else []
        else:
            parent, children = parent_of.get(key), children_of.get(key, [])
        message, message_rel = first_of(node, nm['message'])
        structure = {
            'parent': parent, 'children': children[:CHILDREN_LISTED_MAX], 'children_count': len(children),
            'children_truncated': len(children) > CHILDREN_LISTED_MAX,
            'depth': None if dup else depth.get(key), 'in_cycle': (not dup) and key in cycle_members,
            'on_current_path': None if (on_path is None or dup) else key in on_path,
            'is_current_node': (not dup) and key == current_node,
            'has_message': isinstance(message, dict), 'duplicate_index': dup,
        }
        if not isinstance(message, dict):
            if message is not None:
                item_issues.append('message_not_object')
            for code in item_issues:
                issues.add(code, occurrence_locator)
            stats['structural_nodes'] += 1
            if conv_id is not None:
                structural.append(PlannedRecord('node', conv_id, key, occurrence_locator, None,
                                                {**structure, 'issues': sorted(set(item_issues))}))
            continue
        msg_id, _ = first_of(message, mm['id'])
        role, _ = first_of(message, mm['role'])
        if role is not None and not isinstance(role, str):
            item_issues.append('role_not_string')
        role_text = scalar_or_none(role)
        author_name, _ = first_of(message, mm['author_name'])
        recipient, _ = first_of(message, mm['recipient'])
        model, _ = first_of(message, mm['model'])
        msg_default_model, _ = first_of(message, mm['default_model'])
        content, content_rel = first_of(message, mm['content'])
        content_type, _ = first_of(message, mm['content_type'])
        content_prefix = f'{message_rel}{content_rel or ""}'
        spec = m['content_types'].get(content_type) if isinstance(content_type, str) else None
        texts: list[tuple[str, str]] = []
        if content is None:
            kind = 'empty'
            item_issues.append('content_null')
        elif not isinstance(content, dict):
            kind = 'unknown'
            item_issues.append('content_not_object')
            spec = m['unknown_content_type']
        else:
            if spec is None:
                spec = m['unknown_content_type']
                item_issues.append('unknown_content_type' if content_type is not None else 'content_type_missing')
            kind = spec['kind']
            texts = collect_text(content, spec.get('text', []))
        joiner = (spec or m['unknown_content_type']).get('join', '\n')
        body, body_info = bounded_body([t for t, _ in texts], joiner)
        if body_info.get('lone_surrogates_replaced'):
            item_issues.append('lone_surrogate_replaced_in_body')
        refs: list[PlannedRef] = []
        if isinstance(content, (dict, list)):
            for where, value in iter_key_values(content, asset_cfg['key']):
                match = asset_pattern.match(value)
                refs.append(PlannedRef('asset_pointer', storable_text(value), match.group('file_id') if match else None, None,
                                       f'{node_rel}{content_prefix}{where}'))
        attachments, attachments_rel = first_of(message, mm['attachments'])
        if isinstance(attachments, list):
            for i, attachment in enumerate(attachments):
                where = f'{node_rel}{message_rel}{attachments_rel}/{i}'
                if not isinstance(attachment, dict):
                    item_issues.append('attachment_not_object')
                    continue
                att_id, _ = first_of(attachment, att_map['id'])
                name, _ = first_of(attachment, att_map['name'])
                mime, _ = first_of(attachment, att_map['mime_type'])
                size, _ = first_of(attachment, att_map['size'])
                att_id = scalar_or_none(att_id)
                name = scalar_or_none(name)
                refs.append(PlannedRef('metadata_attachment', att_id or name or f'<unnamed:{i}>', att_id, name, where,
                                       {'mime_type': scalar_or_none(mime), 'size': size if isinstance(size, int) else None}))
        elif attachments is not None:
            item_issues.append('attachments_not_list')
        hidden_reasons = []
        for rule in m['hidden_rules']:
            if any(_same(value, rule['equals']) for _, value in expand(message, rule['pointer'])):
                hidden_reasons.append(rule['reason'])
        times = times_for(message, mm, record_path, item_issues)
        projection = {
            'role': role_text, 'content_type': content_type if isinstance(content_type, str) else None,
            'text': [t.replace('\r\n', '\n') for t, _ in texts],
            'assets': [r.ref for r in refs if r.ref_kind == 'asset_pointer'],
            'attachments': [r.ref for r in refs if r.ref_kind == 'metadata_attachment'],
        }
        if kind == 'unknown' and content is not None:
            projection['raw_content_sha256'] = canonical_sha256(content)
        content_sha256 = canonical_sha256(projection)
        raw_message_metadata, _ = first_of(message, mm['raw_metadata'])
        end_turn, _ = first_of(message, mm['end_turn'])
        metadata = {
            'chat': {
                'chatbot': ctx.chatbot, 'namespace': ctx.namespace, 'conversation_id': conv_id, 'node_id': key,
                'message_id': scalar_or_none(msg_id), 'role': role_text, 'author_name': scalar_or_none(author_name),
                'recipient': scalar_or_none(recipient), 'model': scalar_or_none(model),
                'default_model': scalar_or_none(msg_default_model), 'conversation_default_model': scalar_or_none(default_model),
                'content_type': content_type if isinstance(content_type, str) else None, 'content_kind': kind,
                'hidden': bool(hidden_reasons), 'hidden_reasons': hidden_reasons,
                'conversation_title': title_text, 'tree': structure,
            },
            'locator': {'artifact_file': file, 'element_index': element.index,
                        'element_byte_range': [element.start, element.end], 'element_pointer': node_rel,
                        'json_pointer': f'/{element.index}{node_rel}', 'duplicate_index': dup,
                        'text_pointers': bounded_list([f'{node_rel}{content_prefix}{w}' for _, w in texts])},
            'parser': parser_meta,
            'normalization': {'version': ctx.normalization_version, 'content_sha256': content_sha256},
            'body': body_info,
            'attachments': bounded_list([{'ref_kind': r.ref_kind, 'ref': r.ref, 'file_id': r.file_id, 'name': r.name} for r in refs]),
            'raw': {'message_metadata': bounded_json(raw_message_metadata) if raw_message_metadata is not None else None,
                    'status': scalar_or_none(first_of(message, mm['status'])[0]),
                    'end_turn': end_turn if isinstance(end_turn, bool) else None,
                    'weight': bounded_json(first_of(message, mm['weight'])[0]),
                    'content': bounded_json(content) if kind != 'text' and content is not None else None},
            'issues': sorted(set(item_issues)),
        }
        for code in sorted(set(item_issues)):
            issues.add(code, occurrence_locator)
        stats[f'kind_{kind}'] += 1
        if hidden_reasons:
            stats['hidden_messages'] += 1
        record = None
        if conv_id is not None:
            record = PlannedRecord('message', conv_id, key, occurrence_locator, content_sha256, structure)
        events.append(PlannedEvent(
            key=record_path, event_type='chat_message', metadata=metadata, times=times,
            ts_start=utc_of(times, 'created'), direction=direction_by_role.get(role_text) if role_text else None,
            sender=role_text, recipients=scalar_or_none(recipient), contact_label=ctx.chatbot, subject=title_text,
            body=body, thread_id=conv_id, record=record, refs=refs))

    conv_issues: list[str] = []
    conv_times = times_for(conv, cm, conv_key, conv_issues)
    for code in conv_issues:
        issues.add(f'conversation_{code}', loc(''))
    exclude = set(cm.get('raw_exclude', []))
    raw_fields = {k: v for k, v in conv.items() if k not in exclude}
    path_sha = canonical_sha256(current_path)
    conv_structure = {
        'title': title_text, 'current_node': current_node if isinstance(current_node, str) else None,
        'node_count': node_count, 'node_ids_sha256': canonical_sha256(sorted({k for k, _, _ in nodes})),
        'current_path_length': len(current_path), 'current_path_sha256': path_sha if on_path is not None else None,
    }
    conv_metadata = {
        'chat': {'chatbot': ctx.chatbot, 'namespace': ctx.namespace, 'conversation_id': conv_id, 'title': title_text,
                 'default_model': scalar_or_none(default_model), 'message_events': len(events),
                 'structural_nodes': stats['structural_nodes'], 'cycle_nodes': len(cycle_members),
                 **conv_structure},
        'locator': {'artifact_file': file, 'element_index': element.index, 'element_byte_range': [element.start, element.end],
                    'element_pointer': '', 'json_pointer': f'/{element.index}'},
        'parser': parser_meta,
        'raw': {'conversation_fields': bounded_json(raw_fields)},
        'issues': sorted(set(conv_issues)),
    }
    conv_record = None
    if conv_id is not None:
        conv_record = PlannedRecord('conversation', conv_id, conv_id, loc(''), canonical_sha256({'title': title_text}), conv_structure)
    conversation_event = PlannedEvent(
        key=conv_key, event_type='chat_conversation', metadata=conv_metadata, times=conv_times,
        ts_start=utc_of(conv_times, 'created'), contact_label=ctx.chatbot, subject=title_text, thread_id=conv_id,
        record=conv_record)
    stats['nodes'] += len(nodes)
    return ConversationPlan([conversation_event, *events], structural, stats)


def import_chatgpt_export(paths: list[Path], *, export_label: str | None = None, collection_id: int | None = None,
                          mapping_path: Path | str | None = None, limits: ChatImportLimits | None = None,
                          locator_for: Callable[[Path], str] | None = None) -> dict:
    mapping = load_chatgpt_mapping(mapping_path)
    return run_import([Path(p) for p in paths], mapping=mapping, planner=plan_conversation, parser=PARSER_NAME,
                      parser_version=PARSER_VERSION, normalization_version=NORMALIZATION_VERSION,
                      export_label=export_label, collection_id=collection_id, limits=limits, locator_for=locator_for)

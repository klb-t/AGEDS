"""Google Chat (Google Takeout ``Google Chat/Groups/<DM|Space>/messages.json``) — traces area.

Each ``messages.json`` is one conversation (``thread_id`` = ``google_chat:<folder
name>``; the space name from a sibling ``group_info.json`` when present). A
message keeps ``creator`` (name; e-mail as ``sender_id``), ``message_id`` (record
id), ``topic_id``, text, ``attached_files`` (``export_name`` next to the file),
reactions and quoted-message metadata. ``created_date``/``updated_date`` are
localized display strings: the weekday and joiner are removed (rule
``google_chat_display_v1``) and the Takeout display patterns are applied; only a
literal ``UTC``/``GMT``/numeric zone fixes the instant, anything else stays
``unknown`` with the raw text kept. Direction needs ``owner_id`` (the owner's
e-mail as exported).
"""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from ..traces import messaging
from ..traces.common import TextLayers, mapping, read_json, signal
from .comm_common import DEFAULT_LIMITS, CommLimits, Issues
from .takeout_activity import parse_display_time, settings as display_settings

PARSER_NAME = 'google_chat'
PARSER_VERSION = '1'
MAPPING = 'google_chat.v1.json'


def display_time(raw: str, kind: str, source: str, config: dict, display: dict):
    text = re.sub(config['weekday_prefix_regex'], '', raw.strip())
    for joiner in config['joiners']:
        text = text.replace(joiner, ' ')
    candidate, meta = parse_display_time(text, display, source)
    if candidate is None:
        return None, meta
    metadata = {**candidate.metadata, 'raw_display': raw, 'normalized_display': text,
                'normalization': 'google_chat_display_v1'}
    return dataclasses.replace(candidate, kind=kind, raw=raw, metadata=metadata), meta


def parse(data, ctx: messaging.FileContext) -> list[messaging.Thread]:
    config, _ = mapping(MAPPING)
    display = display_settings()
    if not isinstance(data, dict) or not isinstance(data.get('messages'), list):
        raise ValueError('not a Google Chat messages.json: expected an object with a "messages" list')
    parts = ctx.file.locator.replace('\\', '/').split('/')
    thread_id = parts[-2] if len(parts) >= 2 and parts[-1] == config['messages_file'] else \
        messaging.derived_record_id('google_chat_file', ctx.file.locator)
    title = ctx.file_meta.get('group_name')
    layers_thread = TextLayers(None, ctx.limits)
    messages = []
    for index, item in enumerate(data['messages']):
        if index >= ctx.limits.max_records:
            ctx.issues.add('message_limit', limit=ctx.limits.max_records)
            break
        if not isinstance(item, dict):
            ctx.issues.add('message_not_an_object', index=index)
            continue
        layers = TextLayers(None, ctx.limits)
        problems: list[str] = []
        creator = item.get('creator') if isinstance(item.get('creator'), dict) else {}
        times = []
        for field_name, kind in config['time_fields'].items():
            raw = item.get(field_name)
            if raw is None:
                continue
            if not isinstance(raw, str):
                problems.append(f'time_not_text:{field_name}')
                continue
            candidate, meta = display_time(raw, kind, ctx.time_source(field_name), config, display)
            if candidate is None:
                problems.append(f'time_unparsed:{field_name}')
            else:
                times.append(candidate)
                if candidate.tz_status == 'unknown':
                    problems.append(f'time_zone_not_fixed:{field_name}')
        refs = []
        for position, attached in enumerate((item.get('attached_files') or [])[:ctx.limits.max_raw_items]
                                            if isinstance(item.get('attached_files'), list) else []):
            if isinstance(attached, dict):
                export_name = attached.get('export_name')
                refs.append(messaging.MediaRef('file', export_name if isinstance(export_name, str) else None,
                                               name=attached.get('original_name'),
                                               metadata={'original_name': attached.get('original_name')},
                                               status_hint=None if export_name else 'omitted_by_export'))
        message_id = item.get('message_id')
        if isinstance(message_id, str) and message_id:
            record_id, basis = message_id, 'source_message_id'
        else:
            record_id = messaging.derived_record_id(thread_id, creator.get('email'), item.get('created_date'), item.get('text'))
            basis = 'content_derived_google_chat_v1'
            problems.append('message_id_missing')
        text = layers.text('text', item.get('text'))
        sender_id = creator.get('email') if isinstance(creator.get('email'), str) else None
        direction, direction_basis = ctx.owner.direction(sender_id=sender_id) if sender_id else (None, None)
        chat = {'topic_id': item.get('topic_id'), 'message_id': message_id, 'user_type': creator.get('user_type'),
                'message_state': item.get('message_state'),
                'reactions': [{'emoji': (r.get('emoji') or {}).get('unicode') if isinstance(r.get('emoji'), dict) else None,
                               'reactors': len(r.get('reactor_emails') or [])}
                              for r in (item.get('reactions') or []) if isinstance(r, dict)][:ctx.limits.max_raw_items],
                'quoted_message': bool(item.get('quoted_message_metadata'))}
        content_kind = 'text' if text else 'media' if refs else 'empty'
        messages.append(messaging.Message(
            key=f'{thread_id}/{record_id}#{index}', record_id=record_id, record_id_basis=basis, body=text, times=times,
            content_kind=content_kind, chat=chat, raw=item, text_layers=layers.as_dict(),
            signal=signal('active', 'raw_sample', 'google_chat', 'account'), sender=layers.text('creator', creator.get('name')),
            sender_id=sender_id, direction=direction, direction_basis=direction_basis, refs=refs, issues=problems,
            locator={'json_pointer': f'/messages/{index}', 'message_index': index}))
        for problem in problems:
            ctx.issues.add(problem, index=index)
    return [messaging.Thread(thread_id, layers_thread.text('title', title), [], messages, {})]


def import_chat(path: Path, *, owner_id: str | None = None, export_label: str | None = None,
                source_locator: str | None = None, original_name: str | None = None, collection_id: int | None = None,
                label: str | None = None, resolver=None, limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """One ``messages.json`` or a Takeout ``Google Chat`` folder."""
    config, sha = mapping(MAPPING)
    issues = Issues(limits.max_issues)
    path = Path(path)
    label = label or 'Google Takeout Google Chat'
    names: dict[str, str] = {}
    if path.is_dir():
        files = messaging.walk_inputs(path, lambda p: ('messages', None) if p.name == config['messages_file'] else None,
                                      limits=limits, issues=issues, source_locator=source_locator)
        for item in files:
            info = item.path.parent / config['group_info_file']
            if info.is_file() and not info.is_symlink() and info.stat().st_size < 1024 * 1024:
                try:
                    value = json.loads(info.read_text(encoding='utf-8-sig'))
                except (ValueError, UnicodeDecodeError):
                    issues.add('group_info_unreadable', folder=item.path.parent.name)
                    continue
                if isinstance(value, dict) and isinstance(value.get('name'), str):
                    names[item.locator] = value['name']

        def locator_of(candidate: Path) -> str:
            relative = candidate.relative_to(path).as_posix()
            return f'{source_locator}/{relative}' if source_locator else str(candidate)
        resolver = resolver or messaging.directory_resolver(sorted({f.path.parent for f in files}),
                                                            collection_id=collection_id, label=label,
                                                            locator_of=locator_of)
    else:
        files = [messaging.single_input(path, source_locator=source_locator, original_name=original_name)]
        resolver = resolver or messaging.no_files_resolver()

    def parse_with_names(data, ctx):
        ctx.file_meta = {'group_name': names.get(ctx.file.locator)}
        return parse(data, ctx)
    owner = messaging.Owner(ids=frozenset({owner_id}) if owner_id else frozenset(),
                            basis='owner_id_parameter' if owner_id else None)
    result = messaging.import_files(files, namespace='google_chat', platform='google_chat', parser=PARSER_NAME,
                                    parser_version=PARSER_VERSION, mapping_version=config['version'], mapping_sha256=sha,
                                    parse=parse_with_names, load=read_json, owner=owner, resolver=resolver,
                                    export_label=export_label, collection_id=collection_id, label=label, limits=limits,
                                    options={'owner_id_given': owner_id is not None, 'export_label': export_label})
    result['walk'] = issues.as_dict()
    return result

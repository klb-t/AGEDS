"""Facebook/Messenger and Instagram message downloads (JSON) — traces area.

Layouts (``data/facebook.v1.json``): the long-standing "Download your
information" JSON (``messages/<folder>/<thread>/message_N.json``: ``participants``,
``title``, ``thread_path``, ``messages`` with ``sender_name``, ``timestamp_ms``,
``content``, ``reactions``, ``photos``/``videos``/``audio_files``/``files``/``gifs``,
``sticker``, ``share``, ``type``, ``is_unsent``) and the newer Messenger
end-to-end-encrypted "secure storage" JSON (``senderName``, ``timestamp``,
``text``, ``media``, ``isUnsent``, ``threadName``). Instagram uses the legacy
layout under ``your_instagram_activity/messages/inbox/...``.

Mojibake: the legacy layout escapes every UTF-8 byte as its own ``\\u00XX`` code
point. Rule ``fb_latin1_utf8_v1`` (``traces.common.decode_latin1_utf8``) builds
the decoded layer used for event columns and the body; ``metadata.text`` keeps
the exported string (bounded, with SHA-256) and the per-field status. Text with a
code point above U+00FF or invalid UTF-8 is left as exported. The E2EE layout is
proper UTF-8 and is never decoded.

Messages have no ids in these exports: the logical record id is derived from
(thread, raw sender, raw timestamp, raw text, media URIs) — basis
``content_derived_fb_v1`` — with ``~dupN`` for exact repeats in one thread.
``timestamp_ms`` is epoch milliseconds (``created``, ``utc_epoch``). Media
``creation_timestamp`` values (seconds) are kept on the reference, not as
message times. Direction is set only with an owner name (``owner_name``).
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ..traces import messaging
from ..traces.common import TextLayers, bounded, epoch_time, mapping, read_json, signal
from .comm_common import DEFAULT_LIMITS, CommLimits, Issues

PARSER_NAME = 'facebook_messages'
INSTAGRAM_PARSER_NAME = 'instagram_messages'
PARSER_VERSION = '1'
MAPPING = 'facebook.v1.json'


def _variant(data: dict, config: dict) -> dict | None:
    messages = data.get('messages')
    sample = next((m for m in messages if isinstance(m, dict)), None) if isinstance(messages, list) else None
    for variant in config['variants']:
        if sample is not None and all(k in sample for k in variant['detect_message_keys']):
            return variant
    if isinstance(messages, list) and not messages:
        return config['variants'][0 if 'thread_path' in data or 'title' in data else 1]
    return None


def _thread_id(data: dict, variant: dict, ctx: messaging.FileContext, participants: list) -> tuple[str, str]:
    path_key = variant['thread'].get('thread_path')
    if path_key and isinstance(data.get(path_key), str) and data[path_key].strip():
        return data[path_key].strip(), 'thread_path'
    parts = ctx.file.locator.replace('\\', '/').split('/')
    if len(parts) >= 3 and re.fullmatch(r'message_\d+\.json', parts[-1]):
        return f'{parts[-3]}/{parts[-2]}', 'export_directory_names'
    return messaging.derived_record_id('participants', sorted(str(p) for p in participants)), 'participants_derived'


def parse(data, ctx: messaging.FileContext) -> list[messaging.Thread]:
    config, _ = mapping(MAPPING)
    if not isinstance(data, dict) or not isinstance(data.get('messages'), list):
        raise ValueError('not a Facebook/Instagram message file: expected an object with a "messages" list')
    variant = _variant(data, config)
    if variant is None:
        ctx.issues.add('message_shape_not_recognized')
        return []
    rule = variant['text_decoding']
    keys, thread_keys = variant['message'], variant['thread']
    thread_layers = TextLayers(rule, ctx.limits)
    raw_participants = data.get(thread_keys['participants']) if isinstance(data.get(thread_keys['participants']), list) else []
    participants = []
    for index, item in enumerate(raw_participants[:ctx.limits.max_raw_items]):
        name = item.get(thread_keys['participant_name']) if isinstance(item, dict) and thread_keys['participant_name'] else item
        participants.append(thread_layers.text(f'participants/{index}', name) if isinstance(name, str) else None)
    title = thread_layers.text('title', data.get(thread_keys['title']))
    thread_id, thread_basis = _thread_id(data, variant, ctx, participants)
    provider = 'meta_instagram' if ctx.namespace == 'instagram' else 'meta_facebook'
    seen = Counter()
    messages = []
    for index, item in enumerate(data['messages']):
        if index >= ctx.limits.max_records:
            ctx.issues.add('message_limit', limit=ctx.limits.max_records)
            break
        if not isinstance(item, dict):
            ctx.issues.add('message_not_an_object', index=index)
            continue
        layers = TextLayers(rule, ctx.limits)
        problems = []
        sender_raw = item.get(keys['sender'])
        sender = layers.text('sender', sender_raw)
        text = layers.text('text', item.get(keys['text']))
        times = []
        candidate, problem = epoch_time('created', item.get(keys['timestamp_ms']), ctx.time_source(keys['timestamp_ms']),
                                        unit='ms', metadata={'field': keys['timestamp_ms'],
                                                             'semantics': 'message time as exported (epoch ms)'})
        if problem:
            problems.append(f'{problem}:{keys["timestamp_ms"]}')
        else:
            times.append(candidate)
            if int(candidate.utc[:4]) < config['service_start_year'][ctx.namespace]:
                problems.append('time_before_service_start')  # kept as stated; e.g. seconds written as ms
        refs = []
        for list_key, kind in config['media_lists'].items():
            values = item.get(list_key)
            if not isinstance(values, list):
                continue
            for position, media in enumerate(values[:ctx.limits.max_raw_items]):
                if not isinstance(media, dict):
                    problems.append(f'media_entry_not_an_object:{list_key}')
                    continue
                uri = layers.text(f'{list_key}/{position}/uri', media.get('uri'))
                extra, _ = bounded({k: v for k, v in media.items() if k != 'uri'}, ctx.limits)
                refs.append(messaging.MediaRef(kind, uri, name=uri.rsplit('/', 1)[-1] if uri else None,
                                               metadata={'field': list_key, 'exported': extra,
                                                         'uri_raw_differs': uri != media.get('uri')}))
        for single_key, kind in config['single_media'].items():
            media = item.get(single_key)
            if isinstance(media, dict) and media.get('uri'):
                uri = layers.text(f'{single_key}/uri', media.get('uri'))
                refs.append(messaging.MediaRef(kind, uri, name=uri.rsplit('/', 1)[-1] if uri else None,
                                               metadata={'field': single_key}))
        share = item.get(config['share_key']) if isinstance(item.get(config['share_key']), dict) else None
        reactions = []
        for position, reaction in enumerate((item.get(keys['reactions']) or [])[:ctx.limits.max_raw_items]
                                            if isinstance(item.get(keys['reactions']), list) else []):
            if isinstance(reaction, dict):
                reactions.append({'reaction': layers.text(f'reactions/{position}/reaction', reaction.get('reaction')),
                                  'actor': layers.text(f'reactions/{position}/actor', reaction.get('actor')),
                                  'timestamp_raw': reaction.get('timestamp')})
        kind = config['type_kinds'].get(item.get(keys['type'])) if isinstance(item.get(keys['type']), str) else None
        if item.get(keys['unsent']) is True:
            content_kind = 'unsent'
        elif kind == 'call':
            content_kind = 'call'
        elif kind == 'service':
            content_kind = 'service'
        elif text:
            content_kind = 'share' if share else 'text'
        elif refs:
            content_kind = 'media'
        elif share:
            content_kind = 'share'
        else:
            content_kind = 'empty'
        if item.get(keys['type']) is not None and kind is None:
            problems.append('message_type_unmapped')
        base_id = messaging.derived_record_id(thread_id, sender_raw, item.get(keys['timestamp_ms']),
                                              item.get(keys['text']), sorted(r.uri or '' for r in refs))
        seen[base_id] += 1
        record_id = base_id if seen[base_id] == 1 else f'{base_id}~dup{seen[base_id] - 1}'
        direction, basis = ctx.owner.direction(sender_name=sender)
        chat = {'variant': variant['id'], 'thread_id_basis': thread_basis, 'message_type': item.get(keys['type']),
                'reactions': reactions, 'unsent': item.get(keys['unsent']) is True}
        if share:
            chat['share'] = {'link': layers.text('share/link', share.get('link')),
                             'text': layers.text('share/share_text', share.get('share_text'))}
        if 'call_duration' in item:
            chat['call'] = {'duration_seconds_raw': item.get('call_duration'), 'missed': item.get('missed'),
                            'semantics': 'call record; whether the timestamp is the start or the end is not stated'}
        messages.append(messaging.Message(
            key=f'{thread_id}/m{index}', record_id=record_id, record_id_basis='content_derived_fb_v1', body=text,
            times=times, content_kind=content_kind, chat=chat, raw=item, text_layers=layers.as_dict(),
            signal=signal('active', 'raw_sample', provider, 'account'), sender=sender, direction=direction,
            direction_basis=basis, refs=refs, issues=problems,
            locator={'json_pointer': f'/messages/{index}', 'message_index': index}))
        for problem in problems:
            ctx.issues.add(problem, index=index)
    thread_meta = {'participants': participants, 'text': thread_layers.as_dict(),
                   'still_participant': data.get('is_still_participant')}
    return [messaging.Thread(thread_id, title, participants, messages, thread_meta)]


def _matcher(config: dict):
    regex = re.compile(config['message_file_regex'])

    def match(path: Path):
        hit = regex.fullmatch(path.name)
        if hit and config['messages_dir_name'] in path.parts:
            return 'messages', int(hit.group(1))
        return None
    return match


def import_messages(path: Path, *, namespace: str = 'facebook', owner_name: str | None = None,
                    export_label: str | None = None, source_locator: str | None = None, original_name: str | None = None,
                    collection_id: int | None = None, label: str | None = None, resolver=None,
                    limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """A message_N.json file or an export folder (all ``messages/**/message_N.json``)."""
    if namespace not in ('facebook', 'instagram'):
        raise ValueError('namespace must be facebook or instagram')
    config, sha = mapping(MAPPING)
    issues = Issues(limits.max_issues)
    parser = PARSER_NAME if namespace == 'facebook' else INSTAGRAM_PARSER_NAME
    platform = 'facebook_messenger' if namespace == 'facebook' else 'instagram'
    label = label or ('Facebook download' if namespace == 'facebook' else 'Instagram download')
    path = Path(path)
    if path.is_dir():
        files = messaging.walk_inputs(path, _matcher(config), limits=limits, issues=issues, source_locator=source_locator)
        roots = sorted({path, *[a for f in files for a in f.path.parents if path in a.parents or a == path]})[:64]

        def locator_of(candidate: Path) -> str:
            relative = candidate.relative_to(path).as_posix()
            return f'{source_locator}/{relative}' if source_locator else str(candidate)
        resolver = resolver or messaging.directory_resolver(roots, collection_id=collection_id, label=label,
                                                            locator_of=locator_of)
    else:
        match = re.fullmatch(config['message_file_regex'], original_name or path.name)
        files = [messaging.single_input(path, source_locator=source_locator, original_name=original_name,
                                        shard_index=int(match.group(1)) if match else None)]
        resolver = resolver or messaging.no_files_resolver()
    options = {'owner_name_given': owner_name is not None, 'export_label': export_label, 'namespace': namespace}
    result = messaging.import_files(files, namespace=namespace, platform=platform, parser=parser,
                                    parser_version=PARSER_VERSION, mapping_version=config['version'], mapping_sha256=sha,
                                    parse=parse, load=read_json, owner=messaging.Owner(name=owner_name,
                                                                                       basis='owner_name_parameter'),
                                    resolver=resolver, export_label=export_label, collection_id=collection_id,
                                    label=label, limits=limits, options=options)
    result['walk'] = issues.as_dict()
    return result


def import_instagram_messages(path: Path, **kwargs) -> dict:
    return import_messages(path, namespace='instagram', **kwargs)

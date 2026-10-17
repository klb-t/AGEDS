"""Telegram Desktop ``result.json`` (full or single-chat export) — traces area.

* Full export: ``chats.list`` and ``left_chats.list``; single-chat export: the
  chat object at top level. Every chat becomes a thread (``thread_id`` =
  ``telegram:<chat id>``), every ``message``/``service`` record a ``chat_message``.
* Text is a string or a list of strings and entity objects (``{"type", "text",
  "href"}``); the body is their concatenation, entity types and hrefs are kept.
* Times: ``date`` is the *exporting computer's* wall clock without offset →
  ``created`` with zone ``unknown``; ``date_unixtime`` (newer exports) → a separate
  ``created`` UTC epoch candidate. ``edited``/``edited_unixtime`` → ``updated``
  (the edited text is the last version; ``signal.basis='provider_user_edit'``).
* ``reply_to_message_id`` and ``forwarded_from`` are kept as references; nothing
  is merged. Media paths are relative to the ``result.json`` folder; the export's
  "(File not included…)" placeholder → ``omitted_by_export``.
* Shared locations (``location_information``…) are **not stored**: the raw copy
  marks them ``location_present_not_stored`` (location data needs the location
  importer and its opt-in). ``personal_information`` is read only for the owner
  id (direction) and never stored.
* The HTML export (``messages.html``) is reported as unsupported.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..traces import messaging
from ..traces.common import TextLayers, epoch_time, mapping, read_json, signal
from ..timeinfo import from_iso
from .comm_common import DEFAULT_LIMITS, CommLimits, Issues

PARSER_NAME = 'telegram_desktop'
PARSER_VERSION = '1'
MAPPING = 'telegram.v1.json'


def _text(value) -> tuple[str | None, list]:
    if isinstance(value, str):
        return value, []
    if not isinstance(value, list):
        return None, []
    parts, entities = [], []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            parts.append(item.get('text') if isinstance(item.get('text'), str) else '')
            entities.append({'type': item.get('type'), 'href': item.get('href')})
    return ''.join(parts), entities


def _times(item: dict, ctx: messaging.FileContext, problems: list[str]):
    out = []
    for local_key, epoch_key, kind in (('date', 'date_unixtime', 'created'), ('edited', 'edited_unixtime', 'updated')):
        if epoch_key in item:
            candidate, problem = epoch_time(kind, item.get(epoch_key), ctx.time_source(epoch_key), unit='s',
                                            metadata={'field': epoch_key})
            if problem:
                problems.append(f'{problem}:{epoch_key}')
            else:
                out.append(candidate)
        raw = item.get(local_key)
        if isinstance(raw, str):
            try:
                candidate = from_iso(kind, raw, ctx.time_source(local_key),
                                     metadata={'field': local_key,
                                               'zone_status': 'exporting_computer_local_time_zone_not_stated'})
            except ValueError:
                problems.append(f'time_unparsed:{local_key}')
            else:
                out.append(candidate)
        elif raw is not None:
            problems.append(f'time_not_text:{local_key}')
    return out


def _owner(data: dict, owner: messaging.Owner) -> messaging.Owner:
    info = data.get('personal_information') if isinstance(data, dict) else None
    user_id = info.get('user_id') if isinstance(info, dict) else None
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        return messaging.Owner(ids=frozenset({f'user{user_id}', str(user_id)}), basis='export_personal_information')
    return owner


def parse(data, ctx: messaging.FileContext) -> list[messaging.Thread]:
    config, _ = mapping(MAPPING)
    if not isinstance(data, dict):
        raise ValueError('Telegram result.json must be a JSON object')
    chats = []
    if isinstance(data.get('messages'), list):
        chats = [(None, data)]
    else:
        for list_name in config['chat_lists']:
            container = data.get(list_name)
            items = container.get('list') if isinstance(container, dict) else None
            if isinstance(items, list):
                chats.extend((list_name, chat) for chat in items)
    if not chats:
        raise ValueError('no Telegram chats (chats.list / left_chats.list / messages) in this JSON')
    owner = _owner(data, ctx.owner)
    omitted = re.compile(config['omitted_media_regex'])
    threads = []
    total = 0
    for list_name, chat in chats:
        if not isinstance(chat, dict):
            ctx.issues.add('thread_not_an_object')
            continue
        layers = TextLayers(None, ctx.limits)
        chat_id = chat.get('id')
        thread_id = str(chat_id) if isinstance(chat_id, (int, str)) and not isinstance(chat_id, bool) else \
            messaging.derived_record_id('telegram_chat', chat.get('name'), chat.get('type'))
        title = layers.text('name', chat.get('name'))
        messages = []
        for index, item in enumerate(chat.get('messages') or []):
            if total >= ctx.limits.max_records:
                ctx.issues.add('message_limit', limit=ctx.limits.max_records)
                break
            total += 1
            if not isinstance(item, dict):
                ctx.issues.add('message_not_an_object', chat=thread_id, index=index)
                continue
            problems: list[str] = []
            text, entities = _text(item.get('text'))
            body = layers.text(f'{index}/text', text)
            times = _times(item, ctx, problems)
            utc_years = [int(t.utc[:4]) for t in times if t.utc]
            if utc_years and min(utc_years) < config['service_start_year']:
                problems.append('time_before_service_start')
            record_type = item.get('type')
            refs = []
            for field_name, kind in config['media_fields'].items():
                value = item.get(field_name)
                if not isinstance(value, str) or not value:
                    continue
                hint = 'omitted_by_export' if omitted.match(value) else None
                refs.append(messaging.MediaRef(kind if field_name != 'file' else (item.get('media_type') or 'file'),
                                               None if hint else value, name=value.rsplit('/', 1)[-1] if not hint else None,
                                               status_hint=hint,
                                               metadata={'field': field_name, 'mime_type': item.get('mime_type'),
                                                         'export_text': value if hint else None}))
            raw = {k: v for k, v in item.items() if k not in config['location_fields']}
            location = [k for k in config['location_fields'] if k in item]
            if location:
                raw['__location__'] = 'location_present_not_stored'
                problems.append('shared_location_not_stored')
            if record_type == 'service':
                action = item.get('action')
                content_kind = config['service_kinds'].get(action, 'service')
                sender, sender_id = item.get('actor'), item.get('actor_id')
            else:
                content_kind = 'text' if body else 'media' if refs else 'location' if location else \
                    'sticker' if item.get('sticker_emoji') else 'empty'
                sender, sender_id = item.get('from'), item.get('from_id')
                if record_type != 'message':
                    problems.append('message_type_unmapped')
            message_id = item.get('id')
            if isinstance(message_id, int) and not isinstance(message_id, bool):
                record_id, basis = str(message_id), 'source_message_id'
            else:
                record_id, basis = messaging.derived_record_id(thread_id, item.get('date'), text), 'content_derived_telegram_v1'
                problems.append('message_id_missing')
            direction, direction_basis = owner.direction(sender_id=sender_id) if sender_id is not None else (None, None)
            chat_meta = {'chat_type': chat.get('type'), 'chat_list': list_name, 'message_type': record_type,
                         'message_id': message_id, 'text_entities': entities[:ctx.limits.max_raw_items],
                         'edited': 'edited' in item or 'edited_unixtime' in item}
            if item.get('reply_to_message_id') is not None:
                chat_meta['reply_to'] = {'message_id': item.get('reply_to_message_id'),
                                         'peer_id': item.get('reply_to_peer_id')}
            if item.get('forwarded_from') is not None or item.get('forwarded_from_id') is not None:
                chat_meta['forwarded_from'] = {'name': item.get('forwarded_from'), 'id': item.get('forwarded_from_id')}
            if record_type == 'service':
                chat_meta['service'] = {'action': item.get('action'), 'duration_seconds': item.get('duration_seconds'),
                                        'discard_reason': item.get('discard_reason')}
            if isinstance(item.get('reactions'), list):
                chat_meta['reactions'] = [{'emoji': r.get('emoji'), 'type': r.get('type'), 'count': r.get('count')}
                                          for r in item['reactions'][:ctx.limits.max_raw_items] if isinstance(r, dict)]
            basis_signal = 'provider_user_edit' if chat_meta['edited'] else 'raw_sample'
            messages.append(messaging.Message(
                key=f'{thread_id}/{record_id}#{index}', record_id=record_id, record_id_basis=basis, body=body,
                times=times, content_kind=content_kind, chat=chat_meta, raw=raw, text_layers=layers.as_dict(),
                signal=signal('active', basis_signal, 'telegram', 'account'), sender=layers.text(f'{index}/from', sender),
                sender_id=str(sender_id) if sender_id is not None else None, direction=direction,
                direction_basis=direction_basis, refs=refs, issues=problems,
                locator={'json_pointer': f'{"/" + list_name + "/list" if list_name else ""}/messages/{index}',
                         'chat_id': thread_id, 'message_index': index}))
            for problem in problems:
                ctx.issues.add(problem, chat=thread_id, index=index)
        threads.append(messaging.Thread(thread_id, title, [], messages, {'chat_type': chat.get('type')}))
    return threads


def import_result(path: Path, *, owner_id: str | None = None, export_label: str | None = None,
                  source_locator: str | None = None, original_name: str | None = None,
                  collection_id: int | None = None, label: str | None = None, resolver=None,
                  limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """``result.json`` or an export folder holding one (media resolved next to it)."""
    config, sha = mapping(MAPPING)
    path = Path(path)
    issues = Issues(limits.max_issues)
    label = label or 'Telegram Desktop export'
    if path.is_dir():
        files = messaging.walk_inputs(path, lambda p: ('messages', None) if p.name == 'result.json' else None,
                                      limits=limits, issues=issues, source_locator=source_locator)
        if not files:
            html = list(path.glob('**/messages*.html'))
            raise ValueError('Telegram HTML exports are not supported; export as "Machine-readable JSON"'
                             if html else 'no result.json in this folder')
        roots = sorted({f.path.parent for f in files})

        def locator_of(candidate: Path) -> str:
            relative = candidate.relative_to(path).as_posix()
            return f'{source_locator}/{relative}' if source_locator else str(candidate)
        resolver = resolver or messaging.directory_resolver(roots, collection_id=collection_id, label=label,
                                                            locator_of=locator_of)
    else:
        files = [messaging.single_input(path, source_locator=source_locator, original_name=original_name)]
        resolver = resolver or messaging.no_files_resolver()
    owner = messaging.Owner(ids=frozenset({owner_id, f'user{owner_id}'}) if owner_id else frozenset(),
                            basis='owner_id_parameter' if owner_id else None)
    options = {'owner_id_given': owner_id is not None, 'export_label': export_label}
    result = messaging.import_files(files, namespace='telegram', platform='telegram', parser=PARSER_NAME,
                                    parser_version=PARSER_VERSION, mapping_version=config['version'],
                                    mapping_sha256=sha, parse=parse, load=read_json, owner=owner, resolver=resolver,
                                    export_label=export_label, collection_id=collection_id, label=label,
                                    limits=limits, options=options)
    result['walk'] = issues.as_dict()
    return result

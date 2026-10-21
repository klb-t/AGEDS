"""X/Twitter archive (``data/*.js``) — traces area.

* ``tweets.js`` (older ``tweet.js``): each record (``{"tweet": {...}}`` or the bare
  object) → a ``social_post`` event (``thread_id`` = ``twitter_x:posts``,
  ``direction='outgoing'`` with basis ``account_archive_own_posts``). ``created_at``
  (``Wed Oct 10 20:19:24 +0000 2018``) has an explicit offset. Retweets
  (``RT @`` / ``retweeted_status``) and replies (``in_reply_to_status_id_str``)
  are content kinds; counts are kept raw. Media: ``tweets_media/<tweet id>-<file>``.
* ``direct-messages.js`` / ``direct-messages-group.js``: ``dmConversation`` →
  thread ``twitter_x:<conversationId>``; ``messageCreate`` → ``chat_message`` with
  the source id; ``createdAt`` ISO with ``Z``; reactions, ``editHistory`` and
  join/leave/rename events kept (service kind). Direction needs the owner's
  account id: ``owner_id`` or ``account.js`` next to the file (folder import).
  Media: ``direct_messages(_group)_media/<message id>-<file>``.

Every file starts with ``window.YTD.<name>.partN =``; the prefix is checked and
recorded, the rest must be one JSON array.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..traces import messaging
from ..traces.common import TextLayers, mapping, signal, strip_js_assignment
from ..timeinfo import from_iso
from .comm_common import DEFAULT_LIMITS, CommLimits, Issues, numeric_offset_minutes, offset_candidate

PARSER_NAME = 'twitter_x_archive'
PARSER_VERSION = '1'
MAPPING = 'twitter_x.v1.json'
_MONTHS = {m: i for i, m in enumerate(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}


def load_js(path: Path, limits: CommLimits, issues: Issues):
    size = path.stat().st_size
    if size > limits.max_json_bytes:
        issues.add('document_size_limit', size_bytes=size, limit=limits.max_json_bytes)
        raise ValueError(f'archive file of {size} bytes exceeds max_json_bytes')
    try:
        text = path.read_bytes().decode('utf-8-sig')
    except UnicodeDecodeError:
        text = path.read_bytes().decode('utf-8', errors='replace')
        issues.add('invalid_utf8_replaced')
    body, assigned = strip_js_assignment(text)
    if assigned is None:
        issues.add('ytd_assignment_prefix_missing')
    try:
        value = json.loads(body.rstrip().rstrip(';'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'invalid archive JSON: {exc.msg} at line {exc.lineno}') from exc
    return {'assigned': assigned, 'records': value}


def created_at(raw, source: str, config: dict):
    match = re.match(config['created_at_regex'], raw.strip()) if isinstance(raw, str) else None
    if not match:
        return None
    parts = match.groupdict()
    try:
        naive = datetime(int(parts['year']), _MONTHS[parts['mon']], int(parts['day']), int(parts['h']), int(parts['m']),
                         int(parts['s']))
    except ValueError:
        return None
    offset = numeric_offset_minutes(parts['off'][0], parts['off'][1:3], parts['off'][3:5])
    if offset is None:
        return None
    return offset_candidate('created', raw, source, naive, offset, 'second', {'field': 'created_at'})


def _tweets(records, ctx: messaging.FileContext, config: dict) -> list[messaging.Thread]:
    messages = []
    for index, record in enumerate(records):
        if index >= ctx.limits.max_records:
            ctx.issues.add('message_limit', limit=ctx.limits.max_records)
            break
        tweet = record.get('tweet') if isinstance(record, dict) and isinstance(record.get('tweet'), dict) else record
        if not isinstance(tweet, dict):
            ctx.issues.add('message_not_an_object', index=index)
            continue
        layers = TextLayers(None, ctx.limits)
        problems = []
        tweet_id = tweet.get('id_str') or (str(tweet['id']) if isinstance(tweet.get('id'), (int, str)) else None)
        candidate = created_at(tweet.get('created_at'), ctx.time_source('created_at'), config)
        if candidate is None:
            problems.append('time_unparsed:created_at')
        text = layers.text('full_text', tweet.get('full_text') if 'full_text' in tweet else tweet.get('text'))
        kind = 'retweet' if (text or '').startswith('RT @') or tweet.get('retweeted_status') else \
            'reply' if tweet.get('in_reply_to_status_id_str') else 'post'
        refs = []
        media = ((tweet.get('extended_entities') or {}).get('media') or (tweet.get('entities') or {}).get('media') or [])
        for item in media[:ctx.limits.max_raw_items] if isinstance(media, list) else []:
            if isinstance(item, dict):
                url = item.get('media_url_https') or item.get('media_url')
                name = url.rsplit('/', 1)[-1] if isinstance(url, str) else None
                refs.append(messaging.MediaRef(item.get('type') or 'media', f'{config["tweet_media_dir"]}/{tweet_id}-{name}'
                                               if name and tweet_id else None, name=name, metadata={'media_url': url}))
        if not tweet_id:
            problems.append('message_id_missing')
        record_id = tweet_id or messaging.derived_record_id('tweet', tweet.get('created_at'), text)
        chat = {'post_id': tweet_id, 'in_reply_to_status_id': tweet.get('in_reply_to_status_id_str'),
                'in_reply_to_screen_name': tweet.get('in_reply_to_screen_name'), 'lang': tweet.get('lang'),
                'source_client': tweet.get('source'), 'counts_raw': {'favorite_count': tweet.get('favorite_count'),
                                                                     'retweet_count': tweet.get('retweet_count')}}
        messages.append(messaging.Message(
            key=f'posts/{record_id}#{index}', record_id=record_id,
            record_id_basis='source_message_id' if tweet_id else 'content_derived_tweet_v1', body=text,
            times=[candidate] if candidate else [], content_kind=kind, chat=chat, raw=tweet, text_layers=layers.as_dict(),
            signal=signal('active', 'raw_sample', 'x_twitter', 'account'), direction='outgoing',
            direction_basis='account_archive_own_posts', event_type='social_post', refs=refs, issues=problems,
            locator={'json_pointer': f'/{index}', 'record_index': index}))
        for problem in problems:
            ctx.issues.add(problem, index=index)
    return [messaging.Thread('posts', 'X/Twitter posts', [], messages, {})]


def _dms(records, ctx: messaging.FileContext, config: dict, group: bool) -> list[messaging.Thread]:
    threads, total = [], 0
    media_dir = config['dm_group_media_dir'] if group else config['dm_media_dir']
    owner = ctx.owner
    for position, record in enumerate(records):
        conversation = record.get('dmConversation') if isinstance(record, dict) else None
        if not isinstance(conversation, dict):
            ctx.issues.add('thread_not_an_object', index=position)
            continue
        thread_id = str(conversation.get('conversationId') or messaging.derived_record_id('dm', position))
        messages = []
        for index, entry in enumerate(conversation.get('messages') or []):
            if total >= ctx.limits.max_records:
                ctx.issues.add('message_limit', limit=ctx.limits.max_records)
                break
            total += 1
            if not isinstance(entry, dict) or len(entry) != 1:
                ctx.issues.add('message_not_an_object', thread=thread_id, index=index)
                continue
            (event_kind, item), = entry.items()
            if not isinstance(item, dict):
                ctx.issues.add('message_not_an_object', thread=thread_id, index=index)
                continue
            layers = TextLayers(None, ctx.limits)
            problems = []
            times = []
            raw_time = item.get('createdAt')
            if isinstance(raw_time, str):
                try:
                    times.append(from_iso('created', raw_time, ctx.time_source('createdAt'), metadata={'field': 'createdAt'}))
                except ValueError:
                    problems.append('time_unparsed:createdAt')
            else:
                problems.append('time_missing:createdAt')
            message_id = item.get('id')
            sender_id = item.get('senderId') or item.get('initiatingUserId') or item.get('userId')
            text = layers.text('text', item.get('text'))
            refs = []
            for url in (item.get('mediaUrls') or [])[:ctx.limits.max_raw_items] if isinstance(item.get('mediaUrls'), list) else []:
                name = url.rsplit('/', 1)[-1] if isinstance(url, str) else None
                refs.append(messaging.MediaRef('media', f'{media_dir}/{message_id}-{name}' if name and message_id else None,
                                               name=name, metadata={'media_url': url}))
            content_kind = ('text' if text else 'media' if refs else 'empty') if event_kind == 'messageCreate' else \
                'reaction' if event_kind == 'reactionCreate' else 'service'
            record_id = str(message_id) if message_id else messaging.derived_record_id(thread_id, event_kind, raw_time,
                                                                                         sender_id)
            direction, basis = owner.direction(sender_id=str(sender_id)) if sender_id is not None else (None, None)
            chat = {'event_kind': event_kind, 'message_id': message_id, 'recipient_id': item.get('recipientId'),
                    'reactions': [{'reaction': r.get('reactionKey'), 'sender_id': r.get('senderId')}
                                  for r in (item.get('reactions') or []) if isinstance(r, dict)][:ctx.limits.max_raw_items],
                    'edit_history_count': len(item.get('editHistory') or []) if isinstance(item.get('editHistory'), list) else None,
                    'group': group}
            if event_kind not in ('messageCreate', 'reactionCreate', 'joinConversation', 'participantsJoin',
                                  'participantsLeave', 'conversationNameUpdate'):
                problems.append('message_type_unmapped')
            messages.append(messaging.Message(
                key=f'{thread_id}/{record_id}#{index}', record_id=record_id,
                record_id_basis='source_message_id' if message_id else 'content_derived_dm_v1', body=text, times=times,
                content_kind=content_kind, chat=chat, raw=item, text_layers=layers.as_dict(),
                signal=signal('active', 'raw_sample', 'x_twitter', 'account'),
                sender_id=str(sender_id) if sender_id is not None else None,
                recipients=str(item.get('recipientId')) if item.get('recipientId') is not None else None,
                direction=direction, direction_basis=basis, refs=refs, issues=problems,
                locator={'json_pointer': f'/{position}/dmConversation/messages/{index}', 'message_index': index}))
            for problem in problems:
                ctx.issues.add(problem, thread=thread_id, index=index)
        threads.append(messaging.Thread(thread_id, conversation.get('name'), [], messages, {'group': group}))
    return threads


def parse(data, ctx: messaging.FileContext) -> list[messaging.Thread]:
    config, _ = mapping(MAPPING)
    records = data.get('records') if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise ValueError('archive file must assign a JSON array')
    name = (ctx.file.name or '').lower()
    assigned = data.get('assigned') or ''
    if re.match(config['tweet_files_regex'], name) or assigned.startswith(('tweets.', 'tweet.')):
        return _tweets(records, ctx, config)
    if re.match(config['dm_files_regex'], name) or assigned.startswith('direct_messages'):
        return _dms(records, ctx, config, group='group' in name or 'group' in assigned)
    raise ValueError(f'unsupported X archive file {ctx.file.name!r} (supported: tweets.js, direct-messages*.js)')


def account_id(path: Path) -> str | None:
    """``accountId`` from a sibling ``account.js`` (owner direction for DMs)."""
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        return None
    body, _ = strip_js_assignment(path.read_text(encoding='utf-8-sig', errors='replace'))
    try:
        value = json.loads(body.rstrip().rstrip(';'))
    except json.JSONDecodeError:
        return None
    account = value[0].get('account') if isinstance(value, list) and value and isinstance(value[0], dict) else None
    return str(account['accountId']) if isinstance(account, dict) and account.get('accountId') else None


def import_archive(path: Path, *, owner_id: str | None = None, export_label: str | None = None,
                   source_locator: str | None = None, original_name: str | None = None, collection_id: int | None = None,
                   label: str | None = None, resolver=None, limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """A ``tweets.js``/``direct-messages*.js`` file or an archive folder (its ``data/``)."""
    config, sha = mapping(MAPPING)
    issues = Issues(limits.max_issues)
    path = Path(path)
    label = label or 'X/Twitter archive'
    owner_basis = 'owner_id_parameter' if owner_id else None
    if path.is_dir():
        wanted = re.compile(f'(?:{config["tweet_files_regex"]})|(?:{config["dm_files_regex"]})')
        files = messaging.walk_inputs(path, lambda p: ('archive_data', None) if wanted.match(p.name) else None,
                                      limits=limits, issues=issues, source_locator=source_locator)
        if owner_id is None:
            for item in files:
                found = account_id(item.path.parent / config['account_file'])
                if found:
                    owner_id, owner_basis = found, 'export_account_js'
                    break

        def locator_of(candidate: Path) -> str:
            relative = candidate.relative_to(path).as_posix()
            return f'{source_locator}/{relative}' if source_locator else str(candidate)
        resolver = resolver or messaging.directory_resolver(sorted({f.path.parent for f in files}),
                                                            collection_id=collection_id, label=label, locator_of=locator_of)
    else:
        files = [messaging.single_input(path, source_locator=source_locator, original_name=original_name,
                                        role='archive_data')]
        resolver = resolver or messaging.no_files_resolver()
    owner = messaging.Owner(ids=frozenset({str(owner_id)}) if owner_id else frozenset(), basis=owner_basis)
    result = messaging.import_files(files, namespace='twitter_x', platform='twitter_x', parser=PARSER_NAME,
                                    parser_version=PARSER_VERSION, mapping_version=config['version'], mapping_sha256=sha,
                                    parse=parse, load=load_js, owner=owner, resolver=resolver, export_label=export_label,
                                    collection_id=collection_id, label=label, limits=limits,
                                    options={'owner_id_given': owner_id is not None, 'owner_basis': owner_basis,
                                             'export_label': export_label},
                                    mime_type='text/javascript')
    result['walk'] = issues.as_dict()
    return result

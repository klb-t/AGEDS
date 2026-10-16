"""Synthetic AI chat exports (ChatGPT, Claude.ai) with planted hard cases.

Everything is generated here; no real conversation, account or address is used
(only example.org). ``build(root)`` writes the files and returns a manifest of
the planted cases with the exact expectations the tests assert.

ChatGPT conversation ``C`` appears in three exports:

* e1: root → sys(hidden) → u1 → t1(thoughts) → {a1 → u2 → a2, a1x}; current a2
* e2: adds u3 → a3 under a2; current a3
* e3: u2 edited (same id, new text), a3b regenerated next to a3 (current a3b),
  a1x missing, title renamed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

C_ID = '6b1f0c2e-0000-4000-8000-00000000c001'
S_ID = '6b1f0c2e-0000-4000-8000-00000000c002'
P_ID = '6b1f0c2e-0000-4000-8000-00000000c003'
K_ID = '9d2a7e10-0000-4000-8000-00000000a001'
OLD_ID = '9d2a7e10-0000-4000-8000-00000000a002'
ROOT_SENTINEL = '00000000-0000-4000-8000-000000000000'


def epoch(text: str) -> float:
    return datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(timezone.utc).timestamp()


def _message(node_id, role, content, created, *, model=None, hidden=False, author_name=None, recipient='all',
             metadata=None, update_time=None):
    meta = dict(metadata or {})
    if model:
        meta['model_slug'] = model
        meta['default_model_slug'] = model
    if hidden:
        meta['is_visually_hidden_from_conversation'] = True
    return {
        'id': node_id, 'author': {'role': role, 'name': author_name, 'metadata': {}},
        'create_time': created, 'update_time': update_time, 'content': content, 'status': 'finished_successfully',
        'end_turn': True if role == 'assistant' else None, 'weight': 0.0 if hidden else 1.0,
        'metadata': meta, 'recipient': recipient, 'channel': None,
    }


def _text(*parts):
    return {'content_type': 'text', 'parts': list(parts)}


def _node(node_id, parent, children, message):
    return {'id': node_id, 'message': message, 'parent': parent, 'children': children}


T0 = epoch('2025-02-10T09:00:00Z')


def _conversation_c(version: int) -> dict:
    """The same logical conversation as seen by export 1, 2 or 3."""
    mapping = {
        'root': _node('root', None, ['sys'], None),
        'sys': _node('sys', 'root', ['u1'], _message('sys', 'system', _text(''), None, hidden=True)),
        'u1': _node('u1', 'sys', ['t1'], _message('u1', 'user', _text('Please outline the plan for example.org.'), T0 + 10)),
        't1': _node('t1', 'u1', ['a1', 'a1x'], _message('t1', 'assistant', {
            'content_type': 'thoughts',
            'thoughts': [{'summary': 'Considering scope', 'content': 'Internal reasoning text, kept separate.'}],
            'source_analysis_msg_id': 'x'}, T0 + 12, model='synthetic-model-1')),
        'a1': _node('a1', 't1', ['u2'], _message('a1', 'assistant', _text('Here is the outline.'), T0 + 20, model='synthetic-model-1')),
        'a1x': _node('a1x', 't1', [], _message('a1x', 'assistant', _text('An earlier regenerated outline.'), T0 + 15, model='synthetic-model-1')),
        'u2': _node('u2', 'a1', ['a2'], _message('u2', 'user', _text('Second question?'), T0 + 30)),
        'a2': _node('a2', 'u2', [], _message('a2', 'assistant', _text('Second answer.'), T0 + 40, model='synthetic-model-2')),
    }
    current = 'a2'
    title = 'Planning notes'
    if version >= 2:
        mapping['a2']['children'] = ['u3']
        mapping['u3'] = _node('u3', 'a2', ['a3'], _message('u3', 'user', _text('Third question?'), T0 + 50))
        mapping['a3'] = _node('a3', 'u3', [], _message('a3', 'assistant', _text('Third answer.'), T0 + 60, model='synthetic-model-2'))
        current = 'a3'
    if version >= 3:
        mapping['u2']['message']['content'] = _text('Second question, edited.')
        mapping['u3']['children'] = ['a3', 'a3b']
        mapping['a3b'] = _node('a3b', 'u3', [], _message('a3b', 'assistant', _text('Third answer, regenerated.'), T0 + 70, model='synthetic-model-3'))
        current = 'a3b'
        del mapping['a1x']
        mapping['t1']['children'] = ['a1']
        title = 'Planning notes (renamed)'
    return {'title': title, 'create_time': T0, 'update_time': T0 + 10 * (version + 6), 'mapping': mapping,
            'moderation_results': [], 'current_node': current, 'plugin_ids': None, 'conversation_id': C_ID,
            'conversation_template_id': None, 'gizmo_id': None, 'is_archived': False, 'safe_urls': [],
            'default_model_slug': 'synthetic-model-1', 'id': C_ID}


def _simple_conversation(conv_id, title, start, times, *, model='synthetic-model-1'):
    mapping = {'root': _node('root', None, ['m0'], None)}
    previous = 'root'
    for i, offset in enumerate(times):
        node = f'm{i}'
        role = 'user' if i % 2 == 0 else 'assistant'
        nxt = [f'm{i + 1}'] if i + 1 < len(times) else []
        mapping[node] = _node(node, previous, nxt, _message(node, role, _text(f'{title} message {i}.'), start + offset,
                                                             model=model if role == 'assistant' else None))
        previous = node
    return {'title': title, 'create_time': start, 'update_time': start + times[-1] + 60, 'mapping': mapping,
            'current_node': previous, 'conversation_id': conv_id, 'id': conv_id, 'default_model_slug': model}


def _write(path: Path, value, *, ensure_ascii=False, replace: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=ensure_ascii, indent=1)
    for marker, raw in (replace or {}).items():
        assert marker in text, marker
        text = text.replace(marker, raw)
    path.write_bytes(text.encode('utf-8'))
    return path


LONG_WORDS = 700_010  # "lorem " * N exceeds the parser's 4,000,000-character body bound


def _edge_conversations() -> list:
    null_times = {
        'title': 'Null times', 'create_time': None, 'update_time': None, 'current_node': 'n2',
        'conversation_id': 'edge-null-times', 'id': 'edge-null-times',
        'mapping': {
            'root': _node('root', None, ['n1'], None),
            'n1': _node('n1', 'root', ['n2'], _message('n1', 'user', _text('No time at all.'), None)),
            'n2': _node('n2', 'n1', ['n3'], _message('n2', 'assistant', _text('Zero placeholder time.'), 0,
                                                     update_time='not-a-time')),
            'n3': _node('n3', 'n2', [], _message('n3', 'user', _text('Overflowing time.'), 'OVERFLOW_TIME')),
        },
    }
    cycle = {
        'title': 'Cycle', 'create_time': T0, 'update_time': T0, 'current_node': 'x',
        'conversation_id': 'edge-cycle', 'id': 'edge-cycle',
        'mapping': {
            'root': _node('root', None, [], None),
            'x': _node('x', 'y', ['y'], _message('x', 'user', _text('x in a cycle'), T0 + 1)),
            'y': _node('y', 'x', ['x', 'z'], _message('y', 'assistant', _text('y in a cycle'), T0 + 2)),
            'z': _node('z', 'y', [], _message('z', 'user', _text('z below the cycle'), T0 + 3)),
        },
    }
    unicode_conv = {
        'title': 'Unicode \U0001F600 ‫مرحبا‬', 'create_time': T0, 'update_time': T0, 'current_node': 'u4',
        'conversation_id': 'edge-unicode', 'id': 'edge-unicode',
        'mapping': {
            'root': _node('root', None, ['u1'], None),
            'u1': _node('u1', 'root', ['u2'], _message('u1', 'user', _text('Emoji \U0001F600 math \U0001D518 family \U0001F468‍\U0001F469‍\U0001F467 é'), T0 + 1)),
            'u2': _node('u2', 'u1', ['u3'], _message('u2', 'assistant', _text('‏מה שלומך؟ مرحبا بالعالم‏'), T0 + 2)),
            'u3': _node('u3', 'u2', ['u4'], _message('u3', 'user', _text('Lone LONE_SURROGATE_MARKER surrogate'), T0 + 3)),
            'u4': _node('u4', 'u3', [], _message('u4', 'assistant', _text('lorem ' * LONG_WORDS), T0 + 4)),
        },
    }
    kinds = {
        'title': 'Content kinds and malformed nodes', 'create_time': T0, 'update_time': T0, 'current_node': 'k9',
        'conversation_id': 'edge-kinds', 'id': 'edge-kinds',
        'mapping': {
            'root': _node('root', None, ['k1'], None),
            'k1': _node('k1', 'root', ['k2'], _message('k1', 'system', {
                'content_type': 'user_editable_context', 'user_profile': 'Profile text', 'user_instructions': 'Be brief'},
                T0 + 1, hidden=True, metadata={'is_user_system_message': True})),
            'k2': _node('k2', 'k1', ['k3'], _message('k2', 'user', {
                'content_type': 'multimodal_text', 'parts': [
                    {'content_type': 'image_asset_pointer', 'asset_pointer': 'file-service://file-IMG1', 'size_bytes': 10},
                    'Look at this picture',
                    {'content_type': 'image_asset_pointer', 'asset_pointer': 'sediment://file_00000000aaaa'},
                    {'content_type': 'audio_transcription', 'text': 'spoken words'}]},
                T0 + 2, metadata={'attachments': [{'id': 'file-ATTACH1', 'name': 'notes.txt', 'mime_type': 'text/plain', 'size': 12},
                                                   {'id': 'file-MISSING9', 'name': 'gone.pdf'}]})),
            'k3': _node('k3', 'k2', ['k4'], _message('k3', 'assistant', {'content_type': 'code', 'language': 'python', 'text': 'print(1)'},
                                                     T0 + 3, recipient='python', model='synthetic-model-1')),
            'k4': _node('k4', 'k3', ['k5'], _message('k4', 'tool', {'content_type': 'execution_output', 'text': '1'}, T0 + 4,
                                                     author_name='python')),
            'k5': _node('k5', 'k4', ['k6'], _message('k5', 'assistant', {'content_type': 'reasoning_recap', 'content': 'Thought for 3 seconds'},
                                                     T0 + 5, model='synthetic-model-1')),
            'k6': _node('k6', 'k5', ['k7', 'ghost-child'], _message('k6', 'tool', {
                'content_type': 'tether_browsing_display', 'result': 'search result text', 'summary': None}, T0 + 6, author_name='browser')),
            'k7': _node('k7', 'k6', ['k8'], _message('k7', 'assistant', {
                'content_type': 'mystery_type', 'payload': {'a': [1, 2, 3]}}, T0 + 7, model='synthetic-model-1')),
            'k8': _node('k8', 'k7', ['k9'], _message('k8', 7, None, T0 + 8)),
            'k9': _node('k9', 'k8', [], _message('k9', 'assistant', _text('done'), T0 + 9, model='synthetic-model-1')),
            'orphan': _node('orphan', 'ghost-parent', [], _message('orphan', 'user', _text('orphaned node'), T0 + 10)),
            'bad': 42,
            'dupnode': _node('dupnode', 'k9', [], _message('dupnode', 'user', _text('second copy in file order'), T0 + 11)),
        },
    }
    return [null_times, cycle, unicode_conv, kinds]


def _claude_export() -> list:
    def block_text(text, start, stop):
        return {'start_timestamp': start, 'stop_timestamp': stop, 'type': 'text', 'text': text, 'citations': []}
    k = {
        'uuid': K_ID, 'name': 'Parallel research', 'summary': 'Synthetic summary.',
        'created_at': '2025-03-01T10:05:00.000000Z', 'updated_at': '2025-03-01T10:25:00+00:00',
        'account': {'uuid': '11111111-0000-4000-8000-000000000001'},
        'chat_messages': [
            {'uuid': 'k-m1', 'text': 'Find the files.', 'sender': 'human', 'parent_message_uuid': ROOT_SENTINEL,
             'created_at': '2025-03-01T10:05:10.000000Z', 'updated_at': '2025-03-01T10:05:10.000000Z',
             'content': [block_text('Find the files.', '2025-03-01T10:05:10.000000Z', '2025-03-01T10:05:10.000000Z')],
             'attachments': [{'file_name': 'notes.txt', 'file_size': 15, 'file_type': 'text/plain',
                              'extracted_content': 'synthetic notes'}],
             'files': [{'file_name': 'diagram.png'}]},
            {'uuid': 'k-m2', 'text': 'Answer A\n\nMore answer', 'sender': 'assistant', 'parent_message_uuid': 'k-m1',
             'created_at': '2025-03-01T10:06:00.000000Z', 'updated_at': '2025-03-01T10:06:30.000000Z',
             'content': [
                 {'start_timestamp': '2025-03-01T10:06:00.000000Z', 'stop_timestamp': '2025-03-01T10:06:05.000000Z',
                  'type': 'thinking', 'thinking': 'Private chain of thought, synthetic.',
                  'summaries': [{'summary': 'Planning the search'}], 'cut_off': False},
                 block_text('Answer A', '2025-03-01T10:06:05.000000Z', '2025-03-01T10:06:06.000000Z'),
                 {'start_timestamp': None, 'stop_timestamp': None, 'type': 'tool_use', 'name': 'web_search',
                  'input': {'query': 'example.org files'}, 'id': 'toolu_1'},
                 {'start_timestamp': None, 'stop_timestamp': None, 'type': 'tool_result', 'name': 'web_search',
                  'content': [{'type': 'text', 'text': 'result from example.org'}], 'is_error': False},
                 block_text('More answer', '2025-03-01T10:06:20.000000Z', '2025-03-01T10:06:30.000000Z'),
             ], 'attachments': [], 'files': []},
            {'uuid': 'k-m3', 'text': 'Answer B', 'sender': 'assistant', 'parent_message_uuid': 'k-m1',
             'created_at': '2025-03-01T10:07:00.000000+02:00', 'updated_at': '2025-03-01T10:07:00.000000+02:00',
             'content': [block_text('Answer B', '2025-03-01T08:07:00.000000Z', '2025-03-01T08:07:01.000000Z')],
             'attachments': [], 'files': []},
            {'uuid': 'k-m4', 'text': 'Follow up', 'sender': 'human', 'parent_message_uuid': 'k-m2',
             'created_at': '2025-03-01T10:20:00', 'updated_at': '2025-03-01T10:20:00',
             'content': [block_text('Follow up', None, None), {'type': 'voice_note', 'data': {'seconds': 3}}],
             'attachments': [], 'files': []},
        ],
    }
    old = {
        'uuid': OLD_ID, 'name': 'Older format', 'created_at': '2024-12-01T08:00:00Z', 'updated_at': '2024-12-01T08:10:00Z',
        'chat_messages': [
            {'uuid': 'o-m1', 'text': 'Plain text only', 'sender': 'human', 'created_at': '2024-12-01T08:00:00Z',
             'updated_at': '2024-12-01T08:00:00Z', 'attachments': [], 'files': []},
            {'uuid': 'o-m2', 'text': 'Reply without content blocks', 'sender': 'assistant', 'created_at': '2024-12-01T08:01:00Z',
             'updated_at': '2024-12-01T08:01:00Z', 'attachments': [], 'files': []},
        ],
    }
    return [k, old]


def build(root: Path) -> dict:
    root = Path(root)
    chatgpt = root / 'chatgpt'
    exports = {}
    for version in (1, 2, 3):
        exports[f'e{version}'] = str(_write(chatgpt / f'export{version}' / 'conversations.json', [_conversation_c(version)]).parent)

    s_conv = _simple_conversation(S_ID, 'Shard one', epoch('2025-01-05T12:00:00Z'), [0, 30, 60])
    _write(chatgpt / 'sharded' / 'conversations-000.json', [_conversation_c(2)])
    _write(chatgpt / 'sharded' / 'conversations-001.json', [s_conv])
    (chatgpt / 'sharded' / 'user.json').write_text('{"email": "someone@example.org"}', encoding='utf-8')

    p_start = epoch('2025-03-01T10:00:00Z')
    _write(chatgpt / 'parallel' / 'conversations.json', [_simple_conversation(P_ID, 'Parallel GPT', p_start, [120, 600, 1680])])

    edge = _edge_conversations()
    duplicate_node = ('"dupnode": ' + json.dumps(_node('dupnode', 'k9', [], _message('dupnode', 'user', _text('first copy in file order'), T0 + 12))))
    edge_text_replace = {
        'LONE_SURROGATE_MARKER': '\\ud800',
        '"OVERFLOW_TIME"': '1e400',
    }
    edge_path = _write(chatgpt / 'edge' / 'conversations.json', [*edge, edge[1]], replace=edge_text_replace)
    text = edge_path.read_text(encoding='utf-8')
    marker = '"dupnode": {'
    position = text.index(marker)
    text = text[:position] + duplicate_node + ',\n' + text[position:]
    edge_path.write_text(text, encoding='utf-8')
    (chatgpt / 'edge' / 'file-ATTACH1-notes.txt').write_text('synthetic attachment', encoding='utf-8')
    (chatgpt / 'edge' / 'file-IMG1-a.png').write_bytes(b'\x89PNG\r\n\x1a\nsynthetic-a')
    (chatgpt / 'edge' / 'file-IMG1.dat').write_bytes(b'%PDF-1.4 synthetic pdf in a .dat file')
    (chatgpt / 'edge' / 'nested').mkdir(exist_ok=True)
    (chatgpt / 'edge' / 'nested' / 'file_00000000aaaa-1111.png').write_bytes(b'\x89PNG\r\n\x1a\nsediment')
    (chatgpt / 'edge' / 'file-IMG10-other.png').write_bytes(b'not a match for file-IMG1')

    good = _simple_conversation('malformed-good', 'Survives', T0, [0, 5])
    bad_utf8 = json.dumps(_simple_conversation('malformed-utf8', 'Bad bytes BADBYTES', T0, [0]), ensure_ascii=False).encode('utf-8')
    bad_utf8 = bad_utf8.replace(b'BADBYTES', b'\xff\xfe')
    malformed = (b'[' + json.dumps(good).encode() + b', 42, "a string", [1, 2], ' + bad_utf8 + b', {"id": "broken", "mapping": {"x": '
                 + b'{"id": "x", "message": {"content": {"content_type": "text", "parts": ["unterminated')
    (chatgpt / 'malformed').mkdir(parents=True, exist_ok=True)
    (chatgpt / 'malformed' / 'conversations.json').write_bytes(malformed)

    claude = _write(root / 'claude' / 'export1' / 'conversations.json', _claude_export()).parent
    (root / 'claude' / 'export1' / 'users.json').write_text('[{"email_address": "someone@example.org"}]', encoding='utf-8')

    return {
        'chatgpt_exports': exports,
        'conversation_id': C_ID,
        'expected_c': {
            'present_in': {'root': ['e1', 'e2', 'e3'], 'sys': ['e1', 'e2', 'e3'], 'u1': ['e1', 'e2', 'e3'],
                           't1': ['e1', 'e2', 'e3'], 'a1': ['e1', 'e2', 'e3'], 'a1x': ['e1', 'e2'],
                           'u2': ['e1', 'e2', 'e3'], 'a2': ['e1', 'e2', 'e3'], 'u3': ['e2', 'e3'],
                           'a3': ['e2', 'e3'], 'a3b': ['e3']},
            'content_variants': {'u2': [['e1', 'e2'], ['e3']]},
            'structure_variants': {'t1': [['e1', 'e2'], ['e3']], 'a2': [['e1'], ['e2', 'e3']], 'u3': [['e2'], ['e3']]},
            'current_path_changed': ['a3'],
            'titles': [['Planning notes', ['e1', 'e2']], ['Planning notes (renamed)', ['e3']]],
            'current_node': [['a2', ['e1']], ['a3', ['e2']], ['a3b', ['e3']]],
            'hidden': ['sys'], 'reasoning': ['t1'], 'structural': ['root'],
        },
        'sharded': {'path': str(chatgpt / 'sharded'), 'shards': {C_ID: 0, S_ID: 1}, 'other_files': ['user.json']},
        'parallel': {'chatgpt_path': str(chatgpt / 'parallel'), 'claude_path': str(claude), 'chatgpt_id': P_ID, 'claude_id': K_ID,
                     'overlap_utc': ['2025-03-01T10:05:00Z', '2025-03-01T10:25:00Z']},
        'edge': {'path': str(chatgpt / 'edge'), 'long_words': LONG_WORDS,
                 'attachments': {'file-ATTACH1': 'resolved', 'file-IMG1': 'ambiguous', 'file_00000000aaaa': 'resolved',
                                 'file-MISSING9': 'unresolved'}},
        'malformed': {'path': str(chatgpt / 'malformed'), 'recorded': ['malformed-good', 'malformed-utf8'],
                      'non_object_elements': 3},
        'claude': {'path': str(claude), 'conversation_id': K_ID, 'old_format_id': OLD_ID},
    }

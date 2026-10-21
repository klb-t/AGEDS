"""Opt-in scale run: a large synthetic ChatGPT export is streamed, not loaded whole.

Run with ``AGEDS_CHAT_SCALE_MB=120 python -m pytest server/tests/test_import_chat_scale.py -s``.
Set ``AGEDS_CHAT_SCALE_REPORT=/path/new.json`` to save the measurements.
"""
import json
import os
import resource
import time
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.config import Settings
from server.app.importers import chatgpt

SCALE_MB = int(os.environ.get('AGEDS_CHAT_SCALE_MB', '0') or 0)


def _conversation(index: int, nodes: int, text_bytes: int) -> dict:
    base = 1_735_689_600 + index * 3600
    mapping = {'root': {'id': 'root', 'message': None, 'parent': None, 'children': ['n0']}}
    filler = ('synthetic words for scale ' * (text_bytes // 26 + 1))[:text_bytes]
    for i in range(nodes):
        mapping[f'n{i}'] = {
            'id': f'n{i}', 'parent': 'root' if i == 0 else f'n{i - 1}', 'children': [f'n{i + 1}'] if i + 1 < nodes else [],
            'message': {'id': f'n{i}', 'author': {'role': 'user' if i % 2 == 0 else 'assistant'},
                        'create_time': base + i * 7.25, 'update_time': None,
                        'content': {'content_type': 'text', 'parts': [f'{index}/{i} {filler}']},
                        'metadata': {'model_slug': 'synthetic-model-1'} if i % 2 else {}},
        }
    return {'id': f'scale-{index:06d}', 'conversation_id': f'scale-{index:06d}', 'title': f'Scale {index}',
            'create_time': base, 'update_time': base + nodes * 8, 'current_node': f'n{nodes - 1}', 'mapping': mapping}


@pytest.mark.skipif(SCALE_MB <= 0, reason='opt-in: set AGEDS_CHAT_SCALE_MB')
def test_large_export_is_streamed_with_bounded_memory(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, db_path=tmp_path / 'evidence.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', settings)
    monkeypatch.setattr(evidence, 'settings', settings)
    dbmod.init_db()
    export = tmp_path / 'export'
    export.mkdir()
    path = export / 'conversations.json'
    nodes, text_bytes = 24, 2000
    conversations = 0
    with path.open('w', encoding='utf-8') as out:
        out.write('[')
        while out.tell() < SCALE_MB * 1024 * 1024:
            if conversations:
                out.write(',\n')
            out.write(json.dumps(_conversation(conversations, nodes, text_bytes)))
            conversations += 1
        out.write(']')
    size = path.stat().st_size
    rss_before_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    result = chatgpt.import_chatgpt_export([export], export_label='scale')
    seconds = time.perf_counter() - started
    rss_after_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    measurements = {
        'file_bytes': size, 'conversations': conversations, 'nodes_per_conversation': nodes + 1,
        'events': result['totals'].get('chat_message', 0) + result['totals'].get('chat_conversation', 0),
        'seconds': round(seconds, 2), 'peak_rss_kb_before_import': rss_before_kb, 'peak_rss_kb_after_import': rss_after_kb,
        'peak_rss_increase_mb': round((rss_after_kb - rss_before_kb) / 1024, 1), 'coverage': result['coverage'],
    }
    print(json.dumps(measurements))
    report = os.environ.get('AGEDS_CHAT_SCALE_REPORT')
    if report:
        Path(report).write_text(json.dumps(measurements, indent=2) + '\n', encoding='utf-8')
    assert result['coverage'] == 'complete' and result['totals']['conversations'] == conversations
    # json.load of the whole file would need several times the file size in heap.
    assert (rss_after_kb - rss_before_kb) * 1024 < max(size / 2, 64 * 1024 * 1024)

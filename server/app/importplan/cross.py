"""The same chat conversation in several exports, collections or drives.

Before import (``pre_import``) the plan compares chat-export units cheaply:
identical conversation-file bytes (same SHA-256 set) and, for conversation
files up to ``max_conversation_id_scan_bytes``, the conversation ids read with
the chat importers' own element scanner and mapping pointers. After import
(``post_import``) the logical records written by the chat importers
(``logical_records`` / ``logical_occurrences``) are grouped per conversation
across the exports the plan's items produced.

Nothing is deduplicated or deleted: every occurrence is imported and kept;
this is a view.
"""
from __future__ import annotations

import json
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from .. import db as dbmod
from ..evidence import IntegrityError
from .reader import ReadLimitError

_MAPPINGS = Path(__file__).resolve().parents[1] / 'importers' / 'data'
NAMESPACE = {'chatgpt_export': 'chatgpt', 'claude_export': 'claude'}


def _id_pointers(namespace: str) -> list[str]:
    data = json.loads((_MAPPINGS / f'{namespace}_mapping.v1.json').read_text(encoding='utf-8'))
    return data['conversation']['id']


def conversation_ids(data: bytes, namespace: str, max_elements: int = 1_000_000) -> tuple[list[str], dict]:
    from ..importers.chat_common import IssueLog, decode_element, first_of, iter_array_elements
    issues = IssueLog(20)
    pointers = _id_pointers(namespace)
    found = []
    for element in iter_array_elements(data, max_element_bytes=len(data) + 1, max_elements=max_elements,
                                       issues=issues, file_label='probe'):
        if element.status != 'ok':
            continue
        value = decode_element(element, issues, f'probe#/{element.index}')
        if not isinstance(value, dict):
            continue
        conversation_id, _ = first_of(value, pointers)
        if isinstance(conversation_id, str) and conversation_id:
            found.append(conversation_id)
    return found, issues.as_dict()['issue_counts']


def pre_import(items: list, reader, limits) -> dict:
    """Plan-time comparison of chat-export units (items are detect.Item objects)."""
    by_hashes: dict[tuple, list[int]] = defaultdict(list)
    seen: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    units = []
    for ordinal, item in enumerate(items, 1):
        if item.detection.get('unit') != 'chat_export':
            continue
        files = item.detection['conversation_files']
        hashes = tuple(sorted(f['sha256'] or f'unhashed:{f["entry_id"]}' for f in files))
        by_hashes[hashes].append(ordinal)
        candidates = [c['importer'] for c in item.candidates]
        namespaces = sorted({NAMESPACE[c] for c in candidates if c in NAMESPACE})
        scan = {'ordinal': ordinal, 'locator': item.locator, 'collection_id': item.collection_id,
                'namespaces': namespaces, 'conversation_ids': 0, 'scanned': True, 'issues': {}}
        ids: set[str] = set()
        for f in files:
            size = f['size_bytes'] or 0
            if f['status'] != 'ok' or size > limits.max_conversation_id_scan_bytes:
                scan['scanned'] = False
                scan.setdefault('not_scanned', []).append({'entry_id': f['entry_id'], 'reason':
                    'size_over_max_conversation_id_scan_bytes' if f['status'] == 'ok' else f'entry_status_{f["status"]}'})
                continue
            try:
                with reader.open(f['entry_id']) as stream:
                    data = stream.read(limits.max_conversation_id_scan_bytes + 1)
            except (IntegrityError, ReadLimitError, OSError) as exc:
                scan['scanned'] = False
                scan.setdefault('not_scanned', []).append({'entry_id': f['entry_id'], 'reason': f'{type(exc).__name__}'})
                continue
            for namespace in namespaces:
                found, issues = conversation_ids(data, namespace)
                ids.update(f'{namespace}:{c}' for c in found)
                for code, count in issues.items():
                    scan['issues'][code] = scan['issues'].get(code, 0) + count
        for key in ids:
            seen[key][item.locator].append(ordinal)
        scan['conversation_ids'] = len(ids)
        units.append(scan)
    shared = []
    for key, places in sorted(seen.items()):
        ordinals = sorted({o for values in places.values() for o in values})
        if len(ordinals) > 1:
            namespace, _, conversation = key.partition(':')
            shared.append({'namespace': namespace, 'conversation_id': conversation, 'item_ordinals': ordinals})
    identical = [{'item_ordinals': ordinals, 'conversation_files_sha256': list(hashes)}
                 for hashes, ordinals in sorted(by_hashes.items()) if len(ordinals) > 1]
    return {
        'basis': 'plan time: identical conversation-file bytes and conversation ids read from files within the scan bound',
        'units': units,
        'identical_conversation_files': identical,
        'shared_conversations': shared[:limits.max_conversation_ids_listed],
        'shared_conversations_total': len(shared),
        'shared_conversations_truncated': max(0, len(shared) - limits.max_conversation_ids_listed),
        'policy': 'every occurrence is imported; nothing is deduplicated or deleted',
    }


def post_import(plan_id: int, *, limit: int = 500) -> dict:
    """Conversations whose logical records occur in more than one export produced by this plan."""
    from .plan import done_attempts
    exports: dict[int, dict] = {}
    for attempt in done_attempts(plan_id):
        result = attempt['result']
        export = (result.get('importer_result') or {}).get('export')
        if attempt['importer'] in NAMESPACE and export and export.get('id'):
            exports[int(export['id'])] = {'export_id': int(export['id']), 'label': export.get('label'),
                                          'item_id': attempt['item_id'], 'collection_id': export.get('collection_id')}
    if not exports:
        return {'basis': 'after import: logical records of the chat importers', 'exports': [], 'conversations': [],
                'conversations_total': 0}
    marks = ','.join('?' * len(exports))
    with closing(dbmod.connect()) as db:
        rows = db.execute(f'''SELECT r.namespace, r.container_id, COUNT(DISTINCT o.export_id) AS exports,
                GROUP_CONCAT(DISTINCT o.export_id) AS export_ids, COUNT(DISTINCT o.content_sha256) AS content_variants
                FROM logical_records r JOIN logical_occurrences o ON o.logical_record_id=r.id
                WHERE r.record_kind='conversation' AND o.export_id IN ({marks})
                GROUP BY r.id HAVING COUNT(DISTINCT o.export_id) > 1
                ORDER BY r.namespace, r.container_id LIMIT ?''', (*exports, limit + 1)).fetchall()
        total = db.execute(f'''SELECT COUNT(*) FROM (SELECT r.id FROM logical_records r
                JOIN logical_occurrences o ON o.logical_record_id=r.id
                WHERE r.record_kind='conversation' AND o.export_id IN ({marks})
                GROUP BY r.id HAVING COUNT(DISTINCT o.export_id) > 1)''', tuple(exports)).fetchone()[0]
    conversations = []
    for row in rows[:limit]:
        ids = sorted(int(x) for x in row['export_ids'].split(','))
        conversations.append({'namespace': row['namespace'], 'conversation_id': row['container_id'],
                              'exports': [exports[i] for i in ids], 'title_variants': row['content_variants'],
                              'dedup_report': f'/api/imports/chat/dedup-report?namespace={row["namespace"]}'})
    return {'basis': 'after import: logical records of the chat importers (dedup report has the details)',
            'exports': sorted(exports.values(), key=lambda e: e['export_id']), 'conversations': conversations,
            'conversations_total': total, 'truncated': len(rows) > limit,
            'policy': 'every occurrence is imported; nothing is deduplicated or deleted'}

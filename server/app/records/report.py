"""Logical dedup report: one record observed across several exports.

For each namespace and interpretation (parser + mapping + normalization), the
report lists the exports parsed under it (in import order, which is not the
date the export was produced) and, per conversation and per record:

* ``present_in`` / ``not_observed_in`` — absence means the record was not
  observed in that export under this interpretation. It is never reported as
  deletion; the export's parse coverage is shown next to each absence;
* ``content_variants`` — distinct hashes of the versioned content projection
  (edits or reinterpretation), each with its exports and event ids;
* ``structure_variants`` — distinct parent/children sets (branches);
* ``on_current_path`` per export and conversation-level title/current-node
  changes.

Records are compared only within one interpretation; exports never parsed under
it are listed separately as ``not_parsed_under_interpretation``.
"""
from __future__ import annotations

import json
from collections import OrderedDict, defaultdict

from ..db import session

REPORT_SCHEMA = 'ageds.logical_dedup_report.v1'
MAX_LIMIT = 10_000
ABSENCE_SEMANTICS = ('not_observed_in_export: the record was not seen in that export under this interpretation; '
                     'it does not establish deletion, and partial export coverage is reported beside it')


def _group(items, key_of, export_of):
    groups: OrderedDict = OrderedDict()
    for item in items:
        key = key_of(item)
        entry = groups.setdefault(key, {'exports': [], 'event_ids': []})
        label = export_of(item)
        if label not in entry['exports']:
            entry['exports'].append(label)
        if item['event_id'] is not None and item['event_id'] not in entry['event_ids']:
            entry['event_ids'].append(item['event_id'])
    return groups


def _export_files(db, export_ids: list[int], interpretation: str) -> dict:
    files: dict[int, dict] = {}
    if not export_ids:
        return files
    marks = ','.join('?' * len(export_ids))
    for row in db.execute(f'''SELECT f.id, f.export_id, f.artifact_id, f.file_name, f.file_role, f.shard_index,
                                      p.coverage
                               FROM record_export_files f
                               LEFT JOIN record_export_parses p ON p.export_file_id=f.id AND p.interpretation=?
                               WHERE f.export_id IN ({marks}) ORDER BY f.id, p.id''', (interpretation, *export_ids)):
        entry = files.setdefault(row['id'], {'export_id': row['export_id'], 'artifact_id': row['artifact_id'],
                                             'file_name': row['file_name'], 'file_role': row['file_role'],
                                             'shard_index': row['shard_index'], 'coverages': []})
        if row['coverage'] is not None:
            entry['coverages'].append(row['coverage'])
    for entry in files.values():
        coverages = entry.pop('coverages')
        if entry['file_role'] == 'attachment':
            entry['coverage'] = 'not_parsed'
        elif not coverages:
            entry['coverage'] = 'not_parsed_under_interpretation'
        else:
            entry['coverage'] = 'complete' if 'complete' in coverages else ('partial' if 'partial' in coverages else 'failed')
    return files


def _interpretation_report(db, namespace: str, exports: list, interpretation: str, *, conversation_id: str | None,
                           changes_only: bool, limit: int, offset: int) -> dict:
    files = _export_files(db, [e['id'] for e in exports], interpretation)
    label = {e['id']: e['label'] for e in exports}
    export_coverage: dict[int, str] = {}
    for e in exports:
        coverages = [f['coverage'] for f in files.values() if f['export_id'] == e['id'] and f['file_role'] != 'attachment']
        parsed = [c for c in coverages if c != 'not_parsed_under_interpretation']
        if parsed:
            export_coverage[e['id']] = 'complete' if all(c == 'complete' for c in parsed) and len(parsed) == len(coverages) else 'partial'
    parsed_exports = [e['id'] for e in exports if e['id'] in export_coverage]
    file_of = {(f['export_id'], f['artifact_id']): f for f in files.values()}
    params: list = [namespace, interpretation]
    conv_filter = ''
    if conversation_id is not None:
        conv_filter = ' AND lr.container_id=?'
        params.append(conversation_id)
    total = db.execute(f'''SELECT COUNT(DISTINCT lr.container_id) FROM logical_records lr
                           JOIN logical_occurrences lo ON lo.logical_record_id=lr.id
                           WHERE lr.namespace=? AND lo.interpretation=?{conv_filter}''', params).fetchone()[0]
    containers = [r[0] for r in db.execute(f'''SELECT DISTINCT lr.container_id FROM logical_records lr
                           JOIN logical_occurrences lo ON lo.logical_record_id=lr.id
                           WHERE lr.namespace=? AND lo.interpretation=?{conv_filter}
                           ORDER BY lr.container_id LIMIT ? OFFSET ?''', (*params, limit, offset))]
    summary = defaultdict(int)
    conversations = []
    for container in containers:
        rows = [dict(r) for r in db.execute('''SELECT lr.record_kind, lr.record_id, lo.id occurrence_id, lo.export_id,
                       lo.artifact_id, lo.event_id, lo.locator, lo.content_sha256, lo.structure_json
                       FROM logical_occurrences lo JOIN logical_records lr ON lr.id=lo.logical_record_id
                       WHERE lr.namespace=? AND lr.container_id=? AND lo.interpretation=?
                       ORDER BY lo.id''', (namespace, container, interpretation))]
        for row in rows:
            row['structure'] = json.loads(row.pop('structure_json'))
            row['export'] = label[row['export_id']]
        conversation_rows = [r for r in rows if r['record_kind'] == 'conversation']
        present_exports = []
        for r in conversation_rows:
            if r['export_id'] not in present_exports:
                present_exports.append(r['export_id'])
        titles = _group(conversation_rows, lambda r: r['structure'].get('title'), lambda r: r['export'])
        currents = _group(conversation_rows, lambda r: r['structure'].get('current_node'), lambda r: r['export'])
        node_sets = _group(conversation_rows, lambda r: r['structure'].get('node_ids_sha256'), lambda r: r['export'])
        conversation_changes = []
        if len(titles) > 1:
            conversation_changes.append('title_changed')
        if len(currents) > 1:
            conversation_changes.append('current_node_changed')
        if len(node_sets) > 1:
            conversation_changes.append('node_set_changed')
        if any(e not in present_exports for e in parsed_exports):
            conversation_changes.append('not_observed_in_some_exports')
        by_record: OrderedDict = OrderedDict()
        for row in rows:
            if row['record_kind'] != 'conversation':
                by_record.setdefault((row['record_kind'], row['record_id']), []).append(row)
        record_entries = []
        change_counts = defaultdict(int)
        for (kind, record_id), occurrences in by_record.items():
            seen_exports = []
            per_export = defaultdict(int)
            for o in occurrences:
                per_export[o['export_id']] += 1
                if o['export_id'] not in seen_exports:
                    seen_exports.append(o['export_id'])
            absent = []
            for export_id in parsed_exports:
                if export_id in per_export:
                    continue
                absent.append({'export': label[export_id],
                               'reason': 'record_not_observed_in_conversation' if export_id in present_exports else 'conversation_not_observed',
                               'export_coverage': export_coverage[export_id]})
            content = _group(occurrences, lambda o: o['content_sha256'], lambda o: o['export'])
            structure = _group(occurrences, lambda o: (o['structure'].get('parent'), tuple(sorted(o['structure'].get('children') or []))),
                               lambda o: o['export'])
            on_path: OrderedDict = OrderedDict()
            for o in occurrences:
                on_path.setdefault(o['export'], []).append(o['structure'].get('on_current_path'))
            known = {v for values in on_path.values() for v in values if v is not None}
            changes = []
            if len(content) > 1:
                changes.append('content_variants')
            if len(structure) > 1:
                changes.append('structure_variants')
            if len(known) > 1:
                changes.append('current_path_changed')
            if any(a['reason'] == 'record_not_observed_in_conversation' for a in absent):
                changes.append('not_observed_in_some_exports')
            if any(count > 1 for count in per_export.values()):
                changes.append('multiple_occurrences_in_export')
            for change in changes:
                change_counts[change] += 1
            if changes_only and not changes:
                continue
            record_entries.append({
                'record_kind': kind, 'record_id': record_id,
                'present_in': [label[e] for e in seen_exports],
                'not_observed_in': absent,
                'content_variants': [{'content_sha256': key, **value} for key, value in content.items()],
                'structure_variants': [{'parent': key[0], 'children': list(key[1]), **value} for key, value in structure.items()],
                'on_current_path': {export: values[0] if len(values) == 1 else values for export, values in on_path.items()},
                'occurrences': [{'export': o['export'], 'file_name': file_of.get((o['export_id'], o['artifact_id']), {}).get('file_name'),
                                 'shard_index': file_of.get((o['export_id'], o['artifact_id']), {}).get('shard_index'),
                                 'artifact_id': o['artifact_id'], 'event_id': o['event_id'], 'locator': o['locator']}
                                for o in occurrences],
                'changes': changes,
            })
        for change in conversation_changes:
            summary[f'conversations_{change}'] += 1
        for change, count in change_counts.items():
            summary[f'records_{change}'] += count
        summary['records'] += len(by_record)
        if changes_only and not conversation_changes and not record_entries:
            continue
        conversations.append({
            'conversation_id': container,
            'present_in': [label[e] for e in present_exports],
            'not_observed_in': [{'export': label[e], 'export_coverage': export_coverage[e]} for e in parsed_exports if e not in present_exports],
            'titles': [{'title': key, 'exports': value['exports']} for key, value in titles.items()],
            'current_node': [{'node': key, 'exports': value['exports']} for key, value in currents.items()],
            'occurrences': [{'export': r['export'], 'file_name': file_of.get((r['export_id'], r['artifact_id']), {}).get('file_name'),
                             'shard_index': file_of.get((r['export_id'], r['artifact_id']), {}).get('shard_index'),
                             'artifact_id': r['artifact_id'], 'event_id': r['event_id'], 'locator': r['locator']}
                            for r in conversation_rows],
            'changes': conversation_changes,
            'record_change_counts': dict(sorted(change_counts.items())),
            'records': record_entries,
        })
    return {
        'interpretation': interpretation,
        'exports': [{'label': label[e], 'coverage': export_coverage[e]} for e in parsed_exports],
        'not_parsed_under_interpretation': [e['label'] for e in exports if e['id'] not in export_coverage],
        'conversations_total': total, 'offset': offset, 'limit': limit,
        'has_more': offset + len(containers) < total,
        'summary': dict(sorted(summary.items())),
        'conversations': conversations,
    }


def dedup_report(*, namespace: str | None = None, conversation_id: str | None = None, interpretation: str | None = None,
                 changes_only: bool = False, limit: int = 100, offset: int = 0) -> dict:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f'limit must be between 1 and {MAX_LIMIT}')
    if offset < 0:
        raise ValueError('offset must be >= 0')
    with session() as db:
        if namespace is None:
            namespaces = [r[0] for r in db.execute('SELECT DISTINCT namespace FROM record_exports ORDER BY namespace')]
        else:
            namespaces = [namespace]
        out = []
        for ns in namespaces:
            exports = [dict(r) for r in db.execute('SELECT * FROM record_exports WHERE namespace=? ORDER BY id', (ns,))]
            export_entries = []
            for e in exports:
                export_entries.append({
                    'label': e['label'], 'label_origin': e['label_origin'], 'collection_id': e['collection_id'],
                    'case_id': e['case_id'],
                    'files': [dict(r) for r in db.execute('''SELECT file_name, file_role, shard_index, artifact_id
                                                             FROM record_export_files WHERE export_id=? ORDER BY id''', (e['id'],))],
                })
            interpretations = [r[0] for r in db.execute('''SELECT DISTINCT p.interpretation FROM record_export_parses p
                    JOIN record_export_files f ON f.id=p.export_file_id JOIN record_exports e ON e.id=f.export_id
                    WHERE e.namespace=? ORDER BY p.interpretation''', (ns,))]
            if interpretation is not None:
                interpretations = [i for i in interpretations if i == interpretation]
            out.append({
                'namespace': ns, 'exports': export_entries,
                'interpretations': [_interpretation_report(db, ns, exports, i, conversation_id=conversation_id,
                                                           changes_only=changes_only, limit=limit, offset=offset)
                                    for i in interpretations],
            })
    return {'schema': REPORT_SCHEMA, 'absence_semantics': ABSENCE_SEMANTICS,
            'export_order': 'import order of record_exports, not the date an export was produced',
            'changes_only': changes_only, 'namespaces': out}

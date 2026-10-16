"""AI chat exports and the cross-export logical dedup report (area B1).

Absence of a record in an export means it was not observed there under that
interpretation; it never means deletion. The dedup report is read through its
public function, which opens its own read connection (``snapshot`` says so).
"""
from __future__ import annotations

from ..blocks import kv, notice, table
from . import Section, load_upstream

VERSION = 'chat_records.v1'
UPSTREAM = (('server.app.records.report', 'dedup_report'),)


def provide(ctx) -> dict:
    section = Section(ctx, 'chat_records', __name__, VERSION, 'section_chat_records')
    db, max_rows = ctx.db, ctx.params['max_rows']
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='record_exports'").fetchone():
        return section.unavailable('record_exports table not present (chat importers not migrated)')
    exports = db.execute('SELECT * FROM record_exports ORDER BY id').fetchall()
    section.input_rows('record_exports', exports)
    files = db.execute('SELECT * FROM record_export_files ORDER BY id').fetchall()
    section.input_rows('record_export_files', files)
    parses = db.execute('SELECT * FROM record_export_parses ORDER BY id').fetchall()
    section.input_rows('record_export_parses', parses)
    if not exports:
        section.status('empty', 'no chat exports imported')
    file_counts: dict[int, int] = {}
    for row in files:
        file_counts[row['export_id']] = file_counts.get(row['export_id'], 0) + 1
    coverage: dict[int, set] = {}
    file_export = {row['id']: row['export_id'] for row in files}
    for parse in parses:
        coverage.setdefault(file_export.get(parse['export_file_id']), set()).add(parse['coverage'])
    section.block(table('chat_records.exports', [('id', 'id'), ('namespace', 'text'), ('export_label', 'text'),
                                                 ('label_origin', 'text'), ('collection_id', 'id'), ('files', 'number'),
                                                 ('status', 'text'), ('created_at', 'time')],
                        [{'id': e['id'], 'namespace': e['namespace'], 'export_label': e['label'],
                          'label_origin': e['label_origin'], 'collection_id': e['collection_id'],
                          'files': file_counts.get(e['id'], 0),
                          'status': ','.join(sorted(str(c) for c in coverage.get(e['id'], ()))) or None,
                          'created_at': e['created_at']} for e in exports[:max_rows]],
                        ctx.lang, title_key='exports', total_rows=len(exports), truncated=len(exports) > max_rows))
    function, where = load_upstream(UPSTREAM)
    if function is None:
        section.status('partial', where)
        section.block(notice('chat_records.dedup_unavailable', 'unavailable', where))
        return section.result()
    section.data['snapshot'] = 'separate_read_transaction'
    report = function(changes_only=True, limit=min(max_rows, 10_000))
    section.block(kv('chat_records.semantics', [('absence_semantics', report.get('absence_semantics')),
                                                ('rule', report.get('schema'))], ctx.lang))
    summary_rows, conversation_rows = [], []
    truncated = False
    for namespace in report.get('namespaces', []):
        for interpretation in namespace.get('interpretations', []):
            for metric, value in sorted((interpretation.get('summary') or {}).items()):
                summary_rows.append({'namespace': namespace['namespace'],
                                     'interpretation': interpretation.get('interpretation'),
                                     'metric': metric, 'count': value})
            truncated = truncated or bool(interpretation.get('has_more'))
            for conversation in interpretation.get('conversations', []):
                conversation_rows.append({'namespace': namespace['namespace'],
                                          'conversation_id': conversation.get('conversation_id'),
                                          'changes': ','.join(conversation.get('changes') or []) or None,
                                          'exports': len(conversation.get('present_in') or []),
                                          'count': sum((conversation.get('record_change_counts') or {}).values())})
    section.block(table('chat_records.summary', [('namespace', 'text'), ('interpretation', 'text'),
                                                 ('metric', 'text'), ('count', 'number')], summary_rows, ctx.lang))
    section.block(table('chat_records.changes', [('namespace', 'text'), ('conversation_id', 'text'),
                                                 ('changes', 'text'), ('exports', 'number'), ('count', 'number')],
                        conversation_rows[:max_rows], ctx.lang, title_key='changes',
                        total_rows=len(conversation_rows), truncated=truncated or len(conversation_rows) > max_rows))
    if truncated:
        section.status('partial', 'dedup report page limit reached')
        section.exclude('dedup_conversations_not_listed', True)
    return section.result()

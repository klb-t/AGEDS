"""ChatGPT / Claude.ai export importers and logical dedup across exports.

All inputs come from ``server/tests/corpus/chat_exports.py`` (synthetic).
"""
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.config import Settings
from server.app.importers import chat_common, chatgpt, claude_ai
from server.app.records.report import dedup_report
from server.app.timeinfo import format_utc
from server.tests.corpus import chat_exports

EXPORTS = ('e1', 'e2', 'e3')
COUNTED = ('artifacts', 'events', 'event_times', 'search_fts', 'logical_records', 'logical_occurrences',
           'record_exports', 'record_export_files', 'record_export_parses', 'chat_attachment_refs')


@pytest.fixture(scope='module')
def corpus(tmp_path_factory):
    return chat_exports.build(tmp_path_factory.mktemp('chat_corpus'))


@pytest.fixture
def store(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, db_path=tmp_path / 'evidence.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', settings)
    monkeypatch.setattr(evidence, 'settings', settings)
    dbmod.init_db()
    return tmp_path


def rows(sql, *params):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


def counts():
    return {table: rows(f'SELECT COUNT(*) n FROM {table}')[0]['n'] for table in COUNTED}


def message_events(node_id=None, *, thread=None, event_type='chat_message'):
    sql = 'SELECT * FROM events WHERE event_type=?'
    params = [event_type]
    if node_id is not None:
        sql += " AND json_extract(metadata_json,'$.chat.node_id')=?"
        params.append(node_id)
    if thread is not None:
        sql += ' AND thread_id=?'
        params.append(thread)
    out = rows(sql + ' ORDER BY id', *params)
    for row in out:
        row['meta'] = json.loads(row['metadata_json'])
    return out


def import_three(corpus):
    return [chatgpt.import_chatgpt_export([corpus['chatgpt_exports'][label]], export_label=label) for label in EXPORTS]


def the_interpretation(report, namespace='chatgpt'):
    [ns] = [n for n in report['namespaces'] if n['namespace'] == namespace]
    [interpretation] = ns['interpretations']
    return interpretation


def test_tree_is_preserved_including_non_current_branches(corpus, store):
    result = chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], export_label='e1')
    assert result['coverage'] == 'complete'
    expected = corpus['expected_c']
    events = {e['meta']['chat']['node_id']: e for e in message_events(thread=corpus['conversation_id'])}
    assert set(events) == {'sys', 'u1', 't1', 'a1', 'a1x', 'u2', 'a2'}
    off_path = events['a1x']['meta']['chat']['tree']
    assert off_path['on_current_path'] is False and off_path['parent'] == 't1' and off_path['depth'] == 4
    assert events['a2']['meta']['chat']['tree']['is_current_node'] is True
    assert events['t1']['meta']['chat']['tree']['children'] == ['a1', 'a1x']
    # The synthetic root has no message: a structural logical record, not an event.
    structural = rows("SELECT lr.record_id, lo.event_id, lo.structure_json FROM logical_occurrences lo "
                      "JOIN logical_records lr ON lr.id=lo.logical_record_id WHERE lr.record_kind='node'")
    assert [r['record_id'] for r in structural] == expected['structural'] and structural[0]['event_id'] is None
    assert json.loads(structural[0]['structure_json'])['children'] == ['sys']
    # Hidden/system nodes are kept and flagged.
    sys_chat = events['sys']['meta']['chat']
    assert sys_chat['hidden'] is True and 'system_role' in sys_chat['hidden_reasons']
    assert 'is_visually_hidden_from_conversation' in sys_chat['hidden_reasons']
    # Thoughts are their own kind and are not merged into the answer.
    assert events['t1']['meta']['chat']['content_kind'] == 'reasoning'
    assert 'Internal reasoning text' in events['t1']['body'] and 'Considering scope' in events['t1']['body']
    assert 'Internal reasoning' not in events['a1']['body']
    assert events['a1']['meta']['chat']['content_kind'] == 'text'
    # Per-message model, role as given, chatbot lane identity.
    assert events['a2']['meta']['chat']['model'] == 'synthetic-model-2'
    assert events['a1']['meta']['chat']['model'] == 'synthetic-model-1'
    assert events['u1']['sender'] == 'user' and events['u1']['direction'] == 'outgoing'
    assert events['a1']['contact_label'] == 'chatgpt' and events['a1']['meta']['chat']['chatbot'] == 'chatgpt'
    assert events['a1']['subject'] == 'Planning notes'
    conversation = message_events(event_type='chat_conversation')[0]
    assert conversation['thread_id'] == corpus['conversation_id']
    assert conversation['meta']['chat']['current_node'] == 'a2' and conversation['meta']['chat']['node_count'] == 8
    assert 'mapping' not in conversation['meta']['raw']['conversation_fields']['value']


def test_raw_node_is_recoverable_from_stored_artifact_and_locator(corpus, store):
    chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], export_label='e1')
    [event] = message_events('a1x')
    locator = event['meta']['locator']
    stored = rows('SELECT stored_path FROM artifacts WHERE id=?', event['artifact_id'])[0]['stored_path']
    raw = chat_common.read_raw(stored, locator['element_byte_range'], locator['element_pointer'])
    source = json.loads((Path(corpus['chatgpt_exports']['e1']) / 'conversations.json').read_text())
    assert raw == source[0]['mapping']['a1x']
    assert locator['json_pointer'] == '/0/mapping/a1x'


def test_three_exports_dedup_report_is_exact(corpus, store):
    import_three(corpus)
    expected = corpus['expected_c']
    report = dedup_report(namespace='chatgpt')
    interpretation = the_interpretation(report)
    assert interpretation['exports'] == [{'label': e, 'coverage': 'complete'} for e in EXPORTS]
    [conversation] = interpretation['conversations']
    assert conversation['conversation_id'] == corpus['conversation_id']
    assert conversation['present_in'] == list(EXPORTS) and conversation['not_observed_in'] == []
    records = {r['record_id']: r for r in conversation['records']}
    assert set(records) == set(expected['present_in'])
    for record_id, present in expected['present_in'].items():
        record = records[record_id]
        assert record['present_in'] == present, record_id
        assert [a['export'] for a in record['not_observed_in']] == [e for e in EXPORTS if e not in present], record_id
        assert all(a['reason'] == 'record_not_observed_in_conversation' and a['export_coverage'] == 'complete'
                   for a in record['not_observed_in'])
    content = {rid: [v['exports'] for v in r['content_variants']] for rid, r in records.items() if len(r['content_variants']) > 1}
    assert content == expected['content_variants']
    structure = {rid: [v['exports'] for v in r['structure_variants']] for rid, r in records.items() if len(r['structure_variants']) > 1}
    assert structure == expected['structure_variants']
    assert sorted(rid for rid, r in records.items() if 'current_path_changed' in r['changes']) == expected['current_path_changed']
    assert records['a3']['on_current_path'] == {'e2': True, 'e3': False}
    assert [[t['title'], t['exports']] for t in conversation['titles']] == expected['titles']
    assert [[c['node'], c['exports']] for c in conversation['current_node']] == expected['current_node']
    assert set(conversation['changes']) == {'title_changed', 'current_node_changed', 'node_set_changed'}
    # Each content variant points at the events that carry it.
    old, new = records['u2']['content_variants']
    bodies = {e['id']: e['body'] for e in message_events('u2')}
    assert {bodies[i] for i in old['event_ids']} == {'Second question?'}
    assert {bodies[i] for i in new['event_ids']} == {'Second question, edited.'}
    assert 'deletion' in report['absence_semantics'] and 'not_observed' in report['absence_semantics']
    changed = the_interpretation(dedup_report(namespace='chatgpt', changes_only=True))
    assert {r['record_id'] for r in changed['conversations'][0]['records']} == {'t1', 'a1x', 'u2', 'a2', 'u3', 'a3', 'a3b'}
    assert changed['summary']['records'] == 11


def test_conversation_absent_from_an_export_is_reported_as_not_observed(corpus, store):
    import_three(corpus)
    chatgpt.import_chatgpt_export([corpus['parallel']['chatgpt_path']], export_label='e4-other')
    interpretation = the_interpretation(dedup_report(namespace='chatgpt', conversation_id=corpus['conversation_id']))
    [conversation] = interpretation['conversations']
    assert conversation['not_observed_in'] == [{'export': 'e4-other', 'export_coverage': 'complete'}]
    record = next(r for r in conversation['records'] if r['record_id'] == 'u1')
    assert record['not_observed_in'] == [{'export': 'e4-other', 'reason': 'conversation_not_observed', 'export_coverage': 'complete'}]
    assert 'not_observed_in_some_exports' not in record['changes']


def test_reimport_creates_nothing_new_and_audits_reobservation(corpus, store):
    first = chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], export_label='e1')
    before = counts()
    observations = rows('SELECT COUNT(*) n FROM source_observations')[0]['n']
    second = chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], export_label='e1')
    assert second == first
    assert counts() == before
    # The repeated acquisition itself is a new observation of the same bytes.
    assert rows('SELECT COUNT(*) n FROM source_observations')[0]['n'] == observations + 1
    reobserved = rows("SELECT COUNT(*) n FROM audit_log WHERE action='event_reobserved'")[0]['n']
    assert reobserved == before['events']
    # Default label derives from content hashes, so an unlabelled re-import is idempotent too.
    third = chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e2']])
    fourth = chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e2']])
    assert third['export']['label'].startswith('derived:') and third == fourth


def test_reimport_with_attachments_is_idempotent(corpus, store):
    first = chatgpt.import_chatgpt_export([corpus['edge']['path']], export_label='edge')
    before = counts()
    assert chatgpt.import_chatgpt_export([corpus['edge']['path']], export_label='edge') == first
    assert counts() == before


def test_shards_are_one_export_with_per_file_provenance(corpus, store):
    result = chatgpt.import_chatgpt_export([corpus['sharded']['path']], export_label='sharded')
    files = {f['file_name']: f for f in result['files']}
    assert set(files) == {'conversations-000.json', 'conversations-001.json'}
    assert files['conversations-000.json']['shard_index'] == 0 and files['conversations-001.json']['shard_index'] == 1
    assert {f['file_role'] for f in files.values()} == {'conversations_shard'}
    assert 'user.json' not in {r['original_name'] for r in rows('SELECT original_name FROM artifacts')}
    interpretation = the_interpretation(dedup_report(namespace='chatgpt'))
    shards = {c['conversation_id']: [(o['file_name'], o['shard_index']) for o in c['occurrences']] for c in interpretation['conversations']}
    assert shards == {cid: [(f'conversations-{i:03d}.json', i)] for cid, i in corpus['sharded']['shards'].items()}
    assert rows('SELECT COUNT(*) n FROM record_exports')[0]['n'] == 1


def test_same_conversation_in_shard_export_matches_logical_records(corpus, store):
    chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e2']], export_label='e2')
    chatgpt.import_chatgpt_export([corpus['sharded']['path']], export_label='sharded')
    interpretation = the_interpretation(dedup_report(namespace='chatgpt', conversation_id=corpus['conversation_id']))
    [conversation] = interpretation['conversations']
    assert conversation['changes'] == []
    assert all(r['changes'] == [] and r['present_in'] == ['e2', 'sharded'] for r in conversation['records'])


def test_mapping_file_change_alters_interpretation_without_code_change(corpus, store, tmp_path):
    alternative = json.loads(chatgpt.DEFAULT_MAPPING.read_text())
    alternative['mapping_version'] = '1-alt'
    alternative['content_types']['thoughts']['kind'] = 'text'
    alternative['message']['model'] = ['/metadata/default_model_slug_not_present']
    alt_path = tmp_path / 'chatgpt_mapping.alt.json'
    alt_path.write_text(json.dumps(alternative), encoding='utf-8')
    default = chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], export_label='e1')
    events_before = counts()['events']
    changed = chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], export_label='e1', mapping_path=alt_path)
    assert default['interpretation'] != changed['interpretation']
    assert '/map1-alt:' in changed['interpretation']
    t1 = message_events('t1')
    assert [e['meta']['chat']['content_kind'] for e in t1] == ['reasoning', 'text']
    assert t1[0]['external_id'] != t1[1]['external_id']
    assert [e['meta']['chat']['model'] for e in message_events('a1')] == ['synthetic-model-1', None]
    assert counts()['events'] == 2 * events_before
    report = dedup_report(namespace='chatgpt')
    assert len(report['namespaces'][0]['interpretations']) == 2
    with pytest.raises(ValueError):
        bad = tmp_path / 'bad.json'
        bad.write_text(json.dumps({**alternative, 'namespace': 'claude'}), encoding='utf-8')
        chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], mapping_path=bad)


def test_malformed_file_is_partial_with_visible_issues(corpus, store):
    result = chatgpt.import_chatgpt_export([corpus['malformed']['path']], export_label='bad')
    [file] = result['files']
    assert result['coverage'] == 'partial' and file['coverage'] == 'partial'
    assert file['issue_counts']['non_object_element'] == corpus['malformed']['non_object_elements']
    assert file['issue_counts']['truncated_json'] == 1 and file['issue_counts']['invalid_utf8_replaced'] == 1
    recorded = sorted(e['thread_id'] for e in message_events(event_type='chat_conversation'))
    assert recorded == corpus['malformed']['recorded']
    [utf8] = message_events(event_type='chat_conversation', thread='malformed-utf8')
    assert '�' in utf8['subject']
    parse = rows('SELECT coverage, issues_json FROM record_export_parses')[0]
    assert parse['coverage'] == 'partial' and json.loads(parse['issues_json'])['issue_counts']['truncated_json'] == 1


def test_edge_nodes_cycles_and_unknown_types_do_not_crash(corpus, store):
    result = chatgpt.import_chatgpt_export([corpus['edge']['path']], export_label='edge')
    codes = result['files'][0]['issue_counts']
    for code in ('cycle_in_tree', 'cycle_on_current_path', 'missing_parent', 'missing_child', 'node_not_object',
                 'content_null', 'role_not_string', 'unknown_content_type', 'duplicate_node_id',
                 'conversation_repeated_in_file', 'in_cycle', 'descends_from_cycle'):
        assert codes.get(code), code
    tree = {e['meta']['chat']['node_id']: e['meta']['chat']['tree'] for e in message_events(thread='edge-cycle')}
    assert tree['x']['in_cycle'] and tree['y']['in_cycle'] and tree['x']['depth'] is None
    assert tree['z']['depth'] is None and 'descends_from_cycle' in message_events('z', thread='edge-cycle')[0]['meta']['issues']
    [k8] = message_events('k8')
    assert k8['sender'] is None and k8['meta']['chat']['content_kind'] == 'empty'
    assert {'role_not_string', 'content_null'} <= set(k8['meta']['issues'])
    [k7] = message_events('k7')
    assert k7['meta']['chat']['content_kind'] == 'unknown' and k7['body'] is None
    assert k7['meta']['raw']['content']['value']['payload'] == {'a': [1, 2, 3]}
    [orphan] = message_events('orphan')
    assert 'missing_parent' in orphan['meta']['issues'] and orphan['meta']['chat']['tree']['depth'] == 0
    kinds = {e['meta']['chat']['node_id']: e['meta']['chat']['content_kind'] for e in message_events(thread='edge-kinds')}
    assert kinds['k1'] == 'context' and kinds['k3'] == 'tool_call' and kinds['k4'] == 'tool_output'
    assert kinds['k5'] == 'reasoning' and kinds['k2'] == 'multimodal'
    [k2] = message_events('k2')
    assert k2['body'] == 'Look at this picture\nspoken words'
    # Duplicate JSON keys: both copies kept; the first in file order is the plain locator.
    dup = message_events('dupnode')
    assert [e['body'] for e in dup] == ['first copy in file order', 'second copy in file order']
    assert [e['meta']['locator']['duplicate_index'] for e in dup] == [0, 1]
    stored = rows('SELECT stored_path FROM artifacts WHERE id=?', dup[1]['artifact_id'])[0]['stored_path']
    loc = dup[1]['meta']['locator']
    second = chat_common.read_raw(stored, loc['element_byte_range'], loc['element_pointer'] + '#dup1')
    assert second['message']['content']['parts'] == ['second copy in file order']
    # The repeated conversation in one file is a second occurrence in the same export.
    interpretation = the_interpretation(dedup_report(namespace='chatgpt', conversation_id='edge-cycle'))
    assert {'multiple_occurrences_in_export'} <= set(interpretation['conversations'][0]['records'][0]['changes'])


def test_unicode_long_messages_and_lone_surrogates(corpus, store):
    chatgpt.import_chatgpt_export([corpus['edge']['path']], export_label='edge')
    by_node = {e['meta']['chat']['node_id']: e for e in message_events(thread='edge-unicode')}
    assert by_node['u1']['body'] == 'Emoji \U0001F600 math \U0001D518 family \U0001F468‍\U0001F469‍\U0001F467 é'
    assert by_node['u2']['body'] == '‏מה שלומך؟ مرحبا بالعالم‏'
    assert by_node['u3']['body'] == 'Lone � surrogate'
    assert 'lone_surrogate_replaced_in_body' in by_node['u3']['meta']['issues']
    assert by_node['u3']['meta']['body']['sha256'] == hashlib.sha256('Lone \ud800 surrogate'.encode('utf-8', 'surrogatepass')).hexdigest()
    long = by_node['u4']
    assert len(long['body']) == chat_common.BODY_MAX_CHARS
    assert long['meta']['body']['truncated'] is True
    assert long['meta']['body']['chars'] == len('lorem ') * corpus['edge']['long_words']
    [conversation] = message_events(event_type='chat_conversation', thread='edge-unicode')
    assert conversation['subject'] == 'Unicode \U0001F600 ‫مرحبا‬'


def test_limits_surface_as_partial_coverage(corpus, store):
    limited = chat_common.ChatImportLimits(max_conversation_bytes=4000, max_nodes_per_conversation=3)
    result = chatgpt.import_chatgpt_export([corpus['edge']['path']], export_label='edge-limited', limits=limited)
    [file] = result['files']
    assert file['coverage'] == 'partial'
    assert file['issue_counts']['conversation_bytes_limit_exceeded'] >= 1
    assert file['issue_counts']['node_limit_reached'] >= 1
    exceeded = [i for i in file['issues'] if i['code'] == 'conversation_bytes_limit_exceeded']
    assert all(len(i['byte_range']) == 2 and i['limit'] == 4000 for i in exceeded)
    with pytest.raises(ValueError):
        chat_common.ChatImportLimits(max_conversation_bytes=0)


def test_streaming_splitter_keeps_json_array_indices(store):
    issues = chat_common.IssueLog(100)
    data = b'\xef\xbb\xbf [ {"a": "x]}\\"{"}, 1, "s", [2], {"b": [1, {"c": null}]} , null ]'
    elements = list(chat_common.iter_array_elements(data, max_element_bytes=1000, max_elements=100, issues=issues, file_label='t'))
    assert [(e.index, e.status) for e in elements] == [(0, 'ok'), (3, 'non_object_element'), (4, 'ok')]
    assert chat_common.loads(elements[0].data) == {'a': 'x]}"{'}
    assert issues.counts['non_object_element'] == 4 and issues.counts['utf8_bom_skipped'] == 1
    capped = chat_common.IssueLog(10)
    assert len(list(chat_common.iter_array_elements(b'[{}, {}, {}]', max_element_bytes=10, max_elements=2,
                                                    issues=capped, file_label='t'))) == 2
    assert capped.counts['conversation_limit_reached'] == 1
    for bad, code in ((b'{"not": "array"}', 'top_level_not_array'), (b'   ', 'empty_file'),
                      (b'[{"a": 1}}', 'malformed_json_structure'), (b'[{"a": 1}] trailing', 'trailing_data_after_array'),
                      (b'\xff\xfe[\x00', 'unsupported_encoding')):
        log = chat_common.IssueLog(10)
        list(chat_common.iter_array_elements(bad, max_element_bytes=100, max_elements=10, issues=log, file_label='t'))
        assert log.counts[code] == 1, code


def test_attachment_references_resolve_only_against_export_files(corpus, store):
    chatgpt.import_chatgpt_export([corpus['edge']['path']], export_label='edge')
    refs = {r['file_id']: r for r in rows('SELECT * FROM chat_attachment_refs')}
    for file_id, resolution in corpus['edge']['attachments'].items():
        assert refs[file_id]['resolution'] == resolution, file_id
    attach = rows('SELECT original_name, is_raw FROM artifacts WHERE id=?', refs['file-ATTACH1']['resolved_artifact_id'])[0]
    assert attach['original_name'] == 'file-ATTACH1-notes.txt' and attach['is_raw'] == 1
    assert sorted(c['name'] for c in json.loads(refs['file-IMG1']['candidates_json'])) == ['file-IMG1-a.png', 'file-IMG1.dat']
    assert refs['file-IMG1']['resolved_artifact_id'] is None
    assert refs['file_00000000aaaa']['ref'] == 'sediment://file_00000000aaaa'
    assert refs['file-MISSING9']['resolved_artifact_id'] is None and refs['file-MISSING9']['name'] == 'gone.pdf'
    names = {r['original_name'] for r in rows('SELECT original_name FROM artifacts')}
    assert 'file-IMG10-other.png' not in names
    assert rows("SELECT COUNT(*) n FROM record_export_files WHERE file_role='attachment'")[0]['n'] == 4


@pytest.fixture
def foreign_zone():
    previous = os.environ.get('TZ')
    os.environ['TZ'] = 'Pacific/Kiritimati'
    time.tzset()
    yield
    if previous is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = previous
    time.tzset()


def candidates(event_id):
    return {(c['kind'], c['source'].rsplit('.', 1)[-1]): c for c in rows('SELECT * FROM event_times WHERE event_id=?', event_id)}


def test_time_zones_come_only_from_the_source(corpus, store, foreign_zone):
    chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], export_label='e1')
    chatgpt.import_chatgpt_export([corpus['edge']['path']], export_label='edge')
    claude_ai.import_claude_export([corpus['claude']['path']], export_label='k1')
    statuses = {r['tz_status'] for r in rows("SELECT tz_status FROM event_times WHERE source LIKE 'chatgpt:%'")}
    assert statuses == {'utc_epoch', 'unknown'}
    [t1] = message_events('t1')
    created = candidates(t1['id'])[('created', 'create_time')]
    assert created['utc'] == format_utc(datetime.fromtimestamp(chat_exports.T0 + 12, timezone.utc))
    assert created['source'] == f"chatgpt:conversations.json#{corpus['conversation_id']}/t1.create_time"
    assert t1['ts_start'] == created['utc']
    [n1] = message_events('n1')
    assert candidates(n1['id']) == {} and 'create_time_missing_or_null' in n1['meta']['issues']
    [n2] = message_events('n2')
    zero = candidates(n2['id'])
    assert zero[('created', 'create_time')]['utc'] is None and zero[('created', 'create_time')]['raw'] == '0'
    assert json.loads(zero[('created', 'create_time')]['metadata_json'])['parse_status'] == 'zero_placeholder_not_projected'
    assert json.loads(zero[('updated', 'update_time')]['metadata_json'])['parse_status'] == 'unparsed'
    assert n2['ts_start'] is None
    [n3] = message_events('n3')
    assert candidates(n3['id'])[('created', 'create_time')]['raw'] == '1e400'
    by_id = {e['meta']['chat']['message_id']: e for e in message_events(thread=corpus['claude']['conversation_id'])}
    m3 = candidates(by_id['k-m3']['id'])[('created', 'created_at')]
    assert (m3['tz_status'], m3['tz'], m3['utc']) == ('explicit_offset', '+02:00', '2025-03-01T08:07:00Z')
    m4 = candidates(by_id['k-m4']['id'])[('created', 'created_at')]
    assert (m4['tz_status'], m4['utc'], m4['local']) == ('unknown', None, '2025-03-01T10:20:00')
    assert by_id['k-m4']['ts_start'] is None and 'create_time_without_offset' in by_id['k-m4']['meta']['issues']


def test_claude_blocks_are_separate_kinds_and_branches_are_kept(corpus, store):
    result = claude_ai.import_claude_export([corpus['claude']['path']], export_label='k1')
    assert result['coverage'] == 'complete' and result['files'][0]['file_name'] == 'conversations.json'
    k_id = corpus['claude']['conversation_id']
    messages = {e['meta']['chat']['message_id']: e for e in message_events(thread=k_id)}
    m2 = messages['k-m2']
    assert m2['body'] == 'Answer A\n\nMore answer' and m2['meta']['body']['text_field_matches_visible_blocks'] is True
    assert 'Private chain' not in m2['body'] and m2['sender'] == 'assistant' and m2['direction'] == 'incoming'
    parts = {}
    for part in message_events(thread=k_id, event_type='chat_message_part'):
        parts.setdefault(part['meta']['chat']['message_id'], []).append(part)
    kinds = [(p['meta']['chat']['content_kind'], p['body']) for p in parts['k-m2']]
    assert kinds[0] == ('reasoning', 'Private chain of thought, synthetic.\n\nPlanning the search')
    assert kinds[1][0] == 'tool_call' and 'web_search' in kinds[1][1] and '{"query":"example.org files"}' in kinds[1][1]
    assert kinds[2] == ('tool_output', 'result from example.org')
    reasoning = parts['k-m2'][0]
    assert reasoning['ts_start'] == '2025-03-01T10:06:00Z' and reasoning['ts_end'] == '2025-03-01T10:06:05Z'
    [voice] = parts['k-m4']
    assert voice['meta']['chat']['content_kind'] == 'unknown'
    assert voice['meta']['raw']['block_fields']['value'] == {'type': 'voice_note', 'data': {'seconds': 3}}
    [extract] = parts['k-m1']
    assert extract['meta']['chat']['content_kind'] == 'attachment_extract' and extract['body'] == 'synthetic notes'
    refs = {r['ref']: r['resolution'] for r in rows('SELECT ref, resolution FROM chat_attachment_refs')}
    assert refs == {'notes.txt': 'unresolved', 'diagram.png': 'unresolved'}
    tree = {k: v['meta']['chat']['tree'] for k, v in messages.items()}
    assert tree['k-m1']['children'] == ['k-m2', 'k-m3'] and tree['k-m1']['parent'] is None
    assert tree['k-m1']['parent_is_root_sentinel'] is True
    assert tree['k-m3']['parent'] == 'k-m1' and tree['k-m4']['depth'] == 2
    assert all(t['on_current_path'] is None for t in tree.values())
    old = {e['meta']['chat']['message_id']: e for e in message_events(thread=corpus['claude']['old_format_id'])}
    assert old['o-m2']['body'] == 'Reply without content blocks' and old['o-m2']['meta']['body']['source'] == 'text_field'
    assert old['o-m2']['meta']['chat']['tree']['parent_declared'] is False
    assert old['o-m2']['meta']['chat']['tree']['list_predecessor'] == 'o-m1'
    report = the_interpretation(dedup_report(namespace='claude'), 'claude')
    assert report['conversations_total'] == 2 and report['exports'] == [{'label': 'k1', 'coverage': 'complete'}]


def test_wrong_vendor_file_is_not_recognized(corpus, store):
    result = claude_ai.import_claude_export([corpus['chatgpt_exports']['e1']], export_label='wrong')
    assert result['coverage'] == 'partial'
    assert result['files'][0]['issue_counts'] == {'conversation_shape_not_recognized': 1}
    assert counts()['events'] == 0


def test_parallel_chatbot_conversations_overlap_in_separate_lanes(corpus, store):
    chatgpt.import_chatgpt_export([corpus['parallel']['chatgpt_path']], export_label='p')
    claude_ai.import_claude_export([corpus['parallel']['claude_path']], export_label='k')
    spans = {}
    for conversation in message_events(event_type='chat_conversation'):
        if conversation['thread_id'] not in (corpus['parallel']['chatgpt_id'], corpus['parallel']['claude_id']):
            continue
        times = {c['kind']: c['utc'] for c in rows('SELECT kind, utc FROM event_times WHERE event_id=?', conversation['id'])}
        spans[conversation['contact_label']] = (times['created'], times['updated'])
    assert set(spans) == {'chatgpt', 'claude'}
    assert max(s for s, _ in spans.values()) < min(e for _, e in spans.values())
    lanes = {r['contact_label'] for r in rows("SELECT DISTINCT contact_label FROM events WHERE event_type LIKE 'chat_%'")}
    assert lanes == {'chatgpt', 'claude'}


def test_originals_are_unchanged_and_nothing_is_written_next_to_them(corpus, store):
    root = Path(corpus['chatgpt_exports']['e1']).parents[1]

    def snapshot():
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}
    before = snapshot()
    import_three(corpus)
    for key in ('edge', 'malformed', 'sharded'):
        chatgpt.import_chatgpt_export([corpus[key]['path']], export_label=key)
    claude_ai.import_claude_export([corpus['claude']['path']], export_label='k1')
    assert snapshot() == before


def test_logical_tables_are_append_only(corpus, store):
    chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], export_label='e1')
    for table in ('logical_occurrences', 'logical_records', 'record_exports', 'record_export_files', 'record_export_parses'):
        with pytest.raises(sqlite3.DatabaseError):
            with dbmod.session() as db:
                db.execute(f'DELETE FROM {table}')


def test_export_label_is_bound_to_one_collection(corpus, store):
    with dbmod.session() as db:
        case_id = db.execute('SELECT id FROM cases').fetchone()[0]
        first = db.execute("INSERT INTO collections(case_id,label) VALUES(?, 'Synthetic A')", (case_id,)).lastrowid
        second = db.execute("INSERT INTO collections(case_id,label) VALUES(?, 'Synthetic B')", (case_id,)).lastrowid
    result = chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], export_label='e1', collection_id=first)
    assert result['export']['collection_id'] == first
    observations = rows('SELECT DISTINCT collection_id FROM source_observations')
    assert observations == [{'collection_id': first}]
    with pytest.raises(evidence.IntegrityError):
        chatgpt.import_chatgpt_export([corpus['chatgpt_exports']['e1']], export_label='e1', collection_id=second)
    assert rows('SELECT DISTINCT collection_id FROM source_observations') == [{'collection_id': first}]

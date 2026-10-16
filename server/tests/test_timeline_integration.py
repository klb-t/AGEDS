"""Timeline projection and correlations over real importer output (B1 chat, B2 communications).

Inputs come from the synthetic corpus parts ``chat_exports`` and ``communications``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.config import Settings
from server.tests.corpus import chat_exports, communications


@pytest.fixture
def store(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, db_path=tmp_path / 'evidence.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', settings)
    monkeypatch.setattr(evidence, 'settings', settings)
    dbmod.init_db()
    return tmp_path


def window(**kwargs):
    from server.app.timeline.projection import ensure_projection
    from server.app.timeline.query import make_filters, window as run_window
    from server.app.timeline.ruleset import load_rule
    rule = load_rule(kind='display_rule')
    filters = make_filters(**kwargs.pop('filters', {}))
    with dbmod.session() as db:
        return run_window(db, ensure_projection(db, rule, {}), rule, filters=filters, limit=1000, **kwargs)


def run(rule, params=None):
    from server.app.timeline.correlation import list_hypotheses, run_rule
    with dbmod.session() as db:
        result = run_rule(db, rule, params, actor='integration-test')
        return result, list_hypotheses(db, run_id=result['id'], limit=500)['hypotheses']


def test_chat_exports_lanes_hidden_parts_and_same_conversation(store, tmp_path):
    from server.app.importers import chatgpt, claude_ai
    corpus = chat_exports.build(tmp_path / 'corpus')
    for label in ('e1', 'e2', 'e3'):
        chatgpt.import_chatgpt_export([corpus['chatgpt_exports'][label]], export_label=label)
    chatgpt.import_chatgpt_export([corpus['parallel']['chatgpt_path']], export_label='parallel-gpt')
    claude_ai.import_claude_export([corpus['parallel']['claude_path']], export_label='parallel-claude')

    visible = window(candidates=False, group='chatbot')
    every = window(candidates=False, group='chatbot', filters={'include_hidden': True})
    assert len(every['items']) > len(visible['items'])
    hidden = [i for i in every['items'] if i['hidden']]
    assert hidden and all(i['time']['flags'].get('hidden_reason') == 'event_type:chat_message_part' for i in hidden)
    assert not any(i['event_type'] == 'chat_message_part' or i['hidden'] for i in visible['items'])
    with dbmod.session() as db:
        # The hidden system node of conversation C (no time in the export) is hidden and undated.
        flagged = db.execute('''SELECT ti.hidden, ti.sort_us, ti.flags_json FROM timeline_items ti JOIN events e ON e.id=ti.ref_id
                                WHERE ti.item_kind='event' AND json_extract(e.metadata_json,'$.chat.node_id')='sys' ''').fetchall()
    assert flagged and all(r['hidden'] == 1 and r['sort_us'] is None for r in flagged)
    assert all(json.loads(r['flags_json'])['hidden_reason'] == 'metadata_flag' for r in flagged)
    assert {lane['key'] for lane in visible['lanes']['lanes']} >= {'chatgpt', 'claude'}

    # Parallel conversations of two chatbots overlap in the declared window and sit in separate lanes.
    start, end = corpus['parallel']['overlap_utc']
    from server.app.timeline.timeutil import parse_bound
    overlap = window(candidates=False, group='chatbot', start_us=parse_bound(start), end_us=parse_bound(end))
    assert {i['chatbot'] for i in overlap['items']} == {'chatgpt', 'claude'}

    conversations = window(candidates=False, filters={'event_types': ['chat_conversation']})['items']
    assert any(c['time']['end_us'] is not None for c in conversations)  # created → updated bar

    result, hypotheses = run('same_conversation')
    conversation = [h for h in hypotheses if h['criteria']['mode'] == 'logical_records'
                    and h['criteria']['logical_record']['conversation_id'] == corpus['conversation_id']]
    assert len(conversation) == 3 and {h['candidate_count'] for h in conversation} == {3}
    by_labels = {(h['criteria']['exports']['left']['export_label'], h['criteria']['exports']['right']['export_label']): h
                 for h in conversation}
    e1_e3 = by_labels[('e1', 'e3')]['criteria']
    assert e1_e3['records']['changed_content'] >= 1 and 'a1x' in e1_e3['records']['only_left_examples']
    assert e1_e3['conversation_content']['equal'] is False  # title renamed in e3
    assert 'never deletion' in e1_e3['records']['absence_semantics']
    assert result['result']['logical_records_available'] is True


def test_call_log_and_recording_names_from_importers(store, tmp_path):
    from server.app.importers import call_log, recording_names
    corpus = communications.build(tmp_path / 'corpus')
    call_log.import_calls_xml(Path(corpus['calls']['path']))
    recording_names.observe_paths([Path(corpus['recordings']['dir'])])
    result, hypotheses = run('recording_call_log')
    assert len(hypotheses) == 1
    [h] = hypotheses
    assert h['left_kind'] == 'event'
    with dbmod.session() as db:
        left = db.execute('SELECT subject, event_type, artifact_id FROM events WHERE id=?', (h['left_id'],)).fetchone()
        right = db.execute('SELECT phone_or_address, event_type FROM events WHERE id=?', (h['right_id'],)).fetchone()
    assert left['event_type'] == 'recording' and left['artifact_id'] is None
    assert left['subject'] == corpus['recordings']['names']['matching_call']
    assert right['event_type'] == 'call' and right['phone_or_address'] == communications.NUMBER_A
    criteria = h['criteria']
    assert criteria['time']['implied_utc_offset_minutes'] == 60 and criteria['time']['residual_seconds'] == 0
    assert criteria['number']['status'] == 'exact'
    assert criteria['number']['recording_number_source'].startswith('recording_name_observations:')
    assert criteria['duration']['status'] == 'not_evaluated' and criteria['duration']['call_seconds'] == 125
    unmatched = result['result']['unmatched']
    reasons = {u['left_id']: u['reason'] for u in unmatched['recordings']}
    assert 'only_uncorroborated_unknown_zone_matches' in reasons.values() or 'no_call_within_tolerance' in reasons.values()
    with dbmod.session() as db:
        without_recording = db.execute("SELECT id FROM events WHERE event_type='call' AND phone_or_address=?",
                                       (communications.NUMBER_B,)).fetchone()['id']
    assert without_recording in unmatched['calls']
    # Display: the recording event stays an unknown-zone wall clock; the call is an epoch instant.
    items = window(candidates=False)['items']
    assert {i['time']['basis'] for i in items if i['event_type'] == 'recording'} == {'local_as_utc_unknown_zone'}
    calls = [i for i in items if i['event_type'] == 'call']
    assert {i['time']['basis'] for i in calls} == {'utc_epoch'} and any(i['time']['end_us'] for i in calls)

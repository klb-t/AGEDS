"""Review findings #9 (CSV formula guard), #10 (encoded DOCTYPE), #13 (hypothesis list by origin), U6 (path params)."""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from server.app import evidence
from server.app.importers import call_log
from server.app.importers.comm_common import DEFAULT_LIMITS, Issues
from server.app.integrations import loom_graph, tokens
from server.tests.test_review_fixes_support import env  # noqa: F401 (fixture)

LEADS = ('=', '+', '-', '@', '\t', '\r')


# --------------------------------------------------------------------- finding 9

def test_finding9_timeline_csv_guards_every_text_column(env):
    source = evidence.ensure_source('synthetic', '=SOURCE')
    evidence.add_event(source_id=source, artifact_id=None, event_type='call', ts_start='2024-01-01T10:00:00+00:00',
                       direction='=HYPERLINK("http://example.org","x")', contact_label='+31 6 0000 0001',
                       phone_or_address='-1', subject='@sum', body='=1+1', external_id='billing:row:1',
                       thread_id='=thread')
    with env.client() as http:
        response = http.get('/api/timeline/export', params={'format': 'csv'})
    assert response.status_code == 200, response.text
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows and rows[0]['direction'] == '\'=HYPERLINK("http://example.org","x")'
    for row in rows:
        for column, value in row.items():
            assert not value.startswith(LEADS), (column, value)
    with env.client() as http:
        raw = http.get('/api/timeline/export', params={'format': 'json'}).json()
    assert raw['items'][0]['direction'] == '=HYPERLINK("http://example.org","x")'  # JSON stays verbatim


# --------------------------------------------------------------------- finding 10

DOCTYPE = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE calls [<!ENTITY n "+31600000099">]>'
           '<calls><call number="&n;" date="1704103200000" type="1" duration="5"/></calls>')


@pytest.mark.parametrize('encoding', ['utf-16', 'utf-16-be', 'utf-8'])
def test_finding10_doctype_and_entities_rejected_in_any_encoding(tmp_path, encoding):
    text = DOCTYPE if encoding == 'utf-16' else DOCTYPE.replace('UTF-16', encoding.upper())
    data = text.encode(encoding)
    if encoding == 'utf-16-be':
        data = b'\xfe\xff' + data
    path = tmp_path / f'calls-{encoding}.xml'
    path.write_bytes(data)
    with pytest.raises(ValueError) as refused:
        list(call_log.iter_xml_elements(path, 'call', Issues(10), DEFAULT_LIMITS))
    # Decodable documents reach the declaration handler; a declared encoding expat
    # cannot decode is refused before any element is read. Neither expands the entity.
    assert ('document type' in str(refused.value)) or ('encoding not supported' in str(refused.value))
    if encoding != 'utf-16-be':
        assert 'document type' in str(refused.value)


def test_finding10_plain_utf16_calls_still_parse(tmp_path):
    path = tmp_path / 'calls.xml'
    path.write_bytes('<?xml version="1.0" encoding="UTF-16"?><calls><call number="+31600000099" date="1" type="1"/>'
                     '<extra/></calls>'.encode('utf-16'))
    issues = Issues(10)
    assert list(call_log.iter_xml_elements(path, 'call', issues, DEFAULT_LIMITS)) == [
        ('root', 'calls'), (1, {'number': '+31600000099', 'date': '1', 'type': '1'})]
    assert issues.counts.get('unexpected_element') == 1
    broken = tmp_path / 'broken.xml'
    broken.write_bytes(b'<calls><call number="1"></calls>')
    with pytest.raises(ValueError, match='well-formed'):
        list(call_log.iter_xml_elements(broken, 'call', Issues(10), DEFAULT_LIMITS))


# --------------------------------------------------------------------- finding 13

def _document(count: int) -> dict:
    return {'schema': loom_graph.HYPOTHESES_SCHEMA, 'run': {'tool': 'synthetic'},
            'items': [{'left': f'event:{900 + i}', 'right': f'event:{950 + i}', 'relation': 'same_call'} for i in range(count)]}


def test_finding13_client_hypothesis_list_filters_origin_before_limit(env):
    loom_graph.import_hypotheses(_document(5), origin='someone-else')
    own = loom_graph.import_hypotheses(_document(2), origin='chatadhd')
    token = tokens.create_token('synthetic client', ['read', 'propose'], client_id='chatadhd')['token']
    with env.client() as http:
        listed = http.get('/api/integrations/v1/hypotheses', params={'limit': 2},
                          headers={'Authorization': f'Bearer {token}'})
    assert listed.status_code == 200, listed.text
    assert [h['id'] for h in listed.json()] == own['hypothesis_ids']
    assert {h['origin'] for h in loom_graph.list_hypotheses(origin='someone-else')} == {'someone-else'}


# --------------------------------------------------------------------- U6

@pytest.mark.parametrize('paths', [[], ['not a path'], ['$.ok', "$.x') OR 1=1 --"], 'x', [1]])
def test_u6_invalid_chatbot_paths_are_input_errors_not_busy(env, paths):
    with env.client() as http:
        response = http.post('/api/timeline/correlations/runs',
                             json={'rule': 'same_conversation', 'params': {'chatbot_metadata_paths': paths}})
    assert response.status_code == 422, response.text
    assert env.one('SELECT COUNT(*) FROM timeline_correlation_runs') == 0


def test_u6_valid_single_path_still_runs(env):
    with env.client() as http:
        response = http.post('/api/timeline/correlations/runs',
                             json={'rule': 'same_conversation', 'params': {'chatbot_metadata_paths': ['$.chatbot']}})
    assert response.status_code == 200, response.text

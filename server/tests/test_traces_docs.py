"""Document metadata (OOXML, ODF, PDF Info + XMP) as claims and time candidates (traces area)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.config import settings
from server.app.importers import doc_metadata
from server.tests.corpus import traces as corpus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_settings = replace(settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store')
    test_settings.store_dir.mkdir(parents=True)
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    return {'files': corpus.build(tmp_path / 'corpus')['documents'], 'root': tmp_path / 'corpus' / 'documents'}


def rows(sql, params=()):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


def observe(path):
    result = doc_metadata.import_documents(Path(path))['results'][0]
    row = rows('SELECT * FROM trace_document_metadata WHERE artifact_id=?', (result['artifact_id'],))
    times = rows('SELECT * FROM event_times WHERE event_id=? ORDER BY id', (result['event_id'],)) if result['event_id'] else []
    return result, (row[0] if row else None), times


def by_source(times):
    return {t['source'].split(':', 2)[2] if t['source'].count(':') >= 2 else t['source']: t for t in times}


@pytest.mark.parametrize('raw,expected', [
    ("D:20200101120000+01'00'", ('2020-01-01T11:00:00Z', 'explicit_offset', 'second', [])),
    ("D:2020", (None, 'unknown', 'year', [])),
    ("D:202003", (None, 'unknown', 'month', [])),
    ("D:2020031512", (None, 'unknown', 'hour', [])),
    ("D:20200101120000Z00'00'", ('2020-01-01T12:00:00Z', 'explicit_offset', 'second', [])),
    ("D:20200101120000-05'30", ('2020-01-01T17:30:00Z', 'explicit_offset', 'second', [])),
    ("D:202001011200+0100", ('2020-01-01T11:00:00Z', 'explicit_offset', 'minute', [])),
    ("20200101", (None, 'unknown', 'day', ['pdf_date_without_d_prefix'])),
    ("D:20200101120000+25'00'", (None, 'unknown', 'second', ['pdf_date_offset_out_of_range'])),
    ("D:20200101120000+01'99'", (None, 'unknown', 'second', ['pdf_date_offset_out_of_range'])),
    ("D:20201301", None),
    ("D:20200101120000+01'00'junk", ('2020-01-01T11:00:00Z', 'explicit_offset', 'second', ['pdf_date_trailing_text'])),
])
def test_pdf_date_forms(raw, expected):
    candidate, problems = doc_metadata.pdf_date_candidate('created', raw, 'test', {})
    if expected is None:
        assert candidate is None and problems == ['pdf_date_invalid_calendar_value']
        return
    assert (candidate.utc, candidate.tz_status, candidate.precision, problems) == expected


def test_w3cdtf_and_duration():
    assert doc_metadata.w3cdtf_candidate('created', '2021', 's', {})[0].precision == 'year'
    candidate, problem = doc_metadata.w3cdtf_candidate('created', '2021-05-04T10:20:30.123456789', 's', {})
    assert candidate.tz_status == 'unknown' and candidate.precision == 'microsecond' and problem is None
    assert doc_metadata.w3cdtf_candidate('created', '2020-02-03T25:00:00Z', 's', {}) == (None, 'date_invalid_calendar_value')
    assert doc_metadata.duration_seconds('PT1H2M3S') == 3723 and doc_metadata.duration_seconds('P1M') is None


def test_ooxml_claims_times_and_privacy(env):
    before = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in env['files'].values()}
    result, row, times = observe(env['files']['docx'])
    assert result['status'] == 'parsed' and result['format'] == 'ooxml'
    claims, tools = json.loads(row['claims_json']), json.loads(row['tools_json'])
    assert claims['dc:creator'] == 'Ala Przykładowa' and claims['cp:lastModifiedBy'] == 'Jan Testowy'
    assert claims['cp:revision'] == '7' and claims['TotalTime_minutes'] == 42 and claims['dc:title'] == 'Umowa — wersja 2 ✓'
    assert tools['Application'] == 'Microsoft Office Word'
    found = {t['kind']: t for t in times}
    assert found['created']['utc'] == '2020-01-01T10:00:00Z'
    assert (found['updated']['precision'], found['updated']['tz_status']) == ('year', 'unknown')
    assert (found['other']['precision'], json.loads(found['other']['metadata_json'])['semantics']) == ('month', 'last printed claim')
    event = rows('SELECT * FROM events WHERE id=?', (result['event_id'],))[0]
    assert event['event_type'] == 'document_metadata' and event['subject'] == 'Umowa.docx' and event['sender'] is None
    assert event['ts_start'] == '2020-01-01T10:00:00Z'
    assert json.loads(event['metadata_json'])['signal']['class'] == 'declared'
    assert 'Przykładowa' not in event['metadata_json']  # authors stay in the claims row, not in the event
    assert not rows("SELECT 1 FROM search_fts WHERE search_fts MATCH 'Przykładowa'")
    xlsx, xrow, xtimes = observe(env['files']['xlsx'])
    assert 'date_invalid_calendar_value:dcterms:modified' in json.loads(xrow['issues_json'])
    assert [t['utc'] for t in xtimes] == ['2020-02-02T06:00:00.123000Z']
    pptx, prow, ptimes = observe(env['files']['pptx'])
    assert ptimes[0]['utc'] == '2019-12-31T09:59:59Z' and ptimes[0]['tz'] == '+14:00'
    assert {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in env['files'].values()} == before


def test_hostile_and_broken_containers(env):
    result, row, _ = observe(env['files']['docx_entity'])
    assert 'xml_doctype_or_entity_refused' in result['problems'] and row['status'] == 'partial'
    result, row, _ = observe(env['files']['docx_bomb'])
    assert 'zip_member_over_limit:docProps/core.xml' in result['problems']
    result, row, _ = observe(env['files']['docx_corrupt'])
    assert row['status'] == 'unreadable' and result['event_id'] is None
    assert any(p.startswith('zip_unreadable') for p in result['problems'])
    result = doc_metadata.import_documents(Path(env['files']['ole_doc']))['results'][0]
    assert result['status'] == 'not_supported' and result['format'] == 'ole'


def test_odf_meta(env):
    result, row, times = observe(env['files']['odt'])
    claims = json.loads(row['claims_json'])
    assert claims['meta:initial-creator'] == 'Ala Przykładowa' and claims['editing_duration_seconds'] == 3723
    assert json.loads(row['tools_json'])['meta:generator'].startswith('LibreOffice/7.6')
    found = {t['kind']: t for t in times}
    assert found['created']['tz_status'] == 'unknown' and found['created']['local'] == '2021-05-04T10:20:30.123456'
    assert found['updated']['local'] == '2021-05-05T11:00:00'
    assert json.loads(row['raw_json'])['document-statistic'] == {'page-count': '1', 'word-count': '12'}


def test_pdf_info_variants(env):
    result, row, times = observe(env['files']['pdf_simple'])
    claims, tools = json.loads(row['claims_json']), json.loads(row['tools_json'])
    assert claims['Author'] == 'Ala Przykładowa' and claims['Title'] == 'A \\ backslash'
    assert tools['Producer'] == 'Synthetic PDF (test) écrit' and tools['Creator'] == 'Writer'
    assert json.loads(row['raw_json'])['string_encodings']['Author'] == 'utf-16be'
    found = {t['kind']: t for t in times}
    assert found['created']['utc'] == '2020-01-01T11:00:00Z' and found['updated']['precision'] == 'year'
    result, row, times = observe(env['files']['pdf_incremental'])
    assert 'pdf_info_redefined_by_incremental_update' in result['problems']
    assert {t['kind']: t['utc'] for t in times}['updated'] == '2022-02-02T02:02:02Z'
    assert json.loads(row['raw_json'])['earlier_info'][0]['Producer'] == 'Synthetic PDF (test) écrit'
    result, row, times = observe(env['files']['pdf_objstm'])
    assert json.loads(row['raw_json'])['info_location'] == 'object_stream'
    assert json.loads(row['tools_json'])['Producer'] == 'Synthetic ObjStm' and times[0]['utc'] == '2021-02-03T04:05:06Z'
    result, row, times = observe(env['files']['pdf_xmp'])
    sources = {t['source'].split(':', 1)[1]: t for t in times}
    assert sources['pdf_info:CreationDate']['utc'] == '2020-01-01T11:00:00Z'
    assert sources['xmp:xmp:CreateDate']['utc'] == '2020-01-01T09:00:00Z'  # contradicting claims both kept
    assert sources['xmp:xmp:ModifyDate']['utc'] == '2023-03-03T02:03:03Z'
    xmp = json.loads(row['claims_json'])['xmp']
    assert xmp['dc:creator'] == ['Ala Przykładowa', 'Jan Testowy'] and xmp['xmp:CreatorTool'] == 'Example Writer 1.0'
    result, row, times = observe(env['files']['pdf_bad'])
    assert {'pdf_date_offset_out_of_range:CreationDate', 'pdf_date_invalid_calendar_value:ModDate'} <= set(result['problems'])
    assert [(t['kind'], t['tz_status']) for t in times] == [('created', 'unknown')]
    assert json.loads(times[0]['metadata_json'])['zone_status'] == 'numeric_offset_out_of_range'
    result, row, times = observe(env['files']['pdf_nod'])
    assert 'pdf_date_without_d_prefix:CreationDate' in result['problems']
    result, row, times = observe(env['files']['pdf_encrypted'])
    assert row['status'] == 'partial' and 'pdf_encrypted_strings_not_decoded' in result['problems'] and times == []
    result, row, times = observe(env['files']['pdf_noinfo'])
    assert row['status'] == 'no_metadata' and 'pdf_info_not_referenced' in result['problems']


def test_folder_import_and_idempotence(env):
    first = doc_metadata.import_documents(env['root'])
    assert first['documents'] == len(env['files']) and first['status_not_supported'] == 1
    counts = rows('SELECT (SELECT COUNT(*) FROM events) AS e, (SELECT COUNT(*) FROM event_times) AS t, '
                  '(SELECT COUNT(*) FROM trace_document_metadata) AS d')[0]
    doc_metadata.import_documents(env['root'])
    assert rows('SELECT (SELECT COUNT(*) FROM events) AS e, (SELECT COUNT(*) FROM event_times) AS t, '
                '(SELECT COUNT(*) FROM trace_document_metadata) AS d')[0] == counts

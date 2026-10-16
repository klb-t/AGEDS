"""Reports: numbers traceable to computed JSON, method record, manifest verification,
append-only register, output refusals, sections from providers (AGEDS-TASK-20261016-F)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from server.app import db
from server.app.reports import blocks, definitions, providers
from server.app.reports.common import ReportInputError, ReportOutputRefused, sha256_text
from server.app.reports.manifest import content_digest, verify_report
from server.app.reports.paths import REPOSITORY_ROOT
from server.tests.test_reports_support import CONTROL_BODY, FORMULA_BODY, HTML_BODY, ReportEnv, crosscheck, load_json

TYPES = ('catalog', 'timeline', 'time_budget', 'hypothesis_dossier', 'lawyer_package')


@pytest.fixture(scope='module')
def env():
    with pytest.MonkeyPatch.context() as monkeypatch:
        environment = ReportEnv().start(monkeypatch)
        environment.reports = {name: environment.report(name, {'title': None}) for name in TYPES}
        yield environment
        environment.close()


def _document(result) -> dict:
    return load_json(Path(result['output_dir']) / 'report.json')


def _section(document, name) -> dict:
    return next(section for section in document['sections'] if section['section'] == name)


def _block(section, block_id) -> dict:
    return next(block for block in section['blocks'] if block['id'] == block_id)


@pytest.mark.parametrize('report_type', TYPES)
def test_every_number_in_html_is_a_value_of_the_computed_json(env, report_type):
    result = env.reports[report_type]
    root = Path(result['output_dir'])
    document = _document(result)
    check = crosscheck((root / 'report.html').read_bytes(), document)
    assert check['spans'] > 40
    assert check['mismatches'] == []
    assert check['stray_numerals'] == []
    assert check['title_ok']
    assert 'script' not in check['tags'] and 'img' not in check['tags'] and 'link' not in check['tags']
    assert 'ol' not in check['tags']  # no generated ordinal numbers outside the computed document
    for href in check['hrefs']:  # only in-document anchors and files of this report
        assert href.startswith('#') or (root / href).is_file(), href
    assert content_digest(document) == document['integrity']['content_sha256'] == result['content_sha256']


def test_numbers_match_independent_sql(env):
    document = _document(env.reports['timeline'])
    summary = _block(_section(document, 'communications'), 'communications.summary')
    values = {row['key']: row['value'] for row in summary['rows']}
    with db.session() as conn:
        timed = conn.execute("SELECT COUNT(*) FROM events WHERE ts_start LIKE '____-__-__T%' AND (ts_start LIKE '%+%' OR ts_start LIKE '%Z')").fetchone()[0]
    # Every event with an offset-bearing legacy ts_start is on the axis; chat/mail events add theirs.
    assert values['events'] >= timed > 0
    chronology = _block(_section(document, 'timeline'), 'chronology.events')
    assert chronology['total_rows'] == values['events']
    by_type = _block(_section(document, 'communications'), 'communications.by_type')
    assert sum(row['events'] for row in by_type['rows']) == values['events']
    untimed = {row['untimed_reason']: row['events'] for row in _block(_section(document, 'communications'), 'communications.untimed')['rows']}
    assert untimed.get('unknown_zone', 0) >= 1 and untimed.get('no_time_candidate', 0) >= 1
    assert values['untimed'] == sum(untimed.values())


def test_method_record_is_complete(env):
    document = _document(env.reports['timeline'])
    method = document['method']
    for key in ('ageds_version', 'git_commit', 'git_dirty', 'generated_at', 'parameters', 'parameters_sha256', 'template',
                'model_runs', 'unknowns', 'data_range', 'exclusions', 'cited', 'inputs', 'sections', 'limitations'):
        assert key in method
    assert method['generated_at'].endswith('Z') and method['signed'] is False and method['model_prose'] == 'none'
    assert method['parameters'] == definitions.normalize_params('timeline', {})
    assert all(len(item['sha256']) == 64 for item in method['template']['files'])
    assert {item['name'] for item in method['template']['files']} == {'reports/base.html', 'reports/timeline.html'}
    assert method['git_commit'] == 'unknown' or len(method['git_commit']) == 40
    assert {'section': 'timeline', 'name': 'timeline_projection_unavailable', 'value': True} in method['exclusions']
    events = {entry['id'] for entry in method['inputs']['rows']['events']}
    assert env.events[0] in events and env.events[3] in events  # untimed events are inputs too
    with db.session() as conn:
        row = dict(conn.execute('SELECT * FROM events WHERE id=?', (env.events[0],)).fetchone())
    from server.app.reports.common import row_sha256
    assert {'id': env.events[0], 'row_sha256': row_sha256(row)} in method['inputs']['rows']['events']


def test_citations_and_model_runs_are_pinned(env):
    document = _document(env.reports['lawyer_package'])
    quotes = _block(_section(document, 'citations'), 'citations.list')
    anchors = {row['anchor_id']: row for row in quotes['rows']}
    segment = anchors[env.anchor_segment['id']]
    assert segment['quote'] == env.anchor_segment['quote_text'] and '<b>nr 17</b>' in segment['quote']
    assert segment['quote_hash_status'] == 'match' and segment['derived_text_id'] == env.v1
    assert segment['packet'] == f'citations/packets/a{env.artifact_audio}-c{env.anchor_segment["id"]}.json'
    assert anchors[env.anchor_words['id']]['precision_kind'] == 'word_asr'
    cited = document['method']['cited']
    assert {item['id'] for item in cited['anchors']} >= {env.anchor_segment['id'], env.anchor_words['id']}
    versions = {item['id']: item for item in cited['derived_text']}
    with db.session() as conn:
        text = conn.execute('SELECT text FROM derived_text WHERE id=?', (env.v1,)).fetchone()[0]
    assert versions[env.v1]['text_sha256'] == sha256_text(text)
    assert {'section': 'citations', 'field': f'derived_text.{env.v1}.processing_run'} in document['method']['unknowns']
    html = (Path(env.reports['lawyer_package']['output_dir']) / 'report.html').read_text(encoding='utf-8')
    assert '&lt;b&gt;nr 17&lt;/b&gt;' in html and '<b>nr 17</b>' not in html
    assert '&lt;script&gt;window.__x=1&lt;/script&gt;' in html and '<script' not in html
    assert "default-src 'none'" in html


def test_manifest_verifies_and_tampering_is_detected(env):
    result = env.report('timeline', {'formats': ['html', 'json', 'csv', 'xlsx']}, env.root / 'tamper')
    root = Path(result['output_dir'])
    good = verify_report(Path(result['manifest']), expected_manifest_sha256=result['manifest_sha256'])
    assert good['valid'] and good['counts']['files'] >= 5, good['errors']
    assert {item['code'] for item in good['warnings']} == {'unsigned_manifest'}
    html = root / 'report.html'
    original = html.read_bytes()
    html.write_bytes(original.replace(b'</body>', b'<p>dopisane</p></body>'))
    bad = verify_report(Path(result['manifest']))
    assert not bad['valid'] and 'file_digest_mismatch' in {e['code'] for e in bad['errors']}
    html.write_bytes(original)
    (root / 'extra.txt').write_text('not listed', encoding='utf-8')
    assert 'unlisted_file' in {e['code'] for e in verify_report(Path(result['manifest']))['errors']}
    (root / 'extra.txt').unlink()
    packet = next((root / 'citations' / 'packets').iterdir()) if (root / 'citations').exists() else None
    manifest = json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
    manifest['content_sha256'] = '0' * 64
    Path(result['manifest']).chmod(0o600)
    Path(result['manifest']).write_text(json.dumps(manifest), encoding='utf-8')
    codes = {e['code'] for e in verify_report(Path(result['manifest']), expected_manifest_sha256=result['manifest_sha256'])['errors']}
    assert {'manifest_digest_mismatch', 'manifest_sha256_mismatch', 'content_digest_mismatch'} <= codes
    assert packet is None  # timeline citations are scoped to the selected events' artifacts


def test_bundle_tampering_breaks_report_verification(env):
    result = env.report('lawyer_package', {}, env.root / 'tamper-bundle')
    root = Path(result['output_dir'])
    assert verify_report(Path(result['manifest']))['valid']
    packet = next((root / 'citations' / 'packets').iterdir())
    data = json.loads(packet.read_bytes())
    data['payload']['anchor']['quote_text'] = 'zmieniony cytat'
    packet.write_bytes(json.dumps(data).encode())
    verdict = verify_report(Path(result['manifest']))
    assert not verdict['valid'] and not verdict['bundle_valid'] and not verdict['files_valid']
    assert 'bundle_packet_file_mismatch' in {e['code'] for e in verdict['errors']}


def test_regeneration_creates_a_new_report_and_keeps_the_old(env):
    first = env.report('timeline', {'counterparty': 'Urząd'}, env.root / 'regen')
    second = env.report('timeline', {'counterparty': 'Kancelaria'}, env.root / 'regen')
    assert first['report_id'] != second['report_id'] and first['output_dir'] != second['output_dir']
    assert first['content_sha256'] != second['content_sha256']
    for item in (first, second):
        assert verify_report(Path(item['manifest']), expected_manifest_sha256=item['manifest_sha256'])['valid']
    first_doc, second_doc = _document(first), _document(second)
    assert first_doc['method']['parameters_sha256'] != second_doc['method']['parameters_sha256']
    rows_first = _block(_section(first_doc, 'timeline'), 'chronology.events')['rows']
    assert rows_first and all(row['counterparty'] == 'Urząd Przykładowy' for row in rows_first)
    with db.session() as conn:
        with pytest.raises(sqlite3.DatabaseError, match='append-only'):
            conn.execute('UPDATE reports SET params_json=? WHERE id=?', ('{}', first['report_id']))
    with db.session() as conn:
        with pytest.raises(sqlite3.DatabaseError, match='append-only'):
            conn.execute('DELETE FROM reports WHERE id=?', (first['report_id'],))
    from server.app.reports.service import listing
    ids = [row['id'] for row in listing(report_type='timeline')]
    assert first['report_id'] in ids and second['report_id'] in ids


def test_output_paths_inside_repository_store_or_scan_root_are_refused(env):
    targets = [REPOSITORY_ROOT / 'reports-out', REPOSITORY_ROOT, env.settings.store_dir / 'sub',
               env.root / 'scan' / 'nested', env.root / 'scan']
    link = env.root / 'link-to-store'
    link.symlink_to(env.settings.store_dir)
    targets.append(link / 'x')
    for target in targets:
        with pytest.raises(ReportOutputRefused):
            env.report('timeline', {}, target)
    assert not (REPOSITORY_ROOT / 'reports-out').exists() and not (env.root / 'scan' / 'nested').exists()
    assert not (env.settings.store_dir / 'sub').exists()
    file_target = env.root / 'a-file'
    file_target.write_text('x', encoding='utf-8')
    with pytest.raises(ReportOutputRefused):
        env.report('timeline', {}, file_target)


def test_parameters_are_strict(env):
    for params, message in (({'unknown': 1}, 'unknown parameters'), ({'from': '2026-03-01T10:00:00'}, 'offset'),
                            ({'assume_zone': 'Mars/Base'}, 'IANA'), ({'formats': ['csv']}, 'formats must include'),
                            ({'from': '2026-03-02', 'to': '2026-03-01'}, 'earlier'), ({'max_rows': 0}, 'max_rows'),
                            ({'sections': ['nope']}, 'sections'), ({'workbench_base_url': 'javascript:alert(1)'}, 'http'),
                            ({'collection_id': 999999}, 'does not exist')):
        with pytest.raises(ReportInputError, match=message):
            env.report('timeline', params, env.root / 'strict')
    with pytest.raises(ReportInputError, match='unknown report type'):
        env.report('everything', {}, env.root / 'strict')
    assert not (env.root / 'strict').exists() or not any((env.root / 'strict').iterdir())


def test_time_placement_exclusions_and_assumed_zone(env):
    plain = _document(env.reports['timeline'])
    section = _section(plain, 'timeline')
    assert section['exclusions']['untimed:unknown_zone'] >= 1
    assert section['rule'] == 'analysis_source.v1'
    assert section['exclusions']['hidden_chat_parts_excluded'] >= 1
    assumed = _document(env.report('timeline', {'assume_zone': 'Europe/Warsaw', 'counterparty': 'Osoba B'}, env.root / 'zone'))
    rows = _block(_section(assumed, 'timeline'), 'chronology.events')['rows']
    assert [row['assumed_zone'] for row in rows] == ['Europe/Warsaw']
    assert rows[0]['time_utc'] == '2026-03-01T09:15:00Z' and rows[0]['tz_status'] == 'assumed_zone'
    assert {'section': 'communications', 'field': 'original_zone_of_assumed_zone_items'} in assumed['method']['unknowns']


def test_analysis_sections_are_explicitly_unavailable_with_hashed_disclaimer(env):
    for report_type, section_name in (('time_budget', 'time_budget'), ('hypothesis_dossier', 'hypothesis_dossier')):
        document = _document(env.reports[report_type])
        section = _section(document, section_name)
        assert section['status'] == 'unavailable' and 'not present' in section['reason']
        notices = [b for b in section['blocks'] if b['kind'] == 'notice' and b['level'] == 'disclaimer']
        assert notices and all(b['sha256'] == sha256_text(b['text']) for b in notices)
        assert document['disclaimers'][0]['text'].startswith('Wynik analizatora jest materiałem pomocniczym')


def test_catalog_section_uses_area_a_and_chat_dedup(env):
    document = _document(env.reports['catalog'])
    catalog = _section(document, 'catalog')
    assert catalog['provider'] == 'server.app.reports.providers.catalog'
    assert catalog['snapshot'] == 'separate_read_transaction' and catalog['rule'] == 'ageds.catalog-coverage/v1'
    assert catalog['status'] in ('ok', 'partial'), catalog['reason']
    titles = [b.get('title') for b in catalog['blocks'] if b['kind'] == 'kv']
    assert 'Phone A (synthetic)' in titles and 'Drive B (synthetic)' in titles
    groups = next(b for b in catalog['blocks'] if b['id'] == 'catalog.groups')['rows']
    assert env.corpus['catalog']['shared_pdf']['sha256'] in {row['sha256'] for row in groups}
    types = [row for b in catalog['blocks'] if b['id'].startswith('catalog.types.') for row in b['rows']]
    assert sum(row['type_conflicts'] or 0 for row in types) >= 1
    bases = [row for b in catalog['blocks'] if b['id'].startswith('catalog.bases.') for row in b['rows']]
    assert bases and all(row['tz_status'] for row in bases)
    assert catalog['inputs']['catalog_scans'] and catalog['inputs']['collections']
    assert catalog['exclusions']['catalog_entries_unsupported'] >= 0 and 'catalog_entries_damaged' in catalog['exclusions']
    assert 'chat_export_parses_not_complete' in _section(document, 'chat_records')['exclusions']
    sources = _section(document, 'sources')
    values = {row['key']: row['value'] for row in _block(sources, 'sources.summary')['rows']}
    with db.session() as conn:
        assert values['artifacts'] == conn.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]
        groups = conn.execute('SELECT COUNT(*) FROM (SELECT sha256 FROM artifacts WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*)>1)').fetchone()[0]
    assert values['content_groups'] == groups
    chat = _section(document, 'chat_records')
    assert chat['snapshot'] == 'separate_read_transaction'
    exports = _block(chat, 'chat_records.exports')['rows']
    assert {row['export_label'] for row in exports} >= {'e1', 'e2', 'e3', 'k1'}
    summary = {(row['namespace'], row['metric']): row['count'] for row in _block(chat, 'chat_records.summary')['rows']}
    assert summary[('chatgpt', 'records_content_variants')] >= 1
    media = _section(document, 'media')
    texts = _block(media, 'media.texts')['rows']
    assert {row['derived_text_id'] for row in texts} >= {env.v1, env.v2}
    integrations = _section(document, 'integrations')
    proposals = _block(integrations, 'integrations.proposals')['rows']
    assert proposals[0]['body'] == 'propozycja <i>adnotacji</i>' and 'token_id' not in proposals[0]


def test_lawyer_package_groups_documents_and_human_questions(env):
    result = env.report('lawyer_package', {'open_questions': ['Czy pismo z 3 marca dotarło?'], 'group_by': 'domain'},
                        env.root / 'lawyer')
    document = _document(result)
    groups = {row['group_key'] for row in _block(_section(document, 'counterparties'), 'counterparties.summary')['rows']}
    assert 'example.org' in groups
    questions = _section(document, 'open_questions')
    human = [b for b in questions['blocks'] if b['kind'] == 'prose' and b['author'] == 'human']
    assert human[0]['text'] == 'Czy pismo z 3 marca dotarło?'
    detected = _block(questions, 'open_questions.detected')['rows']
    assert any(row['code'] == 'untimed:unknown_zone' for row in detected)
    documents = _block(_section(document, 'documents'), 'documents.list')['rows']
    assert {row['artifact_id'] for row in documents} >= {env.artifact_sms, env.artifact_audio}
    assert all(row['sha256'] is None or len(row['sha256']) == 64 for row in documents)
    check = crosscheck((Path(result['output_dir']) / 'report.html').read_bytes(), document)
    assert check['mismatches'] == [] and check['stray_numerals'] == []


def test_csv_formula_guard_and_xlsx_string_cells(env):
    result = env.report('timeline', {'formats': ['html', 'json', 'csv', 'xlsx']}, env.root / 'formats')
    root = Path(result['output_dir'])
    manifest = load_json(root / 'manifest.json')
    assert manifest['formats']['csv']['formula_guard'] == 'apostrophe_prefix_v1'
    assert manifest['formats']['csv']['guarded_cells'] >= 1
    chronology = next(path for path in (root / 'tables').iterdir() if 'chronology.events' in path.name)
    text = chronology.read_text(encoding='utf-8')
    assert "'=HYPERLINK" in text and ',=HYPERLINK' not in text
    import openpyxl
    book = openpyxl.load_workbook(root / 'report.xlsx')
    values = [cell for sheet in book.worksheets for row in sheet.iter_rows() for cell in row]
    formula = [cell for cell in values if cell.value == FORMULA_BODY]
    assert formula and all(cell.data_type == 's' for cell in formula)
    assert any(cell.value == CONTROL_BODY.replace('\x01', '\\x01') for cell in values)
    assert manifest['formats']['xlsx']['illegal_characters_escaped'] >= 1
    document = _document(result)
    quotes = [row['quote'] for row in _block(_section(document, 'timeline'), 'chronology.events')['rows']]
    assert FORMULA_BODY in quotes and CONTROL_BODY in quotes and HTML_BODY in quotes  # exact values in JSON


def test_pdf_adapter_is_explicit(env, monkeypatch):
    unavailable = env.report('time_budget', {'formats': ['html', 'json', 'pdf']}, env.root / 'pdf-off')
    manifest = load_json(Path(unavailable['manifest']))
    assert manifest['formats']['pdf']['status'] == 'unavailable' and 'EW_PDF_CHROMIUM' in manifest['formats']['pdf']['reason']
    executable = os.environ.get('AGEDS_TEST_CHROMIUM') or '/opt/pw-browsers/chromium-1194/chrome-linux/chrome'
    if not os.access(executable, os.X_OK):
        pytest.skip('no Chromium executable for the real PDF adapter run (recorded as not run)')
    monkeypatch.setenv('EW_PDF_CHROMIUM', executable)
    result = env.report('lawyer_package', {'formats': ['html', 'json', 'pdf']}, env.root / 'pdf-on')
    manifest = load_json(Path(result['manifest']))
    assert manifest['formats']['pdf']['status'] == 'written', manifest['formats']['pdf']
    pdf = Path(result['output_dir']) / 'report.pdf'
    assert pdf.read_bytes()[:5] == b'%PDF-' and pdf.stat().st_size > 1000
    assert any(item['path'] == 'report.pdf' and item['role'] == 'pdf_from_html' for item in manifest['files'])
    assert verify_report(Path(result['manifest']))['valid']


def test_model_prose_with_digits_is_refused():
    ok = blocks.prose('x', 't', 'Liczba {n} zdarzeń.', {'n': 12}, author='model')
    assert [part.get('value') for part in ok['parts'] if 'slot' in part] == [12]
    with pytest.raises(ReportInputError, match='numeral'):
        blocks.prose('x', 't', 'W marcu było 12 zdarzeń.', {}, author='model')
    with pytest.raises(ReportInputError, match='numeral'):
        blocks.prose('x', 't', 'Było ٣ zdarzeń {n}.', {'n': 1}, author='model')
    with pytest.raises(ReportInputError, match='undeclared'):
        blocks.prose('x', 't', 'Było {missing}.', {}, author='code')
    with pytest.raises(ReportInputError, match='scalar'):
        blocks.prose('x', 't', 'Było {n}.', {'n': [1]}, author='code')
    human = blocks.prose('x', 'human_input', 'Pismo nr 5 z {dnia}', {}, author='human')
    assert human['parts'] == [{'text': 'Pismo nr 5 z {dnia}'}]


def test_provider_registry_contract(env):
    calls = []

    def fake(ctx):
        calls.append(ctx.params['budget_run_id'])
        section = providers.Section(ctx, 'time_budget', 'test.fake', 'fake.v1', 'section_time_budget')
        section.block(blocks.kv('fake', [('count', 7)], ctx.lang))
        return section.result()
    providers.register('time_budget', fake)
    try:
        result = env.report('time_budget', {'budget_run_id': 3}, env.root / 'override')
    finally:
        providers.unregister('time_budget')
    assert calls == [3]
    assert _section(_document(result), 'time_budget')['status'] == 'ok'
    providers.register('time_budget', lambda ctx: {'section': 'time_budget'})
    try:
        with pytest.raises(RuntimeError, match='misses'):
            env.report('time_budget', {}, env.root / 'override-bad')
    finally:
        providers.unregister('time_budget')
    assert not any(p.name.startswith('.ageds-report-staging') for p in (env.root / 'override-bad').iterdir()) \
        if (env.root / 'override-bad').exists() else True
    with pytest.raises(ValueError):
        providers.register('nonexistent', fake)


def test_failed_generation_leaves_no_partial_directory(env, monkeypatch):
    from server.app.reports import service

    def boom(*args, **kwargs):
        raise RuntimeError('render failed')
    monkeypatch.setattr(service, 'render_html', boom)
    target = env.root / 'failed'
    with pytest.raises(RuntimeError, match='render failed'):
        env.report('time_budget', {}, target)
    assert list(target.iterdir()) == []


def test_generic_upstream_dict_adapter_keeps_values():
    value = {'rule': 'r.v1', 'count': 3, 'items': [{'a': 1, 'b': {'x': 2}}, {'a': 2}], 'nested': {'k': 'v'},
             'plain': [1, 'x']}
    out = blocks.generic_blocks(value, 'up', 'pl', max_rows=1)
    kinds = [(b['kind'], b['id']) for b in out]
    assert ('kv', 'up') in kinds and ('table', 'up.items') in kinds and ('kv', 'up.nested') in kinds
    items = next(b for b in out if b['id'] == 'up.items')
    assert items['rows'] == [{'a': 1, 'b': '{"x":2}'}] and items['truncated'] is True and items['total_rows'] == 2

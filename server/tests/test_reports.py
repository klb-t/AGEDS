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
        ids = {'time_budget': {'budget_run_id': environment.budget_run},
               'hypothesis_dossier': {'hypothesis_id': environment.hypothesis}}
        environment.reports = {name: environment.report(name, {'title': None, **ids.get(name, {})}) for name in TYPES}
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
        timed = conn.execute("SELECT COUNT(*) FROM events WHERE ts_start LIKE '____-__-__T%' AND (ts_start LIKE '%+%' OR ts_start LIKE '%Z') "
                             "AND event_type <> 'chat_message_part' AND COALESCE(json_extract(metadata_json, '$.chat.hidden'), 0) "
                             "NOT IN (1, 'true')").fetchone()[0]
    # Every visible event with an offset-bearing legacy ts_start is on the axis; chat/mail events add theirs.
    # Chat parts are hidden by the timeline display rule (include_hidden_chat_parts=false) and counted apart.
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
    assert not any(item['name'] == 'timeline_projection_unavailable' for item in method['exclusions'])
    # The derived timeline cache read by the snapshot is named with its rule hash and build.
    from server.app.timeline.ruleset import load_rule
    rule = load_rule(None, kind='display_rule')
    [cache] = method['derived_caches']
    assert cache['cache'] == 'timeline_projection' and cache['rule_sha256'] == rule.sha256
    assert (cache['rule_id'], cache['rule_version']) == (rule.rule_id, rule.version)
    with db.session() as conn:
        build = conn.execute('SELECT * FROM timeline_projection_builds WHERE id=?', (cache['build_id'],)).fetchone()
    assert build['projection_id'] == cache['projection_id'] and build['finished_at'] == cache['build_finished_at']
    assert {row['id'] for row in method['inputs']['rows']['timeline_projection_builds']} == {cache['build_id']}
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
    assert section['rule'] == 'display_time@1'  # the timeline projection (area C), not the interim rule
    assert section['exclusions']['hidden_chat_parts_excluded'] >= 1
    with pytest.raises(ReportInputError, match='assumption_rule'):  # an operator zone needs a named assumption
        env.report('timeline', {'assume_zone': 'Europe/Warsaw'}, env.root / 'zone')
    with pytest.raises(ReportInputError, match='without assume_zone'):
        env.report('timeline', {'assumption_rule': 'x'}, env.root / 'zone')
    assumed = _document(env.report('timeline', {'assume_zone': 'Europe/Warsaw', 'assumption_rule': 'operator: phone clock',
                                                'counterparty': 'Osoba B'}, env.root / 'zone'))
    rows = _block(_section(assumed, 'timeline'), 'chronology.events')['rows']
    assert [row['assumed_zone'] for row in rows] == ['Europe/Warsaw']
    assert rows[0]['time_utc'] == '2026-03-01T09:15:00Z' and rows[0]['tz_status'] == 'assumed_zone'
    [cache] = assumed['method']['derived_caches']
    assert cache['params'] == {'assume_zone': 'Europe/Warsaw', 'assumption_rule': 'operator: phone clock'}
    assert _section(assumed, 'timeline')['exclusions']['segments_excluded_by_counterparty_filter'] is True
    assert {'section': 'communications', 'field': 'original_zone_of_assumed_zone_items'} in assumed['method']['unknowns']


def test_analysis_sections_without_a_run_are_unavailable_with_hashed_disclaimer(env):
    for report_type, param in (('time_budget', 'budget_run_id'), ('hypothesis_dossier', 'hypothesis_id')):
        for params, reason in (({}, f'parameter {param} not given'), ({param: 999999}, 'does not exist')):
            document = _document(env.report(report_type, params, env.root / 'unavailable'))
            section = _section(document, report_type)
            assert section['status'] == 'unavailable' and reason in section['reason'], section['reason']
            notices = [b for b in section['blocks'] if b['kind'] == 'notice' and b['level'] == 'disclaimer']
            assert notices and all(b['sha256'] == sha256_text(b['text']) for b in notices)
            assert document['disclaimers'][0]['text'].startswith('Wynik analizatora jest materiałem pomocniczym')


def _rows(section, block_id) -> list[dict]:
    return _block(section, block_id)['rows']


def test_time_budget_section_renders_the_stored_run(env):
    from server.app.analysis.runs import get_run
    document = _document(env.reports['time_budget'])
    section = _section(document, 'time_budget')
    assert section['status'] == 'ok' and section['provider'] == 'server.app.reports.providers.time_budget'
    with db.session() as conn:
        stored = get_run(conn, env.budget_run, kind='time_budget')
    result = stored['result']
    run = {row['key']: row['value'] for row in _rows(section, 'time_budget.run')}
    assert run['run_id'] == env.budget_run and run['result_sha256'] == stored['result_sha256']
    assert run['integrity'] == 'hash_matches'
    source = {row['key']: row['value'] for row in _rows(section, 'time_budget.source')}
    assert source['source_rule'] == section['rule'] == 'display_time@1'
    assert source['source_rule_sha256'] == result['input']['source_rule_sha256']
    totals = {row['category']: row for row in _rows(section, 'time_budget.category_totals')}
    for name, entry in result['totals']['categories'].items():
        assert {k: totals[name][k] for k in entry} == entry  # every number is the stored number
    assert totals['nieobserwowane']['probable'] == result['totals']['unobserved']['probable']
    assert totals['sprawy_urzedowe']['lower'] <= totals['sprawy_urzedowe']['probable'] <= totals['sprawy_urzedowe']['upper']
    candles = _rows(section, 'time_budget.candles')
    day = result['candles']['day'][0]
    first = next(r for r in candles if r['granularity'] == 'day' and r['bucket'] == day['bucket']
                 and r['category'] == 'nieobserwowane')
    assert first['upper'] == day['unobserved']['upper'] and first['partial'] == day['partial']
    ranges = {row['category']: row for row in _rows(section, 'time_budget.sensitivity_ranges')}
    assert ranges.keys() == result['sensitivity']['ranges'].keys()
    sensitivity = {row['key']: row['value'] for row in _rows(section, 'time_budget.sensitivity')}
    assert sensitivity['points_evaluated'] == result['grid']['points_evaluated']
    assert sensitivity['unstable'] == result['sensitivity']['unstable']
    definitions = {row['category'] for row in _rows(section, 'time_budget.categories')}
    assert {'sprawy_urzedowe', 'kancelaria', 'nieobserwowane'} <= definitions
    rules = _rows(section, 'time_budget.category_rules')
    assert {row['rule_id'] for row in rules} == {'urzad', 'kancelaria'}
    assert not any('values' in row for row in rules)  # rule provenance only, never private values
    assert {entry['id'] for entry in document['method']['inputs']['rows']['analysis_runs']} == {env.budget_run}
    labels = [b for b in section['blocks'] if b['kind'] == 'notice' and b['level'] == 'disclaimer']
    assert any('nieobserwowane' in b['text'] for b in labels)


def test_hypothesis_dossier_section_renders_registration_dossier_theses_and_event_study(env):
    from server.app.analysis import eventstudy, preregistration
    from server.app.analysis.hypotheses import DISCLAIMER
    document = _document(env.reports['hypothesis_dossier'])
    section = _section(document, 'hypothesis_dossier')
    assert section['status'] == 'ok', section['reason']
    disclaimer = next(b for b in section['blocks'] if b['kind'] == 'notice' and b['level'] == 'disclaimer')
    assert disclaimer['text'] == DISCLAIMER['pl'] and disclaimer['sha256'] == DISCLAIMER['sha256_pl']
    assert disclaimer['source'] == 'server.app.analysis.hypotheses'  # reused, not a copy
    with db.session() as conn:
        version = preregistration.list_versions(conn, env.hypothesis)[-1]
        run = eventstudy.get_run(conn, env.event_study['run_id'])
    versions = _rows(section, 'hypothesis_dossier.versions')
    assert versions[-1]['definition_sha256'] == version['definition_sha256']
    assert versions[-1]['record_sha256'] == version['record_sha256'] and versions[-1]['integrity'] == 'hash_matches'
    theses = {row['thesis_id']: row for row in _rows(section, 'hypothesis_dossier.theses')}
    assert theses[env.thesis_unsupported['thesis_id']]['unsupported'] is True
    assert 'unsupported' in theses[env.thesis_unsupported['thesis_id']]['flags']
    assert theses[env.thesis_supported['thesis_id']]['unsupported'] is False
    assert theses[env.thesis_supported['thesis_id']]['status_type'] == 'paraphrase'
    items = {row['item_id']: row for row in _rows(section, 'hypothesis_dossier.items')}
    assert items[env.dossier_items[0]]['classification'] == 'supporting'
    assert items[env.dossier_items[1]]['classification'] == 'candidate'
    windows = _rows(section, 'hypothesis_dossier.windows')
    stored = run['result']['windows'][0]
    assert windows[0]['p_value'] == stored['naive']['p_value'] and windows[0]['observed_pairs'] == stored['naive']['observed']
    warnings = {row['warning']: row for row in _rows(section, 'hypothesis_dossier.warnings')}
    assert list(warnings) == list(eventstudy.ALWAYS_PRESENT_WARNINGS) and warnings['causation']['flagged'] is True
    runs = {row['run_id']: row for row in _rows(section, 'hypothesis_dossier.runs')}
    assert runs[run['run_id']]['result_sha256'] == run['result_sha256'] and runs[run['run_id']]['integrity'] == 'hash_matches'
    quadrants = {row['quadrant']: row for row in _rows(section, 'hypothesis_dossier.disclosure')}
    assert quadrants['known_unknowns']['human_text'] == 'Brak billingu za marzec.'
    # the anchor pinned in the dossier is the citation the report lists and bundles
    assert section['citations'] == [[env.artifact_audio, env.anchor_segment['id']]]
    listed = _rows(_section(document, 'citations'), 'citations.list')
    assert [row['anchor_id'] for row in listed] == [env.anchor_segment['id']]
    assert section['exclusions']['current_theses_without_evidence'] == 1


def test_timeline_section_uses_the_projection_and_lists_correlation_hypotheses_with_status(env):
    document = _document(env.reports['timeline'])
    section = _section(document, 'timeline')
    assert section['provider'] == 'server.app.reports.providers.timeline' and section['status'] == 'ok'
    projection = {row['key']: row['value'] for row in _rows(section, 'timeline.projection')}
    [cache] = document['method']['derived_caches']
    assert projection['build_id'] == cache['build_id'] and projection['rule_sha256'] == cache['rule_sha256']
    placement = _rows(section, 'timeline.placement')
    communications = {row['key']: row['value'] for row in _rows(_section(document, 'communications'), 'communications.summary')}
    assert sum(row['items'] for row in placement) == communications['events'] + communications['untimed']
    hypotheses = {row['hypothesis_id']: row for row in _rows(section, 'timeline.correlations')}
    assert set(hypotheses) == set(env.correlations) and len(env.correlations) >= 2
    assert hypotheses[env.correlations[0]]['status'] == 'accepted'
    assert {row['status'] for row in hypotheses.values()} == {'accepted', 'proposed'}
    assert all(row['probability'] is None for row in hypotheses.values())
    label = next(b for b in section['blocks'] if b['id'] == 'timeline.correlation_label')
    assert 'nie są faktami' in label['text'] and label['sha256'] == sha256_text(label['text'])
    assert {row['id'] for row in document['method']['inputs']['rows']['timeline_hypotheses']} == set(env.correlations)


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
    monkeypatch.setenv('EW_PDF_CHROMIUM_NO_SANDBOX', '1')  # CI/VM hosts without a usable Chromium sandbox
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


# These two tests add events to the shared environment: keep them last.
def _evidence_digest() -> dict:
    caches = {'reports', 'audit_log', 'timeline_projections', 'timeline_items', 'timeline_projection_builds'}
    out = {}
    with db.session() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for name in names:
            if name in caches or name.startswith('search_fts') or name.startswith('workbench_fts'):
                continue
            rows = conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            out[name] = sha256_text(json.dumps([list(r) for r in rows], default=str))
    return out


def test_report_refreshes_only_the_derived_cache_and_records_it(env):
    from server.app import evidence
    late = evidence.add_event(source_id=env.source, artifact_id=env.artifact_sms, event_type='sms',
                              ts_start='2026-03-06T10:00:00+00:00', direction='in', phone_or_address='+48 000 000 004',
                              body='późne zdarzenie', external_id='sms-late')
    before = _evidence_digest()
    document = _document(env.report('timeline', {}, env.root / 'refresh'))
    assert _evidence_digest() == before  # evidence and analysis tables untouched; only cache + register rows
    [cache] = document['method']['derived_caches']
    assert cache['refreshed_by_report'] is True and cache['snapshot_status'] == 'fresh'
    events = {row['event_id'] for row in _block(_section(document, 'timeline'), 'chronology.events')['rows']}
    assert late in events
    again = _document(env.report('timeline', {}, env.root / 'refresh'))
    assert again['method']['derived_caches'][0]['refreshed_by_report'] is False


def test_stale_projection_inside_the_snapshot_is_retried_then_refused(env, monkeypatch):
    from server.app import evidence
    from server.app.reports import service
    from server.app.timeline.analysis_items import ProjectionNotFresh
    calls = []

    def stale_refresh(params):  # data arrives after every refresh, before the snapshot
        calls.append(params)
        evidence.add_event(source_id=env.source, artifact_id=env.artifact_sms, event_type='sms', ts_start='2026-03-07T10:00:00+00:00',
                           body=f'race {len(calls)}', external_id=f'race-{len(calls)}')
        return {'status': 'fresh', 'build': {'id': -1}}
    monkeypatch.setattr(service, 'refresh_timeline_projection', stale_refresh)
    with pytest.raises(ProjectionNotFresh, match='not fresh'):
        env.report('timeline', {}, env.root / 'stale')
    assert len(calls) == service.SNAPSHOT_ATTEMPTS
    assert not (env.root / 'stale').exists() or list((env.root / 'stale').iterdir()) == []

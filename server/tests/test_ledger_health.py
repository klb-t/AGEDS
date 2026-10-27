"""Health/fitness importers: opt-in, samples vs sessions, offsets and DST, two devices, bounded streaming."""
from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import pytest

from server.app.importers import health_apple, health_fitbit, health_google_fit, health_samsung
from server.app.importers.comm_common import DEFAULT_LIMITS
from server.app.ledger import xmlstream
from server.tests.corpus import ledger as corpus
from server.tests.ledger_support import counts, env, events, rows, sha_tree  # noqa: F401  (fixture)

IMPORTERS = [
    (health_apple.import_apple_health, ('apple', 'export')),
    (health_google_fit.import_google_fit, ('google_fit', 'sleep')),
    (health_samsung.import_samsung_health, ('samsung', 'sleep')),
    (health_fitbit.import_fitbit, ('fitbit', 'sleep')),
]


def samples(artifact_id, **where):
    sql = 'SELECT * FROM health_samples WHERE artifact_id=?'
    params = [artifact_id]
    for key, value in where.items():
        sql += f' AND {key}=?'
        params.append(value)
    out = rows(sql + ' ORDER BY id', params)
    for row in out:
        row['meta'] = json.loads(row['metadata_json'])
    return out


def sessions(artifact_id):
    out = events(artifact_id, 'health_session')
    for event in out:
        event['linked'] = rows('SELECT COUNT(*) n FROM health_samples WHERE session_event_id=?', (event['id'],))[0]['n']
    return out


@pytest.mark.parametrize('importer,key', IMPORTERS)
def test_health_import_refuses_without_opt_in_and_acquires_nothing(env, importer, key):
    path = Path(env['m']['health'][key[0]][key[1]])
    before = sha_tree(env['root'])
    for flag in (False, None, 'yes', 1):
        with pytest.raises(PermissionError, match='enable_health'):
            importer(path, enable_health=flag)
    assert counts() == {k: 0 for k in counts()}
    assert sha_tree(env['root']) == before


def test_apple_samples_sessions_offsets_and_basis(env):
    m = env['m']['health']
    before = sha_tree(env['root'])
    result = health_apple.import_apple_health(Path(m['apple']['export']), enable_health=True)
    assert sha_tree(env['root']) == before
    expect = m['apple_expect']
    assert (result['records'], result['workouts'], result['sleep_samples'], result['sleep_sessions']) == \
        (expect['records'], expect['workouts'], expect['sleep_samples'], expect['sleep_sessions'])
    assert result['opt_in'] == {'enable_health': True, 'processing': 'local_only'}
    audit = rows("SELECT details_json FROM audit_log WHERE action='import_run'")
    assert json.loads(audit[-1]['details_json'])['options']['enable_health'] is True
    artifact = result['artifact_id']
    by_type = {}
    for row in samples(artifact):
        if row['sample_class'] in ('quantity', 'category'):
            by_type.setdefault(row['sample_type'], []).append(row)
    heart = by_type['HKQuantityTypeIdentifierHeartRate'][0]
    assert (heart['start_utc'], heart['end_utc'], heart['start_tz'], heart['end_tz']) == \
        ('2024-03-31T00:59:00Z', '2024-03-31T01:01:00Z', '+01:00', '+02:00')
    assert heart['meta']['span']['seconds'] == 120  # across the DST change: 2 minutes, not 62
    assert heart['value_exact'] == '61.5' and heart['basis'] == 'raw_sample'
    assert heart['device_raw'].startswith('<<HKDevice') and heart['source_name'] == 'Example Watch'
    assert by_type['HKQuantityTypeIdentifierBodyMass'][0]['basis'] == 'provider_user_edit'
    assert by_type['HKQuantityTypeIdentifierRestingHeartRate'][0]['basis'] == 'provider_inference'
    steps = by_type['HKQuantityTypeIdentifierStepCount']
    assert steps[1]['meta']['issues'] == ['end_before_start'] and steps[2]['start_utc'] is None
    assert all(r['device_raw'] != 'DEFAULT-DEVICE-FROM-DTD' for r in samples(artifact))  # no ATTLIST defaults
    pressure = by_type['HKQuantityTypeIdentifierBloodPressureSystolic'][0]
    assert pressure['meta']['correlation']['correlation_type'] == 'HKCorrelationTypeIdentifierBloodPressure'
    summary = {r['sample_type']: r for r in samples(artifact, sample_class='daily_summary')}
    assert summary['ActivitySummary.activeEnergyBurned']['unit'] == 'kcal'
    assert summary['ActivitySummary.activeEnergyBurned']['start_local'] == '2024-03-30T00:00:00'
    assert summary['ActivitySummary.activeEnergyBurned']['start_tz_status'] == 'unknown'
    assert rows('SELECT COUNT(*) n FROM events WHERE artifact_id=? AND event_type!=?', (artifact, 'health_session'))[0]['n'] == 0


def test_apple_sleep_sessions_keep_overlapping_devices_apart(env):
    result = health_apple.import_apple_health(Path(env['m']['health']['apple']['export']), enable_health=True)
    sleep = [s for s in sessions(result['artifact_id']) if s['meta']['session']['kind'] == 'sleep']
    spans = sorted((s['thread_id'], s['ts_start'], s['ts_end'], s['linked']) for s in sleep)
    assert spans == [('health:apple_health:Example Phone', '2024-03-30T22:10:00Z', '2024-03-31T05:00:00Z', 1),
                     ('health:apple_health:Example Watch', '2024-03-30T22:00:00Z', '2024-03-31T05:30:00Z', 6),
                     ('health:apple_health:Example Watch', '2024-03-31T21:30:00Z', '2024-04-01T04:30:00Z', 1)]
    watch_night = next(s for s in sleep if s['linked'] == 6)
    stages = watch_night['meta']['session']['stage_seconds']
    assert stages['HKCategoryValueSleepAnalysisAsleepREM'] == 50 * 60  # 01:50+01:00 -> 03:40+02:00
    assert watch_night['meta']['session']['span']['seconds'] == 7.5 * 3600
    assert watch_night['meta']['signal'] == {'class': 'passive', 'basis': 'provider_inference', 'provider': 'apple_health',
                                             'device_or_account': 'Example Watch',
                                             'note': 'sleep stages are the device/app inference, grouped by a versioned rule'}
    assert watch_night['meta']['session']['grouping'] == {'rule': 'ledger.sleep_session.v1', 'gap_s': 3600,
                                                          'key': 'source_name+device'}
    assert watch_night['meta']['opt_in']['enable_health'] is True
    assert [t['kind'] for t in watch_night['times']] == ['start', 'end']


def test_apple_workouts_declared_duration_and_user_entry(env):
    result = health_apple.import_apple_health(Path(env['m']['health']['apple']['export']), enable_health=True)
    workouts = [s for s in sessions(result['artifact_id']) if s['meta']['session']['kind'] == 'workout']
    run, yoga = workouts
    assert run['subject'] == 'workout: HKWorkoutActivityTypeRunning' and run['linked'] == 3
    assert run['meta']['session']['declared_duration'] == {'value': '30.5', 'unit': 'min', 'exact': '30.5'}
    assert run['meta']['session']['span']['seconds'] == 1830
    assert run['meta']['session']['routes'] == ['/workout-routes/route_2024-03-30.gpx']
    assert {t['kind'] for t in run['times']} == {'start', 'end', 'created'}
    assert (yoga['meta']['signal']['class'], yoga['meta']['signal']['basis']) == ('declared', 'provider_user_edit')


def test_apple_reimport_zip_refusals_limits_and_new_rule_parameters(env):
    m = env['m']['health']['apple']
    first = health_apple.import_apple_health(Path(m['export']), enable_health=True)
    snapshot = counts()
    again = health_apple.import_apple_health(Path(m['export']), enable_health=True)
    assert counts() == snapshot and again['samples_existing'] == first['samples_inserted']
    zipped = health_apple.import_apple_health(Path(m['zip']), enable_health=True)
    assert zipped['zip_member'] == 'apple_health_export/export.xml' and zipped['sleep_sessions'] == 3
    for name in ('entity', 'external_dtd'):
        with pytest.raises(xmlstream.XmlRefused):
            health_apple.import_apple_health(Path(m[name]), enable_health=True)
    limited = health_apple.import_apple_health(Path(m['export']), enable_health=True,
                                               limits=DEFAULT_LIMITS.replace(max_records=5), source_locator='limited')
    assert limited['coverage'] == {'complete': False} and limited['issues']['by_code']['record_limit'] == 1
    wider = health_apple.import_apple_health(Path(m['export']), enable_health=True, sleep_session_gap_s=86400,
                                             source_locator='wider')
    assert wider['sleep_sessions'] == 2  # the watch's two nights join under a 24 h gap; a new result, not an overwrite


def test_apple_streaming_memory_is_bounded(env, tmp_path):
    def build(count):
        records = ''.join(corpus._apple_record('HKQuantityTypeIdentifierHeartRate',
                                               f'2024-03-{1 + i % 28:02d} {i % 24:02d}:00:00 +0100',
                                               f'2024-03-{1 + i % 28:02d} {i % 24:02d}:00:30 +0100', str(60 + i % 40),
                                               'count/min', 'Example Watch') for i in range(count))
        path = tmp_path / f'export-{count}.xml'
        path.write_text(corpus.apple_export(records), encoding='utf-8')
        return path
    peaks = []
    for count in (2000, 8000):
        path = build(count)
        tracemalloc.start()
        result = health_apple.import_apple_health(path, enable_health=True)
        peaks.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
        assert result['records'] == count + 16
    assert peaks[1] < peaks[0] * 2  # 4x the input does not need 2x the memory


def test_google_fit_sessions_metrics_and_location_columns(env):
    m = env['m']['health']['google_fit']
    sleep = health_google_fit.import_google_fit(Path(m['sleep']), enable_health=True)
    (session,) = sessions(sleep['artifact_id'])
    assert (session['ts_start'], session['ts_end'], session['linked']) == ('2024-03-30T22:00:00Z', '2024-03-31T05:30:00Z', 3)
    assert session['meta']['session']['kind'] == 'sleep'
    walk = health_google_fit.import_google_fit(Path(m['walk']), enable_health=True)
    (session,) = sessions(walk['artifact_id'])
    assert 'declared_duration_differs_from_span' in session['meta']['issues']
    calories = samples(walk['artifact_id'], sample_type='com.google.calories.expended')[0]
    assert calories['value_raw'] == calories['value_exact'] == '65.12345678901234567'  # JSON number text, no float
    daily = health_google_fit.import_google_fit(Path(m['daily']), enable_health=True)
    assert daily['location_columns_not_imported'] == ['Low latitude (deg)', 'Low longitude (deg)']
    assert not any('latitude' in r['sample_type'] for r in samples(daily['artifact_id']))
    assert {r['sample_type']: r['unit'] for r in samples(daily['artifact_id'])}['Calories (kcal)'] == 'kcal'
    intervals = health_google_fit.import_google_fit(Path(m['intervals']), enable_health=True)
    rowset = samples(intervals['artifact_id'], sample_type='Step count')
    assert [(r['start_utc'], r['start_tz']) for r in rowset] == [('2024-03-31T00:45:00Z', '+01:00'),
                                                               ('2024-03-31T01:00:00Z', '+02:00')]
    assert rowset[0]['meta']['date_basis'] == 'file_name'


def test_samsung_utc_wall_clock_with_offset_column(env):
    m = env['m']['health']['samsung']
    result = health_samsung.import_samsung_health(Path(m['sleep']), enable_health=True)
    assert result['table'] == 'com.samsung.shealth.sleep' and result['table_metadata_line'].startswith('com.samsung')
    first, second, third = sessions(result['artifact_id'])
    start = first['times'][0]
    assert (start['utc'], start['local'], start['tz']) == ('2024-03-30T22:05:00Z', '2024-03-30T23:05:00', '+01:00')
    assert start['meta']['time_basis'] == 'utc_wall_clock_with_offset_column'
    assert second['times'][0]['tz'] == '+02:00'
    assert third['times'][0]['tz'] == 'UTC' and third['times'][0]['local'] is None  # 'CET' is not an offset
    assert result['issues']['by_code']['offset_column_unparsed'] == 2
    exercise = health_samsung.import_samsung_health(Path(m['exercise']), enable_health=True)
    (workout,) = sessions(exercise['artifact_id'])
    assert workout['subject'] == 'workout: 1002' and workout['meta']['session']['activity_label_example'] == 'running'
    steps = health_samsung.import_samsung_health(Path(m['steps']), enable_health=True)
    assert samples(steps['artifact_id'])[0]['start_utc'] == '2024-03-30T00:00:00Z'
    unmapped = health_samsung.import_samsung_health(Path(m['unmapped']), enable_health=True)
    assert unmapped['issues']['by_code'] == {'samsung_table_not_mapped': 1} and unmapped['coverage']['complete'] is False
    with pytest.raises(ValueError, match='not a Samsung Health'):
        health_samsung.import_samsung_health(Path(m['not_samsung']), enable_health=True)


def test_fitbit_local_times_projections_and_log_types(env):
    m = env['m']['health']['fitbit']
    sleep = health_fitbit.import_fitbit(Path(m['sleep']), enable_health=True)
    main, nap = sessions(sleep['artifact_id'])
    assert main['ts_start'] is None and main['times'][0]['tz_status'] == 'unknown'  # Fitbit states no zone
    assert main['meta']['session']['span']['basis'] == 'wall_clock_difference_zone_unknown'
    stage = samples(sleep['artifact_id'], sample_class='sleep_stage')[0]
    assert (stage['start_local'], stage['end_local']) == ('2024-03-30T23:10:00', '2024-03-30T23:20:00')
    assert (nap['meta']['signal']['class'], nap['meta']['signal']['basis']) == ('declared', 'provider_user_edit')
    exercise = health_fitbit.import_fitbit(Path(m['exercise']), enable_health=True)
    walk, yoga = sessions(exercise['artifact_id'])
    start = walk['times'][0]
    assert start['local'] == '2024-03-30T18:00:00' and start['meta']['year_rule'] == 'two_digit_year_base:2000'
    end = walk['times'][1]
    assert end['local'] == '2024-03-30T18:30:00' and end['meta']['projection'] == 'start_plus_declared_duration_ms'
    distance = samples(exercise['artifact_id'], sample_type='exercise.distance')[0]
    assert distance['value_exact'] == '2.12345678901234567' and distance['unit'] == 'Kilometer'
    assert [t['kind'] for t in yoga['times']] == ['start'] and 'duration_unparsed' in yoga['meta']['issues']


def test_health_sample_types_are_searchable_and_one_sample_can_be_pinned(env):
    from server.app import db as dbmod, search
    from server.app.analysis import dossier, preregistration
    from server.tests.corpus import hypotheses as hyp_corpus
    health_apple.import_apple_health(Path(env['m']['health']['apple']['export']), enable_health=True)
    sample = rows('SELECT * FROM health_samples WHERE session_event_id IS NOT NULL ORDER BY id LIMIT 1')[0]
    content = rows("SELECT content FROM search_fts WHERE object_kind='event' AND object_id=?",
                   (sample['session_event_id'],))[0]['content']
    assert sample['sample_type'] in content
    assert ('event', sample['session_event_id']) in {(h['object_kind'], int(h['object_id']))
                                                     for h in search.search_page(f'"{sample["sample_type"]}"', 200)['results']}
    with dbmod.session() as conn:
        hid = preregistration.register_hypothesis(conn, hyp_corpus.definition_for(hyp_corpus.SCENARIOS['unknown_zone']()))['hypothesis_id']
        item = dossier.add_item(conn, hid, 'event', sample['session_event_id'], health_sample_id=sample['id'])
        assert item['locator']['record'] == {**item['locator']['record'], 'table': 'health_samples', 'id': sample['id'],
                                             'row_sha256': sample['row_sha256']}
        assert item['source_check'] == 'matches'

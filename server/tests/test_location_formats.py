"""Location importers (task L1): every format with adversarial synthetic fixtures (fictional coordinates only)."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
import tracemalloc
import types
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.importers import fit, geojson, gpx, kml, location
from server.app.location import formats, ingest, jsonstream
from server.tests.corpus import location as corpus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_settings = replace(dbmod.settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store')
    test_settings.store_dir.mkdir(parents=True)
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    manifest = corpus.build(tmp_path / 'corpus')
    return {'root': tmp_path / 'corpus', 'm': manifest, 'f': {k: Path(v) for k, v in manifest['files'].items()}}


def rows(sql, params=()):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


def events(event_type=None, artifact_id=None):
    sql, params = 'SELECT * FROM events WHERE 1=1', []
    if event_type:
        sql += ' AND event_type=?'
        params.append(event_type)
    if artifact_id:
        sql += ' AND artifact_id=?'
        params.append(artifact_id)
    out = rows(sql + ' ORDER BY id', params)
    for e in out:
        e['meta'] = json.loads(e['metadata_json'])
    return out


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ opt-in, originals, idempotence

def test_opt_in_required_before_anything_is_acquired(env):
    before = rows('SELECT COUNT(*) AS n FROM artifacts')[0]['n']
    for call in (lambda: location.import_location_history_v2(env['f']['Takeout/Location History/Records.json']),
                 lambda: gpx.import_gpx(env['f']['tracks/walk.gpx']),
                 lambda: kml.import_kml(env['f']['tracks/places.kml']),
                 lambda: geojson.import_geojson(env['f']['tracks/points.geojson']),
                 lambda: fit.import_fit(env['f']['tracks/ride.fit']),
                 lambda: formats.import_file(env['f']['tracks/walk.gpx'])):
        with pytest.raises(location.LocationOptInRequired):
            call()
    assert rows('SELECT COUNT(*) AS n FROM artifacts')[0]['n'] == before
    assert issubclass(location.LocationOptInRequired, PermissionError)


def test_records_json_raw_samples_issues_and_recorded_opt_in(env):
    result = location.import_location_history_v2(env['f']['Takeout/Location History/Records.json'], enable_location=True,
                                                 stream='phone')
    expected = env['m']['expected']
    assert result['format'] == 'records_json' and result['parser_version'] == '2'
    # valid points + one exact duplicate + the valid adversarial tail point (timestamp and timestampMs agree)
    assert result['fixes_observed'] == expected['records_valid_points'] + 1 + 1 + 2  # + 2 with unparsed times
    assert result['fix_observations_new'] == result['fixes_observed']
    assert result['fixes_new'] == result['fixes_observed'] - expected['records_duplicate_locators']
    for code, count in expected['records_invalid'].items():
        assert result['issues']['by_code'][code] == count, code
    assert result['issues']['by_code']['section_not_imported'] == 1  # "extraTopLevel"
    assert result['events'] == 0 and result['sampling_sessions'] >= 3  # samples never become one event each
    assert result['options']['enable_location'] is True and result['options']['reverse_geocoding'] is False
    overflow = [i for i in result['issues']['listed'] if i.get('possible_int32_overflow')]
    assert overflow, 'the int32-overflow-shaped value is flagged, never reinterpreted'
    fixes = rows('SELECT * FROM location_fixes ORDER BY t_us')
    assert {f['decimals'] for f in fixes} == {7} and {f['source_decimals'] for f in fixes} == {7}
    assert {f['signal_basis'] for f in fixes} == {'raw_sample'} and {f['fix_kind'] for f in fixes} == {'raw_position'}
    assert {f['stream_key'] for f in fixes} == {'phone/device:-11', 'phone/device:-12', 'phone'}
    unparsed = [f for f in fixes if f['t_us'] is None]
    assert len(unparsed) == 2 and {f['time_raw'] for f in unparsed} == {'2024-13-45T99:00:00Z', '99999999999999999999999'}
    ms_points = [f for f in fixes if f['time_raw'] and f['time_raw'].isdigit()]
    assert ms_points and all(f['tz_status'] == 'utc_epoch' for f in ms_points if f['t_us'] is not None)
    activity = [json.loads(f['extra_json']) for f in fixes if 'provider_activity_guesses' in f['extra_json']]
    assert activity and activity[0]['provider_activity_guesses'][0]['activity'][0]['type'] == 'STILL'
    sessions = events('location_samples')
    assert sessions and all(s['meta']['signal'] == {'class': 'passive', 'basis': 'raw_sample',
                                                     'provider': 'google_location_history',
                                                     'device_or_account': s['meta']['signal']['device_or_account']}
                            for s in sessions)
    assert all(s['meta']['opt_in']['enable_location'] for s in sessions)
    run = rows("SELECT details_json FROM audit_log WHERE action='import_run' ORDER BY id DESC LIMIT 1")[0]
    assert json.loads(run['details_json'])['options']['enable_location'] is True


def test_reimport_is_idempotent_and_originals_are_unchanged(env):
    before = {name: sha(path) for name, path in env['f'].items()}
    first = location.import_location_history_v2(env['f']['Takeout/Location History/Records.json'], enable_location=True)
    counts = {t: rows(f'SELECT COUNT(*) AS n FROM {t}')[0]['n'] for t in
              ('location_fixes', 'location_fix_observations', 'events', 'event_times', 'location_imports')}
    second = location.import_location_history_v2(env['f']['Takeout/Location History/Records.json'], enable_location=True)
    assert second['import_reused'] and second['location_import_id'] == first['location_import_id']
    assert second['fixes_new'] == 0 and second['fix_observations_new'] == 0
    assert {t: rows(f'SELECT COUNT(*) AS n FROM {t}')[0]['n'] for t in counts} == counts
    assert rows('SELECT COUNT(*) AS n FROM location_import_runs')[0]['n'] == 2  # every run is recorded
    gpx.import_gpx(env['f']['tracks/walk.gpx'], enable_location=True)
    gpx.import_gpx(env['f']['tracks/walk.gpx'], enable_location=True)
    kml.import_kml(env['f']['tracks/places.kmz'], enable_location=True)
    kml.import_kml(env['f']['tracks/places.kmz'], enable_location=True)
    assert {name: sha(path) for name, path in env['f'].items()} == before
    store = Path(evidence.settings.store_dir)
    assert not any(p for p in env['root'].rglob('*') if p.name.startswith(('.capture-', 'payload')))
    assert env['root'] not in store.parents


def test_overlapping_exports_share_samples_within_one_stream_and_keep_both_observations(env):
    first = location.import_location_history_v2(env['f']['Takeout/Location History/Records.json'], enable_location=True,
                                                stream='account-a')
    second = location.import_location_history_v2(env['f']['Takeout-2/Location History/Records.json'],
                                                 enable_location=True, stream='account-a')
    expected = env['m']['expected']
    assert second['fixes_observed'] == expected['overlap_shared_points'] + expected['overlap_new_points']
    assert second['fixes_new'] == expected['overlap_new_points']
    assert second['fix_observations_new'] == second['fixes_observed']
    shared = rows('''SELECT fix_id, COUNT(DISTINCT import_id) AS n FROM location_fix_observations GROUP BY fix_id
                     HAVING n > 1''')
    assert len(shared) == expected['overlap_shared_points']
    # a different (default) stream is never merged
    third = location.import_location_history_v2(env['f']['Takeout-2/Location History/Records.json'], enable_location=True)
    assert third['fixes_new'] == third['fixes_observed']
    assert first['stream'] == 'account-a' and third['stream'].startswith('source:')


def test_store_decimals_rounds_before_hashing_and_is_recorded(env):
    exact = location.import_location_history_v2(env['f']['Takeout-2/Location History/Records.json'],
                                                enable_location=True, stream='s')
    coarse = location.import_location_history_v2(env['f']['Takeout-2/Location History/Records.json'],
                                                 enable_location=True, stream='s', store_decimals=3)
    assert coarse['location_import_id'] != exact['location_import_id']
    assert coarse['options']['parameters']['store_decimals'] == 3
    stored = rows('SELECT lat, lon, decimals, source_decimals, fix_sha256 FROM location_fixes WHERE decimals=3')
    assert stored and all(round(r['lat'], 3) == r['lat'] and r['source_decimals'] == 7 for r in stored)
    precise = {r['fix_sha256'] for r in rows('SELECT fix_sha256 FROM location_fixes WHERE decimals=7')}
    assert not precise & {r['fix_sha256'] for r in stored}
    with pytest.raises(ValueError):
        location.import_location_history_v2(env['f']['Takeout-2/Location History/Records.json'], enable_location=True,
                                            store_decimals=12)


def test_semantic_location_history_provider_inference_layers(env):
    result = location.import_location_history_v2(
        env['f']['Takeout/Location History/Semantic Location History/2024/2024_JANUARY.json'], enable_location=True)
    assert result['format'] == 'semantic_location_history'
    visits, segments = events('location_visit'), events('location_segment')
    assert len(visits) == 4 and len(segments) == 1  # home, its child visit, work, invalid record kept with issues
    home = next(v for v in visits if v['subject'] == 'TYPE_HOME')
    meta = home['meta']
    assert meta['signal']['basis'] == 'provider_user_edit' and meta['user_edit'] == {'editConfirmationStatus': 'CONFIRMED',
                                                                                       'flagged': True}
    assert meta['provider_labels']['placeConfidence'] == 'HIGH_CONFIDENCE'
    assert meta['provider_labels']['location.locationConfidence'] == 91.5
    assert meta['provider_place']['place_id'] == 'synthetic-place-id-home'
    assert meta['provider_place']['resolution'] == 'provider_identifier_kept_raw_never_resolved'
    assert meta['provider_place']['address'].startswith('Fikcyjna')
    assert meta['location'] == {'lat': 0.001, 'lon': 0.002, 'decimals': 7, 'source_decimals': 7, 'basis': 'e7_integer'}
    assert meta['other_candidates'][0]['provider_labels']['locationConfidence'] == 3.2
    assert home['confidence'] is None and 'not an observation of a person' in meta['semantics']
    work = next(v for v in visits if v['meta']['provider_place'] and v['meta']['provider_place'].get('place_id') ==
                'synthetic-place-id-work')
    assert work['meta']['signal']['basis'] == 'provider_inference' and work['meta']['user_edit']['flagged'] is False
    assert result['issues']['by_code']['edit_status_unrecognised'] == 1
    child = next(v for v in visits if v['meta']['locator']['index'] == '0/childVisits[0]')
    times = rows('SELECT * FROM event_times WHERE event_id=? ORDER BY kind', (child['id'],))
    assert {t['tz_status'] for t in times} == {'utc_epoch'}  # startTimestampMs variant
    segment = segments[0]
    assert segment['subject'] == 'IN_PASSENGER_VEHICLE' and segment['meta']['provider_activities'][0]['provider_probability'] == 87.5
    assert segment['meta']['provider_distance_m'] == 3600
    assert segment['meta']['waypoint_path']['points_in_export'] == 2
    path_fixes = rows("SELECT fix_kind, signal_basis FROM location_fixes ORDER BY id")
    assert ('waypoint_path', 'provider_inference') in {(r['fix_kind'], r['signal_basis']) for r in path_fixes}
    assert ('simplified_raw_path', 'raw_sample') in {(r['fix_kind'], r['signal_basis']) for r in path_fixes}
    invalid = [v for v in visits if 'coordinate_out_of_range' in v['meta']['issues']]
    assert len(invalid) == 1 and invalid[0]['ts_start'] is None and invalid[0]['meta']['location'] is None


def test_provider_place_text_can_be_omitted(env):
    location.import_location_history_v2(
        env['f']['Takeout/Location History/Semantic Location History/2024/2024_JANUARY.json'], enable_location=True,
        provider_place_text='omit')
    home = next(v for v in events('location_visit') if v['subject'] == 'TYPE_HOME')
    assert 'address' not in home['meta']['provider_place'] and home['meta']['provider_place']['text_fields'] == \
        'omitted_by_parameter'
    assert 'Fikcyjna' not in home['metadata_json']


def test_device_timeline_raw_signals_offsets_dst_and_profile(env):
    result = location.import_location_history_v2(env['f']['device/Timeline.json'], enable_location=True)
    assert result['format'] == 'timeline_device'
    assert result['signals_observed'] == 2 and result['issues']['by_code']['raw_signal_unrecognised'] == 1
    assert result['issues']['by_code']['provider_offset_differs_from_timestamp_offset'] == 1
    visit = events('location_visit')[0]
    times = {t['kind']: t for t in rows('SELECT * FROM event_times WHERE event_id=?', (visit['id'],))}
    # DST change inside the visit: start +01:00, end +02:00; both instants fixed by the source
    assert times['start']['utc'] == '2024-03-31T00:30:00Z' and times['end']['utc'] == '2024-03-31T01:30:00Z'
    assert json.loads(times['start']['metadata_json'])['provider_utc_offset_minutes'] == 60
    assert json.loads(times['end']['metadata_json'])['provider_utc_offset_minutes'] == 120
    assert visit['meta']['location']['source_decimals'] == 7 and visit['meta']['location']['basis'] == 'degree_string'
    segments = events('location_segment')
    assert {s['subject'] for s in segments} == {'IN_PASSENGER_VEHICLE', 'timeline_path', 'timeline_memory'}
    signals = rows('SELECT signal_kind, signal_basis, summary_json FROM location_signals ORDER BY id')
    wifi = next(s for s in signals if s['signal_kind'] == 'wifi_scan')
    assert json.loads(wifi['summary_json'])['devices'] == 2 and '1000001' not in wifi['summary_json']
    assert next(s for s in signals if s['signal_kind'] == 'activity_record')['signal_basis'] == 'provider_inference'
    raw = rows("SELECT * FROM location_fixes WHERE fix_kind='raw_position' ORDER BY t_us")
    assert len(raw) == 2 and raw[0]['provider_source'] == 'GPS' and raw[0]['accuracy_m'] == 9
    assert raw[0]['time_zone'] == '+02:00' and raw[0]['time_utc'] == '2024-03-31T07:00:00Z'
    path = rows("SELECT signal_basis FROM location_fixes WHERE fix_kind='timeline_path'")
    assert len(path) == 2 and {p['signal_basis'] for p in path} == {'provider_inference'}
    profile = events('location_profile')[0]
    assert profile['ts_start'] is None and len(profile['meta']['provider_frequent_places']) == 2
    assert profile['meta']['provider_frequent_places'][0]['provider_label'] == 'HOME'


def test_ios_location_history_string_values_and_minute_offsets(env):
    result = location.import_location_history_v2(env['f']['ios/location-history.json'], enable_location=True)
    assert result['format'] == 'ios_location_history'
    assert result['issues']['by_code']['segment_not_an_object'] == 1
    visits = events('location_visit')
    good = next(v for v in visits if v['ts_start'])
    assert good['meta']['location']['basis'] == 'geo_uri' and good['meta']['location']['source_decimals'] == 6
    assert good['meta']['provider_labels']['topCandidate.probability'] == '0.91'  # provider label, kept as given
    assert good['ts_start'] == '2024-02-01T07:00:00Z'
    path = rows("SELECT time_utc, time_precision FROM location_fixes WHERE fix_kind='timeline_path' AND t_us IS NOT NULL "
                "ORDER BY t_us")
    assert rows("SELECT COUNT(*) AS n FROM location_fixes WHERE fix_kind='timeline_path' AND t_us IS NULL")[0]['n'] == 1
    assert [p['time_utc'] for p in path] == ['2024-02-01T08:10:00Z', '2024-02-01T08:25:00Z']
    assert {p['time_precision'] for p in path} == {'minute'}
    assert result['issues']['by_code']['time_unparsed'] >= 2  # 'x' offset and 'not-a-time'


def test_truncated_and_malformed_json_are_partial_not_crashes(env):
    result = location.import_location_history_v2(env['f']['Takeout/Location History/Records-truncated.json'],
                                                 enable_location=True)
    assert result['coverage']['complete'] is False and result['json_stream']['truncated'] is True
    assert result['fixes_observed'] > 0
    bad = env['root'] / 'bad.json'
    bad.write_text('{"locations": [{"latitudeE7": 1, "longitudeE7": 2, "timestamp": "2024-01-01T00:00:00Z"}, '
                   '{"latitudeE7": tru}, {"latitudeE7": 3, "longitudeE7": 4, "timestamp": "2024-01-01T00:01:00Z"}]}')
    result = location.import_location_history_v2(bad, enable_location=True)
    assert result['fixes_observed'] == 2 and result['coverage']['complete'] is False
    assert result['json_stream']['skipped_malformed'] == 1
    not_json = env['root'] / 'not.json'
    not_json.write_text('hello')
    result = location.import_location_history_v2(not_json, enable_location=True)
    assert result['json_stream']['not_json'] and result['coverage']['complete'] is False


def test_json_stream_is_bounded_on_a_large_records_file(env):
    big = env['root'] / 'big-Records.json'
    point = '{"latitudeE7": %d, "longitudeE7": 20000, "accuracy": 10, "timestampMs": "%d"}'
    with big.open('w') as handle:
        handle.write('{"locations": [')
        for i in range(60_000):
            handle.write((',' if i else '') + point % (10000 + i % 500, 1705300000000 + i * 1000))
        handle.write(']}')
    size = big.stat().st_size
    assert size > 4_000_000
    report = jsonstream.StreamReport()
    tracemalloc.start()
    with big.open('rb') as handle:
        count = sum(1 for _ in jsonstream.iter_document(handle, chunk_bytes=256 * 1024, report=report))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert count == 60_000 and report.complete
    assert peak < size / 2, f'peak {peak} for a {size}-byte file'
    oversized = jsonstream.StreamReport()
    with big.open('rb') as handle:
        assert sum(1 for _ in jsonstream.iter_document(handle, max_element_bytes=40, report=oversized)) == 0
    assert oversized.skipped_large == 60_000 and not oversized.complete


def test_json_stream_handles_chunk_boundaries_strings_and_numbers():
    doc = '{"a": "x\\"]},y", "locations": [1.50, {"s": "]}\\\\"}, -0.25e-3, [1, [2]], null], "b": {"c": [1]}}'
    for chunk in (1, 2, 3, 7, 64):
        report = jsonstream.StreamReport()
        got = list(jsonstream.iter_document(io.BytesIO(doc.encode()), chunk_bytes=chunk, report=report))
        assert [g[:2] for g in got] == [('a', None), ('locations', 0), ('locations', 1), ('locations', 2),
                                        ('locations', 3), ('locations', 4), ('b', None)], chunk
        assert got[1][2] == 1.5 and got[1][2].text == '1.50' and got[3][2].text == '-0.25e-3'
        assert report.complete
    report = jsonstream.StreamReport()
    assert [v for _, _, v in jsonstream.iter_document(io.BytesIO('﻿[1, 2]'.encode()), report=report)] == [1, 2]
    report = jsonstream.StreamReport()
    values = [v for _, _, v in jsonstream.iter_document(io.BytesIO(b'[NaN, Infinity, 1]'), report=report)]
    assert values[0] != values[0] and values[1] == float('inf')
    report = jsonstream.StreamReport()
    list(jsonstream.iter_document(io.BytesIO(b'[1, 2] [3]'), report=report))
    assert report.trailing_data


# ------------------------------------------------------------------ GPX / KML / GeoJSON / FIT

def test_gpx_tracks_routes_waypoints_and_statistics(env):
    result = gpx.import_gpx(env['f']['tracks/walk.gpx'], enable_location=True)
    assert result['events_by_type'] == {'location_route': 1, 'location_track': 1, 'location_waypoint': 2}
    assert result['issues']['by_code'] == {'coordinate_missing': 1, 'coordinate_not_finite': 1}
    track = events('location_track')[0]
    stats = track['meta']['track']
    assert stats['points'] == 16 and stats['segments'] == 2 and stats['time_order_violations'] == 1
    assert stats['computed_by'] == 'code' and stats['distance_m'] > 0 and stats['duration_s'] == 3540.0
    assert track['meta']['signal']['class'] == 'passive' and track['meta']['producer']['creator'] == 'synthetic-logger'
    route = events('location_route')[0]
    assert route['meta']['signal'] == {'class': 'declared', 'basis': 'declared', 'provider': 'gpx_file',
                                       'device_or_account': route['meta']['signal']['device_or_account']}
    waypoint = next(e for e in events('location_waypoint') if e['ts_start'])
    assert waypoint['subject'] == 'Start & meta' and waypoint['meta']['declared_text']['desc'] == '<b>not markup</b>'
    fixes = rows("SELECT * FROM location_fixes WHERE fix_kind='track_point' ORDER BY id")
    assert fixes[0]['source_decimals'] == 7 and fixes[-1]['source_decimals'] == 4
    extra = json.loads(fixes[0]['extra_json'])
    assert extra['hdop'] == '1.2' and extra['extensions_raw'] == '<hr>90</hr>'
    assert fixes[0]['altitude_m'] == 5.0


def test_gpx_versions_naive_times_truncation_and_refused_doctype(env):
    old = gpx.import_gpx(env['f']['tracks/old.gpx'], enable_location=True)
    assert old['events_by_type'] == {'location_track': 1} and old['sampling_sessions'] == 0
    truncated = gpx.import_gpx(env['f']['tracks/truncated.gpx'], enable_location=True)
    assert truncated['coverage']['complete'] is False and 'xml_truncated' in truncated['issues']['by_code']
    entity = gpx.import_gpx(env['f']['tracks/entity.gpx'], enable_location=True)
    assert entity['issues']['by_code'].get('xml_doctype_refused') == 1 and entity['fixes_observed'] == 0
    naive = env['root'] / 'naive.gpx'
    naive.write_text('<gpx version="1.1"><trk><trkseg><trkpt lat="0.001" lon="0.002"><time>2024-05-01T10:00:00</time>'
                     '</trkpt></trkseg></trk></gpx>')
    gpx.import_gpx(naive, enable_location=True)
    unknown = rows("SELECT tz_status, t_us FROM location_fixes WHERE time_raw='2024-05-01T10:00:00'")
    assert unknown == [{'tz_status': 'unknown', 't_us': None}]
    gpx.import_gpx(naive, enable_location=True, gpx_naive_time='utc_per_schema')
    utc = rows("SELECT tz_status, time_utc FROM location_fixes WHERE time_raw='2024-05-01T10:00:00' AND t_us IS NOT NULL")
    assert utc == [{'tz_status': 'explicit_offset', 'time_utc': '2024-05-01T10:00:00Z'}]
    other = env['root'] / 'other.xml'
    other.write_text('<kml/>')
    assert gpx.import_gpx(other, enable_location=True)['issues']['by_code'] == {'not_a_gpx_document': 1}


def test_kml_and_kmz_tracks_paths_points(env):
    plain = kml.import_kml(env['f']['tracks/places.kml'], enable_location=True)
    packed = kml.import_kml(env['f']['tracks/places.kmz'], enable_location=True)
    assert plain['format'] == 'kml' and packed['format'] == 'kmz'
    for result in (plain, packed):
        assert result['events_by_type'] == {'location_route': 1, 'location_track': 1, 'location_waypoint': 1}
        assert result['issues']['by_code'] == {'coordinate_unparsed': 1}
    assert packed['kml']['kmz_member_used'] == 'doc.kml' and packed['kml']['kmz_members'] == 2
    track = events('location_track')[0]
    assert track['ts_start'] == '2024-01-18T10:00:00Z' and track['ts_end'] == '2024-01-18T10:03:00Z'
    assert track['meta']['track']['points'] == 4
    point = events('location_waypoint')[0]
    assert point['ts_start'] == '2024-01-18T09:00:00Z'  # TimeStamp with +01:00
    route = events('location_route')[0]
    assert route['ts_start'] == '2024-01-18T11:00:00Z' and route['meta']['track']['points'] == 3
    gx = rows("SELECT lat, lon, signal_basis FROM location_fixes WHERE fix_kind='track_point' ORDER BY t_us LIMIT 1")[0]
    assert (gx['lat'], gx['lon'], gx['signal_basis']) == (0.001, 0.03, 'raw_sample')  # gx:coord is "lon lat alt"
    locator = rows("SELECT locator FROM location_fix_observations WHERE locator LIKE 'doc.kml!/%' LIMIT 1")
    assert locator


def test_kmz_without_kml_and_kml_coordinate_chunks(env):
    empty = env['root'] / 'empty.kmz'
    with zipfile.ZipFile(empty, 'w') as archive:
        archive.writestr('readme.txt', 'nothing')
    result = kml.import_kml(empty, enable_location=True)
    assert result['issues']['by_code'] == {'kmz_without_kml': 1} and result['coverage']['complete'] is False
    many = ' '.join(f'{0.002 + i * 1e-5:.5f},{0.001:.5f},0' for i in range(5000))
    big = env['root'] / 'long.kml'
    big.write_text(f'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><LineString><coordinates>{many}'
                   '</coordinates></LineString></Placemark></kml>')
    result = kml.import_kml(big, enable_location=True)
    assert result['fixes_observed'] == 5000 and result['issues']['total'] == 0


def test_geojson_time_properties_are_parameters(env):
    default = geojson.import_geojson(env['f']['tracks/points.geojson'], enable_location=True)
    assert default['geojson']['points_untimed'] == 6 and default['geojson']['points_timed'] == 0  # 'ts' not a default
    custom = geojson.import_geojson(env['f']['tracks/points.geojson'], enable_location=True,
                                    options={'time_properties': ['ts']})
    assert custom['geojson']['points_timed'] == 6 and custom['geojson']['areas_skipped'] == 1
    assert custom['options']['parameters']['format_options']['time_properties'] == ['ts']
    assert custom['issues']['by_code']['coordinate_out_of_range'] == 1
    tracks = [e for e in events('location_track')]
    assert tracks and tracks[-1]['ts_start'] == '2024-01-19T09:00:00Z' and tracks[-1]['meta']['track']['points'] == 3
    with pytest.raises(ValueError):
        geojson.import_geojson(env['f']['tracks/points.geojson'], enable_location=True, options={'nope': 1})
    numeric = env['root'] / 'numeric.geojson'
    numeric.write_text(json.dumps({'type': 'Feature', 'properties': {'time': 1705300000},
                                   'geometry': {'type': 'Point', 'coordinates': [0.002, 0.001]}}))
    unknown_unit = geojson.import_geojson(numeric, enable_location=True)
    assert unknown_unit['issues']['by_code'] == {'time_unit_unknown': 1}
    seconds = geojson.import_geojson(numeric, enable_location=True, options={'epoch_unit': 's'})
    assert seconds['geojson']['points_timed'] == 1


def test_fit_adapter_unavailable_without_fitparse_and_used_when_present(env, monkeypatch):
    assert fit.status()['status'] == ('unavailable' if 'fitparse' not in sys.modules else 'available')
    before = rows('SELECT COUNT(*) AS n FROM artifacts')[0]['n']
    with pytest.raises(ingest.FormatUnavailable):
        fit.import_fit(env['f']['tracks/ride.fit'], enable_location=True)
    assert rows('SELECT COUNT(*) AS n FROM artifacts')[0]['n'] == before
    from datetime import datetime

    class Message:
        def __init__(self, name, values):
            self.name, self._values = name, values

        def get_values(self):
            return self._values

    class FitFile:
        def __init__(self, path):
            self.path = path

        def get_messages(self):
            yield Message('session', {'total_distance': 12.5, 'sport': 'running'})
            for i in range(3):
                yield Message('record', {'position_lat': int(0.001 * 2 ** 31 / 180) + i, 'position_long': int(0.002 * 2 ** 31 / 180),
                                         'timestamp': datetime(2024, 1, 20, 7, 0, i), 'altitude': 3.0})
            yield Message('record', {'heart_rate': 90})
    fake = types.ModuleType('fitparse')
    fake.FitFile = FitFile
    fake.__version__ = 'fake-for-tests'
    monkeypatch.setitem(sys.modules, 'fitparse', fake)
    result = fit.import_fit(env['f']['tracks/ride.fit'], enable_location=True)
    assert result['fixes_observed'] == 3 and result['issues']['by_code'] == {'record_without_position': 1}
    track = events('location_track')[-1]
    assert track['ts_start'] == '2024-01-20T07:00:00Z' and track['meta']['device_sessions_raw'][0]['sport'] == 'running'
    assert abs(fit.semicircles_to_degrees(2 ** 31) - 180.0) < 1e-12


def test_format_detection_by_bytes_not_names(env):
    f = env['f']
    renamed = env['root'] / 'track.json'
    renamed.write_bytes(f['tracks/walk.gpx'].read_bytes())
    assert formats.detect(renamed)['format'] == 'gpx'
    assert formats.detect(f['tracks/places.kmz']) == {'format': 'kml', 'variant': 'kmz', 'reason': 'zip with a .kml member'}
    assert formats.detect(f['tracks/ride.fit'])['format'] == 'fit'
    assert formats.detect(f['ios/location-history.json'])['variant'] == 'ios_location_history'
    assert formats.detect(f['device/Timeline.json'])['variant'] == 'timeline_device'
    assert formats.detect(f['tracks/points.geojson'])['format'] == 'geojson'
    assert formats.detect(f['tracks/entity.gpx'])['format'] == 'gpx'  # then refused by the parser
    assert formats.detect(f['calendar/meetings.ics'])['format'] is None
    with pytest.raises(formats.UnknownFormat):
        formats.import_file(f['calendar/meetings.ics'], enable_location=True)


def test_v1_importer_still_works_and_its_rows_are_never_reinterpreted(env):
    path = env['f']['Takeout-2/Location History/Records.json']
    v1 = location.import_location_history(path, enable_location=True)
    v1_rows = rows("SELECT id, metadata_json FROM events WHERE event_type='presence' ORDER BY id")
    v2 = location.import_location_history_v2(path, enable_location=True)
    assert v1['parser_version'] == '1' and v2['parser_version'] == '2' and v1['artifact_id'] == v2['artifact_id']
    assert rows("SELECT id, metadata_json FROM events WHERE event_type='presence' ORDER BY id") == v1_rows
    assert all(json.loads(r['metadata_json'])['parser']['version'] == '1' for r in v1_rows)


def test_location_tables_are_append_only(env):
    location.import_location_history_v2(env['f']['Takeout-2/Location History/Records.json'], enable_location=True)
    with dbmod.session() as db:
        for sql in ('UPDATE location_fixes SET lat=0', 'DELETE FROM location_fix_observations',
                    'UPDATE location_imports SET format=1', 'DELETE FROM location_import_runs'):
            with pytest.raises(sqlite3.DatabaseError, match='append-only'):
                db.execute(sql)

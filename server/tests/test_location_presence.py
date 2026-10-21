"""Geometry, private places, accuracy-aware geofences, presence runs and correlation hypotheses (task L1)."""
from __future__ import annotations

import json
import math
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.importers import call_log, ics, location
from server.app.location import correlate, geo, places, presence
from server.tests.corpus import location as corpus

M = geo.METERS_PER_DEGREE


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_settings = replace(dbmod.settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store')
    test_settings.store_dir.mkdir(parents=True)
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    manifest = corpus.build(tmp_path / 'corpus')
    files = {k: Path(v) for k, v in manifest['files'].items()}
    location.import_location_history_v2(files['Takeout/Location History/Records.json'], enable_location=True,
                                        stream='phone')
    return {'m': manifest, 'f': files, 'places': places.load_places(manifest['places_path']), 'tmp': tmp_path}


def rows(sql, params=()):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


# ------------------------------------------------------------------ geometry

def test_haversine_antimeridian_poles_and_rounding_error():
    assert geo.haversine_m(0, 0, 1, 0) == pytest.approx(M, rel=1e-12)
    assert geo.haversine_m(0, 179.9995, 0, -179.9997) == pytest.approx(0.0008 * M, rel=1e-6)
    assert geo.haversine_m(89.9995, 10, 89.9995, -170) == pytest.approx(0.001 * M, rel=1e-6)  # across the pole
    assert geo.haversine_m(90, 0, 90, 123) == pytest.approx(0, abs=1e-6)
    assert geo.haversine_m(0, 0, 0, 180) == pytest.approx(math.pi * geo.EARTH_RADIUS_M)
    assert geo.rounding_error_m(0.0, 7, 7) == 0.0
    half = 0.5e-3 * M
    assert geo.rounding_error_m(0.0, 3, 7) == pytest.approx(math.hypot(half, half))
    assert geo.rounding_error_m(89.9999, 3, 7) == pytest.approx(half, rel=1e-6)
    box = geo.bbox([(0.5, 179.9), (0.4, -179.9)])
    assert box['crosses_antimeridian'] and box['west'] == 179.9 and box['east'] == -179.9
    assert geo.bbox([(0, 1), (1, 2)])['crosses_antimeridian'] is False
    assert geo.path_length_m([(0, 0), (0, 1), (0, 2)]) == pytest.approx(2 * M, rel=1e-9)


def test_coordinate_parsing_precision_and_refusals():
    assert geo.parse_pair('48.1234567°, 11.5°').decimals == 7
    assert geo.parse_pair('geo:0.001000,0.002000;u=35').basis == 'geo_uri'
    assert geo.decimals_of('1.50') == 2 and geo.decimals_of('1.5e-3') == 4 and geo.decimals_of('12') == 0
    for bad, code in (('91.0°, 0°', 'coordinate_out_of_range'), ('abc', 'coordinate_unparsed'),
                      (None, 'coordinate_missing')):
        with pytest.raises(geo.CoordinateError) as info:
            geo.parse_pair(bad)
        assert info.value.code == code
    for lat, lon, code in ((float('nan'), 0, 'coordinate_not_finite'), ('nan', '0', 'coordinate_not_finite'),
                           ('x', '0', 'coordinate_unparsed'), (None, 1, 'coordinate_missing'),
                           (0, 180.0000001, 'coordinate_out_of_range')):
        with pytest.raises(geo.CoordinateError) as info:
            geo.from_values(lat, lon)
        assert info.value.code == code
    with pytest.raises(geo.CoordinateError) as info:
        geo.from_e7(4294967296 + 10000, 20000)
    assert info.value.code == 'coordinate_out_of_range' and info.value.details['possible_int32_overflow'] is True
    assert geo.round_to(geo.from_values('0.123456', '0.1'), 3) == (0.123, 0.1, 3)
    assert geo.round_to(geo.from_values('0.12', '0.1'), 5)[2] == 2  # never more decimals than stated


# ------------------------------------------------------------------ places and classification

def _set(*items):
    return places.parse_places({'schema': places.SCHEMA, 'version': 't', 'places': list(items)})


def test_places_file_validation_hash_and_summary(env):
    loaded = env['places']
    assert loaded.origin == 'file' and loaded.file_sha256 and loaded.version == 'synthetic-2024-01'
    again = places.parse_places(json.loads(Path(env['m']['places_path']).read_text()))
    assert again.sha256 == loaded.sha256 and again.origin == 'inline' and again.file_sha256 is None
    summary = places.summary(loaded)
    assert {(p['a'], p['b']) for p in summary['overlapping_pairs']} == {('work', 'shop')}
    assert 'label' not in json.dumps(summary['overlapping_pairs'])
    good = {'id': 'a', 'lat': 0, 'lon': 0, 'radius_m': 10}
    for bad, message in (({'schema': 'x'}, 'schema'), ({'places': []}, 'non-empty'),
                         ({'places': [good, good]}, 'duplicate'), ({'places': [{**good, 'lat': float('nan')}]}, 'finite'),
                         ({'places': [{**good, 'radius_m': 0}]}, 'radius_m'), ({'places': [{**good, 'x': 1}]}, 'unknown'),
                         ({'places': [{**good, 'lat': 91}]}, 'range'), ({'places': [{**good, 'id': '../x'}]}, 'id'),
                         ({'places': [{**good, 'lat': True}]}, 'finite')):
        with pytest.raises(places.PlacesError, match=message):
            places.parse_places({'schema': places.SCHEMA, 'version': 'v', **bad})
    nan_file = env['tmp'] / 'nan.json'
    nan_file.write_text('{"schema": "ageds.location-places.v1", "places": [{"id": "a", "lat": NaN, "lon": 0, "radius_m": 1}]}')
    with pytest.raises(places.PlacesError):
        places.load_places(nan_file)


def test_accuracy_aware_classification_boundaries():
    centre = places.Place('p', 'P', 0.0, 0.0, 100.0)
    north = lambda metres: metres / M  # noqa: E731

    def status(d, accuracy=None, **kw):
        return places.classify(centre, north(d), 0.0, accuracy_m=accuracy, **kw)['status']
    assert status(50, 50 - 1e-6) == 'inside'
    assert status(50, 50 + 1e-6) == 'uncertain'
    assert status(150, 50) == 'uncertain'  # accuracy circle touches the boundary from outside
    assert status(150 + 1e-6, 50) == 'outside'
    assert status(99.999) == 'inside' and status(100.001) == 'outside'  # accuracy not stated: the point itself
    assert places.classify(centre, north(99), 0, accuracy_m=None)['accuracy_basis'] == 'not_stated'
    assert status(95, None, params=places.MatchParams(missing_accuracy_m=10)) == 'uncertain'
    assert status(41, 30, params=places.MatchParams(accuracy_multiplier=2.0)) == 'uncertain'
    assert status(39, 30, params=places.MatchParams(accuracy_multiplier=2.0)) == 'inside'
    assert status(10, 0, rounding_m=geo.rounding_error_m(0, 3, 7)) == 'inside'
    assert status(30, 0, rounding_m=geo.rounding_error_m(0, 3, 7)) == 'uncertain'  # coarse storage widens
    wide = places.Place('w', 'W', 0.5, 179.9995, 200.0)
    assert places.classify(wide, 0.5, -179.9997)['status'] == 'inside'
    pole = places.Place('n', 'N', 89.9995, 10.0, 300.0)
    assert places.classify(pole, 89.9995, -170.0)['status'] == 'inside'


def test_match_points_api_for_other_areas(env):
    out = places.match_points([{'id': 'photo-1', 'lat': 0.0010, 'lon': 0.0020, 'accuracy_m': 5},
                               {'id': 'photo-2', 'lat': 'x', 'lon': 0},
                               {'id': 'photo-3', 'lat': 10.0, 'lon': 10.0},
                               {'id': 'photo-4', 'lat': 0.0010, 'lon': 0.0020, 'decimals': 2, 'source_decimals': 7}],
                              env['places'])
    assert [m['place_id'] for m in out[0]['matches']] == ['home'] and out[0]['matches'][0]['status'] == 'inside'
    assert out[1] == {'id': 'photo-2', 'error': 'coordinate_missing'} or out[1]['error']
    assert out[2]['matches'] == []
    assert out[3]['matches'][0]['status'] == 'uncertain' and out[3]['matches'][0]['rounding_m'] > 700
    everything = places.match_points([{'lat': 0, 'lon': 0}], env['places'], include_outside=True)
    assert len(everything[0]['matches']) == len(env['places'].places)
    keys = [s['key'] for s in places.geotag_sources()]
    assert 'location_fixes' in keys and 'loader' not in places.geotag_sources()[0]
    with pytest.raises(ValueError):
        places.register_geotag_source('location_fixes', label='x', signal_class='passive', basis='raw_sample',
                                      loader=lambda *a: [])
    with pytest.raises(ValueError):
        places.register_geotag_source('Bad Key', label='x', signal_class='passive', basis='raw_sample', loader=None)
    with dbmod.session() as db:
        loaded = list(places.GEOTAG_SOURCES['location_fixes']['loader'](db, None, None, 5))
    assert len(loaded) == 5 and {'t_us', 'lat', 'lon', 'ref'} <= set(loaded[0])


# ------------------------------------------------------------------ presence runs

def _intervals(run_id):
    return presence.get_run(run_id)['intervals']


def test_presence_intervals_bounds_and_statuses(env):
    run = presence.run_presence(env['places'], actor='tester')
    assert run['reused'] is False and run['rule'] == 'place_presence.v1' and not run['published']
    assert run['place_set']['sha256'] == env['places'].sha256 and run['place_set']['version'] == 'synthetic-2024-01'
    intervals = _intervals(run['run_id'])
    home = [i for i in intervals if i['place_id'] == 'home']
    assert len(home) == 1
    home = home[0]
    start, end = env['m']['expected']['home_inside_window']
    assert home['start_utc'] == start and home['status'] == 'inside'
    assert home['end_utc'] == '2024-01-15T08:57:00Z' and home['uncertain_fixes'] == 1  # low-accuracy last sample
    assert home['outer_start_utc'] is None and home['outer_end_utc'] == '2024-01-15T09:00:00Z'
    assert home['evidence']['ended_by'] == 'outside_fix'
    work = [i for i in intervals if i['place_id'] == 'work']
    assert len(work) == env['m']['expected']['work_intervals']
    assert work[0]['evidence']['ended_by'] == 'gap' and work[0]['outer_end_utc'] == '2024-01-15T14:00:00Z'
    assert work[0]['uncertain_fixes'] == 1 and work[0]['inside_fixes'] == 16
    dateline = [i for i in intervals if i['place_id'] == 'dateline']
    assert len(dateline) == 1 and dateline[0]['stream_key'] == 'phone/device:-12' and dateline[0]['fixes'] == 4
    shop = [i for i in intervals if i['place_id'] == 'shop' and i['status'] == 'uncertain']
    assert shop, 'overlapping places are matched independently'
    assert run['counts']['dropped'] == {'shorter_than_min_dwell': 1}  # the lone evening sample at home
    assert all(i['evidence']['accuracy_basis'] == {'stated': i['fixes']} for i in intervals)


def test_presence_parameters_window_streams_and_reuse(env):
    base = presence.run_presence(env['places'], actor='a')
    same = presence.run_presence(env['places'], actor='b')
    assert same['reused'] and same['run_id'] == base['run_id']
    merged = presence.run_presence(env['places'], params={'max_gap_s': 3 * 3600}, actor='a')
    work = [i for i in _intervals(merged['run_id']) if i['place_id'] == 'work']
    assert len(work) == 1 and work[0]['start_utc'] == '2024-01-15T09:30:00Z' and work[0]['end_utc'] == '2024-01-15T15:00:00Z'
    single = presence.run_presence(env['places'], params={'min_dwell_s': 0}, actor='a')
    assert not single['counts']['dropped'] and single['counts']['intervals'] == base['counts']['intervals'] + 1
    strict = presence.run_presence(env['places'], params={'min_inside_fixes': 20}, actor='a')
    statuses = {(i['place_id'], i['status']) for i in _intervals(strict['run_id'])}
    assert ('home', 'inside') in statuses and ('work', 'uncertain') in statuses
    window = presence.run_presence(env['places'], start_utc='2024-01-15T08:00:00Z', end_utc='2024-01-15T10:00:00Z',
                                   actor='a')
    w = _intervals(window['run_id'])
    home = next(i for i in w if i['place_id'] == 'home')
    assert home['start_utc'] == '2024-01-15T08:00:00Z' and home['evidence']['window_edge']['start'] is True
    assert window['window'] == {'from_utc': '2024-01-15T08:00:00Z', 'to_utc': '2024-01-15T10:00:00Z'}
    only = presence.run_presence(env['places'], params={'streams': ['phone/device:-12']}, actor='a')
    assert {i['place_id'] for i in _intervals(only['run_id'])} == {'dateline'}
    rejected_all = presence.run_presence(env['places'], params={'max_accuracy_m': 5}, actor='a')
    assert rejected_all['counts']['intervals'] == 0 and rejected_all['counts']['fixes_skipped_accuracy_over_max'] > 0
    for bad in ({'nope': 1}, {'max_gap_s': 0}, {'signal_bases': ['guess']}, {'accuracy_multiplier': float('nan')},
                {'missing_accuracy_m': -1}, {'merge_streams': 'yes'}):
        with pytest.raises(ValueError):
            presence.run_presence(env['places'], params=bad, actor='a')
    with pytest.raises(ValueError):
        presence.run_presence(env['places'], start_utc='2024-01-15T08:00:00', actor='a')  # no offset
    assert len(presence.list_runs()) >= 7


def test_published_presence_events_are_derived_and_never_duplicated(env):
    run = presence.run_presence(env['places'], publish=True, actor='a')
    published = rows("SELECT * FROM events WHERE event_type='place_presence' ORDER BY id")
    assert len(published) == run['counts']['intervals'] and run['published'] is True
    meta = json.loads(published[0]['metadata_json'])
    assert meta['signal'] == {'class': 'passive', 'basis': 'derived', 'provider': 'ageds:place_presence.v1',
                              'device_or_account': meta['signal']['device_or_account']}
    assert meta['place_set']['sha256'] == env['places'].sha256 and 'not proof of a person' in meta['semantics']
    assert published[0]['artifact_id'] is None and published[0]['confidence'] is None
    times = rows('SELECT * FROM event_times WHERE event_id=? ORDER BY kind DESC', (published[0]['id'],))
    assert {t['tz_status'] for t in times} == {'explicit_offset'} and {t['kind'] for t in times} <= {'start', 'end'}
    again = presence.run_presence(env['places'], publish=True, actor='b')
    assert again['reused'] and rows("SELECT COUNT(*) AS n FROM events WHERE event_type='place_presence'")[0]['n'] == len(published)
    other = presence.run_presence(env['places'], params={'max_gap_s': 3 * 3600}, publish=True, actor='b')
    total = rows("SELECT COUNT(*) AS n FROM events WHERE event_type='place_presence'")[0]['n']
    # identical intervals of both parameter sets share nothing: events are keyed by the parameter hash
    assert total == len(published) + other['counts']['intervals']
    linked = rows('SELECT event_id FROM location_presence_intervals WHERE run_id=? ORDER BY id', (run['run_id'],))
    assert all(r['event_id'] for r in linked)
    with dbmod.session() as db:
        for sql in ('UPDATE location_presence_runs SET actor=1', 'DELETE FROM location_presence_intervals',
                    'UPDATE location_place_sets SET version=1'):
            with pytest.raises(sqlite3.DatabaseError, match='append-only'):
                db.execute(sql)


def test_presence_from_coarse_storage_widens_to_uncertain(env):
    coarse = env['tmp'] / 'coarse.json'
    shutil.copy(env['f']['Takeout/Location History/Records.json'], coarse)
    location.import_location_history_v2(coarse, enable_location=True, stream='coarse', store_decimals=3)
    run = presence.run_presence(env['places'], params={'streams': ['coarse/device:-11']}, actor='a')
    intervals = _intervals(run['run_id'])
    assert intervals and all(i['evidence']['max_rounding_m'] == pytest.approx(78.627, abs=0.01) for i in intervals)
    assert all(i['max_uncertainty_m'] >= 78.6 + 10 for i in intervals)  # accuracy + rounding of 3-decimal cells
    exact = presence.run_presence(env['places'], params={'streams': ['phone/device:-11']}, actor='a')
    assert all(i['evidence']['max_rounding_m'] == 0 for i in _intervals(exact['run_id']))


# ------------------------------------------------------------------ correlations

def _with_other_events(env):
    ics.import_ics(env['f']['calendar/meetings.ics'])
    call_log.import_calls_xml(env['f']['phone/calls.xml'])
    return presence.run_presence(env['places'], actor='a')


def test_presence_calendar_and_communication_hypotheses(env):
    run = _with_other_events(env)
    calendar = correlate.run_correlation('presence_calendar', run['run_id'], actor='tester')
    assert calendar['rule'] == {'rule_id': 'presence_calendar', 'version': 1,
                                'sha256': correlate.RELEASED_SHA256[('presence_calendar', 1)]}
    assert calendar['source_rule'].startswith('display_time@')
    by_place = {}
    for h in calendar['hypotheses']:
        interval = next(i for i in _intervals(run['run_id']) if i['id'] == h['interval_id'])
        by_place[interval['place_id']] = h
    assert set(by_place) == {'work', 'shop'}
    assert by_place['work']['evidence']['location_text_contains_place_label'] is True
    assert by_place['shop']['evidence']['location_text_contains_place_label'] is False
    assert by_place['work']['evidence']['inner_overlap_s'] == 3600 and by_place['work']['status'] == 'proposed'
    assert 'probability' not in json.dumps(calendar['hypotheses'])
    # the entry on another day lies outside the loaded range (intervals' outer bounds + tolerance + 1 day)
    assert calendar['counts']['right_items'] == 1 and calendar['unmatched']['right_events_without_interval_total'] == 0
    comm = correlate.run_correlation('presence_communication', run['run_id'], params={'tolerance_s': 0})
    places_hit = sorted(next(i['place_id'] for i in _intervals(run['run_id']) if i['id'] == h['interval_id'])
                        for h in comm['hypotheses'])
    assert places_hit == ['home', 'shop', 'work'] and comm['hypotheses'][0]['evidence']['same_stream_hint'] == 'unknown'
    groups = {h['ambiguity_group'] for h in comm['hypotheses']}
    assert len(groups) == 3 and all(h['candidate_count'] == 1 for h in comm['hypotheses'])
    for bad in ({'nope': 1}, {'tolerance_s': -5}, {'label_match': 'yes'}):
        with pytest.raises(correlate.RuleError):
            correlate.run_correlation('presence_calendar', run['run_id'], params=bad)
    with pytest.raises(correlate.RuleError):
        correlate.run_correlation('presence_teleport', run['run_id'])
    with pytest.raises(LookupError):
        correlate.run_correlation('presence_calendar', 99999)


def test_correlation_review_history_and_rule_integrity(env, tmp_path, monkeypatch):
    run = _with_other_events(env)
    result = correlate.run_correlation('presence_calendar', run['run_id'])
    hypothesis = result['hypotheses'][0]['id']
    assert correlate.review(hypothesis, 'accept', reason='checked the calendar', actor='me')['to'] == 'accepted'
    with pytest.raises(ValueError):
        correlate.review(hypothesis, 'accept', reason='again', actor='me')
    with pytest.raises(ValueError):
        correlate.review(hypothesis, 'revoke', reason='  ', actor='me')
    assert correlate.review(hypothesis, 'revoke', reason='wrong day', actor='me')['from'] == 'accepted'
    assert correlate.review(hypothesis, 'reject', reason='no', actor='me')['to'] == 'rejected'
    assert correlate.get_correlation_run(result['run_id'])['hypotheses'][0]['status'] == 'rejected'
    assert rows("SELECT COUNT(*) AS n FROM location_correlation_status")[0]['n'] == 3
    with dbmod.session() as db:
        with pytest.raises(sqlite3.DatabaseError, match='append-only'):
            db.execute('DELETE FROM location_correlation_status')
    copy = tmp_path / 'rules'
    shutil.copytree(correlate.RULES_DIR, copy)
    edited = copy / 'presence_calendar.v1.json'
    edited.write_text(edited.read_text().replace('"tolerance_s": 900', '"tolerance_s": 901'))
    monkeypatch.setattr(correlate, 'RULES_DIR', copy)
    with pytest.raises(correlate.RuleIntegrityError):
        correlate.load_rule('presence_calendar')

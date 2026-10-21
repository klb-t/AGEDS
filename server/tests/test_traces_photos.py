"""Google Photos Takeout sidecars, matching hypotheses and photo geotags (traces area)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from server.app import db as dbmod, evidence
from server.app.config import settings
from server.app.importers import exif, photos_takeout
from server.app.traces import geotags
from server.tests.corpus import traces as corpus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_settings = replace(settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'e.db',
                            store_dir=tmp_path / 'data' / 'store')
    test_settings.store_dir.mkdir(parents=True)
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    manifest = corpus.build(tmp_path / 'corpus')
    return {'root': tmp_path / 'corpus', 'm': manifest}


def rows(sql, params=()):
    with dbmod.session() as db:
        return [dict(r) for r in db.execute(sql, params)]


def table_counts():
    return {t: rows(f'SELECT COUNT(*) AS n FROM {t}')[0]['n'] for t in
            ('artifacts', 'events', 'event_times', 'trace_photo_sidecars', 'trace_photo_matches', 'trace_geotags')}


def corpus_hashes(root: Path) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}


def sidecar_by_name(name):
    found = rows('SELECT * FROM trace_photo_sidecars WHERE sidecar_name=? ORDER BY id', (name,))
    assert found, name
    return found


def match_of(sidecar_name):
    sidecar = sidecar_by_name(sidecar_name)[0]
    match = rows('SELECT * FROM trace_photo_matches WHERE sidecar_id=? ORDER BY id', (sidecar['id'],))
    assert len(match) == 1
    return match[0]['status'], json.loads(match[0]['candidates_json'])


def test_name_analysis_rules():
    rules = photos_takeout.rules()
    assert photos_takeout.analyse_name('IMG_1.jpg.json', rules)['base'] == 'IMG_1.jpg'
    sup = photos_takeout.analyse_name('IMG_1.jpg.supplemental-metadata.json', rules)
    assert (sup['base'], sup['variant'], sup['counter']) == ('IMG_1.jpg', 'supplemental_metadata', None)
    dup = photos_takeout.analyse_name('IMG_1.jpg.supplemental-metadata(2).json', rules)
    assert (dup['base'], dup['counter']) == ('IMG_1.jpg', 2)
    cut_marker = photos_takeout.analyse_name('IMG_20200101_120000_123456789012.jpg.supplem.json', rules)
    assert (cut_marker['base'], cut_marker['variant']) == ('IMG_20200101_120000_123456789012.jpg', 'supplemental_metadata_cut')
    bare = photos_takeout.analyse_name('IMG_1.json', rules)
    assert bare['variant'] == 'json_without_media_extension'
    assert photos_takeout.analyse_name('notes.txt', rules)['variant'] == 'not_sidecar_name'
    candidates = photos_takeout.match_candidates(bare, None, ['IMG_1.jpg', 'IMG_1.mp4', 'IMG_10.jpg'], rules)
    assert [c['name'] for c in candidates] == ['IMG_1.jpg', 'IMG_1.mp4']  # live photo pair: two candidates


def test_directory_import_without_location_opt_in(env):
    m = env['m']['photos']
    before = corpus_hashes(env['root'])
    result = photos_takeout.import_photos_takeout(Path(m['root']))
    assert corpus_hashes(env['root']) == before  # originals untouched
    assert result['options']['enable_location'] is False and result['geotags_stored'] == 0
    assert result['json_not_sidecar'] == 1  # album metadata.json
    assert result['media_exif_container_not_supported'] == 1  # HEIC
    assert rows('SELECT COUNT(*) AS n FROM trace_geotags WHERE coordinates_stored=1')[0]['n'] == 0
    assert {r['status'] for r in rows('SELECT status FROM trace_geotags')} >= {'present_not_stored', 'zero_placeholder',
                                                                              'invalid'}
    # no coordinate value leaks into events, sidecar rows or geotag raw values without the opt-in
    blob = json.dumps(rows('SELECT metadata_json FROM events') + rows('SELECT * FROM trace_photo_sidecars') +
                      rows('SELECT raw_json, lat, lon FROM trace_geotags'))
    for value in ('30.25', '10.5', '-45.0', '60.5', '20.125'):
        assert value not in blob
    # matching hypotheses
    assert match_of('IMG_20210101_101500.jpg.json')[0] == 'single_candidate'
    status, candidates = match_of(m['expect']['duplicate'][0])
    assert status == 'single_candidate' and [c['name'] for c in candidates] == [m['expect']['duplicate'][1]]
    assert candidates[0]['rules'] == ['duplicate_counter_v1']
    status, candidates = match_of(m['expect']['cut_sidecar'][0])
    assert status == 'single_candidate' and candidates[0]['name'] == m['expect']['cut_sidecar'][1]
    assert set(candidates[0]['rules']) == {'title_exact_v1', 'truncated_sidecar_prefix_v1'}
    status, candidates = match_of(m['expect']['ambiguous_sidecar'])
    assert status == 'ambiguous' and len(candidates) == 2 and not any(c['described'] for c in candidates)
    status, candidates = match_of(m['expect']['cut_media'][0])
    assert status == 'single_candidate' and candidates[0]['rules'] == ['truncated_media_name_v1']
    status, candidates = match_of('IMG_20210105_090000.jpg.json')
    assert status == 'single_candidate'
    assert {c['name']: c['relation'] for c in candidates}[m['expect']['edited']] == 'edited_variant'
    assert match_of(m['expect']['sidecar_without_media'])[0] == 'no_candidate'
    status, candidates = match_of('Zdjęcie_łąka_ü.jpg.json')
    assert status == 'single_candidate' and candidates[0]['name'] == m['expect']['nfd_media']
    listed = [i for i in result['issues']['listed'] if i['code'] == 'media_without_sidecar']
    assert m['expect']['without_sidecar'] in {i['name'] for i in listed}
    # third-party names stay raw in the sidecar row, never in event columns or the search index
    people = json.loads(sidecar_by_name('IMG_20210102_120000.jpg.supplemental-metadata.json')[0]['people_json'])
    assert [p['name'] for p in people] == ['Ala Przykładowa', 'Jan Testowy']
    assert not rows("SELECT 1 FROM search_fts WHERE search_fts MATCH 'Przykładowa'")
    assert 'Przykładowa' not in json.dumps(rows('SELECT * FROM events'), ensure_ascii=False)


def test_times_conflicts_and_edge_values(env):
    m = env['m']['photos']
    photos_takeout.import_photos_takeout(Path(m['root']), enable_location=True)
    sidecar = sidecar_by_name('SCAN_1960.jpg.json')[0]
    times = rows('SELECT * FROM event_times WHERE event_id=?', (sidecar['event_id'],))
    assert [t['utc'] for t in times if t['kind'] == 'created'] == ['1960-01-01T00:00:00Z']
    ms = sidecar_by_name('IMG_MS.jpg.json')[0]
    assert 'epoch_implausible_for_unit_s:photoTakenTime' in json.loads(ms['issues_json'])
    assert not rows("SELECT 1 FROM event_times WHERE event_id=? AND kind='created'", (ms['event_id'],))
    exact = sidecar_by_name('IMG_20210101_101500.jpg.json')[0]
    kinds = {t['kind']: t for t in rows('SELECT * FROM event_times WHERE event_id=?', (exact['event_id'],))}
    assert kinds['created']['utc'] == '2021-01-01T09:15:00Z' and kinds['other']['utc'] == '2021-01-02T15:06:40Z'
    assert json.loads(kinds['created']['metadata_json'])['formatted_parsed'] is False
    conflict = sidecar_by_name('IMG_20210102_120000.jpg.supplemental-metadata.json')[0]
    assert json.loads(conflict['geo_json'])['comparison'] == 'differ'
    assert 'geo_data_differs_from_geo_data_exif' in json.loads(conflict['issues_json'])
    geo = {r['source_kind']: r for r in rows('SELECT * FROM trace_geotags WHERE sidecar_id=?', (conflict['id'],))}
    assert (geo['takeout_geoData']['lat'], geo['takeout_geoDataExif']['lat']) == (m['coordinates']['b_geo'][0],
                                                                                  m['coordinates']['b_geo_exif'][0])
    assert geo['takeout_geoData']['basis'] == 'provider_inference_or_user_edit'
    assert geo['takeout_geoDataExif']['basis'] == 'raw_sample'
    assert json.loads(geo['takeout_geoData']['raw_json'])['distance_to_geoDataExif_m'] > 1_000_000
    zero = sidecar_by_name('IMG_20210105_090000.jpg.json')[0]
    assert {r['status'] for r in rows('SELECT status FROM trace_geotags WHERE sidecar_id=?', (zero['id'],))} == {'zero_placeholder'}


def test_exif_gps_geotags_and_utc_candidate(env):
    m = env['m']['photos']
    photos_takeout.import_photos_takeout(Path(m['root']), enable_location=True)
    sha_a = m['sha256']['a']
    tags = [t for t in geotags.iter_geotags(source_kinds=('exif_gps',)) if t['media_sha256'] == sha_a]
    assert len(tags) == 2  # the same bytes under two folders: two artifacts, two observations
    tag = tags[0]
    assert (round(tag['lat'], 6), round(tag['lon'], 6), tag['alt']) == (10.5, -30.25, 12.5)
    assert tag['time_utc'] == m['expect']['gps_time_a'] and tag['time_basis'] == 'exif_gps_receiver_utc'
    assert tag['accuracy_m'] == 4.5 and tag['opt_in']['enable_location'] is True
    event = rows("SELECT * FROM events WHERE id=?", (tag['event_id'],))[0]
    assert event['event_type'] == 'image_capture'  # exif_minimal v1 event, unchanged
    assert json.loads(event['metadata_json'])['gps'] == {'ifd_present': True, 'coordinates_decoded': False}
    candidates = rows('SELECT * FROM event_times WHERE event_id=? ORDER BY id', (event['id'],))
    assert [(c['source'], c['tz_status']) for c in candidates] == [
        ('exif:DateTimeOriginal', 'unknown'), ('exif_gps@1:GPSDateStamp+GPSTimeStamp', 'explicit_offset')]
    south = [t for t in geotags.iter_geotags() if t['media_sha256'] == m['sha256']['n']][0]
    assert south['lat'] < 0 and south['lon'] < 0 and south['alt'] == -3.0
    assert south['time_utc'] == m['expect']['south_gps_time']
    noref = rows('SELECT * FROM trace_geotags WHERE media_sha256=?', (m['sha256']['o'],))[0]
    assert noref['status'] == 'invalid' and 'gps_latitude_ref_missing_or_invalid' in json.loads(noref['issues_json'])
    png_tag = [t for t in geotags.iter_geotags() if t['media_sha256'] == m['sha256']['p']]
    assert len(png_tag) == 1 and png_tag[0]['time_utc'] is None
    # sidecar geotags resolve their media only through a single described candidate
    sidecar_tags = [t for t in geotags.iter_geotags(source_kinds=('takeout_geoDataExif',))]
    assert any(t['media_sha256'] == sha_a for t in sidecar_tags)
    assert list(geotags.iter_geotags(include_provider=False, source_kinds=('takeout_geoData',))) == []
    since = list(geotags.iter_geotags(since='2021-02-01T00:00:00Z'))
    assert since and all(t['time_utc'] >= '2021-02-01T00:00:00Z' for t in since)


def test_reimport_is_idempotent_and_a_later_opt_in_adds_rows(env):
    m = env['m']['photos']
    photos_takeout.import_photos_takeout(Path(m['root']))
    first = table_counts()
    photos_takeout.import_photos_takeout(Path(m['root']))
    second = table_counts()
    assert {k: v for k, v in second.items() if k != 'artifacts'} == {k: v for k, v in first.items() if k != 'artifacts'}
    assert second['artifacts'] == first['artifacts']
    photos_takeout.import_photos_takeout(Path(m['root']), enable_location=True, coordinate_decimals=3)
    third = table_counts()
    assert third['events'] == first['events'] and third['trace_photo_sidecars'] == first['trace_photo_sidecars']
    assert third['trace_geotags'] > first['trace_geotags']
    stored = rows('SELECT * FROM trace_geotags WHERE coordinates_stored=1')
    assert stored and {r['stored_precision'] for r in stored} == {'decimals:3'}
    assert all(round(r['lat'], 3) == r['lat'] for r in stored)
    with pytest.raises(ValueError, match='coordinate_decimals'):
        photos_takeout.import_photos_takeout(Path(m['root']), enable_location=True, coordinate_decimals=12)


def test_single_sidecar_and_photo_entry_points(env, tmp_path):
    m = env['m']['photos']
    result = photos_takeout.import_sidecar(Path(m['files']['a_json']), enable_location=True)
    assert result['status'] == 'sidecar_observed' and result['match'] is None and result['geotags_stored'] == 2
    with pytest.raises(ValueError, match='not a Photos sidecar'):
        photos_takeout.import_sidecar(Path(m['files']['album_meta']))
    photo = photos_takeout.import_photo(Path(m['files']['n']))
    assert photo['gps'] == 'coordinates' and photo['geotags_stored'] == 0
    assert exif.read_exif(Path(m['files']['n']))[0].get('gps') is None  # default reader unchanged
    broken = tmp_path / 'broken.json'
    broken.write_text('{"photoTakenTime": {"timestamp": "1"}, "title": ')
    with pytest.raises(ValueError, match='not valid'):
        photos_takeout.import_sidecar(broken)

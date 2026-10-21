"""Photo geotags (traces area) as a registered point source for place matching (integration 2026-10-21)."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from server.app import db, evidence
from server.app.location import places, sources


def _place_set():
    return places.parse_places({'schema': places.SCHEMA, 'version': 'test', 'places': [
        {'id': 'office', 'label': 'Synthetic office', 'lat': 0.001, 'lon': 0.001, 'radius_m': 100},
    ]})


def _tags():
    return [
        {'geotag_id': 1, 'source_kind': 'exif_gps', 'basis': 'raw_sample', 'lat': 0.0010, 'lon': 0.0010, 'alt': None,
         'accuracy_m': 5.0, 'stored_precision': 'source', 'time_utc': '2024-03-01T09:00:00Z',
         'time_basis': 'exif_gps_receiver_utc'},
        {'geotag_id': 2, 'source_kind': 'takeout_geoData', 'basis': 'provider_inference_or_user_edit', 'lat': 0.02,
         'lon': 0.02, 'alt': None, 'accuracy_m': None, 'stored_precision': 'decimals:4',
         'time_utc': '2024-03-01T12:00:00Z', 'time_basis': 'google_photos_photoTakenTime'},
        {'geotag_id': 3, 'source_kind': 'exif_gps', 'basis': 'raw_sample', 'lat': 0.001, 'lon': 0.001, 'alt': None,
         'accuracy_m': None, 'stored_precision': 'source', 'time_utc': None, 'time_basis': None},
    ]


@pytest.fixture
def temp_db(tmp_path):
    settings = replace(db.settings, data_dir=tmp_path, db_path=tmp_path / 'db.sqlite', store_dir=tmp_path / 'store')
    with patch.object(db, 'settings', settings), patch.object(evidence, 'settings', settings):
        db.init_db()
        yield


def test_photo_geotags_are_a_registered_source_and_match_read_only(temp_db):
    assert sources.PHOTO_SOURCE in {s['key'] for s in places.geotag_sources()}
    with patch('server.app.traces.geotags.iter_geotags', lambda **kw: iter(_tags())):
        result = sources.match_source(sources.PHOTO_SOURCE, _place_set(), include_outside=True)
    assert result['rule'] == sources.MATCH_RULE and result['points'] == 2  # the untimed tag is not a point
    by_ref = {item['ref']: item for item in result['items']}
    inside = by_ref['trace_geotags/1']
    assert inside['basis'] == 'raw_sample' and inside['time_utc'] == '2024-03-01T09:00:00Z'
    assert [m['place_id'] for m in inside['matches'] if m['status'] == 'inside'] == ['office']
    far = by_ref['trace_geotags/2']
    assert far['basis'] == 'provider_inference_or_user_edit'
    assert all(m['status'] == 'outside' for m in far['matches'])
    assert result['with_place'] == 1 and result['place_set']['sha256']
    with db.session() as connection:  # nothing was written by the projection
        assert connection.execute("SELECT count(*) FROM location_presence_runs").fetchone()[0] == 0


def test_unknown_source_is_refused(temp_db):
    with pytest.raises(ValueError, match='unknown point source'):
        sources.match_source('nope', _place_set())

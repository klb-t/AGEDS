"""Synthetic location corpus (task L1): every format with planted hard cases.

All coordinates are fictional: points within a few kilometres of "Null Island"
(0°N 0°E), one cluster next to the antimeridian and one near the North Pole.
No real place, address or device is represented. Times are synthetic (2024).

Run ``python -m server.tests.corpus.location`` for the isolated browser fixture
(temporary database, imported corpus, one presence run; prints JSON and serves).
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

E7 = 10_000_000
DAY = datetime(2024, 1, 15, tzinfo=timezone.utc)

PLACES = {
    'schema': 'ageds.location-places.v1', 'version': 'synthetic-2024-01',
    'places': [
        {'id': 'home', 'label': 'Dom (fikcyjny)', 'lat': 0.0010, 'lon': 0.0020, 'radius_m': 100, 'kind': 'home'},
        {'id': 'work', 'label': 'Praca (fikcyjna)', 'lat': 0.0200, 'lon': 0.0300, 'radius_m': 150, 'kind': 'work'},
        {'id': 'shop', 'label': 'Sklep (fikcyjny)', 'lat': 0.0205, 'lon': 0.0300, 'radius_m': 80},
        {'id': 'dateline', 'label': 'Przy antypołudniku', 'lat': 0.5, 'lon': 179.9995, 'radius_m': 200},
        {'id': 'pole', 'label': 'Przy biegunie', 'lat': 89.9995, 'lon': 10.0, 'radius_m': 300},
    ],
}


def _iso(moment: datetime) -> str:
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + f'{moment.microsecond // 1000:03d}Z'


def _ms(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1000))


def _point(lat: float, lon: float, moment: datetime, accuracy: int, *, ms: bool = False, **extra) -> dict:
    point = {'latitudeE7': round(lat * E7), 'longitudeE7': round(lon * E7), 'accuracy': accuracy, 'source': 'WIFI'}
    if ms:
        point['timestampMs'] = _ms(moment)
    else:
        point['timestamp'] = _iso(moment)
    point.update(extra)
    return point


def records_points() -> list[dict]:
    """A synthetic day: home 07:00-08:55, travel, work 09:30-12:00 and 14:00-15:00."""
    points = []
    for i in range(24):  # home, every 5 minutes, jitter within ~15 m
        moment = DAY + timedelta(hours=7, minutes=5 * i)
        points.append(_point(0.0010 + (i % 3) * 0.00004, 0.0020 - (i % 2) * 0.00003, moment, 12,
                             ms=(i % 4 == 1), deviceTag=-11, activity=[{'timestamp': _iso(moment),
                                                                         'activity': [{'type': 'STILL', 'confidence': 90}]}]))
    points.append(_point(0.0010, 0.0020 + 0.0006, DAY + timedelta(hours=8, minutes=57), 150, deviceTag=-11))  # uncertain
    for i in range(6):  # travel: clearly outside every place
        points.append(_point(0.0030 + i * 0.003, 0.0050 + i * 0.004, DAY + timedelta(hours=9, minutes=5 * i), 25,
                             deviceTag=-11))
    for i in range(16):  # work, every 10 minutes
        points.append(_point(0.0200 - 0.0002, 0.0300 - (i % 2) * 0.0001, DAY + timedelta(hours=9, minutes=30 + 10 * i), 15,
                             deviceTag=-11))
    points.append(_point(0.0200 + 0.00126, 0.0300, DAY + timedelta(hours=12, minutes=1), 20, deviceTag=-11))  # ~140 m
    for i in range(7):  # work again after a two-hour gap
        points.append(_point(0.0199, 0.0301, DAY + timedelta(hours=14, minutes=10 * i), 10, deviceTag=-11))
    for i in range(4):  # antimeridian cluster on another day
        lon = 179.9990 if i % 2 == 0 else -179.9997
        points.append(_point(0.5, lon, DAY + timedelta(days=1, hours=2, minutes=10 * i), 30, deviceTag=-12))
    return points


def records_document() -> str:
    points = records_points()
    duplicate = dict(points[3])
    body = [json.dumps(p) for p in points]
    body.append(json.dumps(duplicate))  # identical point twice in one export
    # adversarial records, written as raw JSON text
    body += [
        '{"latitudeE7": NaN, "longitudeE7": 20000, "timestamp": "2024-01-15T20:00:00.000Z"}',
        '{"latitudeE7": 1e999, "longitudeE7": 20000, "timestamp": "2024-01-15T20:01:00.000Z"}',
        '{"latitudeE7": 123456789012345678901234567890, "longitudeE7": 20000, "timestamp": "2024-01-15T20:02:00.000Z"}',
        '{"latitudeE7": 4294967296, "longitudeE7": 20000, "timestamp": "2024-01-15T20:03:00.000Z"}',
        '{"latitudeE7": "10000", "longitudeE7": 20000, "timestamp": "2024-01-15T20:04:00.000Z"}',
        '{"longitudeE7": 20000, "timestamp": "2024-01-15T20:05:00.000Z"}',
        '{"latitudeE7": 10000, "longitudeE7": 20000, "timestamp": "2024-13-45T99:00:00Z"}',
        '{"latitudeE7": 10000, "longitudeE7": 20000, "timestampMs": "99999999999999999999999"}',
        '"not a point"',
        '{"latitudeE7": 10000, "longitudeE7": 20000, "timestamp": "2024-01-15T20:09:00.000Z", '
        '"timestampMs": "1705349340000", "futureField": {"x": 1}}',
    ]
    return '{"locations": [\n' + ',\n'.join(body) + '\n], "extraTopLevel": {"note": "unknown section"}}\n'


def overlap_document() -> str:
    """A second export of the same account: overlaps the first day and adds one later point."""
    points = records_points()[20:30]
    points.append(_point(0.0010, 0.0020, DAY + timedelta(days=2, hours=7), 12, deviceTag=-11))
    return json.dumps({'locations': points})


def semantic_document() -> dict:
    start = DAY + timedelta(hours=7)
    return {'timelineObjects': [
        {'placeVisit': {
            'location': {'latitudeE7': 10000, 'longitudeE7': 20000, 'placeId': 'synthetic-place-id-home',
                         'address': 'Fikcyjna 1, Null Island', 'name': 'Synthetic Home', 'semanticType': 'TYPE_HOME',
                         'sourceInfo': {'deviceTag': -11}, 'locationConfidence': 91.5},
            'duration': {'startTimestamp': _iso(start), 'endTimestamp': _iso(start + timedelta(hours=1, minutes=55))},
            'placeConfidence': 'HIGH_CONFIDENCE', 'visitConfidence': 95, 'editConfirmationStatus': 'CONFIRMED',
            'otherCandidateLocations': [{'latitudeE7': 12000, 'longitudeE7': 21000, 'placeId': 'synthetic-place-id-alt',
                                         'semanticType': 'TYPE_UNKNOWN', 'locationConfidence': 3.2}],
            'simplifiedRawPath': {'points': [{'latE7': 10010, 'lngE7': 20010, 'accuracyMeters': 11,
                                              'timestamp': _iso(start + timedelta(minutes=30))}]},
            'childVisits': [{'location': {'latitudeE7': 10020, 'longitudeE7': 20020, 'placeId': 'synthetic-child'},
                             'duration': {'startTimestampMs': _ms(start + timedelta(minutes=10)),
                                          'endTimestampMs': _ms(start + timedelta(minutes=40))},
                             'placeConfidence': 'LOW_CONFIDENCE'}]}},
        {'activitySegment': {
            'startLocation': {'latitudeE7': 10000, 'longitudeE7': 20000},
            'endLocation': {'latitudeE7': 200000, 'longitudeE7': 300000},
            'duration': {'startTimestampMs': _ms(DAY + timedelta(hours=9)), 'endTimestampMs': _ms(DAY + timedelta(hours=9, minutes=28))},
            'distance': 3600, 'activityType': 'IN_PASSENGER_VEHICLE', 'confidence': 'HIGH',
            'activities': [{'activityType': 'IN_PASSENGER_VEHICLE', 'probability': 87.5},
                           {'activityType': 'CYCLING', 'probability': 6.1}],
            'waypointPath': {'waypoints': [{'latE7': 50000, 'lngE7': 80000}, {'latE7': 150000, 'lngE7': 240000}],
                             'source': 'INFERRED', 'distanceMeters': 3400.5, 'travelMode': 'DRIVE', 'confidence': 0.9},
            'simplifiedRawPath': {'points': [{'latE7': 100000, 'lngE7': 160000, 'accuracyMeters': 20,
                                              'timestampMs': _ms(DAY + timedelta(hours=9, minutes=15))}]},
            'editConfirmationStatus': 'NOT_CONFIRMED'}},
        {'placeVisit': {'location': {'latitudeE7': 200000, 'longitudeE7': 300000, 'placeId': 'synthetic-place-id-work'},
                        'duration': {'startTimestamp': _iso(DAY + timedelta(hours=9, minutes=30)),
                                     'endTimestamp': _iso(DAY + timedelta(hours=12))},
                        'placeConfidence': 'MEDIUM_CONFIDENCE', 'editConfirmationStatus': 'SOMETHING_NEW'}},
        {'someFutureObject': {'x': 1}},
        {'placeVisit': {'location': {'latitudeE7': 999999999999, 'longitudeE7': 0},
                        'duration': {'startTimestamp': 'yesterday'}}},
    ]}


def device_timeline_document() -> dict:
    return {
        'semanticSegments': [
            {'startTime': '2024-03-31T01:30:00.000+01:00', 'endTime': '2024-03-31T03:30:00.000+02:00',
             'startTimeTimezoneUtcOffsetMinutes': 60, 'endTimeTimezoneUtcOffsetMinutes': 120,
             'visit': {'hierarchyLevel': 0, 'probability': 0.83,
                       'topCandidate': {'placeId': 'synthetic-device-home', 'semanticType': 'HOME', 'probability': 0.91,
                                        'placeLocation': {'latLng': '0.0010000°, 0.0020000°'}}}},
            {'startTime': '2024-03-31T08:00:00.000+02:00', 'endTime': '2024-03-31T08:40:00.000+02:00',
             'startTimeTimezoneUtcOffsetMinutes': 60,  # stated offset disagrees with the timestamp's
             'activity': {'start': {'latLng': '0.0010°, 0.0020°'}, 'end': {'latLng': '0.0200°, 0.0300°'},
                          'distanceMeters': 3700.25, 'probability': 0.99,
                          'topCandidate': {'type': 'IN_PASSENGER_VEHICLE', 'probability': 0.97}}},
            {'startTime': '2024-03-31T08:00:00.000+02:00', 'endTime': '2024-03-31T10:00:00.000+02:00',
             'timelinePath': [{'point': '0.0050000°, 0.0080000°', 'time': '2024-03-31T08:10:00.000+02:00'},
                              {'point': '0.0150000°, 0.0240000°', 'time': '2024-03-31T08:25:00.000+02:00'},
                              {'point': 'garbage', 'time': '2024-03-31T08:26:00.000+02:00'}]},
            {'startTime': '2024-03-31T11:00:00.000+02:00', 'endTime': '2024-03-31T12:00:00.000+02:00',
             'timelineMemory': {'trip': {'distanceFromOriginKms': 4}}},
        ],
        'rawSignals': [
            {'position': {'LatLng': '0.0200100°, 0.0300100°', 'accuracyMeters': 9, 'altitudeMeters': 3.5,
                          'source': 'GPS', 'timestamp': '2024-03-31T09:00:00.000+02:00', 'speedMetersPerSecond': 0.0}},
            {'position': {'LatLng': '0.0200200°, 0.0300100°', 'accuracyMeters': 11, 'source': 'WIFI',
                          'timestamp': '2024-03-31T09:05:00.000+02:00'}},
            {'position': {'LatLng': '91.0°, 0.0°', 'accuracyMeters': 5, 'timestamp': '2024-03-31T09:06:00.000+02:00'}},
            {'wifiScan': {'deliveryTime': '2024-03-31T09:01:00.000+02:00',
                          'devicesRecords': [{'mac': 1000001, 'rawRssi': -48}, {'mac': 1000002, 'rawRssi': -71}]}},
            {'activityRecord': {'probableActivities': [{'type': 'STILL', 'confidence': 0.88},
                                                       {'type': 'ON_FOOT', 'confidence': 0.07}],
                                'timestamp': '2024-03-31T09:02:00.000+02:00'}},
            {'unknownSignal': {}},
        ],
        'userLocationProfile': {
            'frequentPlaces': [{'placeId': 'synthetic-device-home', 'placeLocation': '0.0010000°, 0.0020000°', 'label': 'HOME'},
                               {'placeId': 'synthetic-device-work', 'placeLocation': '0.0200000°, 0.0300000°', 'label': 'WORK'}],
            'persona': {'travelModeAffinities': [{'mode': 'WALKING', 'affinity': 0.4}]}},
    }


def ios_document() -> list:
    return [
        {'startTime': '2024-02-01T08:00:00.000+01:00', 'endTime': '2024-02-01T09:00:00.000+01:00',
         'visit': {'hierarchyLevel': '0', 'probability': '0.876',
                   'topCandidate': {'probability': '0.91', 'semanticType': 'Home', 'placeID': 'synthetic-ios-home',
                                    'placeLocation': 'geo:0.001000,0.002000'}}},
        {'startTime': '2024-02-01T09:00:00.000+01:00', 'endTime': '2024-02-01T09:30:00.000+01:00',
         'activity': {'start': 'geo:0.001000,0.002000', 'end': 'geo:0.020000,0.030000', 'distanceMeters': '3712.5',
                      'probability': '0.99', 'topCandidate': {'type': 'in passenger vehicle', 'probability': '0.95'}}},
        {'startTime': '2024-02-01T09:00:00.000+01:00', 'endTime': '2024-02-01T11:00:00.000+01:00',
         'timelinePath': [{'point': 'geo:0.005000,0.008000', 'durationMinutesOffsetFromStartTime': '10'},
                          {'point': 'geo:0.015000,0.024000', 'durationMinutesOffsetFromStartTime': '25'},
                          {'point': 'geo:0.019000,0.029000', 'durationMinutesOffsetFromStartTime': 'x'}]},
        {'startTime': 'not-a-time', 'visit': {'topCandidate': {'placeLocation': 'geo:abc'}}},
        42,
    ]


def gpx_11() -> str:
    pts = []
    for i in range(10):
        moment = DAY + timedelta(days=1, hours=7, minutes=i)
        pts.append(f'<trkpt lat="{0.0010 + i * 0.0001:.7f}" lon="{0.0020 + i * 0.0001:.7f}"><ele>{5 + i}.0</ele>'
                   f'<time>{_iso(moment)}</time><hdop>1.2</hdop><extensions><hr>9{i}</hr></extensions></trkpt>')
    second = []
    for i in range(5):
        moment = DAY + timedelta(days=1, hours=8, minutes=i)
        second.append(f'<trkpt lat="{0.0200:.4f}" lon="{0.0300 + i * 0.0002:.4f}"><time>{_iso(moment)}</time></trkpt>')
    second.append('<trkpt lat="0.0201" lon="0.0301"><time>2024-01-16T07:59:00Z</time></trkpt>')  # time goes backwards
    second.append('<trkpt lat="NaN" lon="0.0301"><time>2024-01-16T08:06:00Z</time></trkpt>')
    second.append('<trkpt lon="0.0301"></trkpt>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="synthetic-logger" xmlns="http://www.topografix.com/GPX/1/1">'
            '<metadata><name>Syntetyczny ślad</name><time>2024-01-16T06:00:00Z</time></metadata>'
            '<wpt lat="0.0010" lon="0.0020"><ele>5</ele><time>2024-01-16T06:59:00Z</time><name>Start &amp; meta</name>'
            '<desc>&lt;b&gt;not markup&lt;/b&gt;</desc></wpt>'
            '<wpt lat="0.0300" lon="0.0400"><name>Bez czasu</name></wpt>'
            f'<trk><name>Spacer</name><type>walking</type><trkseg>{"".join(pts)}</trkseg><trkseg>{"".join(second)}</trkseg></trk>'
            '<rte><name>Plan</name><rtept lat="0.0010" lon="0.0020"/><rtept lat="0.0200" lon="0.0300"/></rte>'
            '</gpx>\n')


def gpx_10() -> str:
    return ('<?xml version="1.0"?>\n<gpx version="1.0" creator="old-device" xmlns="http://www.topografix.com/GPX/1/0">'
            '<trk><trkseg><trkpt lat="0.001" lon="0.002"/><trkpt lat="0.002" lon="0.003"/></trkseg></trk></gpx>\n')


def kml_document() -> str:
    whens = ''.join(f'<when>{_iso(DAY + timedelta(days=3, hours=10, minutes=i))}</when>' for i in range(4))
    coords = ''.join(f'<gx:coord>{0.0300 + i * 0.0001:.4f} {0.0010:.4f} 12</gx:coord>' for i in range(4))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2"><Document>'
            '<name>Syntetyczny KML</name><Folder>'
            '<Placemark><name>Punkt</name><TimeStamp><when>2024-01-18T10:00:00+01:00</when></TimeStamp>'
            '<Point><coordinates>0.002,0.001,0</coordinates></Point></Placemark>'
            '<Placemark><name>Linia</name><TimeSpan><begin>2024-01-18T11:00:00Z</begin><end>2024-01-18T11:30:00Z</end></TimeSpan>'
            '<LineString><coordinates>0.002,0.001,0 0.010,0.005,0\n 0.030,0.020,0</coordinates></LineString></Placemark>'
            f'<Placemark><name>Ślad</name><gx:Track>{whens}{coords}</gx:Track></Placemark>'
            '<Placemark><name>Zły</name><Point><coordinates>abc,def</coordinates></Point></Placemark>'
            '</Folder></Document></kml>\n')


def kmz_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('doc.kml', kml_document())
        archive.writestr('files/icon.png', b'\x89PNG\r\n\x1a\n' + b'\0' * 16)
    return buffer.getvalue()


def geojson_document() -> dict:
    features = []
    for i in range(6):
        moment = DAY + timedelta(days=4, hours=7, minutes=10 * i)
        features.append({'type': 'Feature', 'properties': {'ts': _iso(moment), 'acc': 10},
                         'geometry': {'type': 'Point', 'coordinates': [0.0020 + i * 0.00001, 0.0010]}})
    line_times = [_iso(DAY + timedelta(days=4, hours=9, minutes=i)) for i in range(3)]
    features.append({'type': 'Feature', 'properties': {'name': 'linia', 'coordTimes': line_times},
                     'geometry': {'type': 'LineString', 'coordinates': [[0.002, 0.001], [0.01, 0.005, 3], [0.03, 0.02]]}})
    features.append({'type': 'Feature', 'properties': {'name': 'obszar'},
                     'geometry': {'type': 'Polygon', 'coordinates': [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0]]]}})
    features.append({'type': 'Feature', 'properties': {}, 'geometry': {'type': 'Point', 'coordinates': [200.0, 0.0]}})
    features.append({'type': 'Feature', 'properties': {}, 'geometry': None})
    return {'type': 'FeatureCollection', 'features': features}


def fit_bytes() -> bytes:
    """A FIT-like header only (14 bytes, ``.FIT`` signature): enough for detection, not a valid activity."""
    return bytes([14, 0x10, 0x2C, 0x08, 0, 0, 0, 0]) + b'.FIT' + b'\0\0'


def calendar_text() -> str:
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//synthetic//L1//EN\r\nBEGIN:VEVENT\r\nUID:l1-meeting@example.org\r\n'
            'DTSTAMP:20240101T000000Z\r\nDTSTART:20240115T100000Z\r\nDTEND:20240115T110000Z\r\nSUMMARY:Spotkanie (fikcyjne)\r\n'
            'LOCATION:Praca (fikcyjna)\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:l1-far@example.org\r\nDTSTAMP:20240101T000000Z\r\n'
            'DTSTART:20240120T100000Z\r\nDTEND:20240120T110000Z\r\nSUMMARY:Bez obecności\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')


def calls_text() -> str:
    start = int((DAY + timedelta(hours=7, minutes=30)).timestamp() * 1000)
    later = int((DAY + timedelta(hours=10, minutes=15)).timestamp() * 1000)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<calls count="2">'
            f'<call number="+48000000101" duration="120" date="{start}" type="1" contact_name="(fikcyjny)" />'
            f'<call number="+48000000102" duration="60" date="{later}" type="2" contact_name="(fikcyjny)" />'
            '</calls>\n')


def build(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    files = {}

    def write(name: str, content) -> str:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                            encoding='utf-8')
        files[name] = str(path)
        return str(path)

    write('Takeout/Location History/Records.json', records_document())
    write('Takeout-2/Location History/Records.json', overlap_document())
    write('Takeout/Location History/Semantic Location History/2024/2024_JANUARY.json', semantic_document())
    write('device/Timeline.json', device_timeline_document())
    write('ios/location-history.json', ios_document())
    write('tracks/walk.gpx', gpx_11())
    write('tracks/old.gpx', gpx_10())
    write('tracks/truncated.gpx', gpx_11()[:900])
    write('tracks/entity.gpx', '<?xml version="1.0"?>\n<!DOCTYPE gpx [<!ENTITY a "aaaaaaaa">]>\n'
                               '<gpx version="1.1"><wpt lat="0" lon="0"><name>&a;</name></wpt></gpx>\n')
    write('tracks/places.kml', kml_document())
    write('tracks/places.kmz', kmz_bytes())
    write('tracks/points.geojson', geojson_document())
    write('tracks/ride.fit', fit_bytes())
    write('Takeout/Location History/Records-truncated.json', records_document()[:2000])
    write('calendar/meetings.ics', calendar_text())
    write('phone/calls.xml', calls_text())
    places_dir = root.parent / f'{root.name}-private'
    places_dir.mkdir(parents=True, exist_ok=True)
    places_path = places_dir / 'places.json'
    places_path.write_text(json.dumps(PLACES, ensure_ascii=False), encoding='utf-8')
    return {
        'files': files, 'places_path': str(places_path), 'places': PLACES,
        'expected': {
            'records_valid_points': len(records_points()), 'records_duplicate_locators': 1,
            'records_invalid': {'coordinate_not_finite': 2, 'coordinate_out_of_range': 2, 'coordinate_not_integer': 1,
                                'coordinate_missing': 1, 'time_unparsed': 2, 'point_not_an_object': 1},
            'overlap_shared_points': 10, 'overlap_new_points': 1,
            'home_inside_window': ('2024-01-15T07:00:00Z', '2024-01-15T08:55:00Z'),
            'work_intervals': 2,
            'semantic_visits': 3, 'semantic_segments': 1,
            'ios_events': 3,
        },
    }


def _browser_fixture() -> None:  # pragma: no cover - exercised by server/tests/browser/location.cjs
    import os
    import tempfile
    with tempfile.TemporaryDirectory(prefix='ageds-location-browser-') as directory:
        base = Path(directory)
        os.environ.update(EW_DATA_DIR=str(base / 'data'), EW_DB_PATH=str(base / 'data' / 'evidence.sqlite'),
                          EW_STORE_DIR=str(base / 'data' / 'store'))
        from server.app import db
        from server.app.importers import gpx, location
        from server.app.location import presence
        from server.app.location.places import parse_places
        import uvicorn
        db.init_db()
        manifest = build(base / 'corpus')
        files = manifest['files']
        imported = [location.import_location_history_v2(Path(files['Takeout/Location History/Records.json']),
                                                         enable_location=True, stream='synthetic-phone'),
                    location.import_location_history_v2(
                        Path(files['Takeout/Location History/Semantic Location History/2024/2024_JANUARY.json']),
                        enable_location=True, stream='synthetic-phone'),
                    gpx.import_gpx(Path(files['tracks/walk.gpx']), enable_location=True, stream='synthetic-phone')]
        hostile = json.loads(json.dumps(PLACES))
        hostile['places'][1]['label'] = 'Praca <img src=x onerror="window.__locationExecuted=1">'
        run = presence.run_presence(parse_places(hostile), actor='browser-fixture', publish=True)
        print(json.dumps({'imports': [i['location_import_id'] for i in imported], 'presence_run': run['run_id'],
                          'intervals': run['counts']['intervals'], 'hostile_label': hostile['places'][1]['label']}),
              flush=True)
        uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']),
                    log_level='warning')


if __name__ == '__main__':  # pragma: no cover
    _browser_fixture()

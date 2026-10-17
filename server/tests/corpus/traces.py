"""Hard synthetic personal-data exports for the traces area (generated, never committed).

``build(root)`` writes every fixture below ``root`` and returns a manifest of the
planted cases. Names are fictional, e-mail addresses use example.org/.com/.net,
IP addresses come from the documentation ranges (192.0.2.0/24, 198.51.100.0/24,
203.0.113.0/24, 2001:db8::/32) and coordinates are open-sea points.

Photos (``photos/Takeout/Google Photos/...``):

* exact sidecar (``.jpg.json``) whose geoData equals geoDataExif and EXIF GPS with
  a UTC receiver time an hour before the zone-less DateTimeOriginal;
* ``.supplemental-metadata.json`` sidecar whose geoData contradicts geoDataExif,
  with third-party ``people`` names and an RTL/emoji description;
* a ``(1)`` duplicate pair (``IMG.jpg(1).json`` ↔ ``IMG(1).jpg``);
* a 62-character screenshot name with its sidecar cut at 51 characters, and two
  long names sharing the cut prefix (ambiguous);
* a media name cut at 47 characters whose sidecar title is the full name;
* an ``-edited`` copy, a media file without sidecar, a sidecar without media,
  a zero ``0,0`` placeholder, an NFD file name with an NFC title, a HEIC file,
  an album ``metadata.json``, a pre-1970 (negative) and a millisecond-in-seconds
  timestamp, EXIF GPS in the southern/western hemisphere below sea level and an
  EXIF GPS block without a latitude reference;
* the same photo and sidecar in a second album folder (identical bytes).
"""
from __future__ import annotations

import hashlib
import json
import struct
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# TIFF/EXIF writer (little endian) with ASCII, BYTE, SHORT, LONG and RATIONAL values

_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 10: 8}


def _ascii(text: str) -> tuple[int, bytes, int]:
    raw = text.encode('latin-1') + b'\x00'
    return 2, raw, len(raw)


def _rationals(pairs) -> tuple[int, bytes, int]:
    return 5, b''.join(struct.pack('<II', n, d) for n, d in pairs), len(pairs)


def _bytes(values) -> tuple[int, bytes, int]:
    return 1, bytes(values), len(values)


def tiff(ifd0: dict, exif: dict | None = None, gps: dict | None = None) -> bytes:
    """IFD0 + optional EXIF and GPS IFDs; values are (type, payload bytes, count) triples."""
    ifds = [('ifd0', dict(ifd0)), ('exif', dict(exif or {})), ('gps', dict(gps or {}))]
    if exif is not None:
        ifds[0][1][0x8769] = (4, b'\x00\x00\x00\x00', 1)
    if gps is not None:
        ifds[0][1][0x8825] = (4, b'\x00\x00\x00\x00', 1)
    present = [(name, entries) for name, entries in ifds if name == 'ifd0' or (name == 'exif' and exif is not None)
               or (name == 'gps' and gps is not None)]
    offsets, position = {}, 8
    for name, entries in present:
        offsets[name] = position
        position += 2 + 12 * len(entries) + 4
    data_start = position
    data = bytearray()
    out = bytearray(b'II*\x00' + struct.pack('<I', 8))
    for name, entries in present:
        block = bytearray(struct.pack('<H', len(entries)))
        for tag in sorted(entries):
            kind, payload, count = entries[tag]
            if tag == 0x8769:
                payload = struct.pack('<I', offsets['exif'])
            if tag == 0x8825:
                payload = struct.pack('<I', offsets['gps'])
            if len(payload) <= 4:
                block += struct.pack('<HHI', tag, kind, count) + payload.ljust(4, b'\x00')
            else:
                offset = data_start + len(data)
                data += payload + (b'\x00' if len(payload) % 2 else b'')
                block += struct.pack('<HHII', tag, kind, count, offset)
        block += struct.pack('<I', 0)
        out += block
    return bytes(out + data)


def jpeg(block: bytes | None, payload: bytes = b'synthetic-photo') -> bytes:
    out = b'\xff\xd8'
    if block is not None:
        segment = b'Exif\x00\x00' + block
        out += b'\xff\xe1' + struct.pack('>H', len(segment) + 2) + segment
    comment = b'AGEDS synthetic ' + payload
    return out + b'\xff\xfe' + struct.pack('>H', len(comment) + 2) + comment + b'\xff\xd9'


def png(block: bytes | None, payload: bytes = b'synthetic') -> bytes:
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    out = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 0, 0, 0, 0))
    if block is not None:
        out += chunk(b'eXIf', block)
    return out + chunk(b'tEXt', b'Comment\x00' + payload) + chunk(b'IEND', b'')


def dms(value: float) -> list[tuple[int, int]]:
    value = abs(value)
    degrees = int(value)
    minutes_full = (value - degrees) * 60
    minutes = int(minutes_full)
    seconds = round((minutes_full - minutes) * 60 * 10000)
    return [(degrees, 1), (minutes, 1), (seconds, 10000)]


def gps_ifd(lat: float | None, lon: float | None, *, alt: float | None = None, date: str | None = None,
            time_hms: tuple | None = None, lat_ref: str | None = 'auto', datum: str | None = 'WGS-84',
            accuracy: float | None = None) -> dict:
    out = {0x0000: _bytes([2, 3, 0, 0])}
    if lat is not None:
        out[0x0002] = _rationals(dms(lat))
        if lat_ref == 'auto':
            out[0x0001] = _ascii('N' if lat >= 0 else 'S')
        elif lat_ref is not None:
            out[0x0001] = _ascii(lat_ref)
    if lon is not None:
        out[0x0003] = _ascii('E' if lon >= 0 else 'W')
        out[0x0004] = _rationals(dms(lon))
    if alt is not None:
        out[0x0005] = _bytes([1 if alt < 0 else 0])
        out[0x0006] = _rationals([(round(abs(alt) * 10), 10)])
    if time_hms is not None:
        out[0x0007] = _rationals(time_hms)
    if date is not None:
        out[0x001D] = _ascii(date)
    if datum is not None:
        out[0x0012] = _ascii(datum)
    if accuracy is not None:
        out[0x001F] = _rationals([(round(accuracy * 100), 100)])
    return out


def exif_photo(original: str | None, *, offset: str | None = None, gps: dict | None = None, make: str = 'Example',
               container: str = 'jpeg', payload: bytes = b'photo') -> bytes:
    exif = {}
    if original is not None:
        exif[0x9003] = _ascii(original)
    if offset is not None:
        exif[0x9011] = _ascii(offset)
    block = tiff({0x010F: _ascii(make), 0x0110: _ascii('Synthetic Cam 1')}, exif, gps)
    return jpeg(block, payload) if container == 'jpeg' else png(block, payload)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)
    return path


def sidecar(title: str, taken: str | int | None, *, created: str | int | None = None, geo=None, geo_exif=None,
            people=None, description: str = '', url: str | None = None, formatted: str | None = None) -> str:
    data = {'title': title, 'description': description, 'imageViews': '3'}
    if created is not None:
        data['creationTime'] = {'timestamp': str(created), 'formatted': 'synthetic creation display text'}
    if taken is not None:
        data['photoTakenTime'] = {'timestamp': str(taken), 'formatted': formatted or 'synthetic display text UTC'}
    if geo is not None:
        data['geoData'] = {'latitude': geo[0], 'longitude': geo[1], 'altitude': geo[2], 'latitudeSpan': 0.0,
                           'longitudeSpan': 0.0}
    if geo_exif is not None:
        data['geoDataExif'] = {'latitude': geo_exif[0], 'longitude': geo_exif[1], 'altitude': geo_exif[2],
                               'latitudeSpan': 0.0, 'longitudeSpan': 0.0}
    if people is not None:
        data['people'] = [{'name': name} for name in people]
    data['url'] = url or f'https://photos.google.com/photo/SYNTHETIC-{_sha(title.encode())[:12]}'
    data['googlePhotosOrigin'] = {'mobileUpload': {'deviceType': 'ANDROID_PHONE'}}
    return json.dumps(data, ensure_ascii=False, indent=2)


# Open-sea points (no address, no private place).
SEA_A = (10.5, -30.25, 12.5)
SEA_B = (-20.125, 60.5, -3.0)
SEA_C = (35.0, -45.0, 0.0)


def build_photos(root: Path) -> dict:
    year = root / 'Takeout' / 'Google Photos' / 'Photos from 2021'
    album = root / 'Takeout' / 'Google Photos' / 'Wakacje (album)'
    files: dict[str, Path] = {}
    # 1. exact sidecar; geoData == geoDataExif; EXIF GPS time 09:15:00 UTC, DateTimeOriginal 10:15:00 (no zone)
    gps_a = gps_ifd(SEA_A[0], SEA_A[1], alt=SEA_A[2], date='2021:01:01', time_hms=[(9, 1), (15, 1), (0, 1)],
                    accuracy=4.5)
    photo_a = exif_photo('2021:01:01 10:15:00', gps=gps_a, payload=b'photo-a')
    files['a'] = _write(year / 'IMG_20210101_101500.jpg', photo_a)
    files['a_json'] = _write(year / 'IMG_20210101_101500.jpg.json',
                             sidecar('IMG_20210101_101500.jpg', 1609492500, created=1609600000, geo=SEA_A, geo_exif=SEA_A))
    # 2. supplemental-metadata; contradictory geoData vs geoDataExif; people + RTL/emoji description
    files['b'] = _write(year / 'IMG_20210102_120000.jpg', exif_photo('2021:01:02 12:00:00', offset='+01:00',
                                                                     payload=b'photo-b'))
    files['b_json'] = _write(year / 'IMG_20210102_120000.jpg.supplemental-metadata.json',
                             sidecar('IMG_20210102_120000.jpg', 1609585200, geo=SEA_C, geo_exif=SEA_A,
                                     people=['Ala Przykładowa', 'Jan Testowy'],
                                     description='שלום 👋 opis z emoji ‮ odwrócony'))
    # 3. duplicate counter
    files['c'] = _write(year / 'IMG_20210103_080000.jpg', exif_photo('2021:01:03 08:00:00', payload=b'photo-c'))
    files['c1'] = _write(year / 'IMG_20210103_080000(1).jpg', exif_photo('2021:01:03 08:00:01', payload=b'photo-c1'))
    files['c_json'] = _write(year / 'IMG_20210103_080000.jpg.json', sidecar('IMG_20210103_080000.jpg', 1609660800))
    files['c1_json'] = _write(year / 'IMG_20210103_080000.jpg(1).json', sidecar('IMG_20210103_080000.jpg', 1609660801))
    # 4. long screenshot name; sidecar name cut at 51 characters
    long_name = 'Screenshot_20210104-235959_Very_Long_Application_Name_Here.png'
    cut = (long_name + '.supplemental-metadata')[:46] + '.json'
    files['d'] = _write(year / long_name, png(None, b'screenshot'))
    files['d_json'] = _write(year / cut, sidecar(long_name, 1609804799))
    # 5. two media names share the cut prefix -> ambiguous
    shared = 'Screenshot_20210109-101010_Another_Very_Long_Nam'
    files['e1'] = _write(year / (shared + 'e_A.png'), png(None, b'e1'))
    files['e2'] = _write(year / (shared + 'e_B.png'), png(None, b'e2'))
    files['e_json'] = _write(year / ((shared + 'e_A.png.supplemental-metadata')[:46] + '.json'),
                             sidecar(shared + 'e.png', 1610187010))  # title names neither file
    # 6. media name cut at 47 characters, sidecar title holds the full name
    full = 'Very_long_original_file_name_from_messenger_app_2021_01.jpg'
    cut_media = full[:43] + '.jpg'
    files['f'] = _write(year / cut_media, exif_photo(None, payload=b'photo-f'))
    files['f_json'] = _write(year / (full[:46] + '.json'), sidecar(full, 1610000000))
    # 7. edited copy, 8. media without sidecar, 9. sidecar without media, 10. zero placeholder
    files['g'] = _write(year / 'IMG_20210105_090000.jpg', exif_photo('2021:01:05 09:00:00', payload=b'photo-g'))
    files['g_edited'] = _write(year / 'IMG_20210105_090000-edited.jpg', exif_photo('2021:01:05 09:00:00', payload=b'photo-g-ed'))
    files['g_json'] = _write(year / 'IMG_20210105_090000.jpg.json',
                             sidecar('IMG_20210105_090000.jpg', 1609837200, geo=(0.0, 0.0, 0.0), geo_exif=(0.0, 0.0, 0.0)))
    files['h'] = _write(year / 'IMG_20210106_070000.jpg', exif_photo('2021:01:06 07:00:00', payload=b'photo-h'))
    files['i_json'] = _write(year / 'IMG_20210107_070000.jpg.json', sidecar('IMG_20210107_070000.jpg', 1610002800))
    # 11. NFD file name, NFC title
    nfc = 'Zdjęcie_łąka_ü.jpg'
    files['j'] = _write(year / unicodedata.normalize('NFD', nfc), exif_photo('2021:01:08 10:00:00', payload=b'photo-j'))
    files['j_json'] = _write(year / (nfc + '.json'), sidecar(nfc, 1610100000))
    # 12. HEIC (EXIF not readable here) with sidecar; 13. album metadata.json
    files['k'] = _write(year / 'IMG_20210110_101010.HEIC', b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic' + b'\x00' * 64)
    files['k_json'] = _write(year / 'IMG_20210110_101010.HEIC.supplemental-metadata.json',
                             sidecar('IMG_20210110_101010.HEIC', 1610273410, geo=SEA_B, geo_exif=SEA_B))
    files['album_meta'] = _write(album / 'metadata.json', json.dumps(
        {'title': 'Wakacje (album)', 'description': '', 'access': 'protected',
         'date': {'timestamp': '1610000000', 'formatted': 'synthetic'}}))
    # 14. pre-1970 negative timestamp; 15. milliseconds written where seconds are declared
    files['l'] = _write(year / 'SCAN_1960.jpg', exif_photo(None, payload=b'scan-1960'))
    files['l_json'] = _write(year / 'SCAN_1960.jpg.json', sidecar('SCAN_1960.jpg', -315619200))
    files['m'] = _write(year / 'IMG_MS.jpg', exif_photo(None, payload=b'photo-ms'))
    files['m_json'] = _write(year / 'IMG_MS.jpg.json', sidecar('IMG_MS.jpg', 1609459200000))
    # 16. southern/western hemisphere below sea level; 17. GPS without latitude ref
    files['n'] = _write(year / 'IMG_SOUTH.jpg', exif_photo(
        '2021:02:01 06:00:00', gps=gps_ifd(SEA_B[0], -SEA_B[1], alt=SEA_B[2], date='2021:02:01',
                                           time_hms=[(5, 1), (59, 1), (5925, 100)]), payload=b'south'))
    files['o'] = _write(year / 'IMG_NOREF.jpg', exif_photo('2021:02:02 06:00:00', gps=gps_ifd(SEA_A[0], SEA_A[1], lat_ref=None),
                                                          payload=b'noref'))
    files['p'] = _write(year / 'IMG_PNG_GPS.png', exif_photo('2021:02:03 06:00:00', container='png',
                                                             gps=gps_ifd(SEA_C[0], SEA_C[1]), payload=b'pnggps'))
    # 18. same photo + sidecar in an album folder (identical bytes, second locator)
    files['a_album'] = _write(album / 'IMG_20210101_101500.jpg', photo_a)
    files['a_album_json'] = _write(album / 'IMG_20210101_101500.jpg.json', files['a_json'].read_text(encoding='utf-8'))
    return {
        'root': str(root / 'Takeout'),
        'files': {k: str(v) for k, v in files.items()},
        'sha256': {k: _sha(v.read_bytes()) for k, v in files.items()},
        'coordinates': {'a': SEA_A, 'b_geo': SEA_C, 'b_geo_exif': SEA_A, 'south': (SEA_B[0], -SEA_B[1], SEA_B[2])},
        'expect': {
            'exact': 'IMG_20210101_101500.jpg', 'gps_time_a': '2021-01-01T09:15:00Z',
            'duplicate': ('IMG_20210103_080000.jpg(1).json', 'IMG_20210103_080000(1).jpg'),
            'cut_sidecar': (cut, long_name), 'ambiguous_sidecar': files['e_json'].name,
            'cut_media': (files['f_json'].name, cut_media), 'edited': 'IMG_20210105_090000-edited.jpg',
            'without_sidecar': 'IMG_20210106_070000.jpg', 'sidecar_without_media': 'IMG_20210107_070000.jpg.json',
            'nfd_media': unicodedata.normalize('NFD', nfc), 'heic': 'IMG_20210110_101010.HEIC',
            'south_gps_time': '2021-02-01T05:59:59.250000Z',
        },
    }


def mojibake(text: str) -> str:
    """How Facebook/Instagram JSON downloads write text: each UTF-8 byte as its own code point."""
    return ''.join(chr(b) for b in text.encode('utf-8'))


def _json(path: Path, value, *, ascii_escapes: bool = True) -> Path:
    return _write(path, json.dumps(value, ensure_ascii=ascii_escapes, indent=1))


FB_T0 = 1_600_000_000_000  # 2020-09-13T12:26:40Z in milliseconds


def build_facebook(root: Path) -> dict:
    """Facebook download (legacy JSON with mojibake) plus a Messenger E2EE secure-storage file."""
    base = root / 'facebook-ala-2024' / 'your_activity_across_facebook'
    thread_dir = base / 'messages' / 'inbox' / 'alaprzykladowa_10abc'
    photo = _write(thread_dir / 'photos' / 'p_1.jpg', exif_photo('2020:09:13 14:26:00', payload=b'fb-photo'))
    owner, friend = 'Owner Testowy', 'Ala Przykładowa'
    messages = [
        {'sender_name': mojibake(friend), 'timestamp_ms': FB_T0 + 60_000, 'content': mojibake('Cześć! Zażółć gęślą jaźń 😀'),
         'reactions': [{'reaction': mojibake('😆'), 'actor': mojibake(owner)}], 'is_geoblocked_for_viewer': False},
        {'sender_name': mojibake(owner), 'timestamp_ms': FB_T0 + 120_000,
         'photos': [{'uri': 'your_activity_across_facebook/messages/inbox/alaprzykladowa_10abc/photos/p_1.jpg',
                     'creation_timestamp': 1_600_000_100},
                    {'uri': 'your_activity_across_facebook/messages/inbox/alaprzykladowa_10abc/photos/gone.jpg',
                     'creation_timestamp': 1_600_000_101}]},
        {'sender_name': mojibake(friend), 'timestamp_ms': FB_T0 + 180_000, 'content': 'https://example.org/a',
         'share': {'link': 'https://example.org/a', 'share_text': mojibake('Ładny link')}, 'type': 'Share'},
        {'sender_name': mojibake(owner), 'timestamp_ms': FB_T0 + 240_000, 'is_unsent': True},
        {'sender_name': mojibake(friend), 'timestamp_ms': FB_T0 + 300_000, 'call_duration': 125, 'type': 'Call'},
        {'sender_name': mojibake(friend), 'timestamp_ms': FB_T0 + 360_000, 'content': 'already proper → arrow'},
        {'sender_name': mojibake(friend), 'timestamp_ms': FB_T0 + 420_000, 'content': 'bad \u00c3 sequence'},
        {'sender_name': mojibake(friend), 'timestamp_ms': FB_T0 + 480_000, 'content': mojibake('שלום עולם') + ' \ud83d'},
        {'sender_name': mojibake(friend), 'timestamp_ms': FB_T0 + 540_000, 'content': 'twice'},
        {'sender_name': mojibake(friend), 'timestamp_ms': FB_T0 + 540_000, 'content': 'twice'},
        {'sender_name': mojibake(friend), 'timestamp_ms': 1_600_000_600, 'content': 'seconds written as ms'},
        {'sender_name': mojibake(friend), 'timestamp_ms': 'soon', 'content': 'unparsed time'},
        'not a message object',
        {'sender_name': mojibake(friend), 'timestamp_ms': FB_T0 + 660_000, 'type': 'Mystery', 'content': 'unknown type'},
    ]
    part1 = {'participants': [{'name': mojibake(friend)}, {'name': mojibake(owner)}], 'messages': messages[::-1],
             'title': mojibake(friend), 'is_still_participant': True, 'thread_path': 'inbox/alaprzykladowa_10abc',
             'magic_words': []}
    text = json.dumps(part1, ensure_ascii=True, indent=1)  # \u00XX escapes as in a real download
    m1 = _write(thread_dir / 'message_1.json', text)
    m2 = _json(thread_dir / 'message_2.json', {
        'participants': [{'name': mojibake(friend)}, {'name': mojibake(owner)}],
        'messages': [{'sender_name': mojibake(owner), 'timestamp_ms': FB_T0 - 86_400_000, 'content': mojibake('Pierwsza wiadomość')}],
        'title': mojibake(friend), 'thread_path': 'inbox/alaprzykladowa_10abc'})
    group = _json(base / 'messages' / 'archived_threads' / 'grupa_20def' / 'message_1.json', {
        'participants': [{'name': 'Group Member A'}, {'name': 'Group Member B'}],
        'messages': [{'sender_name': 'Group Member A', 'timestamp_ms': FB_T0 + 1, 'content': 'plain ascii'},
                     {'sender_name': 'Group Member B', 'timestamp_ms': FB_T0 + 2, 'content': 'Group Member A joined',
                      'type': 'Subscribe', 'users': [{'name': 'Group Member A'}]}],
        'title': 'Grupa testowa'})
    e2ee = _json(root / 'messenger-secure-storage' / 'ala_przykladowa_1.json', {
        'participants': [friend, owner], 'threadName': friend,
        'messages': [{'isUnsent': False, 'media': [], 'reactions': [{'reaction': '❤', 'actor': owner}], 'senderName': friend,
                      'text': 'Zażółć — proper UTF-8, never decoded', 'timestamp': FB_T0 + 900_000, 'type': 'text'},
                     {'isUnsent': False, 'media': [{'uri': './media/voice.mp4'}], 'reactions': [], 'senderName': owner,
                      'text': '', 'timestamp': FB_T0 + 960_000, 'type': 'media'}]}, ascii_escapes=False)
    insta_dir = root / 'instagram-ala-2024' / 'your_instagram_activity' / 'messages' / 'inbox' / 'ala_17842'
    insta = _json(insta_dir / 'message_1.json', {
        'participants': [{'name': mojibake('Ala Przykładowa')}, {'name': 'owner.example'}],
        'messages': [{'sender_name': mojibake('Ala Przykładowa'), 'timestamp_ms': FB_T0 + 5000,
                      'content': mojibake('Hej 👋 co słychać?'), 'reactions': [{'reaction': mojibake('❤️'), 'actor': 'owner.example', 'timestamp': 1600000010}],
                      'is_geoblocked_for_viewer': False}],
        'title': mojibake('Ala Przykładowa'), 'is_still_participant': True,
        'thread_path': 'inbox/ala_17842', 'magic_words': []})
    return {'root': str(root / 'facebook-ala-2024'), 'thread_dir': str(thread_dir), 'message_1': str(m1),
            'message_2': str(m2), 'group': str(group), 'e2ee': str(e2ee), 'photo': str(photo),
            'instagram_root': str(root / 'instagram-ala-2024'), 'instagram_message': str(insta),
            'owner': owner, 'friend': friend, 'first_text': 'Cześć! Zażółć gęślą jaźń 😀'}


def build_telegram(root: Path) -> dict:
    full = root / 'DataExport_2024-01-01'
    _write(full / 'photos' / 'photo_1@01-03-2021_11-02-00.jpg', exif_photo(None, payload=b'tg-photo'))
    owner, friend = 'Owner Testowy', 'Ala Przykładowa'

    def msg(mid, unix, text='', **extra):
        local = None
        if unix is not None:
            from datetime import datetime, timedelta, timezone
            local = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=unix) + timedelta(hours=1)).strftime(
                '%Y-%m-%dT%H:%M:%S')
        item = {'id': mid, 'type': 'message', 'date': local, 'date_unixtime': str(unix), 'from': friend,
                'from_id': 'user6000001', 'text': text, 'text_entities': []}
        if unix is None:
            item.pop('date_unixtime')
        item.update(extra)
        return item
    t0 = 1614592800  # 2021-03-01T10:00:00Z; the exporting computer wrote local UTC+1
    personal = [
        msg(10, t0, 'Cześć 😀 שלום'),
        msg(11, t0 + 60, ['See ', {'type': 'link', 'text': 'https://example.org'}, ' and ', {'type': 'bold', 'text': 'bold'}],
            **{'from': owner, 'from_id': 'user5000001', 'reply_to_message_id': 10, 'edited': '2021-03-01T11:05:00',
               'edited_unixtime': str(t0 + 300)}),
        msg(12, t0 + 120, '', photo='photos/photo_1@01-03-2021_11-02-00.jpg', width=1, height=1),
        msg(13, t0 + 180, '', file='(File not included. Change data exporting settings to download.)',
            media_type='voice_message', mime_type='audio/ogg', duration_seconds=3),
        msg(14, t0 + 240, '', location_information={'latitude': SEA_A[0], 'longitude': SEA_A[1]}),
        {'id': 15, 'type': 'service', 'date': '2021-03-01T11:05:00', 'date_unixtime': str(t0 + 300), 'actor': owner,
         'actor_id': 'user5000001', 'action': 'phone_call', 'duration_seconds': 61, 'discard_reason': 'hangup',
         'text': '', 'text_entities': []},
        {'id': 16, 'type': 'message', 'date': '2021-03-28T02:30:00', 'from': friend, 'from_id': 'user6000001',
         'forwarded_from': 'Kanał Przykładowy', 'text': 'forwarded, older export without date_unixtime'},
        {'id': 17, 'type': 'message', 'date': 'not a date', 'from': friend, 'from_id': 'user6000001', 'text': 'bad date'},
        {**msg(18, t0, 'milliseconds where seconds are declared'), 'date_unixtime': str(t0 * 1000)},
        'not an object',
        {'id': 19, 'type': 'message', 'date': '2021-03-01T12:00:00', 'date_unixtime': str(t0 + 3600), 'from': friend,
         'from_id': 'user6000001', 'text': 'reacted', 'reactions': [{'type': 'emoji', 'count': 1, 'emoji': '👍',
                                                                    'recent': [{'from': owner, 'from_id': 'user5000001'}]}]},
    ]
    data = {'about': 'Synthetic export.',
            'personal_information': {'user_id': 5000001, 'first_name': 'Owner', 'last_name': 'Testowy',
                                     'phone_number': '+48 000 000 101', 'username': '@owner_example', 'bio': ''},
            'chats': {'about': 'chats', 'list': [
                {'name': friend, 'type': 'personal_chat', 'id': 6000001, 'messages': personal},
                {'name': 'Grupa testowa', 'type': 'private_supergroup', 'id': 7000001,
                 'messages': [msg(1, t0 + 7200, 'group hello', **{'from': 'Group Member A', 'from_id': 'user7000002'})]}]},
            'left_chats': {'about': 'left', 'list': [
                {'name': 'Stara grupa', 'type': 'private_group', 'id': 8000001,
                 'messages': [msg(1, t0 - 86400, 'left chat message')]}]}}
    result = _json(full / 'result.json', data, ascii_escapes=False)
    single = _json(root / 'ChatExport_2021-03-02' / 'result.json',
                   {'name': friend, 'type': 'personal_chat', 'id': 6000001, 'messages': personal[:2]}, ascii_escapes=False)
    html = _write(root / 'HtmlExport' / 'messages.html', '<html><body><div class="message">x</div></body></html>')
    return {'root': str(full), 'result': str(result), 'single': str(single), 'html_dir': str(html.parent),
            'owner': owner, 'friend': friend}


def build_google_chat(root: Path) -> dict:
    groups = root / 'Takeout' / 'Google Chat' / 'Groups'
    dm = groups / 'DM abc123'
    _json(dm / 'group_info.json', {'members': [{'name': 'Ala Przykładowa', 'email': 'ala@example.org', 'user_type': 'Human'},
                                               {'name': 'Owner Testowy', 'email': 'owner@example.com', 'user_type': 'Human'}]})
    _write(dm / 'File-photo.jpg', exif_photo(None, payload=b'gchat'))
    messages = [
        {'creator': {'name': 'Ala Przykładowa', 'email': 'ala@example.org', 'user_type': 'Human'},
         'created_date': 'Tuesday, March 2, 2021 at 10:00:00 AM UTC', 'text': 'Cześć z Google Chat',
         'topic_id': 'TOPIC1', 'message_id': 'abc123/TOPIC1',
         'reactions': [{'emoji': {'unicode': '👍'}, 'reactor_emails': ['owner@example.com']}]},
        {'creator': {'name': 'Owner Testowy', 'email': 'owner@example.com', 'user_type': 'Human'},
         'created_date': 'Tuesday, March 2, 2021 at 10:05:00 AM CET', 'updated_date': 'Tuesday, 2 March 2021 at 10:06:00 UTC',
         'text': 'abbreviated zone, edited', 'topic_id': 'TOPIC1', 'message_id': 'abc123/MSG2',
         'attached_files': [{'original_name': 'photo.jpg', 'export_name': 'File-photo.jpg'},
                            {'original_name': 'gone.pdf', 'export_name': 'File-gone.pdf'}]},
        {'creator': {'name': 'Ala Przykładowa', 'email': 'ala@example.org', 'user_type': 'Human'},
         'created_date': 'wtorek, 2 marca 2021 o 10:10:00 UTC', 'text': 'localized date not in the month table',
         'message_id': 'abc123/MSG3'},
        {'creator': {'name': 'Ala Przykładowa', 'email': 'ala@example.org', 'user_type': 'Human'},
         'created_date': 'Tuesday, March 2, 2021 at 10:15:00 PM GMT+02:00', 'text': 'no message id here'},
    ]
    _json(dm / 'messages.json', {'messages': messages}, ascii_escapes=False)
    space = groups / 'Space AAAA'
    _json(space / 'group_info.json', {'name': 'Projekt testowy', 'members': []})
    _json(space / 'messages.json', {'messages': [
        {'creator': {'name': 'Bot', 'email': None, 'user_type': 'Bot'}, 'created_date': 'March 3, 2021, 9:00:00 AM UTC',
         'text': 'space message', 'message_id': 'AAAA/T1'}]})
    return {'root': str(root / 'Takeout' / 'Google Chat'), 'dm': str(dm / 'messages.json'), 'owner': 'owner@example.com'}


def _ytd(path: Path, name: str, records, *, prefix: bool = True) -> Path:
    body = json.dumps(records, ensure_ascii=False, indent=1)
    return _write(path, (f'window.YTD.{name}.part0 = ' if prefix else '') + body)


def build_x(root: Path) -> dict:
    data = root / 'twitter-2024' / 'data'
    _ytd(data / 'account.js', 'account', [{'account': {'email': 'owner@example.com', 'username': 'owner_example',
                                                        'accountId': '111', 'createdAt': '2010-01-01T00:00:00.000Z'}}])
    tweets = [
        {'tweet': {'id_str': '1300000000000000001', 'created_at': 'Wed Sep 02 10:00:00 +0000 2020',
                   'full_text': 'Hello 👋 #test', 'lang': 'en', 'favorite_count': '1', 'retweet_count': '0',
                   'source': '<a href="https://example.org">Example client</a>'}},
        {'tweet': {'id_str': '1300000000000000002', 'created_at': 'Thu Sep 03 23:59:59 +0530 2020',
                   'full_text': 'RT @someone: retweeted text'}},
        {'tweet': {'id_str': '1300000000000000003', 'created_at': 'not a date', 'full_text': 'a reply with a photo',
                   'in_reply_to_status_id_str': '1200000000000000000',
                   'extended_entities': {'media': [{'media_url_https': 'https://pbs.example.org/media/AbC.jpg',
                                                    'type': 'photo'}]}}},
    ]
    _ytd(data / 'tweets.js', 'tweets', tweets)
    _write(data / 'tweets_media' / '1300000000000000003-AbC.jpg', exif_photo(None, payload=b'tweet-media'))
    _ytd(data / 'tweets-part1.js', 'tweets', [{'id_str': '1300000000000000004', 'created_at': 'Fri Sep 04 08:00:00 -0700 2020',
                                                'full_text': 'old layout without the tweet wrapper and no prefix'}],
         prefix=False)
    dms = [{'dmConversation': {'conversationId': '111-222', 'messages': [
        {'messageCreate': {'recipientId': '222', 'reactions': [{'senderId': '222', 'reactionKey': 'like'}], 'urls': [],
                           'text': 'hi from the owner', 'mediaUrls': [], 'senderId': '111', 'id': '5001',
                           'createdAt': '2020-09-02T10:00:00.000Z', 'editHistory': []}},
        {'messageCreate': {'recipientId': '111', 'text': 'reply with media',
                           'mediaUrls': ['https://ton.example.org/dm/5002/1/xyz.jpg'], 'senderId': '222', 'id': '5002',
                           'createdAt': '2020-09-02T10:01:00.000Z'}},
        {'messageCreate': {'recipientId': '111', 'text': 'no time', 'senderId': '222', 'id': '5003'}}]}}]
    _ytd(data / 'direct-messages.js', 'direct_messages', dms)
    group = [{'dmConversation': {'conversationId': '9001', 'messages': [
        {'joinConversation': {'initiatingUserId': '333', 'participantsSnapshot': ['111', '333'],
                              'createdAt': '2020-09-05T10:00:00.000Z'}},
        {'conversationNameUpdate': {'initiatingUserId': '333', 'name': 'Grupa X', 'createdAt': '2020-09-05T10:01:00.000Z'}},
        {'messageCreate': {'text': 'group message', 'senderId': '111', 'id': '6001', 'createdAt': '2020-09-05T10:02:00.000Z'}},
        {'mysteryEvent': {'createdAt': '2020-09-05T10:03:00.000Z'}}]}}]
    _ytd(data / 'direct-messages-group.js', 'direct_messages_group', group)
    return {'root': str(root / 'twitter-2024'), 'tweets': str(data / 'tweets.js'), 'dms': str(data / 'direct-messages.js'),
            'owner_id': '111'}


def build_linkedin(root: Path) -> dict:
    header = 'CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER'
    rows = [
        '2-abc,,Ala Przykładowa,https://www.linkedin.com/in/ala-example,Owner Testowy,https://www.linkedin.com/in/owner-example,'
        '2023-03-01 10:00:00 UTC,,Dzień dobry,INBOX',
        '2-abc,,Owner Testowy,https://www.linkedin.com/in/owner-example,Ala Przykładowa,https://www.linkedin.com/in/ala-example,'
        '2023-03-01 10:05:00 UTC,,"<p>Multi<br>line, with ""quotes""</p>\nsecond physical line",SENT',
        '2-def,Oferta,Recruiter Example,,Owner Testowy,,2023-03-02 11:00,Oferta pracy,Plain text without zone,INBOX',
        '2-def,Oferta,Recruiter Example,,Owner Testowy,,yesterday,,bad date,INBOX',
        '2-def,Oferta,Recruiter Example',
        '2-ghi,,Ala Przykładowa,,Owner Testowy,,2023-03-03 09:00:00 +02:00,,with offset,INBOX',
    ]
    path = _write(root / 'Basic_LinkedInDataExport_03-2023' / 'messages.csv', '\ufeff' + header + '\r\n' + '\r\n'.join(rows) + '\r\n')
    return {'root': str(path.parent), 'messages': str(path), 'owner': 'Owner Testowy'}


def build_account_activity(root: Path) -> dict:
    fb = root / 'facebook-ala-2024' / 'security_and_login_information'
    files = {}
    files['fb_activity'] = _json(fb / 'account_activity.json', {'account_activity_v2': [
        {'action': 'Login', 'timestamp': 1600000000, 'ip_address': '192.0.2.10', 'user_agent': 'Mozilla/5.0 (X11; Linux x86_64)',
         'datr_cookie': 'SYNTHETICcookieVALUE', 'city': mojibake('Łódź'), 'region': mojibake('Łódzkie'), 'country': 'PL',
         'site_name': 'www.facebook.com'},
        {'action': 'Session updated', 'timestamp': 0, 'ip_address': '2001:db8::1', 'user_agent': 'App/1.0',
         'site_name': 'm.facebook.com'},
        {'action': 'Login', 'timestamp': '1600000500', 'ip_address': 'not-an-ip'},
        'not a record']})
    files['fb_logins'] = _json(fb / 'logins_and_logouts.json', {'account_accesses_v2': [
        {'action': 'Log in', 'timestamp': 1600001000, 'site': 'www.facebook.com', 'ip_address': '198.51.100.20'},
        {'action': 'Log out', 'timestamp': 1600004600, 'site': 'www.facebook.com', 'ip_address': '198.51.100.20'}]})
    files['fb_sessions'] = _json(fb / "where_you're_logged_in.json", {'active_sessions_v2': [
        {'created_timestamp': 1599000000, 'updated_timestamp': 1600009000, 'ip_address': '203.0.113.9',
         'user_agent': 'Mozilla/5.0', 'datr_cookie': 'OTHERcookie', 'device': 'Synthetic Phone', 'location': 'Warszawa, Polska',
         'app': 'Messenger', 'session_type': 'mobile'}]})
    files['fb_label_values'] = _json(fb / 'authorized_logins.json', [
        {'timestamp': 1700000000, 'media': [], 'fbid': '100000000000001',
         'label_values': [{'label': 'Name', 'value': 'Firefox on Linux'}, {'label': 'IP address', 'value': '198.51.100.7'},
                          {'label': 'Estimated location', 'value': 'Kraków, PL'}, {'label': 'datr cookie', 'value': 'abc'},
                          {'label': 'Created', 'timestamp_value': 1699990000}]}])
    ig = root / 'instagram-ala-2024' / 'security_and_login_information' / 'login_and_profile_creation'
    files['ig_login'] = _json(ig / 'login_activity.json', {'account_history_login_history': [
        {'title': '2023-01-01T10:00:00+00:00', 'media_map_data': {}, 'string_map_data': {
            'Cookie Name': {'href': '', 'value': 'SYNTHETICIGCOOKIE', 'timestamp': 0},
            'IP Address': {'href': '', 'value': '203.0.113.5', 'timestamp': 0},
            'Language Code': {'href': '', 'value': 'pl', 'timestamp': 0},
            'Time': {'href': '', 'value': '', 'timestamp': 1672567200},
            'User Agent': {'href': '', 'value': 'Instagram 300.0 Android', 'timestamp': 0}}}]})
    files['ig_logout'] = _json(ig / 'logout_activity.json', {'account_history_logout_history': [
        {'title': '2023-01-01T12:00:00+00:00', 'string_map_data': {
            'IP Address': {'value': '203.0.113.5'}, 'Time': {'value': '', 'timestamp': 1672574400}}}]})
    files['ig_old'] = _json(root / 'instagram-2019' / 'login_history.json', {'login_history': [
        {'cookie_name': 'OLDCOOKIE', 'ip_address': '192.0.2.200', 'language_code': 'en', 'timestamp': '2019-05-01T08:00:00+02:00',
         'user_agent': 'Mozilla/5.0'}]})
    access = root / 'Takeout' / 'Access Log Activity'
    files['google_activities'] = _write(access / 'Activities - A list of Google services accessed by.csv', '\r\n'.join([
        'Gaia ID,Activity Timestamp,IP Address,Activity Country,Activity Region,Activity City,User Agent String,'
        'Product Name,Sub-Product Name,Activity Type',
        '100000000000000000001,2023-01-01 10:00:00 UTC,192.0.2.33,PL,Mazowieckie,Warszawa,Mozilla/5.0,Gmail,Inbox,Read',
        '100000000000000000001,2023-01-01 10:05:00,192.0.2.33,PL,,,Mozilla/5.0,Drive,,Open',
        '100000000000000000001,yesterday,2001:db8::42,NL,,,,Search,,Query']) + '\r\n')
    files['google_devices'] = _write(access / 'Devices - A list of devices (i.e. Nest, Pixel, iPh.csv', '\r\n'.join([
        'Device Type,Device Model,Device Brand,OS,OS Version,Device First Activity Timestamp,Device Last Activity Timestamp,'
        'Device Last Location',
        'Phone,Synthetic Model 1,Example,Android,14,2022-06-01 08:00:00 UTC,2023-01-01 09:00:00 UTC,"Warszawa, PL"']) + '\r\n')
    files['not_activity'] = _json(root / 'misc' / 'other.json', {'something_else': []})
    return {k: str(v) for k, v in files.items()}


CORE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator><cp:lastModifiedBy>{modifier}</cp:lastModifiedBy><cp:revision>{revision}</cp:revision>
{times}</cp:coreProperties>"""
APP_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Template>Normal.dotm</Template><TotalTime>{total}</TotalTime><Pages>1</Pages><Words>3</Words><Application>{app}</Application><Company>Example Sp. z o.o.</Company><AppVersion>16.0000</AppVersion></Properties>"""
CONTENT_TYPES = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>'


def _zip(path: Path, members: dict, *, compression=None) -> Path:
    import zipfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w', compression=compression or zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _times_xml(created=None, modified=None, printed=None) -> str:
    out = ''
    if created:
        out += f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>'
    if modified:
        out += f'<dcterms:modified xsi:type="dcterms:W3CDTF">{modified}</dcterms:modified>'
    if printed:
        out += f'<cp:lastPrinted>{printed}</cp:lastPrinted>'
    return out


def _ooxml(path: Path, *, word: str, created=None, modified=None, printed=None, creator='Ala Przykładowa',
           modifier='Jan Testowy', app='Microsoft Office Word', total='42', core=None) -> Path:
    members = {'[Content_Types].xml': CONTENT_TYPES, f'{word}/document.xml': '<document/>',
               'docProps/core.xml': core if core is not None else CORE_XML.format(
                   title='Umowa — wersja 2 ✓', creator=creator, modifier=modifier, revision='7',
                   times=_times_xml(created, modified, printed)),
               'docProps/app.xml': APP_XML.format(total=total, app=app)}
    return _zip(path, members)


def _pdf(objects: list[bytes], trailer: bytes | None, *, version=b'1.4', tail: bytes = b'') -> bytes:
    out = b'%PDF-' + version + b'\n%\xe2\xe3\xcf\xd3\n'
    for item in objects:
        out += item + b'\n'
    if trailer is not None:
        out += b'xref\n0 1\n0000000000 65535 f \ntrailer\n' + trailer + b'\nstartxref\n0\n%%EOF\n'
    return out + tail


def _utf16_hex(text: str) -> bytes:
    return b'<' + (b'\xfe\xff' + text.encode('utf-16-be')).hex().upper().encode() + b'>'


def build_documents(root: Path) -> dict:
    import zlib
    files = {}
    files['docx'] = _ooxml(root / 'Umowa.docx', word='word', created='2020-01-01T10:00:00Z', modified='2021',
                           printed='2020-06')
    files['xlsx'] = _ooxml(root / 'Budzet.xlsx', word='xl', created='2020-02-02T08:00:00.123+02:00',
                           modified='2020-02-03T25:00:00Z', app='Microsoft Excel', total='0')
    files['pptx'] = _ooxml(root / 'Slajdy.pptx', word='ppt', created='2019-12-31T23:59:59+14:00', modified=None,
                           app='LibreOffice/7.6', creator='', modifier='')
    files['docx_entity'] = _ooxml(root / 'Entity.docx', word='word', core='<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
                                  '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
                                  'core-properties"><cp:revision>&a;</cp:revision></cp:coreProperties>')
    big_core = CORE_XML.format(title='x' * (5 * 1024 * 1024), creator='a', modifier='b', revision='1', times='')
    files['docx_bomb'] = _ooxml(root / 'Bomba.docx', word='word', core=big_core)
    good = files['docx'].read_bytes()
    files['docx_corrupt'] = _write(root / 'Uszkodzony.docx', good[:len(good) // 2])
    files['odt'] = _zip(root / 'Notatka.odt', {
        'mimetype': 'application/vnd.oasis.opendocument.text',
        'meta.xml': '<?xml version="1.0" encoding="UTF-8"?><office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" xmlns:dc="http://purl.org/dc/elements/1.1/" office:version="1.3">'
                    '<office:meta><meta:creation-date>2021-05-04T10:20:30.123456789</meta:creation-date><dc:date>2021-05-05T11:00:00</dc:date>'
                    '<meta:editing-duration>PT1H2M3S</meta:editing-duration><meta:editing-cycles>4</meta:editing-cycles>'
                    '<meta:generator>LibreOffice/7.6.4.1$Linux_X86_64</meta:generator><meta:initial-creator>Ala Przykładowa</meta:initial-creator>'
                    '<dc:creator>Jan Testowy</dc:creator><meta:document-statistic meta:page-count="1" meta:word-count="12"/></office:meta>'
                    '</office:document-meta>',
        'content.xml': '<office:document-content/>'})
    info = (b'3 0 obj\n<< /Producer (Synthetic PDF \\(test\\) \\351crit) /Creator (Writer) /Author ' + _utf16_hex('Ala Przykładowa')
            + b' /CreationDate (D:20200101120000+01\'00\') /ModDate (D:2020) /Title (A \\\\ backslash) >>\nendobj')
    files['pdf_simple'] = _write(root / 'prosty.pdf', _pdf([b'1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj',
                                                           b'2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj', info],
                                                          b'<< /Size 4 /Root 1 0 R /Info 3 0 R >>'))
    update = (b'3 0 obj\n<< /Producer (Synthetic PDF) /CreationDate (D:20200101120000+01\'00\') '
              b'/ModDate (D:20220202020202Z00\'00\') >>\nendobj\nxref\n0 1\n0000000000 65535 f \n'
              b'trailer\n<< /Size 4 /Root 1 0 R /Info 3 0 R /Prev 0 >>\nstartxref\n0\n%%EOF\n')
    files['pdf_incremental'] = _write(root / 'aktualizowany.pdf', files['pdf_simple'].read_bytes() + update)
    objstm_body = b'3 0 << /Producer (Synthetic ObjStm) /CreationDate (D:20210203040506Z) /Author (Ala) >>'
    packed = zlib.compress(objstm_body)
    objstm = (b'5 0 obj\n<< /Type /ObjStm /N 1 /First 4 /Filter /FlateDecode /Length ' + str(len(packed)).encode()
              + b' >>\nstream\n' + packed + b'\nendstream\nendobj')
    xref = b'6 0 obj\n<< /Type /XRef /Size 7 /Root 1 0 R /Info 3 0 R /W [1 2 1] /Length 0 >>\nstream\n\nendstream\nendobj'
    files['pdf_objstm'] = _write(root / 'strumien.pdf', _pdf([b'1 0 obj << /Type /Catalog >> endobj', objstm, xref], None,
                                                            version=b'1.5', tail=b'startxref\n0\n%%EOF\n'))
    xmp = ('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
           'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
           'xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:pdf="http://ns.adobe.com/pdf/1.3/" '
           'xmlns:dc="http://purl.org/dc/elements/1.1/" xmp:CreateDate="2020-01-01T09:00:00Z" '
           'xmp:CreatorTool="Example Writer 1.0"><xmp:ModifyDate>2023-03-03T03:03:03+01:00</xmp:ModifyDate>'
           '<pdf:Producer>Example XMP Producer</pdf:Producer><dc:creator><rdf:Seq><rdf:li>Ala Przykładowa</rdf:li>'
           '<rdf:li>Jan Testowy</rdf:li></rdf:Seq></dc:creator></rdf:Description></rdf:RDF></x:xmpmeta>'
           '<?xpacket end="w"?>').encode('utf-8')
    meta_obj = b'4 0 obj\n<< /Type /Metadata /Subtype /XML /Length ' + str(len(xmp)).encode() + b' >>\nstream\n' + xmp + b'\nendstream\nendobj'
    info_x = b'3 0 obj\n<< /Producer (Info Producer) /CreationDate (D:20200101120000+01\'00\') /ModDate (D:20230303) >>\nendobj'
    files['pdf_xmp'] = _write(root / 'xmp.pdf', _pdf([b'1 0 obj << /Type /Catalog /Metadata 4 0 R >> endobj', info_x, meta_obj],
                                                    b'<< /Size 5 /Root 1 0 R /Info 3 0 R >>'))
    bad = (b'3 0 obj\n<< /CreationDate (D:20200101120000+25\'00\') /ModDate (D:20201301) /Producer (20200101 is not a date) '
           b'>>\nendobj')
    files['pdf_bad'] = _write(root / 'zle-daty.pdf', _pdf([b'1 0 obj << /Type /Catalog >> endobj', bad],
                                                         b'<< /Size 4 /Root 1 0 R /Info 3 0 R >>'))
    nodprefix = (b'3 0 obj\n<< /CreationDate (20200101) /ModDate (D:20200101120000Z00\'00\') /Producer (x) >>\nendobj')
    files['pdf_nod'] = _write(root / 'bez-prefiksu.pdf', _pdf([b'1 0 obj << /Type /Catalog >> endobj', nodprefix],
                                                             b'<< /Size 4 /Root 1 0 R /Info 3 0 R >>'))
    encrypted = b'3 0 obj\n<< /Producer <A1B2C3D4> /CreationDate <00112233> >>\nendobj'
    files['pdf_encrypted'] = _write(root / 'szyfrowany.pdf', _pdf([b'1 0 obj << /Type /Catalog >> endobj', encrypted],
                                                                 b'<< /Size 4 /Root 1 0 R /Info 3 0 R /Encrypt 9 0 R >>'))
    files['pdf_noinfo'] = _write(root / 'bez-info.pdf', _pdf([b'1 0 obj << /Type /Catalog >> endobj'], b'<< /Size 2 /Root 1 0 R >>'))
    files['ole_doc'] = _write(root / 'stary.doc', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 504)
    return {k: str(v) for k, v in files.items()}


def build_keep(root: Path) -> dict:
    keep = root / 'Takeout' / 'Keep'
    usec = 1_600_000_000_000_000
    files = {}
    files['list'] = _json(keep / 'Zakupy.json', {
        'color': 'YELLOW', 'isTrashed': False, 'isPinned': True, 'isArchived': False, 'title': 'Zakupy',
        'listContent': [{'text': 'mleko', 'textHtml': '<p>mleko</p>', 'isChecked': True},
                        {'text': 'chleb 🍞', 'isChecked': False}],
        'labels': [{'name': 'dom'}], 'userEditedTimestampUsec': usec + 500_000_000, 'createdTimestampUsec': usec,
        'sharees': [{'isOwner': True, 'type': 'WRITER', 'email': 'owner@example.com'},
                    {'isOwner': False, 'type': 'WRITER', 'email': 'ala@example.org'}]}, ascii_escapes=False)
    _write(keep / 'Zakupy.html', '<html><body>Zakupy</body></html>')
    _write(keep / 'abc.png', png(None, b'keep'))
    files['text'] = _json(keep / 'Pomysł.json', {
        'color': 'DEFAULT', 'isTrashed': False, 'isPinned': False, 'isArchived': True, 'title': 'Pomysł',
        'textContent': 'Notatka z emoji 😀 i tekstem RTL: שלום', 'textContentHtml': '<p>Notatka</p>',
        'annotations': [{'description': '', 'source': 'WEBLINK', 'title': 'Example', 'url': 'https://example.org/x'}],
        'attachments': [{'filePath': 'abc.png', 'mimetype': 'image/png'}, {'filePath': 'missing.jpg', 'mimetype': 'image/jpeg'}],
        'userEditedTimestampUsec': usec + 3_600_000_000, 'createdTimestampUsec': usec + 60_000_000}, ascii_escapes=False)
    files['trashed'] = _json(keep / 'Kosz.json', {'isTrashed': True, 'title': '', 'textContent': 'usunięta',
                                                  'userEditedTimestampUsec': usec, 'createdTimestampUsec': usec})
    files['wrong_unit'] = _json(keep / 'Jednostki.json', {'title': 'ms in a Usec field', 'textContent': 'x',
                                                          'createdTimestampUsec': 1_600_000_000_000,
                                                          'userEditedTimestampUsec': 'not a number'})
    files['not_note'] = _json(keep / 'Labels.json', {'labels': ['dom']})
    _write(keep / 'Labels.txt', 'dom\n')
    return {'root': str(keep), **{k: str(v) for k, v in files.items()}}


WEBKIT_OFFSET_S = 11_644_473_600


def _webkit(unix_seconds: float) -> int:
    return int(round((unix_seconds + WEBKIT_OFFSET_S) * 1_000_000))


def build_browsers(root: Path) -> dict:
    import shutil
    import sqlite3
    import tempfile
    t0 = 1_614_592_800  # 2021-03-01T10:00:00Z
    chrome_dir = root / 'chrome-profile' / 'Default'
    chrome_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='ageds-corpus-browser-'))
    db_path = work / 'History'
    connection = sqlite3.connect(db_path)
    connection.executescript("""
        CREATE TABLE urls(id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR, visit_count INTEGER DEFAULT 0,
                          typed_count INTEGER DEFAULT 0, last_visit_time INTEGER NOT NULL DEFAULT 0, hidden INTEGER DEFAULT 0);
        CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER NOT NULL, visit_time INTEGER NOT NULL, from_visit INTEGER,
                            transition INTEGER DEFAULT 0, segment_id INTEGER, visit_duration INTEGER DEFAULT 0);
        PRAGMA journal_mode=WAL;
    """)
    long_url = 'https://example.org/' + 'a' * 5000
    connection.executemany('INSERT INTO urls(id,url,title) VALUES (?,?,?)', [
        (1, 'https://example.org/start', 'Start'), (2, 'https://example.com/wiadomości?q=zażółć', 'Zażółć 😀'),
        (3, 'https://ads.example.net/frame', None), (4, long_url, 'Long')])
    connection.executemany('INSERT INTO visits(id,url,visit_time,from_visit,transition,visit_duration) VALUES (?,?,?,?,?,?)', [
        (1, 1, _webkit(t0), 0, 1 | 0x30000000, 5_000_000),
        (2, 2, _webkit(t0 + 600), 1, 0, 0),
        (3, 3, _webkit(t0 + 601), 2, 3, 0),          # auto subframe: counted, not a session visit
        (4, 4, _webkit(t0 + 1200), 2, 0, 0),
        (5, 1, _webkit(t0 + 7200), 0, 1, 0),         # two hours later: a new session
        (6, 99, _webkit(t0 + 7300), 5, 0, 0),        # url row missing
        (7, 1, 0, 0, 0, 0),                          # zero placeholder
        (8, 1, 2**62, 0, 0, 0),                      # out of range
        (9, 1, (t0 * 1_000_000), 0, 0, 0)])          # unix microseconds written into a WebKit field
    connection.commit()
    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute('INSERT INTO visits(id,url,visit_time,from_visit,transition) VALUES (10,1,?,0,1)', (_webkit(t0 + 7400),))
    connection.commit()
    shutil.copyfile(db_path, chrome_dir / 'History')
    shutil.copyfile(work / 'History-wal', chrome_dir / 'History-wal')  # committed visit 10 lives only in the WAL
    connection.close()
    shutil.rmtree(work, ignore_errors=True)
    nowal_dir = root / 'chrome-copy'
    nowal_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='ageds-corpus-browser-'))
    shutil.copyfile(chrome_dir / 'History', work / 'History')
    shutil.copyfile(chrome_dir / 'History-wal', work / 'History-wal')
    merged = sqlite3.connect(work / 'History')
    merged.execute('PRAGMA journal_mode=DELETE')
    merged.close()
    shutil.copyfile(work / 'History', nowal_dir / 'History')
    shutil.rmtree(work, ignore_errors=True)
    firefox = root / 'firefox-profile' / 'abcd1234.default-release'
    firefox.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(firefox / 'places.sqlite')
    connection.executescript("""
        CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR, rev_host LONGVARCHAR,
                                visit_count INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0 NOT NULL, typed INTEGER DEFAULT 0 NOT NULL,
                                frecency INTEGER DEFAULT -1 NOT NULL, last_visit_date INTEGER, guid TEXT);
        CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, from_visit INTEGER, place_id INTEGER, visit_date INTEGER,
                                       visit_type INTEGER, session INTEGER, source INTEGER DEFAULT 0 NOT NULL);
    """)
    connection.executemany('INSERT INTO moz_places(id,url,title) VALUES (?,?,?)', [
        (1, 'https://example.org/ff', 'Firefox start'), (2, 'https://cdn.example.net/embed', None)])
    connection.executemany('INSERT INTO moz_historyvisits(id,from_visit,place_id,visit_date,visit_type) VALUES (?,?,?,?,?)', [
        (1, 0, 1, t0 * 1_000_000, 2), (2, 1, 2, t0 * 1_000_000 + 1, 4), (3, 0, 1, (t0 + 60) * 1_000_000, 1),
        (4, 0, 1, t0 * 1000, 1)])  # milliseconds written into a PRTime (µs) field
    connection.commit()
    connection.close()
    other = root / 'other.sqlite'
    connection = sqlite3.connect(other)
    connection.execute('CREATE TABLE t(x)')
    connection.commit()
    connection.close()
    return {'chrome_dir': str(chrome_dir), 'chrome_history': str(chrome_dir / 'History'),
            'chrome_nowal': str(nowal_dir / 'History'), 'firefox_dir': str(firefox),
            'firefox_places': str(firefox / 'places.sqlite'), 'other_sqlite': str(other), 't0': t0}


def build(root: Path) -> dict:
    return {'photos': build_photos(root / 'photos'), 'facebook': build_facebook(root / 'facebook'),
            'telegram': build_telegram(root / 'telegram'), 'google_chat': build_google_chat(root / 'google_chat'),
            'x': build_x(root / 'x'), 'linkedin': build_linkedin(root / 'linkedin'),
            'account_activity': build_account_activity(root / 'account_activity'),
            'documents': build_documents(root / 'documents'), 'keep': build_keep(root / 'keep'),
            'browsers': build_browsers(root / 'browsers')}

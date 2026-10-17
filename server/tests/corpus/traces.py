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


def build(root: Path) -> dict:
    return {'photos': build_photos(root / 'photos'), 'facebook': build_facebook(root / 'facebook')}

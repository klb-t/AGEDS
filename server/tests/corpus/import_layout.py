"""Multi-device layout for the import planner (AGEDS-TASK-20261016-I).

Three source roots stand for three collections:

* ``phone_a`` — WhatsApp export zip and a WhatsApp folder export, call-recorder
  files, ``calls-*.xml`` and ``sms-*.xml`` (SMS Backup & Restore), photos with
  and without EXIF, an operator bill (CSV), a file whose name looks like HTML;
* ``drive_b`` — a Google Takeout zip holding ``archive_browser.html``, a nested
  zip with Mail/Calendar/Contacts/My Activity and Location/Chrome files; a
  ChatGPT export zip with ``conversations-NNN.json`` shards, ``user.json`` and a
  ``file-….dat`` that is a PDF; a Claude export directory; an ambiguous
  ``conversations.json`` (empty list, no companions);
* ``drive_c`` — a byte-identical copy of the ChatGPT export zip and a later
  ChatGPT export that contains the same conversation id.

It reuses the generators of the ``chat_exports`` and ``communications`` parts
(built into ``root/_parts``, outside the three source roots). All content is
synthetic; numbers use the fictional ranges and names the example domains of
those parts. ``build`` returns the planted expectations.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

from . import chat_exports, communications
from .chat_exports import C_ID, S_ID, _claude_export, _conversation_c, _message, _node, _simple_conversation, epoch

DAT_ID = 'file-DAT1'
DAT_CONV = '6b1f0c2e-0000-4000-8000-00000000c0d1'
PDF_DAT = (b'%PDF-1.7\n% AGEDS synthetic ChatGPT export attachment in a .dat file\n'
           b'1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n')
DOS = (2024, 10, 1, 12, 0, 0)
XSS_ICS_NAME = '<img src=x onerror=window.__ageds_xss=1>.ics'  # a planned item whose locator looks like markup
SOURCE_MTIME = 1704628800  # 2024-01-07T12:00:00Z, set on every source file (filesystem time is not an event time)


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(zipfile.ZipInfo(name, DOS), data, zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def _ftyp(major: bytes, compatible: list[bytes]) -> bytes:
    body = major + b'\x00\x00\x02\x00' + b''.join(compatible)
    import struct
    return struct.pack('>I', 8 + len(body)) + b'ftyp' + body + b'\x00\x00\x00\x08free' + b'\x00' * 64


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=1).encode('utf-8')


def _dat_conversation() -> dict:
    start = epoch('2025-03-02T08:00:00Z')
    mapping = {
        'root': _node('root', None, ['d1'], None),
        'd1': _node('d1', 'root', ['d2'], _message('d1', 'user', {
            'content_type': 'multimodal_text',
            'parts': [{'content_type': 'image_asset_pointer', 'asset_pointer': f'file-service://{DAT_ID}', 'size_bytes': len(PDF_DAT)},
                      'Please read the attached scan.']}, start + 5,
            metadata={'attachments': [{'id': DAT_ID, 'name': 'scan.pdf', 'mime_type': 'application/pdf'}]})),
        'd2': _node('d2', 'd1', [], _message('d2', 'assistant', {'content_type': 'text', 'parts': ['Read it.']}, start + 20,
                                             model='synthetic-model-1')),
    }
    return {'title': 'Attachment as .dat', 'create_time': start, 'update_time': start + 30, 'mapping': mapping,
            'current_node': 'd2', 'conversation_id': DAT_CONV, 'id': DAT_CONV, 'default_model_slug': 'synthetic-model-1'}


def chatgpt_export_zip() -> bytes:
    shard0 = _json([_conversation_c(2), _dat_conversation()])
    shard1 = _json([_simple_conversation(S_ID, 'Shard one', epoch('2025-01-05T12:00:00Z'), [0, 30, 60])])
    return _zip([
        ('conversations-000.json', shard0),
        ('conversations-001.json', shard1),
        ('user.json', b'{"id": "user-synthetic", "email": "someone@example.org"}'),
        (f'{DAT_ID}.dat', PDF_DAT),
        ('file-ATTACH1-notes.txt', b'synthetic attachment not referenced by any message'),
    ])


def tampered_takeout(parts: Path, *, grow: bool = False) -> bytes:
    """The Takeout zip with the nested MBOX member changed after the scan (same names, same order).

    By default one byte is replaced (same size, different SHA-256); ``grow`` appends a byte instead.
    """
    return _takeout(parts, tamper='grow' if grow else 'replace')


def _takeout(parts: Path, *, tamper: str | None = None) -> bytes:
    comm = parts / 'communications'
    activity = comm / 'takeout' / 'My Activity'
    mbox = (comm / 'mail' / 'takeout.mbox').read_bytes()
    if tamper == 'replace':
        middle = len(mbox) // 2
        mbox = mbox[:middle] + (b'Y' if mbox[middle:middle + 1] != b'Y' else b'Z') + mbox[middle + 1:]
    elif tamper == 'grow':
        mbox += b'X'
    inner = _zip([
        ('Mail/All mail Including Spam and Trash.mbox', mbox),
        ('Calendar/Example.ics', (comm / 'calendar' / 'calendar.ics').read_bytes()),
        ('Contacts/All Contacts/All Contacts.vcf', (comm / 'contacts' / 'contacts.vcf').read_bytes()),
        ('My Activity/Gemini Apps/MyActivity.json', (activity / 'Gemini Apps' / 'MyActivity.json').read_bytes()),
        ('My Activity/Search/MyActivity.html', (activity / 'Search' / 'MyActivity.html').read_bytes()),
        ('My Activity/YouTube/MyActivity.json', (activity / 'YouTube' / 'watch-history.json').read_bytes()),
    ])
    return _zip([
        ('Takeout/archive_browser.html', b'<!DOCTYPE html><html><body><h1>Archive overview (synthetic)</h1></body></html>'),
        ('Takeout/takeout-inner.zip', inner),
        ('Takeout/Location History/Records.json', (comm / 'takeout' / 'Location History' / 'Records.json').read_bytes()),
        ('Takeout/Chrome/BrowserHistory.json', (comm / 'takeout' / 'Chrome' / 'BrowserHistory.json').read_bytes()),
    ])


SMS_XML = '''<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<smses count="2" backup_set="synthetic">
  <sms protocol="0" address="+31600000001" date="1704446200000" type="1" subject="null" body="Synthetic hello" read="1" status="-1" readable_date="5 Jan 2024 10:16:40" contact_name="Example A" />
  <sms protocol="0" address="+48000000101" date="1704537100000" type="2" subject="null" body="Synthetic reply &lt;b&gt;not markup&lt;/b&gt;" read="1" status="-1" readable_date="6 Jan 2024 11:31:40" contact_name="(Unknown)" />
</smses>
'''


def build(root: Path) -> dict:
    parts = root / '_parts'
    chat_manifest = chat_exports.build(parts / 'chat_exports')
    comm_manifest = communications.build(parts / 'communications')
    comm = parts / 'communications'
    phone, drive_b, drive_c = root / 'phone_a', root / 'drive_b', root / 'drive_c'

    # ---- phone A
    _write(phone / 'WhatsApp' / 'export.zip', (comm / 'whatsapp' / 'export.zip').read_bytes())
    folder = comm / 'whatsapp' / 'folder_export'
    for item in sorted(folder.iterdir()):
        _write(phone / 'WhatsApp' / 'folder_export' / item.name, item.read_bytes())
    m4a = _ftyp(b'M4A ', [b'M4A ', b'isom'])
    amr = b'#!AMR\n' + b'\x3c' * 64
    recordings = {
        'matching_call': ('Call recording Example A_+31600000001_20240105_101500.m4a', m4a),
        'without_log': ('out-+48000000102-2024-01-06-18-30-00.amr', amr),
        'parenthesized': ('Call@Example B(+31600000002)_20240107093000.m4a', m4a + b'\x01'),
        'invalid_date': ('Call@Example C(+31600000004)_20241345101500.m4a', m4a + b'\x02'),
        'unmatched': ('voice note.m4a', m4a + b'\x03'),
    }
    for name, data in recordings.values():
        _write(phone / 'Recordings' / name, data)
    _write(phone / 'backup' / 'calls-20240107.xml', (comm / 'calls' / 'calls.xml').read_bytes())
    _write(phone / 'backup' / 'sms-20240107.xml', SMS_XML.encode('utf-8'))
    _write(phone / 'DCIM' / 'with_exif_offset.jpg', (comm / 'images' / 'with_exif_offset.jpg').read_bytes())
    _write(phone / 'DCIM' / 'without_exif.jpg', (comm / 'images' / 'without_exif.jpg').read_bytes())
    _write(phone / 'billing' / 'bill_daymonth.csv', (comm / 'billing' / 'bill_daymonth.csv').read_bytes())
    _write(phone / 'notes' / '<img src=x onerror=alert(1)>.txt', b'a file name that looks like markup')
    _write(phone / 'calendar' / XSS_ICS_NAME, (comm / 'calendar' / 'calendar.ics').read_bytes())

    # ---- drive B
    _write(drive_b / 'takeout-20241001.zip', _takeout(parts))
    export = chatgpt_export_zip()
    _write(drive_b / 'chatgpt-export-2025-03.zip', export)
    _write(drive_b / 'exports' / 'claude' / 'conversations.json', _json(_claude_export()))
    _write(drive_b / 'exports' / 'claude' / 'users.json', b'[{"email_address": "someone@example.org"}]')
    _write(drive_b / 'exports' / 'ambiguous' / 'conversations.json', b'[]')

    # ---- drive C
    _write(drive_c / 'copy' / 'chatgpt-export-2025-03.zip', export)
    _write(drive_c / 'later-export' / 'conversations.json', _json([_conversation_c(3)]))

    for source_root in (phone, drive_b, drive_c):
        for path in source_root.rglob('*'):
            if path.is_file():
                os.utime(path, (SOURCE_MTIME, SOURCE_MTIME))

    takeout_inner = 'takeout-20241001.zip!/Takeout/takeout-inner.zip'
    expected_items = {
        'phone_a': [
            ('file', 'DCIM/with_exif_offset.jpg', 'exif', []),
            ('file', f'Recordings/{recordings["matching_call"][0]}', 'recording_names', []),
            ('file', f'Recordings/{recordings["parenthesized"][0]}', 'recording_names', []),
            ('file', f'Recordings/{recordings["invalid_date"][0]}', 'recording_names', []),
            ('file', f'Recordings/{recordings["without_log"][0]}', 'recording_names', []),
            ('archive', 'WhatsApp/export.zip', 'whatsapp_export', []),
            ('directory', 'WhatsApp/folder_export', 'whatsapp_export', []),
            ('file', 'backup/calls-20240107.xml', 'calls_xml', []),
            ('file', 'backup/sms-20240107.xml', 'sms_backup', []),
            ('file', 'billing/bill_daymonth.csv', 'billing', ['parameter_missing:profile_id']),
            ('file', f'calendar/{XSS_ICS_NAME}', 'ics', []),
        ],
        'drive_b': [
            ('archive', 'chatgpt-export-2025-03.zip', 'chatgpt_export', []),
            ('directory', 'exports/ambiguous', None, ['importer_not_chosen:ambiguous_detection']),
            ('directory', 'exports/claude', 'claude_export', []),
            ('file', 'takeout-20241001.zip!/Takeout/Chrome/BrowserHistory.json', 'browser_history',
             ['opt_in_disabled:enable_browser_history']),
            ('file', 'takeout-20241001.zip!/Takeout/Location History/Records.json', 'location_history',
             ['opt_in_disabled:enable_location']),
            ('file', f'{takeout_inner}!/Calendar/Example.ics', 'ics', []),
            ('file', f'{takeout_inner}!/Contacts/All Contacts/All Contacts.vcf', 'vcf', []),
            ('file', f'{takeout_inner}!/Mail/All mail Including Spam and Trash.mbox', 'email_mbox', []),
            ('file', f'{takeout_inner}!/My Activity/Gemini Apps/MyActivity.json', 'takeout_activity', []),
            ('file', f'{takeout_inner}!/My Activity/Search/MyActivity.html', 'takeout_activity', []),
            ('file', f'{takeout_inner}!/My Activity/YouTube/MyActivity.json', 'browser_history',
             ['opt_in_disabled:enable_browser_history']),
        ],
        'drive_c': [
            ('archive', 'copy/chatgpt-export-2025-03.zip', 'chatgpt_export', []),
            ('directory', 'later-export', 'chatgpt_export', []),
        ],
    }
    return {
        'roots': {'phone_a': str(phone), 'drive_b': str(drive_b), 'drive_c': str(drive_c)},
        'parts': str(parts),
        'expected_items': expected_items,
        'ambiguous': ('drive_b', 'exports/ambiguous'),
        'opt_in_items': [('drive_b', 'takeout-20241001.zip!/Takeout/Chrome/BrowserHistory.json'),
                         ('drive_b', 'takeout-20241001.zip!/Takeout/Location History/Records.json'),
                         ('drive_b', f'{takeout_inner}!/My Activity/YouTube/MyActivity.json')],
        'not_planned': {
            'phone_a': {'DCIM/without_exif.jpg': 'structure_probe_did_not_match',
                        f'Recordings/{recordings["unmatched"][0]}': 'structure_probe_did_not_match',
                        'notes/<img src=x onerror=alert(1)>.txt': 'no_detector_matched'},
            'drive_b': {'takeout-20241001.zip!/Takeout/archive_browser.html': 'structure_probe_did_not_match'},
        },
        'takeout_context': 'google_takeout',
        'tampered_member': f'{takeout_inner}!/Mail/All mail Including Spam and Trash.mbox',
        'chatgpt_zip_sha256': hashlib.sha256(export).hexdigest(),
        'dat': {'member': f'{DAT_ID}.dat', 'sha256': hashlib.sha256(PDF_DAT).hexdigest()},
        'shared_conversations': {'chatgpt': {C_ID: 3, S_ID: 2, DAT_CONV: 2}},
        'whatsapp': comm_manifest['whatsapp'],
        'chat_manifest_keys': sorted(chat_manifest),
        'source_mtime_utc': '2024-01-07T12:00:00+00:00',
        'xss_locator': f'calendar/{XSS_ICS_NAME}',
    }

"""Multi-device synthetic layout for the end-to-end acceptance (AGEDS-TASK-20261016-Z).

It composes the existing corpus parts instead of duplicating them:

* ``import_layout`` gives three source roots — ``phone_a`` (device), ``drive_b``
  (account drive with a Google Takeout zip holding a nested zip, a ChatGPT export
  zip whose ``file-DAT1.dat`` is a PDF, a Claude export, an ambiguous empty
  ``conversations.json``) and ``drive_c`` (a byte-identical copy of the ChatGPT
  export zip on a second drive plus a later export of the same conversation);
* ``chat_exports`` adds the third ChatGPT export of conversation C (version 1,
  so C exists in three distinct exports: an edit of ``u2`` and the branches
  ``a1x``/``a3b``) together with the parallel ChatGPT conversation that overlaps
  the Claude conversation in time (two chatbots at once);
* ``communications`` adds the PDF on disk that is byte-identical to the e-mail
  attachment in the MBOX (and to the WhatsApp ``DOC-…pdf`` member), and the
  billing table with an AM/PM time and an unknown day/month order;
* ``audio`` adds the interleaved PL/NL/EN fixture under a call-recorder style
  name. It is SYNTHETIC SINE TONES, NOT SPEECH: a fake ASR model maps tone
  frequencies to languages; nothing about real recognition quality follows;
* ``catalog_files`` style planted cases: a file-name collision with different
  bytes across collections and two filesystem-time clusters with a long gap.

The planted call log (``backup/calls-20240107.xml``) has a call with a matching
recording, a call without a recording (``+48000000101``) and the recordings
directory has a recording without any call-log entry (``+48000000102``) plus the
code-switch recording (``+31600000005``, no call either) one minute after the
call that has a recording, so the time gate sees two recordings near one call.

All content is synthetic; numbers use the fictional ranges of those parts and
addresses the example domains. ``build`` returns the planted expectations.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ..corpus import audio, chat_exports, communications, import_layout
from ..corpus.catalog_files import CLUSTER1, CLUSTER2

SOURCE_MTIME = import_layout.SOURCE_MTIME
CODESWITCH_NAME = 'Call recording Example D_+31600000005_20240105_101600.wav'
E1_DIR = 'exports/chatgpt-2024-12'
PDF_ON_DISK = 'Documents/raport.pdf'
BILLING_INFER = 'billing/bill_infer.csv'
BILLING_DAYMONTH = 'billing/bill_daymonth.csv'
INFER_PROFILE = 'example_datetime_infer_order_v1'
DAYMONTH_PROFILE = 'example_semicolon_daymonth_v1'
COLLECTIONS = {
    'phone_a': {'label': 'Telefon A (syntetyczny)', 'device_or_account': 'phone A (synthetic)',
                'export_date_raw': '2024-01-07', 'acquisition_method': 'directory copy', 'operator': 'e2e'},
    'drive_b': {'label': 'Dysk konto B (syntetyczny)', 'device_or_account': 'account B drive (synthetic)',
                'export_date_raw': '2025-03', 'acquisition_method': 'takeout + exports', 'operator': 'e2e'},
    'drive_c': {'label': 'Dysk C kopia (syntetyczny)', 'device_or_account': 'second drive C (synthetic)',
                'export_date_raw': '2025-04', 'acquisition_method': 'manual copy', 'operator': 'e2e'},
}


def _write(path: Path, data: bytes, mtime: float = SOURCE_MTIME) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(root: Path) -> dict:
    root = Path(root)
    base = import_layout.build(root)
    parts = Path(base['parts'])
    phone, drive_b, drive_c = (Path(base['roots'][k]) for k in ('phone_a', 'drive_b', 'drive_c'))
    audio_manifest = audio.build(parts / 'audio')
    comm = parts / 'communications'

    # --- third distinct ChatGPT export of conversation C (+ the parallel ChatGPT conversation)
    parallel = json.loads((parts / 'chat_exports' / 'chatgpt' / 'parallel' / 'conversations.json').read_text('utf-8'))
    e1 = [chat_exports._conversation_c(1), *parallel]
    _write(drive_b / E1_DIR / 'conversations.json', json.dumps(e1, ensure_ascii=False, indent=1).encode('utf-8'))

    # --- PDF on disk == e-mail attachment (and WhatsApp DOC member)
    _write(phone / PDF_ON_DISK, communications.PDF_BYTES)

    # --- AM/PM + unknown day/month order billing table (needs an explicit profile decision)
    _write(phone / BILLING_INFER, (comm / 'billing' / 'bill_infer.csv').read_bytes())

    # --- interleaved PL/NL/EN fixture (synthetic tones) under a call-recorder style name
    wav = Path(audio_manifest['audio_codeswitch']).read_bytes()
    _write(phone / 'Recordings' / CODESWITCH_NAME, wav)

    # --- file-name collision with different bytes across two collections
    _write(phone / 'notes' / 'todo.txt', b'phone todo list (synthetic)')
    _write(drive_b / 'todo.txt', b'drive todo list (synthetic) - different bytes')

    # --- filesystem-time clusters with a long gap (catalog coverage must report it)
    for index, moment in enumerate(CLUSTER1, 1):
        _write(phone / 'timeline' / 'cluster1' / f'day{index}.txt', f'cluster one {index}'.encode(), moment)
    for index, moment in enumerate(CLUSTER2, 1):
        _write(phone / 'timeline' / 'cluster2' / f'day{index}.txt', f'cluster two {index}'.encode(), moment)

    for directory in (phone, drive_b, drive_c):
        for path in directory.rglob('*'):
            if path.is_dir():
                os.utime(path, (SOURCE_MTIME, SOURCE_MTIME))

    return {
        **base,
        'collections': COLLECTIONS,
        'audio': {'label': audio_manifest['manifest']['label'], 'locator': f'Recordings/{CODESWITCH_NAME}',
                  'sha256': _sha(wav), 'languages': ['pl', 'nl', 'en'],
                  'segments': audio_manifest['manifest']['files']['codeswitch.wav']['segments']},
        'chatgpt_exports_of_c': {
            'e1': ('drive_b', E1_DIR), 'e2': ('drive_b', 'chatgpt-export-2025-03.zip'),
            'e2_copy': ('drive_c', 'copy/chatgpt-export-2025-03.zip'), 'e3': ('drive_c', 'later-export')},
        'conversation_c': chat_exports.C_ID,
        'c_expect': {'edited': 'u2', 'branch_only_in_e1_e2': 'a1x', 'regenerated_in_e3': 'a3b'},
        'parallel': {'chatgpt_id': chat_exports.P_ID, 'claude_id': chat_exports.K_ID,
                     'overlap_utc': ['2025-03-01T10:05:00Z', '2025-03-01T10:25:00Z'], 'day': '2025-03-01'},
        'pdf': {'disk': ('phone_a', PDF_ON_DISK), 'sha256': _sha(communications.PDF_BYTES),
                'mbox_member': base['tampered_member'], 'whatsapp_member': 'WhatsApp/export.zip!/DOC-20240105-WA0003.pdf'},
        'dat': base['dat'],
        'billing_profiles': {BILLING_DAYMONTH: DAYMONTH_PROFILE, BILLING_INFER: INFER_PROFILE},
        'ampm': {'billing': f'{BILLING_INFER}: 04/05/2024 11:00 PM',
                 'my_activity_html': 'Takeout My Activity Search HTML: 10:15:30 AM CET'},
        'unknown_zone': ['WhatsApp wall clocks', 'recording file-name times', 'ICS floating DTSTART',
                         'ICS TZID Mars/Olympus_Mons'],
        'dst': ['ICS weekly meeting across 2024-03-31', 'ICS 02:30 in the spring gap', 'ICS 02:30 in the autumn fold'],
        'collision': {'name': 'todo.txt', 'locations': [('phone_a', 'notes/todo.txt'), ('drive_b', 'todo.txt')]},
        'catalog_gap': {'collection': 'phone_a', 'last_before': '2024-01-09', 'first_after': '2024-09-01'},
        'event_gap': {'from': '2024-10-28', 'to': '2024-12-01',
                      'note': 'no event of any source between the DST-fold calendar entry and the old Claude conversation'},
        'recordings': {'with_call': 'Recordings/Call recording Example A_+31600000001_20240105_101500.m4a',
                       'without_call': 'Recordings/out-+48000000102-2024-01-06-18-30-00.amr',
                       'codeswitch_without_call': f'Recordings/{CODESWITCH_NAME}',
                       'call_without_recording_number': communications.NUMBER_B},
        'zip_in_zip': 'drive_b/takeout-20241001.zip!/Takeout/takeout-inner.zip',
    }

"""Isolated synthetic fixture for the owner's browser path (legacy_open on loopback).

Test-only: a fake Vertex transport (never a network call) and a worker thread that drains the
queue. The files offered for upload are generated here and live in a temporary directory.
"""
import io
import json
import math
import os
import struct
import tempfile
import threading
import time
import wave
import zipfile
import zlib
from pathlib import Path


def png() -> bytes:
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))


def files(root: Path) -> dict:
    root.mkdir()
    chat = io.BytesIO()
    with zipfile.ZipFile(chat, 'w') as z:
        z.writestr('WhatsApp-chat met Testcontact.txt',
                   '14-10-2026 10:42 - Testcontact: Hallo, hier is de brief\n'
                   '14-10-2026 10:43 - Testcontact: IMG-20261014-WA0003.jpg (bestand bijgevoegd)\n'
                   '15-10-2026 09:00 - Eigenaar: Dank je\n')
        z.writestr('IMG-20261014-WA0003.jpg', b'\xff\xd8\xff\xe0' + b'\x00' * 40)
    (root / 'WhatsApp-chat met Testcontact.zip').write_bytes(chat.getvalue())
    with wave.open(str(root / 'rozmowa-syntetyczna.wav'), 'wb') as audio:
        audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b''.join(struct.pack('<h', int(800 * math.sin(2 * math.pi * 330 * i / 8000))) for i in range(24000)))
    (root / 'zrzut-ekranu.png').write_bytes(png())
    return {name: str(root / name) for name in ('WhatsApp-chat met Testcontact.zip', 'rozmowa-syntetyczna.wav', 'zrzut-ekranu.png')}


def fake_vertex(config, body):
    """Deterministic replies shaped like Gemini's (synthetic test text)."""
    schema = body['generationConfig']['responseSchema']
    if 'segments' in schema['properties']:
        reply = {'segments': [{'start': 0.0, 'end': 1.5, 'speaker': 'S1', 'language': 'pl',
                               'text': 'Syntetyczna rozmowa testowa o terminie wizyty.'}]}
    else:
        reply = {'pages': [{'page': 1, 'lines': ['Syntetyczny zrzut ekranu']}]}
    return {'candidates': [{'content': {'parts': [{'text': json.dumps(reply, ensure_ascii=False)}]}}],
            'usageMetadata': {'promptTokenCount': 900, 'candidatesTokenCount': 60}}


def main():
    with tempfile.TemporaryDirectory(prefix='ageds-owner-browser-') as directory:
        root = Path(directory)
        os.environ.update(EW_DATA_DIR=str(root / 'data'), EW_DB_PATH=str(root / 'data' / 'evidence.sqlite'),
                          EW_STORE_DIR=str(root / 'data' / 'store'))
        (root / 'data' / 'store').mkdir(parents=True)
        from server.app import db, providers, worker
        from server.app.jobs import claim_job
        import uvicorn
        db.init_db()
        providers.vertex_transport = fake_vertex  # test-only transport; the network is never used

        def drain():
            while True:
                job = claim_job(worker_id='browser-fixture')
                if job is None:
                    time.sleep(0.3)
                    continue
                try:
                    worker.process_job(job)
                except Exception:  # recorded on the job by process_job
                    pass
        threading.Thread(target=drain, daemon=True).start()
        print(json.dumps({'files': files(root / 'upload')}), flush=True)
        from server.app.main import app
        uvicorn.run(app, host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    main()

"""Isolated synthetic fixture for the /media browser script (track D pages); never uses an existing database.

A generated WAV artifact with two transcript versions whose model text contains
markup-like words, an OCR version on a generated PNG and one queued job.
"""
import json
import math
import os
import struct
import tempfile
import wave
from pathlib import Path

LITERAL = '<img src=x onerror="window.__ageds_media_xss=1">'


def main():
    with tempfile.TemporaryDirectory(prefix='ageds-media-browser-') as directory:
        root = Path(directory)
        os.environ.update(EW_DATA_DIR=str(root), EW_DB_PATH=str(root / 'evidence.sqlite'), EW_STORE_DIR=str(root / 'store'))
        from server.app import db, evidence
        from server.app.media.queue import enqueue_media_job
        import uvicorn
        db.init_db()
        path = root / 'synthetic-tone.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b''.join(struct.pack('<h', int(1000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(32000)))
        artifact = evidence.ingest_file(path, source_id=evidence.ensure_source('synthetic', 'media browser fixture'),
                                        original_name='<b>tone</b>.wav', mime_type='audio/wav')
        words_a = [{'start': .1, 'end': .4, 'word': ' Dzień'}, {'start': .4, 'end': .8, 'word': ' dobry'},
                   {'start': .8, 'end': 1.2, 'word': ' ' + LITERAL}]
        words_b = [{'start': .1, 'end': .4, 'word': ' Goedemorgen'}, {'start': .4, 'end': .8, 'word': ' dobry'},
                   {'start': .8, 'end': 1.2, 'word': ' <script>window.__ageds_media_xss=2</script>'}]
        old = evidence.add_derived_text(artifact, 'transcript', ''.join(w['word'] for w in words_a), language='pl',
                                        segments=[{'start': .1, 'end': 1.2, 'text': ''.join(w['word'] for w in words_a), 'words': words_a}])
        new = evidence.add_derived_text(artifact, 'transcript', ''.join(w['word'] for w in words_b), language='nl',
                                        segments=[{'start': .1, 'end': 1.2, 'text': ''.join(w['word'] for w in words_b), 'words': words_b}])
        png = root / 'screenshot.png'
        png.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
        image = evidence.ingest_file(png, source_id=evidence.ensure_source('synthetic', 'media browser image'),
                                     original_name='<script>.png', mime_type='image/png')
        evidence.add_derived_text(image, 'ocr', 'OCR says ' + LITERAL, segments=[])
        job = enqueue_media_job("transcribe", artifact)["job_id"]
        print(json.dumps({'artifact': artifact, 'old': old, 'new': new, 'image': image, 'job': job, 'literal': LITERAL}), flush=True)
        uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    main()

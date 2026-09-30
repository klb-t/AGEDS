"""Isolated synthetic browser fixture; never points at an existing database."""
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import wave


def main():
    with tempfile.TemporaryDirectory(prefix='ageds-browser-') as directory:
        root = Path(directory)
        os.environ.update(EW_DATA_DIR=str(root), EW_DB_PATH=str(root / 'evidence.sqlite'), EW_STORE_DIR=str(root / 'store'))
        from server.app import db, evidence
        import uvicorn
        db.init_db()
        path = root / 'synthetic-tone.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b''.join(struct.pack('<h', int(1000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(64000)))
        artifact = evidence.ingest_file(path, source_id=evidence.ensure_source('synthetic', 'N12 browser fixture'))
        literal = ' <img src=x onerror="window.__sourceExecuted=1">'
        words = [{'start': .2, 'end': .5, 'word': ' Zażółć'},
                 {'start': .5, 'end': 1.1, 'word': '  gęślą'},
                 {'start': 1.1, 'end': 1.8, 'word': literal}]
        old = evidence.add_derived_text(artifact, 'transcript', ''.join(w['word'] for w in words), segments=[{'start': .2, 'end': 1.8, 'text': ''.join(w['word'] for w in words), 'words': words}])
        new = evidence.add_derived_text(artifact, 'transcript', ' NEW VERSION', segments=[{'start': 2, 'end': 3, 'text': ' NEW VERSION'}])
        print(json.dumps({'artifact': artifact, 'old': old, 'new': new, 'literal': literal}), flush=True)
        uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    main()

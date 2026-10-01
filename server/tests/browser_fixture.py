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
        from server.app import db, evidence, citations
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
        repeated_artifact = evidence.ingest_file(path, source_id=evidence.ensure_source('synthetic', 'save response fixture'),
                                               source_locator='synthetic:save-response')
        repeated = evidence.add_derived_text(repeated_artifact, 'transcript', ' echo echo', segments=[
            {'start': .0005, 'end': .0025, 'text': ' echo',
             'words': [{'start': .0005, 'end': .0025, 'word': ' echo'}]} for _ in range(2)])
        repeated_new = evidence.add_derived_text(repeated_artifact, 'transcript', ' NEW',
                                               segments=[{'start': 1, 'end': 2, 'text': ' NEW'}])
        history_artifact = evidence.ingest_file(path, source_id=evidence.ensure_source('synthetic', 'paged history fixture'),
                                                source_locator='synthetic:history')
        history_versions = []
        for index in range(125):
            raw = f' History {index} ' + literal
            version_id = evidence.add_derived_text(history_artifact, 'transcript', raw,
                segments=[{'start': .2, 'end': 1.2, 'text': raw}])
            history_versions.append(version_id)
            citations.create_citation(history_artifact, version_id, [0])
        with db.session() as connection:
            connection.executemany('INSERT INTO annotations(artifact_id,derived_text_id,kind,body) VALUES (?,?,?,?)',
                [(history_artifact, v, 'note', f'Note {i} ' + literal) for i, v in enumerate(history_versions)])
        budget_artifact = None
        if os.environ.get('AGEDS_BROWSER_BUDGET_FIXTURE') == '1':
            budget_artifact = evidence.ingest_file(path, source_id=evidence.ensure_source('synthetic', 'payload budget fixture'),
                                                  source_locator='synthetic:payload-budget')
            for index in range(6):
                raw = f'budget-{index} ' + ('x' * 900_000)
                version_id = evidence.add_derived_text(budget_artifact, 'transcript', raw,
                    segments=[{'start': .2, 'end': 1.2, 'text': raw}])
                citations.create_citation(budget_artifact, version_id, [0])
        print(json.dumps({'artifact': artifact, 'old': old, 'new': new, 'literal': literal,
                          'historyArtifact': history_artifact, 'historyVersions': history_versions, 'budgetArtifact': budget_artifact,
                          'repeatedArtifact': repeated_artifact, 'repeated': repeated, 'repeatedNew': repeated_new}), flush=True)
        uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    main()

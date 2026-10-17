"""Isolated synthetic fixture for the /traces browser script (L2 page); never uses an existing database.

Builds the traces corpus in a temporary directory, imports photos (without the
location opt-in), the Facebook folder (with a markup-like export label), the
Telegram export, documents and Keep notes, then serves the app.
"""
import json
import os
import tempfile
from pathlib import Path

LITERAL = '<img src=x onerror="window.__ageds_traces_xss=1">'


def main():
    with tempfile.TemporaryDirectory(prefix='ageds-traces-browser-') as directory:
        root = Path(directory)
        os.environ.update(EW_DATA_DIR=str(root / 'data'), EW_DB_PATH=str(root / 'data' / 'evidence.sqlite'),
                          EW_STORE_DIR=str(root / 'data' / 'store'))
        from server.app import db
        from server.app.importers import doc_metadata, facebook, google_keep, photos_takeout, telegram
        from server.tests.corpus import traces as corpus
        import uvicorn
        db.init_db()
        manifest = corpus.build(root / 'corpus')
        photos_takeout.import_photos_takeout(Path(manifest['photos']['root']))
        facebook.import_messages(Path(manifest['facebook']['root']), export_label=LITERAL)
        telegram.import_result(Path(manifest['telegram']['root']))
        doc_metadata.import_documents(root / 'corpus' / 'documents')
        google_keep.import_keep(Path(manifest['keep']['root']))
        print(json.dumps({'literal': LITERAL, 'people': 'Ala Przykładowa'}), flush=True)
        uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    main()

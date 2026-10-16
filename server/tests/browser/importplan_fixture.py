"""Isolated synthetic fixture for the /import browser script; never points at an existing database.

Builds the multi-device import layout (three collections) in a temporary
directory, scans it into a temporary SQLite catalog, prints one JSON line and
serves the app on ``AGEDS_BROWSER_PORT``.
"""
import json
import os
import tempfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix='ageds-importplan-browser-') as directory:
        root = Path(directory)
        data = root / 'data'
        os.environ.update(EW_DATA_DIR=str(data), EW_DB_PATH=str(data / 'evidence.sqlite'), EW_STORE_DIR=str(data / 'store'),
                          EW_SCAN_ROOTS='')
        from server.app import db
        from server.app.catalog import store
        from server.app.catalog.scan import run_catalog_scan
        from server.tests.corpus import import_layout
        import uvicorn
        db.init_db()
        expected = import_layout.build(root / 'corpus')
        labels = {'phone_a': 'Telefon A (synthetic)', 'drive_b': 'Dysk B / Takeout (synthetic)',
                  'drive_c': 'Dysk C (kopia, synthetic)'}
        collections = {}
        for key, label in labels.items():
            collections[key] = store.create_collection(label, device_or_account=key)['id']
            run_catalog_scan(expected['roots'][key], collections[key], temp_dir=root / 'spool')
        print(json.dumps({'collections': collections, 'labels': labels, 'xssLocator': expected['xss_locator'],
                          'ambiguous': expected['ambiguous'][1], 'corpus': str(root / 'corpus'),
                          'work': str(data / 'importplan-work')}), flush=True)
        uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    main()

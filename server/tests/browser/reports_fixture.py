"""Isolated synthetic fixture for the /reports browser acceptance; never a live catalog.

Builds the ReportEnv corpus in a temporary directory configured through the
environment before the application is imported, prints one JSON line with the
planted ids and the private report directory, then serves the app.
"""
import json
import os
from pathlib import Path
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix='ageds-reports-browser-') as directory:
        root = Path(directory)
        # Configure before any server.app import: settings are read at import time.
        os.environ.update({'EW_DATA_DIR': str(root / 'data'), 'EW_DB_PATH': str(root / 'data' / 'evidence.db'),
                           'EW_STORE_DIR': str(root / 'data' / 'store'), 'EW_SCAN_ROOTS': str(root / 'scan')})
        os.environ['EW_REPORTS_DIR'] = str(root / 'private-reports')
        os.environ['EW_REQUIRE_TOKEN'] = ''
        from server.app import db
        from server.tests.test_reports_support import ReportEnv
        import uvicorn
        db.init_db()
        env = ReportEnv(root=root)
        env.plant()
        print(json.dumps({'reportsDir': os.environ['EW_REPORTS_DIR'], 'artifactAudio': env.artifact_audio,
                          'anchorSegment': env.anchor_segment['id'], 'events': env.events,
                          'pdf': bool(os.environ.get('EW_PDF_CHROMIUM'))}), flush=True)
        uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    main()

"""Billing parser version 1 output stays byte-identical after its header/date helpers moved.

The digest below was computed on base commit 790d386 (before the shared helpers moved
to ``importers/tabular_profiles.py``) with exactly this procedure. A changed digest
means billing's version-1 interpretation changed: that needs a new parser version,
not a new constant.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from server.app import db as dbmod, evidence
from server.app.config import settings
from server.app.importers import billing, tabular_profiles
from server.tests.corpus import communications

GOLDEN_SHA256 = 'a190bb7894accc20af48d69966122c86e7e8ecbe832c5b7cc20b5d4774fafd48'


def test_billing_output_is_byte_identical_to_the_base_commit(tmp_path, monkeypatch):
    test_settings = replace(settings, data_dir=tmp_path / 'd', db_path=tmp_path / 'd' / 'e.db',
                            store_dir=tmp_path / 'd' / 's')
    test_settings.store_dir.mkdir(parents=True)
    monkeypatch.setattr(dbmod, 'settings', test_settings)
    monkeypatch.setattr(evidence, 'settings', test_settings)
    dbmod.init_db()
    manifest = communications.build(tmp_path / 'c')
    out = {}
    for key in ('daymonth', 'infer', 'infer_dayfirst', 'xlsx'):
        if manifest['billing'].get(key):
            out[key] = billing.import_billing(Path(manifest['billing'][key]), source_locator=f'golden:{key}')
    with dbmod.session() as db:
        out['events'] = [dict(r) for r in db.execute(
            'SELECT id,artifact_id,source_id,event_type,ts_start,ts_end,direction,phone_or_address,external_id,'
            'metadata_json FROM events ORDER BY id')]
        out['times'] = [dict(r) for r in db.execute(
            'SELECT id,event_id,kind,raw,utc,local,tz_status,tz,precision,source,metadata_json FROM event_times ORDER BY id')]
    assert manifest['billing'].get('xlsx'), 'openpyxl is required for the golden digest (it covered the XLSX file)'
    digest = hashlib.sha256(json.dumps(out, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert (digest, len(out['events']), len(out['times'])) == (GOLDEN_SHA256, 13, 29)
    assert billing.PARSER_VERSION == '1'


def test_billing_uses_the_shared_helpers():
    for name in ('_norm', '_resolve_columns', '_date_parts', '_time_parts', 'infer_date_order', '_DATETIME_SPLIT'):
        assert getattr(billing, name) is getattr(tabular_profiles, name)

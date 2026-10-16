"""Model-produced labels and features as hypotheses (interface only; no real model).

Model output never becomes a deterministic number or replaces a rule or a human
correction. Each attempt is its own append-only row in ``analysis_model_runs``
with provider, model, version, parameters and the per-run opt-in. Providers:

* ``none`` (default) — records that no model ran; produces no output;
* ``fake`` — a deterministic synthetic stand-in for tests (hash of the event
  id). It is explicitly not a model and its outputs say so. It is a test double:
  every entry point (HTTP, CLI, Python) refuses it unless the process sets
  ``EW_ANALYSIS_TEST_PROVIDERS=1`` (review 2 finding 13; like the catalog's
  ``EW_CATALOG_TEST_PROVIDERS``).

Any other provider name is refused as ``unavailable``: no network or external
adapter exists in this increment.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

from .categories import CategoryConfig
from .provenance import canonical_json, event_set_sha256

PROVIDERS = ('none', 'fake')
TEST_PROVIDERS = ('fake',)
TEST_PROVIDER_ENV = 'EW_ANALYSIS_TEST_PROVIDERS'
PURPOSES = ('labels', 'feature:tone')
FAKE_MODEL = 'synthetic-fake'
FAKE_VERSION = 'fake.v1'
FAKE_RATIONALE = 'synthetic fake provider for tests: deterministic hash of the event id; not a model judgement'


def _fraction(event_id: int, salt: str) -> int:
    return int(hashlib.sha256(f'{salt}:{event_id}'.encode()).hexdigest()[:8], 16)


def test_providers_enabled() -> bool:
    """Synthetic test doubles are usable only when the operator/test process opts in by environment."""
    return os.environ.get(TEST_PROVIDER_ENV, '').strip().lower() in ('1', 'true', 'yes', 'on')


def run_model(db: sqlite3.Connection, items: list[dict], *, purpose: str, provider: str = 'none',
              opt_in: bool = False, config: CategoryConfig | None = None, parameters: dict | None = None) -> dict:
    if purpose not in PURPOSES:
        raise ValueError(f'purpose must be one of {PURPOSES}')
    if provider not in PROVIDERS:
        raise ValueError(f'provider {provider!r} is unavailable: no external model adapter is implemented')
    if provider != 'none' and opt_in is not True:
        raise ValueError('a model provider runs only with an explicit per-run opt_in=true')
    if provider in TEST_PROVIDERS and not test_providers_enabled():
        raise ValueError(f'provider {provider!r} is a synthetic test double, not a model; it is available only when '
                         f'the server/CLI process sets {TEST_PROVIDER_ENV}=1 (tests)')
    if purpose == 'labels' and provider != 'none' and config is None:
        raise ValueError('label hypotheses need a category configuration')
    ids = [it['event_id'] for it in items]
    status = 'not_run' if provider == 'none' else 'done'
    metadata = {'artifact_class': 'interpretive', 'output_is_hypothesis': True,
                'note': 'no model was called' if provider == 'none' else FAKE_RATIONALE}
    cur = db.execute('''INSERT INTO analysis_model_runs(purpose,provider,model,model_version,opt_in,status,
                        parameters_json,config_sha256,input_sha256,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                     (purpose, provider, FAKE_MODEL if provider == 'fake' else 'unknown',
                      FAKE_VERSION if provider == 'fake' else 'unknown', 1 if opt_in else 0, status,
                      canonical_json(parameters or {}), config.sha256 if config else None, event_set_sha256(ids),
                      canonical_json(metadata)))
    run_id = int(cur.lastrowid)
    outputs = 0
    if provider == 'fake':
        rows = []
        for eid in sorted(set(ids)):
            if purpose == 'labels':
                cats = config.category_ids
                output = {'categories': {cats[_fraction(eid, 'label') % len(cats)]: 1.0}}
            else:
                output = {'value': round(((_fraction(eid, 'tone') % 2001) - 1000) / 1000, 3), 'scale': '[-1, 1]'}
            rows.append((run_id, eid, canonical_json(output), FAKE_RATIONALE))
        db.executemany('INSERT INTO analysis_model_outputs(run_id,event_id,output_json,rationale) VALUES(?,?,?,?)', rows)
        outputs = len(rows)
    return {'model_run_id': run_id, 'purpose': purpose, 'provider': provider, 'status': status,
            'opt_in': bool(opt_in), 'outputs': outputs}


def get_model_run(db: sqlite3.Connection, run_id: int) -> dict | None:
    db.row_factory = sqlite3.Row
    r = db.execute('SELECT * FROM analysis_model_runs WHERE id=?', (run_id,)).fetchone()
    if r is None:
        return None
    return {'model_run_id': r['id'], 'purpose': r['purpose'], 'provider': r['provider'], 'model': r['model'],
            'model_version': r['model_version'], 'opt_in': bool(r['opt_in']), 'status': r['status'],
            'parameters': json.loads(r['parameters_json']), 'config_sha256': r['config_sha256'],
            'input_sha256': r['input_sha256'], 'metadata': json.loads(r['metadata_json']), 'created_at': r['created_at']}


def load_outputs(db: sqlite3.Connection, run_id: int, *, purpose: str) -> dict[int, dict]:
    run = get_model_run(db, run_id)
    if run is None:
        raise LookupError(f'model run {run_id} not found')
    if run['purpose'] != purpose:
        raise ValueError(f'model run {run_id} has purpose {run["purpose"]!r}, not {purpose!r}')
    out = {}
    for r in db.execute('SELECT event_id, output_json, rationale FROM analysis_model_outputs WHERE run_id=? ORDER BY event_id',
                        (run_id,)):
        out[r[0]] = {'run_id': run_id, **json.loads(r[1]), 'rationale': r[2]}
    return out

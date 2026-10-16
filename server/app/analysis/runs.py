"""Append-only storage of interpretive analysis runs (table ``analysis_runs``).

A run stores its parameters, configuration hash/version, code version, source
rule, input event-id set hash and the full result JSON with its hash. A request
whose stored row would be byte-identical (same kind, parameters, configuration,
inputs, code and result) returns that row with ``reused: true``; anything else
is a new row, and earlier rows are never changed (triggers refuse it).
"""
from __future__ import annotations

import json
import sqlite3

from .provenance import canonical_json, code_sha256, code_version, sha256_text


def store_run(db: sqlite3.Connection, *, kind: str, parameters: dict, config_sha256: str | None,
              config_version: str | None, source_rule: str, input_sha256: str, label_run_id: int | None,
              result: dict) -> dict:
    params_json = canonical_json(parameters)
    params_sha = sha256_text(params_json)
    result_json = canonical_json(result)
    result_sha = sha256_text(result_json)
    version, code_hash = code_version(), code_sha256()
    existing = db.execute('''SELECT id, created_at FROM analysis_runs WHERE kind=? AND parameters_sha256=?
                             AND config_sha256 IS ? AND input_sha256=? AND result_sha256=? AND code_version=?
                             AND code_sha256 IS ? ORDER BY id LIMIT 1''',
                          (kind, params_sha, config_sha256, input_sha256, result_sha, version, code_hash)).fetchone()
    if existing:
        return {'run_id': existing[0], 'reused': True, 'result_sha256': result_sha, 'parameters_sha256': params_sha,
                'created_at': existing[1]}
    cur = db.execute('''INSERT INTO analysis_runs(kind,parameters_json,parameters_sha256,config_sha256,config_version,
                        code_version,code_sha256,source_rule,input_sha256,label_run_id,result_json,result_sha256)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (kind, params_json, params_sha, config_sha256, config_version, version, code_hash, source_rule,
                      input_sha256, label_run_id, result_json, result_sha))
    created = db.execute('SELECT created_at FROM analysis_runs WHERE id=?', (cur.lastrowid,)).fetchone()[0]
    return {'run_id': int(cur.lastrowid), 'reused': False, 'result_sha256': result_sha, 'parameters_sha256': params_sha,
            'created_at': created}


def _summary(row) -> dict:
    return {'run_id': row['id'], 'kind': row['kind'], 'artifact_class': row['artifact_class'],
            'parameters': json.loads(row['parameters_json']), 'parameters_sha256': row['parameters_sha256'],
            'config_sha256': row['config_sha256'], 'config_version': row['config_version'],
            'code_version': row['code_version'], 'code_sha256': row['code_sha256'], 'source_rule': row['source_rule'],
            'input_sha256': row['input_sha256'], 'label_run_id': row['label_run_id'],
            'result_sha256': row['result_sha256'], 'created_at': row['created_at']}


def get_run(db: sqlite3.Connection, run_id: int, *, kind: str | None = None) -> dict | None:
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM analysis_runs WHERE id=?', (run_id,)).fetchone()
    if row is None or (kind is not None and row['kind'] != kind):
        return None
    out = _summary(row)
    out['result'] = json.loads(row['result_json'])
    return out


def list_runs(db: sqlite3.Connection, *, kind: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    db.row_factory = sqlite3.Row
    columns = ('id, kind, artifact_class, parameters_json, parameters_sha256, config_sha256, config_version, '
               'code_version, code_sha256, source_rule, input_sha256, label_run_id, result_sha256, created_at')
    if kind:
        rows = db.execute(f'SELECT {columns} FROM analysis_runs WHERE kind=? ORDER BY id DESC LIMIT ? OFFSET ?',
                          (kind, limit, offset)).fetchall()
    else:
        rows = db.execute(f'SELECT {columns} FROM analysis_runs ORDER BY id DESC LIMIT ? OFFSET ?',
                          (limit, offset)).fetchall()
    return [_summary(r) for r in rows]

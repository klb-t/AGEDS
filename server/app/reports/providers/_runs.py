"""Model-run records for the method section (not a section)."""
from __future__ import annotations

import json

from ..common import sha256_text

RUN_FIELDS = ('tool', 'tool_version', 'provider', 'model', 'model_version')


def _recipe(row) -> tuple[str, str]:
    try:
        provenance = json.loads(row['provenance_json'] or '{}')
    except (TypeError, ValueError):
        return 'unknown', 'unknown'
    recipe = provenance.get('recipe') if isinstance(provenance, dict) else None
    if not isinstance(recipe, dict):
        return 'unknown', 'unknown'
    return str(recipe.get('id') or 'unknown'), str(recipe.get('sha256') or 'unknown')


def run_record(row) -> tuple[dict, list[str]]:
    """``(record, unknown_field_names)``; empty or missing values are ``unknown``."""
    record = {'table': 'processing_runs', 'run_id': row['id'], 'status': row['status'] or 'unknown',
              'started_at': row['started_at'], 'finished_at': row['finished_at']}
    unknowns = []
    for field in RUN_FIELDS:
        value = row[field]
        if value in (None, '', 'unknown'):
            unknowns.append(f'processing_run.{row["id"]}.{field}')
            value = 'unknown'
        record[field] = value
    record['parameters_sha256'] = sha256_text(row['parameters_json']) if row['parameters_json'] is not None else 'unknown'
    record['recipe_id'], record['recipe_sha256'] = _recipe(row)
    return record, unknowns

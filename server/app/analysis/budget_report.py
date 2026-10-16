"""Report payload of a stored time-budget run (track E1 → reports, track F).

``report_payload(db, run_id)`` reads one append-only ``analysis_runs`` row of
kind ``time_budget`` and returns the values a report prints, flattened into
rows. Nothing is recomputed: every number is the stored result's number, the
stored result is re-hashed against its recorded SHA-256 (``integrity``), and
the run row itself is returned under ``inputs`` for the report's method record.
Read-only; safe inside a read-only snapshot.
"""
from __future__ import annotations

import sqlite3

from .provenance import sha256_text
from .runs import get_run

SCHEMA = 'ageds.time-budget-report-payload/v1'
VARIANT_KEYS = ('lower', 'probable', 'upper', 'wick_low', 'wick_high', 'body_low', 'body_high')


def _bounds(entry: dict) -> dict:
    return {key: entry.get(key) for key in VARIANT_KEYS}


def report_payload(db: sqlite3.Connection, run_id: int) -> dict:
    db.row_factory = sqlite3.Row
    run = get_run(db, int(run_id), kind='time_budget')
    if run is None:
        raise LookupError(f'time budget run {run_id} does not exist')
    row = db.execute('SELECT * FROM analysis_runs WHERE id=?', (int(run_id),)).fetchone()
    integrity = 'hash_matches' if sha256_text(row['result_json']) == row['result_sha256'] else 'hash_mismatch'
    result = run['result']
    totals = result.get('totals') or {}
    window_length = totals.get('window_length_s')
    category_totals = [{'category': name, **_bounds(entry)} for name, entry in (totals.get('categories') or {}).items()]
    unobserved = totals.get('unobserved') or {}
    category_totals.append({'category': 'nieobserwowane', **_bounds(unobserved), 'unobserved': True})
    candles = []
    for granularity, buckets in (result.get('candles') or {}).items():
        for bucket in buckets:
            base = {'granularity': granularity, 'bucket': bucket.get('bucket'), 'start_utc': bucket.get('start_utc'),
                    'end_utc': bucket.get('end_utc'), 'length_s': bucket.get('length_s'), 'partial': bucket.get('partial')}
            for name, entry in (bucket.get('categories') or {}).items():
                candles.append({**base, 'category': name, **_bounds(entry), 'event_count': entry.get('event_count'),
                                'event_ids_truncated': entry.get('event_ids_truncated'),
                                'unstable': entry.get('unstable') or []})
            candles.append({**base, 'category': 'nieobserwowane', **_bounds(bucket.get('unobserved') or {}),
                            'event_count': None, 'event_ids_truncated': None, 'unstable': bucket.get('unstable') or []})
    sensitivity = result.get('sensitivity') or {}
    grid = result.get('grid') or {}
    exclusions = result.get('exclusions') or {}
    untimed = exclusions.get('untimed') or {}
    filtered = exclusions.get('filtered') or {}
    exclusion_rows = [{'exclusion': f'untimed:{reason}', 'count': count} for reason, count in (untimed.get('by_reason') or {}).items()]
    exclusion_rows += [{'exclusion': f'filtered:{reason}', 'count': count} for reason, count in (filtered.get('by_reason') or {}).items()]
    for key in ('assumed_zone_items', 'hidden_excluded_by_seam', 'ambiguous_fold_items_carried', 'source_truncated'):
        if key in exclusions:
            exclusion_rows.append({'exclusion': key, 'count': exclusions[key]})
    config = result.get('config') or {}
    labels = result.get('labels') or {}
    source = result.get('input') or {}
    return {
        'schema': SCHEMA, 'artifact_class': result.get('artifact_class', 'interpretive'),
        'disclaimer': result.get('disclaimer'),
        'run': {'run_id': run['run_id'], 'kind': run['kind'], 'created_at': run['created_at'],
                'result_schema': result.get('schema'), 'result_sha256': run['result_sha256'], 'integrity': integrity,
                'parameters_sha256': run['parameters_sha256'], 'config_sha256': run['config_sha256'],
                'config_version': run['config_version'], 'code_version': run['code_version'],
                'code_sha256': run['code_sha256'], 'model_rule': run['source_rule'], 'input_sha256': run['input_sha256'],
                'label_run_id': run['label_run_id']},
        'window': dict(result.get('window') or {}),
        'source': {'seam': source.get('seam'), 'source_rule': source.get('source_rule'),
                   'source_rule_sha256': source.get('source_rule_sha256'),
                   'projection_id': (source.get('projection') or {}).get('id'),
                   'projection_build_id': (source.get('projection') or {}).get('build_id'),
                   'projection_params': source.get('projection_params'), 'items_used': source.get('items_used'),
                   'items_in_window': source.get('items_in_window'), 'items_sha256': source.get('items_sha256'),
                   'event_ids_sha256': source.get('event_ids_sha256'),
                   'time_uncertainty': source.get('time_uncertainty')},
        'method': [{'key': key, 'value': value} for key, value in (result.get('method') or {}).items()],
        'parameters': [{'parameter': key, 'value': value} for key, value in sorted((result.get('parameters') or {}).items())],
        'categories': [{'category': c.get('id'), 'label': c.get('label'), 'color': c.get('color'),
                        'reserved': bool(c.get('reserved'))} for c in result.get('categories') or []],
        'category_config': {'schema': config.get('schema'), 'sha256': config.get('sha256'), 'version': config.get('version'),
                            'origin': config.get('origin'), 'combine': config.get('combine'),
                            'default_category': config.get('default_category'),
                            'official_categories': config.get('official_categories')},
        'category_rules': [dict(rule) for rule in config.get('rules') or []],
        'labels': {'label_run_id': labels.get('label_run_id'), 'labels_sha256': labels.get('labels_sha256'),
                   'by_path': labels.get('by_path'), 'issues': labels.get('issues')},
        'totals': {'window_length_s': window_length, 'observed': totals.get('observed'), 'overlap_s': totals.get('overlap_s'),
                   'double_counted_probable_s': totals.get('double_counted_probable_s')},
        'category_totals': category_totals,
        'candles': candles,
        'sensitivity': {'grid_mode': grid.get('mode'), 'points_evaluated': grid.get('points_evaluated'),
                        'points_total': grid.get('points_total'), 'grid_truncated': grid.get('truncated'),
                        'tolerance_s': sensitivity.get('tolerance_s'), 'unstable': sensitivity.get('unstable'),
                        'flip_counts': sensitivity.get('flip_counts'),
                        'flip_records_truncated': sensitivity.get('flip_records_truncated')},
        'sensitivity_ranges': [{'category': name, **entry} for name, entry in (sensitivity.get('ranges') or {}).items()],
        'sensitivity_points': [{'index': point.get('index'), 'differs': point.get('differs'),
                                'unobserved_probable': (point.get('unobserved') or {}).get('probable')}
                               for point in sensitivity.get('points') or []],
        'flips': [{'kind': kind, 'point_index': entry.get('point_index'), 'differs': entry.get('differs'),
                   'detail': {k: v for k, v in entry.items() if k not in ('point_index', 'differs')}}
                  for kind, entries in (sensitivity.get('flips') or {}).items() for entry in entries],
        'exclusions': exclusion_rows,
        'untimed_event_ids_listed': untimed.get('event_ids') or [],
        'inputs': {'analysis_runs': [dict(row)]},
    }

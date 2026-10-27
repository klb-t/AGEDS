"""Check generated reports against their JSON and the stored analysis run (subprocess helper).

``python -m server.tests.e2e.report_check [--budget-run ID] REPORT_DIR...`` runs the
reports test suite's ``crosscheck`` (every ``data-k`` span equals its JSON value and
no numeral appears outside the spans) on each report directory, and, for a
time-budget report, compares the category totals and every candle row with the
stored run named by ``--budget-run`` (whose result hash is recomputed). The
database is the one named by ``EW_DB_PATH``; nothing is written. Prints one JSON
object.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

VARIANTS = ('lower', 'probable', 'upper', 'wick_low', 'wick_high', 'body_low', 'body_high')


def _blocks(document: dict) -> dict:
    return {block['id']: block for section in document['sections'] for block in section.get('blocks', [])}


def budget_equality(document: dict, run_id: int) -> dict:
    from server.app import db
    from server.app.analysis import runs
    from server.app.analysis.provenance import canonical_json, sha256_text
    with db.session() as connection:
        run = runs.get_run(connection, run_id, kind='time_budget')
    result = run['result']
    blocks = _blocks(document)
    mismatches = []
    stored_totals = result['totals']['categories']
    for row in blocks['time_budget.category_totals']['rows']:
        expected = stored_totals.get(row['category'])
        if expected is None:
            if row['category'] != 'nieobserwowane':
                mismatches.append(('category_missing_in_run', row['category']))
            continue
        for variant in VARIANTS:
            if row.get(variant) != expected.get(variant):
                mismatches.append(('total', row['category'], variant, row.get(variant), expected.get(variant)))
    candle_rows = blocks['time_budget.candles']['rows']
    compared = 0
    for row in candle_rows:
        bucket = next((c for c in result['candles'][row['granularity']] if c['bucket'] == row['bucket']), None)
        if bucket is None:
            mismatches.append(('bucket_missing', row['granularity'], row['bucket']))
            continue
        source = bucket['unobserved'] if row['category'] == 'nieobserwowane' else bucket['categories'].get(row['category'])
        if source is None:
            mismatches.append(('candle_category_missing', row['bucket'], row['category']))
            continue
        for variant in VARIANTS:
            if variant in source and row.get(variant) != source[variant]:
                mismatches.append(('candle', row['bucket'], row['category'], variant, row.get(variant), source[variant]))
        compared += 1
    run_block = {r['key']: r['value'] for r in blocks['time_budget.run']['rows']}
    recomputed = sha256_text(canonical_json(result))
    return {'run_id': run_id, 'equal': not mismatches and run_block.get('result_sha256') == run['result_sha256'] == recomputed,
            'totals_rows': len(blocks['time_budget.category_totals']['rows']), 'candle_rows_compared': compared,
            'result_sha256': run['result_sha256'], 'recomputed_sha256': recomputed, 'mismatches': mismatches[:20]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m server.tests.e2e.report_check')
    parser.add_argument('--budget-run', type=int, default=None)
    parser.add_argument('reports', nargs='+', type=Path)
    args = parser.parse_args(argv)
    from server.tests.test_reports_support import crosscheck
    out = {'command': 'report_check', 'reports': [], 'budget': None}
    for directory in args.reports:
        document = json.loads((directory / 'report.json').read_text('utf-8'))
        checked = crosscheck((directory / 'report.html').read_bytes(), document)
        out['reports'].append({'report_type': document['report_type'], 'spans': checked['spans'],
                               'mismatches': len(checked['mismatches']), 'mismatch_examples': checked['mismatches'][:5],
                               'stray_numerals': len(checked['stray_numerals']),
                               'stray_examples': checked['stray_numerals'][:5], 'title_ok': checked['title_ok']})
        if document['report_type'] == 'time_budget' and args.budget_run:
            out['budget'] = budget_equality(document, args.budget_run)
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the end-to-end acceptance and write its receipt to a directory OUTSIDE the repository.

    python scripts/e2e_acceptance.py /absolute/new/output-directory [--heavy] [--pdf-chromium PATH]

The output directory must be new or empty and outside the checkout. It receives:

* ``work/`` — the generated synthetic sources, the fresh data directory (SQLite +
  content store), reports, exports and the service log (delete it when done);
* ``receipt.json`` — stages with every command and measured seconds, every check with
  its evidence, each acceptance criterion mapped to checks or marked not verifiable;
* ``receipt.md`` — a short human summary of the same.

All data is synthetic and generated at run time; paths in the receipt are shown
relative to ``<work>``. Exit code 0 when every check passed, 1 otherwise.
Use the repository virtual environment's Python (FastAPI, uvicorn, openpyxl).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def markdown(receipt: dict) -> str:
    lines = [f"# End-to-end acceptance receipt ({receipt['variant']})", '',
             f"- git head: `{receipt['git']['head']}` (dirty tracked files: {receipt['git']['dirty']})",
             f"- started: {receipt['started_at']}; wall time: {receipt['wall_seconds']} s; python {receipt['python']}",
             f"- checks: {receipt['summary']['passed']}/{receipt['summary']['checks']} passed; "
             f"failed: {receipt['summary']['failed'] or 'none'}; run error: {receipt['error'] or 'none'}",
             f"- PDF: {receipt['environment']['pdf_chromium'] or 'not requested'}; ASR: {receipt['environment']['asr_model']}",
             '', '## Criteria', '', '| Criterion | Status | Checks |', '|---|---|---|']
    for key, value in receipt['criteria'].items():
        lines.append(f"| {key} {value['criterion']} | {value['status']} | {', '.join(value['checks']) or value['note']} |")
    lines += ['', '## Stages (measured wall-clock seconds)', '', '| Stage | Seconds | Commands |', '|---|---:|---:|']
    for stage in receipt['stages']:
        lines.append(f"| {stage['name']} | {stage['seconds']} | {len(stage['commands'])} |")
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('output', type=Path, help='new or empty directory outside the repository')
    parser.add_argument('--heavy', action='store_true', help='add the adversarial catalog collection and a full-grid budget')
    parser.add_argument('--pdf-chromium', default=None, help='Chromium/Chrome executable for PDF reports (EW_PDF_CHROMIUM)')
    parser.add_argument('--skip-repository-check', action='store_true', help='do not run scripts/verify_repository.py')
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output == REPO or REPO in output.parents:
        parser.error('the output directory must be outside the repository checkout')
    if output.exists() and any(output.iterdir()):
        parser.error(f'the output directory must be new or empty: {output}')
    from server.tests.e2e.pipeline import Pipeline
    pipeline = Pipeline(output / 'work', heavy=args.heavy, pdf_chromium=args.pdf_chromium,
                        verify_repository=not args.skip_repository_check)
    receipt = pipeline.run()
    (output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    (output / 'receipt.md').write_text(markdown(receipt), encoding='utf-8')
    print(json.dumps({'receipt': str(output / 'receipt.json'), 'checks': receipt['summary']['checks'],
                      'passed': receipt['summary']['passed'], 'failed': receipt['summary']['failed'],
                      'criteria': receipt['summary']['criteria'], 'wall_seconds': receipt['wall_seconds'],
                      'error': receipt['error']}, ensure_ascii=False))
    return 0 if not receipt['summary']['failed'] and receipt['error'] is None else 1


if __name__ == '__main__':
    raise SystemExit(main())

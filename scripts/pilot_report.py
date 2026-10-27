#!/usr/bin/env python3
"""Pilot report filled from an AGEDS database, read-only (Markdown + JSON).

    .venv/bin/python scripts/pilot_report.py [--db PATH] [--out DIR]

Defaults to the pilot of scripts/pilot_local.sh (~/ageds-pilot). Every figure is read from
the database; nothing is inferred. The report keeps the layers apart: source bytes and
their acquisitions, parser observations, model output (ASR/OCR, with model and run),
and interpretations. Model text is quoted only as a short excerpt and labelled as such.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.app.pilot_report import collect, communications, markdown  # noqa: E402,F401  (re-exported for tests)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, default=Path.home() / 'ageds-pilot' / 'data' / 'evidence.db')
    parser.add_argument('--out', type=Path, default=Path.home() / 'ageds-pilot' / 'reports')
    args = parser.parse_args()
    db = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    report = collect(db)
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    (args.out / f'pilot-{stamp}.json').write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n')
    (args.out / f'pilot-{stamp}.md').write_text(markdown(report))
    print(args.out / f'pilot-{stamp}.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Check active documentation links and byte-preserved historical snapshots."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

def main() -> int:
    failures = []
    snapshots = 0
    for manifest in (ROOT / 'docs/archive').glob('*/manifest.json'):
        for record in json.loads(manifest.read_text())['records']:
            path = ROOT / record['archived_path']
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
                failures.append('Historical snapshot differs: ' + record['archived_path'])
            snapshots += 1
    for manifest in (ROOT / 'coordination/archive').glob('*/manifest.json'):
        for record in json.loads(manifest.read_text())['records']:
            path = ROOT / record['archived_path']
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
                failures.append('Historical coordination differs: ' + record['archived_path'])
            snapshots += 1
    pages = list(ROOT.glob('*.md')) + list((ROOT / 'docs').rglob('*.md')) + list((ROOT / 'research').rglob('*.md')) + [ROOT / 'server/README.md', ROOT / 'coordination/README.md']
    pages = [p for p in pages if 'archive' not in p.relative_to(ROOT).parts]
    pages += list((ROOT / 'docs/archive').glob('*/README.md')) + list((ROOT / 'coordination/archive').glob('*/README.md'))
    links = 0
    for page in pages:
        for target in re.findall(r'\[[^\]\n]*\]\(([^)]+)\)', page.read_text()):
            if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', target) or target.startswith('#'):
                continue
            target = unquote(target.split('#', 1)[0])
            if target and not (page.parent / target).exists():
                failures.append(f'Broken link in {page.relative_to(ROOT)}: {target}')
            links += 1
    print(f'Historical snapshots: {snapshots}; active pages: {len(pages)}; relative links: {links}')
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0

if __name__ == '__main__':
    raise SystemExit(main())

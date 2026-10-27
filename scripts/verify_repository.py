#!/usr/bin/env python3
"""Check active documentation links, byte-preserved historical snapshots and
privacy heuristics for tracked files (the repository is public)."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

# Privacy heuristics: a match is a candidate for review, not proof of private
# data; an allowed fictional value is not proof that a string is synthetic.
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})')
ALLOWED_EMAIL_DOMAINS = ('example.org', 'example.com', 'example.net', 'example.edu', 'example',
                         'invalid', 'test', 'localhost', 'anthropic.com', 'users.noreply.github.com')
PHONE = re.compile(r'(?<![0-9.,A-Za-z_])(\+[1-9][0-9][0-9 ().-]{7,16}[0-9]|00[1-9][0-9]{1,2}[ .-]?[0-9][0-9 .-]{6,13}[0-9]|\b0?6[- ]?[0-9]{8}\b|\b[4-9][0-9]{2}[ -][0-9]{3}[ -][0-9]{3}\b)')
USER_PATH = re.compile(r'(/home/(?!user/)[a-z][a-z0-9_.-]*/|/Users/(?!Shared/)[A-Za-z][^/\s]*/|[A-Za-z]:\\Users\\[^\\\s]+\\)')
PRIVACY_SKIP_PARTS = {'archive'}
PRIVACY_MAX_BYTES = 4 * 1024 * 1024


def _fictional_phone(text: str) -> bool:
    digits = re.sub(r'\D', '', text)
    # Reserved/fictional conventions: zero blocks, ascending 1..8(9) runs and
    # NANP 555-01xx; a nearly constant tail is also treated as a placeholder.
    return ('0000' in digits or '5550' in digits or digits.endswith(('12345678', '123456789'))
            or len(set(digits[-7:])) <= 2)


def _email_allowed(domain: str) -> bool:
    domain = domain.lower()
    return any(domain == allowed or domain.endswith('.' + allowed) for allowed in ALLOWED_EMAIL_DOMAINS)


def _tracked_files() -> list[Path]:
    try:
        output = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True, capture_output=True).stdout
        return [ROOT / name for name in output.decode('utf-8', 'surrogateescape').split('\0') if name]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.parts]


def privacy_findings() -> tuple[int, list[str]]:
    findings = []
    scanned = 0
    for path in _tracked_files():
        relative = path.relative_to(ROOT)
        if PRIVACY_SKIP_PARTS & set(relative.parts) or not path.is_file() or path.stat().st_size > PRIVACY_MAX_BYTES:
            continue
        data = path.read_bytes()
        if b'\0' in data[:8192]:
            continue
        scanned += 1
        for number, line in enumerate(data.decode('utf-8', 'replace').splitlines(), 1):
            for match in EMAIL.finditer(line):
                if not _email_allowed(match.group(1)):
                    findings.append(f'Possible private e-mail in {relative}:{number}')
            for match in PHONE.finditer(line):
                if not _fictional_phone(match.group(1)):
                    findings.append(f'Possible phone number in {relative}:{number}')
            if USER_PATH.search(line):
                findings.append(f'Possible user directory path in {relative}:{number}')
    return scanned, findings


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
    scanned, privacy = privacy_findings()
    failures.extend(privacy)
    print(f'Historical snapshots: {snapshots}; active pages: {len(pages)}; relative links: {links}; privacy-scanned files: {scanned}')
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0

if __name__ == '__main__':
    raise SystemExit(main())

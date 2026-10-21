"""Method-record helpers shared by interpretive analyses.

Canonical JSON and hashes make results comparable: the same inputs and
parameters give the same bytes. ``code_version`` records the checked-out git
head when it is available and ``unknown`` otherwise; ``code_sha256`` hashes the
analysis package sources and default presets so an uncommitted edit is still
distinguishable.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[2]


def canonical_json(value) -> str:
    """Sorted keys, no whitespace, UTF-8 text, NaN/inf refused."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sha256_json(value) -> str:
    return sha256_text(canonical_json(value))


def event_set_sha256(event_ids) -> str:
    """Hash of the sorted, de-duplicated event-id set (``ageds.event_id_set.v1``)."""
    ids = sorted({int(i) for i in event_ids})
    return sha256_text('ageds.event_id_set.v1\n' + '\n'.join(str(i) for i in ids))


@lru_cache(maxsize=1)
def code_version() -> str:
    try:
        completed = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=REPO_ROOT, capture_output=True,
                                   text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    head = completed.stdout.strip()
    if completed.returncode != 0 or len(head) != 40:
        return 'unknown'
    return head


def code_sha256() -> str:
    """Hash of analysis/*.py and analysis/data/*.json (sorted by relative path)."""
    digest = hashlib.sha256()
    files = sorted([*PACKAGE_DIR.glob('*.py'), *PACKAGE_DIR.glob('data/*.json')])
    for path in files:
        digest.update(path.relative_to(PACKAGE_DIR).as_posix().encode('utf-8') + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()

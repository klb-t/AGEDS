"""Append-only writes of ledger rows, events and time candidates inside one caller transaction.

Every row carries ``row_sha256`` over its canonical payload. Re-importing the
same artifact with the same parser version finds the row by its unique key and
must find the same hash; a different hash is a changed interpretation under the
same version and raises ``IntegrityError`` (never an overwrite).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from .. import evidence
from ..evidence import IntegrityError
from ..timeinfo import TimeCandidate, record_event_times

SIGNAL_CLASSES = ('passive', 'active', 'declared')
SIGNAL_BASES = ('raw_sample', 'provider_inference', 'provider_user_edit', 'derived')


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha256_of(value) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def signal(cls: str, basis: str, provider: str, device_or_account: str | None = None, **extra) -> dict:
    """``metadata.signal`` of the digital-traces contract (docs/plans/DIGITAL_TRACES_2026-10-17.md)."""
    if cls not in SIGNAL_CLASSES or basis not in SIGNAL_BASES:
        raise ValueError(f'invalid signal {cls}/{basis}')
    return {'class': cls, 'basis': basis, 'provider': provider, 'device_or_account': device_or_account, **extra}


_UNIQUE = {
    'ledger_statements': ('artifact_id', 'parser', 'parser_version', 'reading_key', 'statement_key'),
    'ledger_entries': ('artifact_id', 'parser', 'parser_version', 'reading_key', 'source_position'),
    'health_samples': ('artifact_id', 'parser', 'parser_version', 'source_position'),
}
_JSON_SUFFIX = '_json'


def _prepare(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        out[key] = canonical(value) if key.endswith(_JSON_SUFFIX) and not isinstance(value, str) else value
    out['row_sha256'] = sha256_of({k: v for k, v in out.items() if k != 'row_sha256'})
    return out


def insert_row(db: sqlite3.Connection, table: str, row: dict) -> int:
    """Insert one row or return the identical existing row's id."""
    prepared = _prepare(row)
    key = _UNIQUE[table]
    existing = db.execute(f'SELECT id, row_sha256 FROM {table} WHERE ' + ' AND '.join(f'{k}=?' for k in key),
                          [prepared[k] for k in key]).fetchone()
    if existing:
        if existing['row_sha256'] != prepared['row_sha256']:
            raise IntegrityError(f'{table} row {prepared[key[-1]]!r} conflicts with an earlier interpretation under '
                                 f'parser version {prepared["parser_version"]}; use a new parser version')
        return int(existing['id'])
    columns = list(prepared)
    cur = db.execute(f'INSERT INTO {table}({",".join(columns)}) VALUES({",".join("?" * len(columns))})',
                     [prepared[c] for c in columns])
    return int(cur.lastrowid)


def insert_many(db: sqlite3.Connection, table: str, rows: list[dict]) -> dict:
    """Batched variant of ``insert_row`` (``INSERT OR IGNORE`` + hash check of ignored rows)."""
    if not rows:
        return {'inserted': 0, 'existing': 0}
    prepared = [_prepare(r) for r in rows]
    columns = list(prepared[0])
    key = _UNIQUE[table]
    before = db.total_changes
    db.executemany(f'INSERT OR IGNORE INTO {table}({",".join(columns)}) VALUES({",".join("?" * len(columns))})',
                   [[p[c] for c in columns] for p in prepared])
    inserted = db.total_changes - before
    if inserted < len(prepared):
        fixed = key[:-1]
        head = prepared[0]
        positions = [p[key[-1]] for p in prepared]
        stored = {}
        for offset in range(0, len(positions), 500):
            chunk = positions[offset:offset + 500]
            for r in db.execute(f'SELECT {key[-1]} AS k, row_sha256 FROM {table} WHERE '
                                + ' AND '.join(f'{k}=?' for k in fixed) + f' AND {key[-1]} IN ({",".join("?" * len(chunk))})',
                                [head[k] for k in fixed] + chunk):
                stored[r['k']] = r['row_sha256']
        for p in prepared:
            if stored.get(p[key[-1]]) != p['row_sha256']:
                raise IntegrityError(f'{table} row {p[key[-1]]!r} conflicts with an earlier interpretation under '
                                     f'parser version {p["parser_version"]}; use a new parser version')
    return {'inserted': inserted, 'existing': len(prepared) - inserted}


def add_event(db: sqlite3.Connection, candidates: list[TimeCandidate], **event) -> int:
    """Create or re-observe one event and append its time candidates in the caller's transaction."""
    event_id = evidence.add_event(db=db, **event)
    if candidates:
        record_event_times(db, event_id, candidates)
    return event_id


def mask_account(value: str | None, keep: int = 4) -> str | None:
    """Display masking only (stored values stay raw): keep the country/check prefix and last digits."""
    if value is None:
        return None
    compact = ''.join(str(value).split())
    if len(compact) <= keep + 4:
        return '*' * max(0, len(compact) - keep) + compact[-keep:]
    return compact[:4] + '*' * (len(compact) - 4 - keep) + compact[-keep:]


def iban_check(value: str | None) -> dict:
    """ISO 13616 mod-97 check as an observation of the text (not of the account's existence)."""
    if value is None:
        return {'status': 'missing'}
    compact = ''.join(str(value).split()).upper()
    if not (15 <= len(compact) <= 34) or not compact[:2].isalpha() or not compact[2:4].isdigit() \
            or not compact.isalnum():
        return {'status': 'not_iban_shaped'}
    rearranged = compact[4:] + compact[:4]
    digits = ''.join(str(int(ch, 36)) for ch in rearranged)
    return {'status': 'checksum_valid' if int(digits) % 97 == 1 else 'checksum_invalid', 'country': compact[:2]}

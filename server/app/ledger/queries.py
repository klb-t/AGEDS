"""Read-only views of ledger rows for the CLI, the HTTP API and the page.

Sums are computed by code from integer minor units per currency (rows whose
minor units are unknown are counted separately, never estimated). Account
masking is a display option; stored values stay raw.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..db import session
from . import money, store

ENTRY_COLUMNS = ('id', 'event_id', 'artifact_id', 'statement_id', 'record_kind', 'parser', 'parser_version', 'profile_id',
                 'reading_key', 'source_position', 'amount_exact', 'currency', 'amount_minor', 'currency_exponent', 'flow',
                 'counterparty_name', 'counterparty_account', 'description', 'duration_s', 'content_key',
                 'content_occurrence')


def profiles(path: Path | None = None) -> dict:
    from ..importers.tabular_profiles import load_profiles, profile_set_label
    data = load_profiles(path)
    return {'profile_set': profile_set_label(data), 'notice': data.get('notice'),
            'profiles': [{'id': p['id'], 'label': p.get('label'), 'record_kind': p['record_kind'],
                          'required': p.get('required', []), 'columns': sorted(p['columns']),
                          'times': {k: v.get('kind') for k, v in p.get('times', {}).items()},
                          'amount_mode': p.get('amount', {}).get('mode'),
                          'encoding': p.get('read', {}).get('encoding', 'auto'),
                          'delimiter': p.get('read', {}).get('delimiter', 'auto')} for p in data['profiles']]}


def entries(*, artifact_id: int | None = None, record_kind: str | None = None, limit: int = 100, after_id: int = 0,
            mask_accounts: bool = False) -> dict:
    clauses, params = ['id>?'], [after_id]
    if artifact_id is not None:
        clauses.append('artifact_id=?')
        params.append(artifact_id)
    if record_kind is not None:
        clauses.append('record_kind=?')
        params.append(record_kind)
    with session() as db:
        rows = db.execute(f'SELECT * FROM ledger_entries WHERE {" AND ".join(clauses)} ORDER BY id LIMIT ?',
                          (*params, limit + 1)).fetchall()
    items = []
    for row in rows[:limit]:
        item = {k: row[k] for k in ENTRY_COLUMNS}
        item['metadata'] = json.loads(row['metadata_json'])
        if mask_accounts:
            item['counterparty_account'] = store.mask_account(item['counterparty_account'])
            item['account_masking'] = 'display_only'
        items.append(item)
    return {'items': items, 'has_more': len(rows) > limit, 'next_after_id': items[-1]['id'] if items else after_id,
            'semantics': ('parser observations of exported records; amount_exact is signed for the account holder '
                          '(negative = money out); dates are candidates in event_times')}


def totals(*, artifact_id: int | None = None) -> dict:
    """Per record kind, currency and flow: count and exact sum from integer minor units."""
    where, params = ('WHERE artifact_id=?', [artifact_id]) if artifact_id is not None else ('', [])
    with session() as db:
        rows = db.execute(f'''SELECT record_kind, currency, flow, currency_exponent, COUNT(*) n,
                                     SUM(CASE WHEN amount_minor IS NOT NULL THEN 1 ELSE 0 END) known,
                                     SUM(amount_minor) minor
                              FROM ledger_entries {where} GROUP BY record_kind, currency, flow, currency_exponent
                              ORDER BY record_kind, currency, flow''', params).fetchall()
    out = []
    for r in rows:
        exact = money.from_minor(r['minor'], r['currency_exponent']) if r['minor'] is not None and \
            r['currency_exponent'] is not None else None
        out.append({'record_kind': r['record_kind'], 'currency': r['currency'], 'flow': r['flow'], 'entries': r['n'],
                    'entries_with_minor_units': r['known'], 'entries_without_minor_units': r['n'] - r['known'],
                    'sum_exact': exact, 'sum_minor': r['minor'], 'currency_exponent': r['currency_exponent']})
    return {'method': 'SUM of integer minor units per currency, computed by SQLite; rows without minor units are counted, '
                      'not summed', 'rows': out}


def statements(*, artifact_id: int | None = None) -> dict:
    where, params = ('WHERE artifact_id=?', [artifact_id]) if artifact_id is not None else ('', [])
    with session() as db:
        rows = db.execute(f'SELECT * FROM ledger_statements {where} ORDER BY id', params).fetchall()
    items = []
    for row in rows:
        items.append({k: row[k] for k in ('id', 'artifact_id', 'parser', 'parser_version', 'reading_key',
                                          'statement_key', 'account_raw', 'currency', 'entry_count', 'balance_check')}
                     | {'opening': json.loads(row['opening_json']), 'closing': json.loads(row['closing_json']),
                        'check': json.loads(row['balance_check_json']), 'locator': json.loads(row['locator_json'])})
    return {'items': items, 'semantics': 'stated balances and a check computed by code; a mismatch is reported, not corrected'}


def health_sessions(*, artifact_id: int | None = None, limit: int = 100) -> dict:
    clauses, params = ["event_type='health_session'"], []
    if artifact_id is not None:
        clauses.append('artifact_id=?')
        params.append(artifact_id)
    with session() as db:
        rows = db.execute(f'SELECT id, artifact_id, subject, thread_id, ts_start, ts_end, metadata_json FROM events '
                          f'WHERE {" AND ".join(clauses)} ORDER BY id LIMIT ?', (*params, limit + 1)).fetchall()
        linked = {r[0]: r[1] for r in db.execute(
            'SELECT session_event_id, COUNT(*) FROM health_samples WHERE session_event_id IN (%s) GROUP BY session_event_id'
            % ','.join('?' * len(rows[:limit])), [r['id'] for r in rows[:limit]])} if rows else {}
    items = []
    for row in rows[:limit]:
        meta = json.loads(row['metadata_json'])
        session_meta = meta.get('session', {})
        items.append({'event_id': row['id'], 'artifact_id': row['artifact_id'], 'subject': row['subject'],
                      'source': row['thread_id'], 'ts_start': row['ts_start'], 'ts_end': row['ts_end'],
                      'kind': session_meta.get('kind'), 'span': session_meta.get('span'), 'signal': meta.get('signal'),
                      'samples': linked.get(row['id'], 0)})
    return {'items': items, 'has_more': len(rows) > limit,
            'semantics': 'device/app inferences about a person (provider_inference), not observations of the person'}


def overview() -> dict:
    with session() as db:
        counts = {t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                  for t in ('ledger_entries', 'ledger_statements', 'health_samples')}
        kinds = {r[0]: r[1] for r in db.execute('SELECT record_kind, COUNT(*) FROM ledger_entries GROUP BY record_kind')}
        checks = {r[0]: r[1] for r in db.execute('SELECT balance_check, COUNT(*) FROM ledger_statements GROUP BY balance_check')}
        sessions = db.execute("SELECT COUNT(*) FROM events WHERE event_type='health_session'").fetchone()[0]
        bases = {r[0]: r[1] for r in db.execute('SELECT basis, COUNT(*) FROM health_samples GROUP BY basis')}
    return {'counts': counts, 'entries_by_kind': kinds, 'balance_checks': checks, 'health_sessions': sessions,
            'health_samples_by_basis': bases}

"""Bank statement mechanics shared by CAMT.053 and MT940: balance checks and statement writes.

The balance check is arithmetic over exact decimals done by code: stated
opening balance + the signed sum of the statement's booked entries, compared
with the stated closing balance. A mismatch is reported with the difference and
never corrected; entries in another currency or not booked are excluded and
counted, which makes the check ``not_checkable`` when any booked entry had to
be left out.
"""
from __future__ import annotations

from decimal import Decimal

from ..db import session
from . import money, store

BATCH = 500


def balance(raw_mark: str | None, exact: str | None, currency: str | None, *, date: str | None = None,
            kind: str | None = None, raw: str | None = None) -> dict:
    """A stated balance: credit marks positive, debit marks negative (signed for the account holder)."""
    out = {'raw': raw, 'mark': raw_mark, 'unsigned_exact': exact, 'currency': currency, 'date': date, 'type': kind}
    if exact is None or raw_mark not in ('C', 'D', 'CRDT', 'DBIT'):
        return {**out, 'exact': None, 'status': 'unparsed'}
    signed = exact if raw_mark in ('C', 'CRDT') else money.negate(exact)
    return {**out, 'exact': signed, 'status': 'ok', 'minor': money.minor_units(signed, currency)}


def balance_check(opening: dict | None, closing: dict | None, movements: list[dict]) -> tuple[str, dict]:
    """``movements``: [{'exact': signed, 'currency', 'booked': bool}] in statement order."""
    detail: dict = {'method': 'stated opening + sum of signed booked entries, computed by code with exact decimals',
                    'corrected': False}
    if not opening or not closing or opening.get('exact') is None or closing.get('exact') is None:
        return 'not_checkable', {**detail, 'reason': 'opening_or_closing_balance_missing'}
    currency = opening.get('currency')
    if closing.get('currency') != currency:
        return 'not_checkable', {**detail, 'reason': 'opening_and_closing_currencies_differ'}
    counted, excluded = [], {'not_booked': 0, 'other_currency': 0, 'amount_unparsed': 0}
    for move in movements:
        if not move.get('booked', True):
            excluded['not_booked'] += 1
        elif move.get('exact') is None:
            excluded['amount_unparsed'] += 1
        elif move.get('currency') not in (None, currency):
            excluded['other_currency'] += 1
        else:
            counted.append(move['exact'])
    total = money.add_exact(counted) if counted else '0'
    computed = money.add_exact([opening['exact'], total])
    difference = Decimal(closing['exact']) - Decimal(computed)
    detail.update(opening=opening['exact'], currency=currency, sum_of_entries=total, entries_counted=len(counted),
                  entries_excluded=excluded, computed_closing=computed, stated_closing=closing['exact'],
                  difference=format(difference, 'f'))
    if excluded['other_currency'] or excluded['amount_unparsed']:
        return 'not_checkable', {**detail, 'reason': 'booked_entries_left_out_of_the_sum'}
    return ('match' if difference == 0 else 'mismatch'), detail


def write_statement(statement: dict, records: list[dict]) -> int:
    """Insert one statement row, then its events and entries (``records`` as in tabular_profiles)."""
    statement_id = None
    for offset in range(0, max(len(records), 1), BATCH):
        with session() as db:
            db.execute('BEGIN IMMEDIATE')
            if statement_id is None:
                statement_id = store.insert_row(db, 'ledger_statements', statement)
            for record in records[offset:offset + BATCH]:
                event_id = store.add_event(db, record['candidates'], **record['event'])
                for entry in record['entries']:
                    store.insert_row(db, 'ledger_entries', {**entry, 'event_id': event_id, 'statement_id': statement_id})
                    store.append_search_text(db, event_id, store.entry_search_text(entry))
    return statement_id

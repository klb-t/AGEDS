"""CAMT.053 and MT940: exact amounts, date kinds, entry-year inference, balance checks reported not corrected."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from server.app.importers import camt053, mt940
from server.app.ledger import bank, xmlstream
from server.tests.ledger_support import counts, env, events, rows, sha_tree  # noqa: F401  (fixture)


def statements(artifact_id):
    out = rows('SELECT * FROM ledger_statements WHERE artifact_id=? ORDER BY id', (artifact_id,))
    for row in out:
        row['check'] = json.loads(row['balance_check_json'])
    return out


# ---------------------------------------------------------------- CAMT.053

def test_camt053_entries_dates_and_balance_checks(env):
    m = env['m']['camt']
    before = sha_tree(env['root'])
    result = camt053.import_camt053(Path(m['two_statements']))
    assert sha_tree(env['root']) == before
    assert result['namespace'].endswith('camt.053.001.02') and result['statement_count'] == 2
    assert (result['entries'], result['balance_mismatches'], result['not_booked']) == (5, 1, 1)
    first, second = statements(result['artifact_id'])
    assert first['balance_check'] == 'match' and first['account_raw'] == env['m']['ibans']['nl_a']
    assert first['check']['entries_excluded']['not_booked'] == 1  # PDNG entry left out, counted
    assert first['check']['summary_check']['all_agree'] is True
    assert second['balance_check'] == 'mismatch'
    assert (second['check']['computed_closing'], second['check']['stated_closing'], second['check']['difference']) == \
        ('1185.25', '1200.00', '14.75')
    assert second['check']['corrected'] is False
    evs = events(result['artifact_id'])
    credit, debit, pending, batch, reversal = evs
    e = credit['entries'][0]
    assert (e['amount_exact'], e['amount_minor'], e['flow'], e['counterparty_name']) == ('100.00', 10000, 'credit',
                                                                                       'Payer Example')
    assert credit['phone_or_address'] == env['m']['ibans']['nl_b'] and credit['body'] == 'Invoice 1'
    assert [(t['kind'], t['meta']['date_kind'], t['precision'], t['tz_status']) for t in credit['times']] == [
        ('created', 'booking_date', 'day', 'unknown'), ('other', 'value_date', 'day', 'unknown')]
    booking = debit['times'][0]
    assert (booking['utc'], booking['tz_status'], booking['precision']) == ('2024-02-02T09:15:00Z', 'explicit_offset',
                                                                            'second')
    value = debit['times'][1]
    assert (value['tz_status'], value['meta']['element']) == ('unknown', 'ValDt/DtTm')  # DtTm without offset
    assert debit['ts_start'] == '2024-02-02T09:15:00Z' and debit['entries'][0]['amount_exact'] == '-9.75'
    assert pending['meta']['status'] == 'PDNG' and pending['meta']['booked'] is False
    assert len(batch['meta']['transactions']) == 2 and batch['contact_label'] is None
    assert 'batch_entry_several_transactions_counterparty_not_assigned' in batch['meta']['issues']
    assert reversal['meta']['amount']['reversal_indicator'] == 'true' and reversal['entries'][0]['amount_exact'] == '-5.00'
    assert all(e['meta']['signal']['basis'] == 'raw_sample' for e in evs)


def test_camt053_v8_nested_party_and_prior_closing_as_opening(env):
    result = camt053.import_camt053(Path(env['m']['camt']['v8']))
    (statement,) = statements(result['artifact_id'])
    assert statement['balance_check'] == 'match' and json.loads(statement['opening_json'])['exact'] == '-10.00'
    (event,) = events(result['artifact_id'])
    assert event['contact_label'] == 'Nested Party' and event['meta']['namespace'].endswith('.08')


def test_camt053_refusals(env):
    with pytest.raises(xmlstream.XmlRefused):
        camt053.import_camt053(Path(env['m']['camt']['entity']))
    with pytest.raises(ValueError, match='not a CAMT.053'):
        camt053.import_camt053(Path(env['m']['camt']['notification']))
    assert rows('SELECT COUNT(*) n FROM events')[0]['n'] == 0


def test_camt053_reimport_is_idempotent(env):
    path = Path(env['m']['camt']['two_statements'])
    camt053.import_camt053(path)
    snapshot = counts()
    camt053.import_camt053(path)
    assert counts() == snapshot


# ---------------------------------------------------------------- MT940

@pytest.mark.parametrize('value,mmdd,expected,flags', [
    (date(2025, 12, 31), '0102', ['2026-01-02'], {'year_boundary'}),
    (date(2026, 1, 2), '1231', ['2025-12-31'], {'year_boundary'}),
    (date(2025, 12, 31), '1231', ['2025-12-31'], set()),
    (date(2025, 7, 1), '0101', ['2025-01-01', '2026-01-01'], {'ambiguous'}),
    (date(2024, 3, 1), '0229', ['2024-02-29'], set()),
    (date(2025, 3, 1), '0229', ['2024-02-29'], {'year_boundary', 'ambiguous'}),
])
def test_entry_year_is_inferred_relative_to_the_value_date(value, mmdd, expected, flags):
    result = mt940.infer_entry_date(value, mmdd)
    assert result['readings'] == expected
    assert {k for k in ('year_boundary', 'ambiguous') if result.get(k)} == flags


def test_statement_line_grammar():
    g = mt940._LINE61.match('2512310102CR100,00NTRFREF1//BANK9\nSUPP').groupdict()
    assert (g['mark'], g['fc'], g['amount'], g['type'], g['cref'], g['bref'], g['supp']) == \
        ('C', 'R', '100,00', 'NTRF', 'REF1', 'BANK9', 'SUPP')
    g = mt940._LINE61.match('251231RD5,NCHGNONREF').groupdict()
    assert (g['ed'], g['mark'], g['fc'], g['amount']) == (None, 'RD', None, '5,')
    assert mt940.swift_amount('1000,') == '1000' and mt940.swift_amount('0012,5') == '12.5'
    assert mt940.swift_amount('12.50') is None


def test_details_structures_are_observations():
    hints = mt940.load_data('mt940_details.v1.json')
    numbered = mt940.parse_details('166?00GUTSCHRIFT?20Invoice ?2142?32Name Part ?33Two', hints)
    assert numbered['structure'] == 'numbered_subfields:?' and numbered['prefix'] == '166'
    assert numbered['mapped'] == {'posting_text': 'GUTSCHRIFT', 'remittance': 'Invoice 42', 'counterparty_name': 'Name Part Two'}
    slash = mt940.parse_details('/EREF/X1/NAME/Shop/REMI/Line\none', hints)
    assert slash['mapped'] == {'end_to_end_reference': 'X1', 'counterparty_name': 'Shop', 'remittance': 'Lineone'}
    assert mt940.parse_details('plain words / not codes', hints)['structure'] == 'free_text'


def test_mt940_statements_signs_dates_and_checks(env):
    path = Path(env['m']['mt940']['path'])
    before = sha_tree(env['root'])
    result = mt940.import_mt940(path)
    assert sha_tree(env['root']) == before
    assert (result['statement_count'], result['entries'], result['balance_mismatches']) == (3, 7, 1)
    assert (result['ambiguous_entry_dates'], result['year_boundary_entry_dates']) == (1, 1)
    match, mismatch, broken = statements(result['artifact_id'])
    assert match['balance_check'] == 'match' and match['check']['sum_of_entries'] == '80.50'
    assert mismatch['balance_check'] == 'mismatch' and mismatch['check']['difference'] == '30.50'
    assert broken['balance_check'] == 'not_checkable' and broken['check']['reason'] == 'booked_entries_left_out_of_the_sum'
    evs = events(result['artifact_id'])
    debit, credit, reversal, ambiguous, dup_a, dup_b, unparsed = evs
    assert (debit['entries'][0]['amount_exact'], debit['contact_label'], debit['body']) == ('-25.50', 'Shop Example',
                                                                                          'Groceries')
    assert debit['meta']['statement_line']['supplementary'].strip() == 'SUPPLEMENTARY DETAILS'
    assert debit['meta']['counterparty_account_check']['status'] == 'checksum_valid'
    assert (credit['entries'][0]['amount_exact'], credit['entries'][0]['amount_minor']) == ('100', 10000)
    assert (credit['contact_label'], credit['body']) == ('Firma Przyklad', 'Zwrot za usluge')
    booking = [t for t in credit['times'] if t['meta']['date_kind'] == 'booking_date'][0]
    assert booking['local'] == '2026-01-02T00:00:00' and 'year_boundary' in booking['meta']
    assert reversal['entries'][0]['amount_exact'] == '5.00'  # RD: reversal of a debit increases the balance
    assert reversal['body'] == 'Free text line one continuing line two'
    alternatives = [t['local'] for t in ambiguous['times'] if t['meta']['date_kind'] == 'booking_date']
    assert alternatives == ['2025-01-01T00:00:00', '2026-01-01T00:00:00']
    assert dup_a['entries'][0]['content_key'] == dup_b['entries'][0]['content_key']
    assert (dup_a['entries'][0]['content_occurrence'], dup_b['entries'][0]['content_occurrence']) == (1, 2)
    assert unparsed['times'] == [] and 'statement_line_61_unparsed' in unparsed['meta']['issues']
    assert all(t['tz_status'] == 'unknown' and t['precision'] == 'day' for e in evs for t in e['times'])


def test_mt940_year_base_is_a_recorded_reading_and_reimport_is_idempotent(env):
    path = Path(env['m']['mt940']['path'])
    first = mt940.import_mt940(path)
    snapshot = counts()
    mt940.import_mt940(path)
    assert counts() == snapshot
    other = mt940.import_mt940(path, two_digit_year_base=1900)
    assert other['reading_key'] != first['reading_key']
    old = [t['local'] for e in events(first['artifact_id']) for t in e['times']
           if e['meta']['reading_key'] == other['reading_key'] and t['meta']['date_kind'] == 'value_date']
    assert old and all(value.startswith('19') for value in old)
    assert counts()['events'] == 2 * snapshot['events']  # a new result next to the old one
    with pytest.raises(ValueError, match='century'):
        mt940.import_mt940(path, two_digit_year_base=1950)


def test_mt940_without_fields_is_refused(env):
    with pytest.raises(ValueError, match='no MT940'):
        mt940.import_mt940(Path(env['m']['mt940']['no_fields']))


def test_balance_check_arithmetic_is_exact():
    opening = bank.balance('C', '0.10', 'EUR')
    closing = bank.balance('C', '0.40', 'EUR')
    status, detail = bank.balance_check(opening, closing, [{'exact': '0.10', 'currency': 'EUR'},
                                                           {'exact': '0.20', 'currency': 'EUR'}])
    assert status == 'match' and detail['difference'] == '0.00'
    status, detail = bank.balance_check(opening, closing, [{'exact': '0.30', 'currency': 'USD'}])
    assert status == 'not_checkable' and detail['entries_excluded']['other_currency'] == 1
    assert bank.balance_check(None, closing, [])[0] == 'not_checkable'


def test_ledger_values_are_searchable_pinnable_and_gated_like_their_event(env):
    from server.app import authz, db as dbmod, search
    from server.app.analysis import dossier, preregistration
    from server.app.assignments import create_principal
    from server.tests.corpus import hypotheses as corpus
    result = camt053.import_camt053(Path(env['m']['camt']['two_statements']))
    entry = rows("SELECT * FROM ledger_entries WHERE artifact_id=? AND amount_exact IS NOT NULL ORDER BY id LIMIT 1",
                 (result['artifact_id'],))[0]
    amount = entry['amount_exact'].lstrip('-+')
    hits = search.search_page(f'"{amount}"')['results']
    assert ('event', entry['event_id']) in {(h['object_kind'], int(h['object_id'])) for h in hits}
    content = rows("SELECT content FROM search_fts WHERE object_kind='event' AND object_id=?", (entry['event_id'],))[0]['content']
    camt053.import_camt053(Path(env['m']['camt']['two_statements']))  # re-import appends nothing
    assert rows("SELECT content FROM search_fts WHERE object_kind='event' AND object_id=?", (entry['event_id'],))[0]['content'] == content
    with dbmod.session() as conn:
        hid = preregistration.register_hypothesis(conn, corpus.definition_for(corpus.SCENARIOS['unknown_zone']()))['hypothesis_id']
        item = dossier.add_item(conn, hid, 'event', entry['event_id'], ledger_entry_id=entry['id'],
                                classification='contradicting')
        assert (item['locator']['record']['row_sha256'], item['source_check']) == (entry['row_sha256'], 'matches')
        assert amount in item['excerpt']
        whole = dossier.add_item(conn, hid, 'event', entry['event_id'])  # the event alone is a different item
        assert whole['item_id'] != item['item_id'] and 'record' not in whole['locator']
        other = rows('SELECT id FROM ledger_entries WHERE event_id<>? LIMIT 1', (entry['event_id'],))[0]['id']
        with pytest.raises(dossier.HypothesisNotFound):  # a row must belong to the event it is pinned under
            dossier.add_item(conn, hid, 'event', entry['event_id'], ledger_entry_id=other)
        # a recipient sees the ledger text exactly when it may read the event, as before
        recipient = create_principal(conn, 'recipient')
        gate = authz.ContentGate(conn, recipient)
        assert not gate.allows('event', entry['event_id'], 'read_text')
        assert search.scoped_search_page(f'"{amount}"', 20, gate)['results'] == []
        case_id = conn.execute('SELECT s.case_id FROM artifacts a JOIN sources s ON s.id=a.source_id WHERE a.id=?',
                               (result['artifact_id'],)).fetchone()[0]
        authz.grant(conn, recipient, authz.create_scope(conn, 'case', {'case_id': case_id}), ['read_text', 'read_metadata'])
        search.clear_cache()
        gate = authz.ContentGate(conn, recipient)
        assert gate.allows('event', entry['event_id'], 'read_text')
        assert ('event', entry['event_id']) in {(h['object_kind'], int(h['object_id']))
                                                for h in search.scoped_search_page(f'"{amount}"', 20, gate)['results']}

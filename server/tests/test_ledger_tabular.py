"""Generic tabular profiles: encodings, separators, negative notations, dates, duplicates, transit and rides."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.app.evidence import IntegrityError
from server.app.importers import tabular_profiles as tp
from server.app.ledger import money, store, textio, timeparse
from server.tests.ledger_support import counts, env, events, rows, sha_tree  # noqa: F401  (fixture)


def entry(event):
    assert len(event['entries']) == 1
    return event['entries'][0]


# ---------------------------------------------------------------- money / text units

@pytest.mark.parametrize('text,fmt,exact,notation', [
    ('-1,00', ',', '-1.00', 'leading_minus'),
    ('1,00-', ',', '-1.00', 'trailing_minus'),
    ('(1.00)', '.', '-1.00', 'parentheses'),
    ('−1 234,50', ',', '-1234.50', 'leading_minus'),
    ('1.234,56', ',', '1234.56', None),
    ('1 234,56', ',', '1234.56', None),
    ('+5,5', ',', '5.5', 'leading_plus'),
    (',50', ',', '0.50', None),
    ('0,00-', ',', '0.00', 'trailing_minus'),
])
def test_number_formats_give_exact_decimal_strings(text, fmt, exact, notation):
    parsed = money.parse_number(text, money.NumberFormat(fmt, (' ', ' ', '.') if fmt == ',' else (',',)))
    assert (parsed['status'], parsed['exact'], parsed['sign_notation']) == ('ok', exact, notation)
    assert isinstance(parsed['exact'], str)


@pytest.mark.parametrize('text,reason', [
    ('(−1,00)', 'conflicting_sign_notations'), ('1,2,3', 'not_a_number_under_profile_separators'),
    ('1.23,4', 'not_a_number_under_profile_separators'), ('12a', 'not_a_number_under_profile_separators'),
])
def test_malformed_numbers_are_not_guessed(text, reason):
    parsed = money.parse_number(text, money.NumberFormat(',', ('.', ' ')))
    assert parsed['status'] == 'unparsed' and parsed['reason'] == reason


def test_auto_decimal_reports_both_readings_and_minor_units_are_integers():
    auto = money.NumberFormat('auto')
    assert money.parse_number('1,234', auto)['readings'] == [{'decimal': ',', 'exact': '1.234'},
                                                             {'decimal': '.', 'exact': '1234'}]
    assert money.parse_number('1,5', auto)['decimal_inferred'] == ','
    assert money.parse_number('1,234.56', auto)['exact'] == '1234.56'
    assert money.minor_units('-1234.50', 'PLN') == {'minor': -123450, 'exponent': 2, 'status': 'ok'}
    assert money.minor_units('5', 'JPY')['minor'] == 5
    assert money.minor_units('1.005', 'EUR')['status'] == 'fraction_beyond_currency_exponent'
    assert money.minor_units('1.230', 'BHD') == {'minor': 1230, 'exponent': 3, 'status': 'ok'}
    assert money.minor_units('1', 'XQQ')['status'] == 'currency_exponent_unknown'
    assert money.add_exact(['0.10', '0.20', '-0.30']) == '0.00'  # floats would not give exactly zero
    assert money.currency_code('$')['status'] == 'ambiguous' and money.currency_code('zł')['code'] == 'PLN'


def test_encoding_hypotheses_are_recorded_not_assumed():
    text = 'Tytuł;Kwota\nZażółć gęślą jaźń ŚŹĄ;-1,00\n'
    for encoding in ('cp1250', 'iso-8859-2'):
        hypothesis = textio.encoding_hypotheses(text.encode(encoding))
        assert hypothesis['chosen'] == encoding and not hypothesis['ambiguous']
    assert textio.encoding_hypotheses(text.encode('utf-8'))['basis'] == 'valid_utf8_non_ascii'
    assert textio.encoding_hypotheses(b'\xef\xbb\xbfa;b\n')['chosen'] == 'utf-8-sig'
    western = 'Naam;Bedrag\nnaïve één;1,00\n'.encode('cp1252')
    tie = textio.encoding_hypotheses(western)
    assert tie['ambiguous'] and tie['chosen'] == 'cp1250'  # default preference; 'ï' reads as 'ď' too
    assert textio.encoding_hypotheses(western, ('cp1252',))['chosen'] == 'cp1252'
    text, info = textio.decode(western, 'windows-1252')
    assert text.startswith('Naam') and info['basis'] == 'explicit'
    with pytest.raises(ValueError):
        textio.decode(b'\x81\x81', 'cp1250')  # undefined byte in CP1250: strict, never replaced


def test_time_formats_and_two_digit_years():
    assert timeparse.parse('20240131', ['YYYYMMDD'])['naive'].isoformat() == '2024-01-31T00:00:00'
    assert timeparse.parse('01/02/24 18:00:00', ['MM/DD/YY HH:mm:ss'])['status'] == 'two_digit_year_century_not_assumed'
    assert timeparse.parse('01/02/24 18:00:00', ['MM/DD/YY HH:mm:ss'], two_digit_year_base=2000)['naive'].month == 1
    parsed = timeparse.parse('2024-03-31 03:20:00 +0200', ['YYYY-MM-DD HH:mm:ss ZZ'])
    assert parsed['offset_minutes'] == 120 and parsed['precision'] == 'second'
    assert timeparse.parse('30.02.2024', ['DD.MM.YYYY'])['status'] == 'invalid_calendar_value'
    assert timeparse.parse('2024-01-01 10:00 +2400', ['YYYY-MM-DD HH:mm ZZ'])['status'] == 'offset_out_of_range'


def test_iban_check_and_masking_are_display_observations():
    from server.tests.corpus.ledger import IBAN_INVALID, IBAN_NL_A, IBAN_PL_A
    assert store.iban_check(IBAN_NL_A)['status'] == 'checksum_valid'
    assert store.iban_check(IBAN_PL_A)['status'] == 'checksum_valid'
    assert store.iban_check(IBAN_INVALID)['status'] == 'checksum_invalid'
    masked = store.mask_account(IBAN_NL_A)
    assert masked.startswith(IBAN_NL_A[:4]) and masked.endswith(IBAN_NL_A[-4:]) and '*' in masked


# ---------------------------------------------------------------- bank tables

@pytest.mark.parametrize('variant,encoding', [('cp1250', 'cp1250'), ('iso88592', 'iso-8859-2'),
                                              ('utf8bom', 'utf-8-sig'), ('utf8', 'utf-8')])
def test_pl_style_bank_csv_in_every_encoding(env, variant, encoding):
    m = env['m']['bank_pl']
    path = Path(m['files'][variant])
    before = sha_tree(env['root'])
    result = tp.import_tabular(path)
    assert sha_tree(env['root']) == before  # originals unchanged
    table = result['tables'][0]
    assert table['profile_id'] == 'example_bank_pl_style_v1' and table['selection'] == 'unique_header_match'
    assert table['reading']['encoding']['used'] == encoding and table['preamble_rows'] == m['preamble_rows']
    assert result['rows'] == m['data_rows'] and result['events'] == 8 and result['skipped_rows'] == 1
    evs = events(result['artifact_id'])
    first = evs[0]
    assert first['contact_label'] == 'Sklep Żółty Przykład' and first['body'] == 'Zakupy gęślą jaźń'
    assert first['phone_or_address'] == m['counterparty_account']  # account numbers are data: kept raw
    assert first['meta']['counterparty_account_check']['status'] == 'checksum_valid'
    e = entry(first)
    assert (e['amount_exact'], e['currency'], e['amount_minor'], e['currency_exponent'], e['flow']) == \
        ('-1234.56', 'PLN', -123456, 2, 'debit')
    assert first['direction'] == 'debit' and first['ts_start'] is None  # bank dates: zone unknown
    by_kind = {t['meta']['date_kind']: t for t in first['times']}
    assert {k: (t['kind'], t['local'], t['precision'], t['tz_status']) for k, t in by_kind.items()} == {
        'transaction_date': ('start', '2024-01-02T00:00:00', 'day', 'unknown'),
        'booking_date': ('created', '2024-01-03T00:00:00', 'day', 'unknown'),
        'value_date': ('other', '2024-01-03T00:00:00', 'day', 'unknown')}
    assert first['meta']['signal'] == {'class': 'active', 'basis': 'raw_sample', 'provider': 'bank_csv_export',
                                       'device_or_account': None}
    # identical rows on the same day are two records (different source positions)
    a, b = evs[m['duplicate_rows'][0] - 2], evs[m['duplicate_rows'][1] - 2]
    assert entry(a)['content_key'] == entry(b)['content_key']
    assert (entry(a)['content_occurrence'], entry(b)['content_occurrence']) == (1, 2)
    assert a['external_id'] != b['external_id']
    minus = evs[4]
    assert entry(minus)['amount_exact'] == '-1000.00'  # U+2212 or '-' with a no-break-space group
    bad_date = evs[5]
    assert [t['meta']['date_kind'] for t in bad_date['times']] == ['booking_date', 'value_date']
    assert 'date_invalid_calendar_value' in bad_date['meta']['issues']
    eur = entry(evs[6])
    assert (eur['currency'], eur['amount_minor']) == ('EUR', -1000)
    unparsed = entry(evs[7])
    assert unparsed['amount_exact'] is None and unparsed['flow'] is None
    assert result['issues']['by_code']['row_without_parseable_time'] == 1  # the closing-balance footer
    assert table['running_balance_check']['inconsistent'] == 0
    assert table['running_balance_check']['order_inferred'] == 'file_order_oldest_first'


def test_reimport_is_idempotent_by_source_position(env):
    path = Path(env['m']['bank_pl']['files']['cp1250'])
    first = tp.import_tabular(path)
    snapshot = counts()
    again = tp.import_tabular(path)
    assert again['artifact_id'] == first['artifact_id']
    after = counts()
    assert {k: v for k, v in after.items()} == snapshot
    assert len(rows('SELECT * FROM audit_log WHERE action=?', ('import_run',))) == 2


def test_changed_reading_creates_a_new_result_not_an_overwrite(env):
    path = Path(env['m']['bank_pl']['files']['cp1250'])
    first = tp.import_tabular(path)
    other = tp.import_tabular(path, encoding='iso-8859-2')
    assert other['artifact_id'] == first['artifact_id']
    assert other['tables'][0]['reading_key'] != first['tables'][0]['reading_key']
    keys = {e['external_id'].split(':')[2] for e in events(first['artifact_id'])}
    assert len(keys) == 2  # both interpretations are kept, distinguishable by reading key


def test_tied_single_byte_encoding_is_flagged(env):
    result = tp.import_tabular(Path(env['m']['bank_pl']['files']['tie']))
    hypothesis = result['tables'][0]['reading']['encoding']['hypothesis']
    assert hypothesis['ambiguous'] and hypothesis['tied_with'] == ['iso-8859-2']


def test_nl_style_indicator_amounts(env):
    m = env['m']['bank_nl']
    result = tp.import_tabular(Path(m['path']))
    assert result['tables'][0]['reading']['encoding']['used'] == 'cp1252'
    evs = events(result['artifact_id'])
    values = [(entry(e)['amount_exact'], entry(e)['flow']) for e in evs]
    assert values == [('-12.50', 'debit'), ('1000.00', 'credit'), ('-3.00', 'debit'), (None, None), (None, None),
                      ('-0.99', 'debit')]
    assert evs[0]['contact_label'] == 'Café Voorbeeld'
    assert 'sign_contradicts_indicator' in evs[3]['meta']['issues']
    assert 'indicator_unmapped' in evs[4]['meta']['issues']
    assert evs[4]['meta']['counterparty_account_check']['status'] == 'checksum_invalid'
    assert evs[5]['body'] == 'Regel met "aanhalingstekens" en,komma'
    assert evs[5]['meta']['amount']['currency_basis'] == 'profile_default_currency'
    assert [t['local'] for t in evs[1]['times']] == ['2024-02-01T00:00:00', '2024-02-02T00:00:00']


def test_debit_credit_columns_and_undecidable_day_month_order(env):
    result = tp.import_tabular(Path(env['m']['bank_dc']['ambiguous']))
    reading = result['tables'][0]['reading']
    assert reading['inference']['booking_date']['decision'] == 'undetermined'
    assert reading['delimiter'] == ',' and reading['delimiter_basis'] == 'header_match_under_hypothesis'
    evs = events(result['artifact_id'])
    first = evs[0]
    assert sorted(t['source'].split(':')[-1] for t in first['times']) == ['day_first', 'day_first', 'month_first',
                                                                          'month_first']
    assert {t['local'] for t in first['times'] if t['meta']['date_kind'] == 'booking_date'} == \
        {'2024-04-03T00:00:00', '2024-03-04T00:00:00'}
    values = [(entry(e)['amount_exact'], entry(e)['flow']) for e in evs]
    assert values == [('-25.00', 'debit'), ('10.00', 'credit'), ('-12.00', 'debit'), ('-5.00', 'debit'),
                      (None, None), ('-1250.00', 'debit')]
    assert 'debit_column_value_already_negative' in evs[2]['meta']['issues']
    assert 'credit_column_value_already_negative' in evs[3]['meta']['issues']
    assert 'both_debit_and_credit_present' in evs[4]['meta']['issues']
    assert result['ambiguous_dates'] == 6
    decided = tp.import_tabular(Path(env['m']['bank_dc']['dayfirst']))
    assert decided['tables'][0]['reading']['inference']['booking_date']['decision'] == 'day_first'
    first = events(decided['artifact_id'])[0]
    assert [t['local'] for t in first['times'] if t['meta']['date_kind'] == 'booking_date'] == ['2024-04-03T00:00:00']


def test_explicit_profile_and_private_profile_file(env, tmp_path):
    path = Path(env['m']['bank_dc']['ambiguous'])
    with pytest.raises(ValueError, match='does not match'):
        tp.import_tabular(path, profile_id='example_bank_pl_style_v1')
    with pytest.raises(ValueError, match='profile not found'):
        tp.import_tabular(path, profile_id='nope')
    private = tmp_path / 'private.json'
    profile = json.loads(json.dumps(tp.load_profiles()['profiles'][2]))
    profile.update(id='mine', assumed_zone='Europe/Warsaw')
    profile['times']['booking_date']['order'] = 'month_first'
    private.write_text(json.dumps({'schema': 'private.tabular', 'version': '3', 'profiles': [profile]}))
    result = tp.import_tabular(path, profile_id='mine', profiles_path=private)
    first = events(result['artifact_id'])[0]
    booking = [t for t in first['times'] if t['meta']['date_kind'] == 'booking_date']
    assert [(t['tz_status'], t['local']) for t in booking] == [('unknown', '2024-03-04T00:00:00'),
                                                              ('assumed_zone', '2024-03-04T00:00:00')]
    assert booking[1]['utc'] == '2024-03-03T23:00:00Z' and booking[1]['meta']['assumption_rule'] == 'tabular_profile:mine'
    assert first['meta']['profile']['set'] == 'private.tabular/v3'


def test_conflicting_interpretation_under_same_version_is_refused(env):
    path = Path(env['m']['bank_nl']['path'])
    result = tp.import_tabular(path)
    event = events(result['artifact_id'])[0]
    with pytest.raises(IntegrityError):
        from server.app.db import session
        with session() as db:
            db.execute('BEGIN IMMEDIATE')
            store.insert_row(db, 'ledger_entries', {
                'event_id': event['id'], 'artifact_id': result['artifact_id'], 'statement_id': None,
                'parser': tp.PARSER_NAME, 'parser_version': tp.PARSER_VERSION,
                **{k: v for k, v in event['entries'][0].items() if k not in ('id', 'event_id', 'artifact_id',
                                                                             'statement_id', 'parser', 'parser_version',
                                                                             'row_sha256', 'created_at')},
                'amount_exact': '-99.99'})
    with pytest.raises(Exception, match='append-only'):
        from server.app.db import session
        with session() as db:
            db.execute('UPDATE ledger_entries SET amount_exact=? WHERE id=?', ('0', event['entries'][0]['id']))


# ---------------------------------------------------------------- transit and rides

def test_transit_journey_rows_rollover_unpaired_and_top_up(env):
    result = tp.import_tabular(Path(env['m']['transit']['same_row']))
    evs = events(result['artifact_id'])
    types = [e['event_type'] for e in evs]
    assert types == ['travel_segment', 'travel_segment', 'travel_tap', 'financial_transaction', 'travel_segment',
                     'travel_segment']
    first, night, unpaired, top_up, dup_a, dup_b = evs
    assert entry(first)['duration_s'] == 36 * 60 and entry(first)['amount_exact'] == '-2.45'
    assert first['meta']['signal']['basis'] == 'provider_inference'
    assert first['meta']['transport']['duration']['basis'] == 'wall_clock_difference_zone_unknown'
    out = [t for t in night['times'] if t['kind'] == 'end'][0]
    assert out['local'] == '2024-03-16T00:15:00' and out['meta']['rollover'].startswith('next_day_assumed')
    assert entry(night)['duration_s'] == 25 * 60 and 'time_rolled_over_to_next_day' in night['meta']['issues']
    assert unpaired['meta']['pairing'] == {'status': 'unpaired', 'reason': 'missing_check_out'}
    assert [t['kind'] for t in unpaired['times']] == ['start']
    assert top_up['direction'] == 'credit' and entry(top_up)['amount_minor'] == 2000
    assert [t['meta']['date_kind'] for t in top_up['times']] == ['transaction_date']
    assert entry(dup_a)['content_key'] == entry(dup_b)['content_key'] and dup_a['id'] != dup_b['id']


def test_tap_rows_are_paired_by_a_versioned_rule(env):
    result = tp.import_tabular(Path(env['m']['transit']['taps']))
    assert result['tables'][0]['tap_pairing'] == {'rule': 'ledger.tap_pairing.v1', 'max_pair_gap_s': 18000}
    evs = events(result['artifact_id'])
    segments = [e for e in evs if e['event_type'] == 'travel_segment']
    taps = [e for e in evs if e['event_type'] == 'travel_tap']
    assert [(s['meta']['transport']['origin'], s['meta']['transport']['destination'], s['entries'][0]['duration_s'])
            for s in segments] == [('Stop A', 'Stop B', 1200), ('Stop E', 'Stop F', 1200), ('Stop C', 'Stop G', 6900)]
    assert all(s['meta']['signal']['basis'] == 'derived' and len(s['entries']) == 2 for s in segments)
    reasons = {t['contact_label']: t['meta']['pairing']['reason'] for t in taps}
    assert reasons == {'Stop D': 'next_tap_was_check_in', 'Stop H': 'no_preceding_check_in',
                       'Stop I': 'gap_exceeds_max_pair_gap', 'Stop J': 'gap_exceeds_max_pair_gap',
                       'Stop K': 'tap_type_unmapped'}
    assert rows('SELECT COUNT(*) n FROM ledger_entries WHERE artifact_id=?', (result['artifact_id'],))[0]['n'] == 11
    fares = [e['amount_exact'] for s in segments for e in s['entries']]
    assert fares == ['0.00', '-1.80', '0.00', '-2.10', '0.00', '-1.00']  # positive fare = money out


def test_ride_trips_offsets_dst_and_statuses(env):
    result = tp.import_tabular(Path(env['m']['rides']['path']))
    evs = events(result['artifact_id'])
    assert [e['event_type'] for e in evs] == ['travel_segment', 'ride_request', 'travel_segment', 'travel_segment']
    completed, canceled, dst, broken = evs
    assert (completed['ts_start'], completed['ts_end']) == ('2024-03-30T23:48:10Z', '2024-03-31T00:31:00Z')
    distance = completed['meta']['transport']['distance']
    assert distance['unit'] == 'mi' and distance['meters_exact'] == '8449.05600' and distance['unit_basis'] == 'header_name'
    assert (entry(completed)['amount_exact'], entry(completed)['flow']) == ('-18.40', 'debit')
    assert entry(dst)['duration_s'] == 25 * 60  # +01:00 to +02:00 across the DST change: 25 minutes, not 85
    assert 'utc_label_with_nonzero_offset' in canceled['meta']['issues']
    assert canceled['meta']['transport']['status']['label'] == 'canceled'
    assert 'dropoff_before_begin' in broken['meta']['issues'] and entry(broken)['duration_s'] is None
    assert 'ride_status_unmapped' in broken['meta']['issues']


def test_profiles_are_labelled_examples_and_validated(tmp_path):
    shipped = tp.load_profiles()
    assert 'EXAMPLE' in shipped['notice'] and len({p['id'] for p in shipped['profiles']}) == len(shipped['profiles'])
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'profiles': [{'id': 'x', 'record_kind': 'financial_transaction', 'columns': {},
                                             'times': {'t': {'kind': 'nonsense'}}}]}))
    with pytest.raises(ValueError, match='unknown kind'):
        tp.load_profiles(bad)
    bad.write_text(json.dumps({'profiles': [{'id': 'x', 'record_kind': 'financial_transaction', 'columns': {},
                                             'number': {'decimal': ',', 'thousands': [',']}}]}))
    with pytest.raises(ValueError, match='decimal separator'):
        tp.load_profiles(bad)


def test_corpus_part_is_registered_and_privacy_safe(tmp_path):
    from server.tests.corpus import PARTS, build_corpus
    assert 'ledger' in PARTS
    manifest = build_corpus(tmp_path, ('ledger',))['ledger']
    assert manifest['ibans']['invalid'].startswith('NL00')
    import re
    text = ''.join(p.read_bytes().decode('latin-1') for p in tmp_path.rglob('*') if p.is_file())
    assert not re.search(r'[A-Za-z0-9._%+-]+@(?!example\.(?:org|com|net))[A-Za-z0-9-]+\.[A-Za-z]{2,}', text)
    for value in manifest['ibans'].values():
        assert 'XXXX' in value or value[4:].startswith('0000')  # fictional bank codes only

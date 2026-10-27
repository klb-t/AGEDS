"""Hard synthetic ledger corpus: bank tables, CAMT.053, MT940, transit, rides and health exports.

Everything is generated here at test time. Account numbers are fictional: IBAN
check digits are computed for a bank code ``XXXX`` (NL form) or all-zero bank
and branch digits (PL form), which no real institution uses; one value has a
deliberately invalid check sum (``00``) and is declared as such. Names are
generic placeholders, e-mail-like text uses example.org, and no value comes
from a real statement, card history, trip log or health record.
"""
from __future__ import annotations

import json
from pathlib import Path


def iban(country: str, bban: str) -> str:
    """ISO 13616 check digits computed for a fictional BBAN."""
    rearranged = bban + country + '00'
    digits = ''.join(str(int(ch, 36)) for ch in rearranged.upper())
    check = 98 - int(digits) % 97
    return f'{country}{check:02d}{bban}'


IBAN_NL_A = iban('NL', 'XXXX0000000001')
IBAN_NL_B = iban('NL', 'XXXX0000000002')
IBAN_PL_A = iban('PL', '000000000000000000000001')
IBAN_PL_B = iban('PL', '000000000000000000000002')
IBAN_INVALID = 'NL00XXXX0000000003'  # check digits 00 never validate: declared invalid on purpose


def _write(path: Path, text: str, encoding: str) -> str:
    path.write_bytes(text.encode(encoding))
    return str(path)


def _bank_pl(root: Path) -> dict:
    lines = [
        'Wyciąg z rachunku;Przykładowy Bank Testowy',
        f'Rachunek;{IBAN_PL_A}',
        'Okres;01.01.2024 - 31.01.2024',
        '',
        'Data operacji;Data księgowania;Data waluty;Kontrahent;Rachunek kontrahenta;Tytuł;Kwota;Waluta;Saldo po operacji',
        f'02.01.2024;03.01.2024;03.01.2024;Sklep Żółty Przykład;{IBAN_PL_B};Zakupy gęślą jaźń;-1 234,56;PLN;8 765,44',
        f'05.01.2024;05.01.2024;05.01.2024;Pracodawca Przykład;{IBAN_PL_B};Wynagrodzenie;2 000,00;PLN;10 765,44',
        '06.01.2024;06.01.2024;06.01.2024;Kawiarnia Ąę;;Kawa;-12,50;PLN;10 752,94',
        '06.01.2024;06.01.2024;06.01.2024;Kawiarnia Ąę;;Kawa;-12,50;PLN;10 740,44',
        '07.01.2024;08.01.2024;08.01.2024;Opłata;;Prowizja bankowa;−1 000,00;PLN;9 740,44',
        '31.02.2024;09.01.2024;09.01.2024;Błąd daty;;Zła data operacji;-1,00;PLN;9 739,44',
        '10.01.2024;10.01.2024;10.01.2024;Waluta obca;;Płatność EUR;-10,00;EUR;',
        '11.01.2024;11.01.2024;11.01.2024;Niejasna kwota;;Kwota;12,3,4;PLN;',
        'Saldo końcowe;;;;;;;;9 739,44',
    ]
    text = '\r\n'.join(lines) + '\r\n'
    out = {}
    for encoding, suffix in (('cp1250', 'cp1250'), ('iso-8859-2', 'iso88592'), ('utf-8-sig', 'utf8bom'), ('utf-8', 'utf8')):
        # U+2212 MINUS SIGN exists only in the Unicode variants; single-byte files use '-'.
        body = text if encoding.startswith('utf') else text.replace('\u2212', '-')
        out[suffix] = _write(root / f'bank_pl_{suffix}.csv', body, encoding)
    # A text that decodes differently under CP1250 and ISO-8859-2 only through 'ą' (tie: preference decides)
    tie = 'Data operacji;Data księgowania;Kwota\n01.01.2024;01.01.2024;-1,00\n02.01.2024;02.01.2024;ąą\n'
    out['tie'] = _write(root / 'bank_pl_tie.csv', tie, 'cp1250')
    return {'files': out, 'own_account': IBAN_PL_A, 'counterparty_account': IBAN_PL_B, 'data_rows': 9,
            'preamble_rows': 4, 'duplicate_rows': (4, 5)}


def _bank_nl(root: Path) -> dict:
    rows = [
        '"Boekdatum","Rentedatum","Naam tegenpartij","Rekening","Tegenrekening","Af Bij","Bedrag","Valuta","Mutatiesoort","Mededelingen"',
        f'"20240131","20240131","Café Voorbeeld","{IBAN_NL_A}","{IBAN_NL_B}","Af","12,50","EUR","Betaalautomaat","Koffie één"',
        f'"20240201","20240202","Werkgever Voorbeeld","{IBAN_NL_A}","{IBAN_NL_B}","Bij","1.000,00","EUR","Overschrijving","Salaris"',
        f'"20240202","20240202","Teken Akkoord","{IBAN_NL_A}","","Af","-3,00","EUR","Diversen","Teken en indicator zeggen af"',
        f'"20240203","20240203","Teken Tegenspraak","{IBAN_NL_A}","","Bij","-3,00","EUR","Diversen","Teken zegt af, indicator bij"',
        f'"20240204","20240204","Onbekend","{IBAN_NL_A}","{IBAN_INVALID}","X","4,00","EUR","Diversen","Indicator onbekend"',
        f'"20240205","20240205","Komma, in naam","{IBAN_NL_A}","","Af","0,99","","Diversen","Regel met ""aanhalingstekens"" en,komma"',
    ]
    text = '\r\n'.join(rows) + '\r\n'
    return {'path': _write(root / 'bank_nl.csv', text, 'cp1252'), 'own_account': IBAN_NL_A, 'invalid_iban': IBAN_INVALID}


def _bank_dc(root: Path) -> dict:
    ambiguous = '\n'.join([
        'Date,Value date,Description,Debit,Credit,Currency,Balance',
        '03/04/2024,03/04/2024,Groceries,25.00,,USD,975.00',
        '04/05/2024,04/05/2024,Refund,,10.00,USD,985.00',
        '05/06/2024,05/06/2024,Trailing minus,12.00-,,USD,973.00',
        '06/07/2024,06/07/2024,Parentheses credit,,(5.00),USD,968.00',
        '07/08/2024,07/08/2024,Both columns,1.00,2.00,USD,969.00',
        '08/09/2024,08/09/2024,"Thousands, quoted","1,250.00",,USD,-281.00',
    ]) + '\n'
    dayfirst = '\n'.join([
        'Date;Value date;Description;Debit;Credit;Currency',
        '03/04/2024;03/04/2024;Ambiguous alone;1.00;;EUR',
        '25/04/2024;26/04/2024;Unambiguous day first;2.00;;EUR',
    ]) + '\n'
    return {'ambiguous': _write(root / 'bank_dc_ambiguous.csv', ambiguous, 'utf-8'),
            'dayfirst': _write(root / 'bank_dc_dayfirst.csv', dayfirst, 'utf-8')}


def _transit(root: Path) -> dict:
    same_row = '\n'.join([
        'Datum;Check-in;Vertrek;Check-uit;Bestemming;Bedrag;Transactie',
        '15-03-2024;08:05;Halte Noord;08:41;Halte Zuid;-2,45;Reis',
        '15-03-2024;23:50;Halte Zuid;00:15;Halte Oost;-3,10;Reis',
        '16-03-2024;12:00;Halte Oost;;;-4,00;Reis',
        '16-03-2024;;;;;20,00;Saldo opgeladen',
        '17-03-2024;09:00;Halte West;09:30;Halte Noord;-2,45;Reis',
        '17-03-2024;09:00;Halte West;09:30;Halte Noord;-2,45;Reis',
    ]) + '\n'
    taps = '\n'.join([
        'Date,Time,Stop,Tap,Fare,Card',
        '2024-03-20,08:00:00,Stop A,entry,0.00,card-1',
        '2024-03-20,08:20:00,Stop B,exit,1.80,card-1',
        '2024-03-20,08:05:00,Stop C,entry,0.00,card-2',
        '2024-03-20,09:00:00,Stop D,entry,0.00,card-1',
        '2024-03-20,09:10:00,Stop E,entry,0.00,card-1',
        '2024-03-20,09:30:00,Stop F,exit,2.10,card-1',
        '2024-03-20,10:00:00,Stop G,exit,1.00,card-2',
        '2024-03-20,18:00:00,Stop H,exit,1.00,card-3',
        '2024-03-21,06:00:00,Stop I,entry,0.00,card-2',
        '2024-03-21,13:00:00,Stop J,exit,3.00,card-2',
        '2024-03-21,14:00:00,Stop K,wave,0.00,card-2',
    ]) + '\n'
    return {'same_row': _write(root / 'transit_same_row.csv', same_row, 'cp1252'),
            'taps': _write(root / 'transit_taps.csv', taps, 'utf-8')}


def _rides(root: Path) -> dict:
    text = '\n'.join([
        'City,Product Type,Trip or Order Status,Request Time,Begin Trip Time,Dropoff Time,Distance (miles),Fare Amount,Fare Currency',
        'Example City,Example X,COMPLETED,2024-03-30 23:40:00 +0000 UTC,2024-03-30 23:48:10 +0000 UTC,2024-03-31 00:31:00 +0000 UTC,5.25,18.40,EUR',
        'Example City,Example X,CANCELED,2024-03-31 01:10:00 +0100 UTC,,,0,0.00,EUR',
        'Example City,Example X,COMPLETED,2024-03-31 01:50:00 +0100,2024-03-31 01:55:00 +0100,2024-03-31 03:20:00 +0200,10.00,30.00,EUR',
        'Example City,Example X,DRIVER_PAUSED,2024-04-01 10:00:00 +0200 UTC,2024-04-01 10:05:00 +0200 UTC,2024-04-01 10:00:00 +0200 UTC,1.00,5.00,EUR',
    ]) + '\n'
    return {'path': _write(root / 'rides.csv', text, 'utf-8')}


CAMT_NS2 = 'urn:iso:std:iso:20022:tech:xsd:camt.053.001.02'
CAMT_NS8 = 'urn:iso:std:iso:20022:tech:xsd:camt.053.001.08'


def _camt_entry(amount, ccy, indicator, booking, value, *, status='BOOK', reversal=None, tx='', ref='REF'):
    rvsl = f'<RvslInd>{reversal}</RvslInd>' if reversal else ''
    return (f'<Ntry><NtryRef>{ref}</NtryRef><Amt Ccy="{ccy}">{amount}</Amt><CdtDbtInd>{indicator}</CdtDbtInd>{rvsl}'
            f'<Sts>{status}</Sts><BookgDt>{booking}</BookgDt><ValDt>{value}</ValDt><AcctSvcrRef>{ref}-SVC</AcctSvcrRef>'
            '<BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>RCDT</Cd><SubFmlyCd>ESCT</SubFmlyCd></Fmly></Domn></BkTxCd>'
            f'<NtryDtls>{tx}</NtryDtls></Ntry>')


def _camt_tx(name_role, name, account, text, *, nested=False, amount=None):
    party = f'<{name_role}><Pty><Nm>{name}</Nm></Pty></{name_role}>' if nested else f'<{name_role}><Nm>{name}</Nm></{name_role}>'
    amt = f'<AmtDtls><TxAmt><Amt Ccy="EUR">{amount}</Amt></TxAmt></AmtDtls>' if amount else ''
    return (f'<TxDtls><Refs><EndToEndId>E2E-{name[:3].upper()}</EndToEndId></Refs>{amt}<RltdPties>{party}'
            f'<{name_role}Acct><Id><IBAN>{account}</IBAN></Id></{name_role}Acct></RltdPties>'
            f'<RmtInf><Ustrd>{text}</Ustrd></RmtInf></TxDtls>')


def _camt_balance(code, amount, indicator, day):
    return (f'<Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">{amount}</Amt>'
            f'<CdtDbtInd>{indicator}</CdtDbtInd><Dt><Dt>{day}</Dt></Dt></Bal>')


def _camt(root: Path) -> dict:
    stmt1 = ('<Stmt><Id>STMT-1</Id><ElctrncSeqNb>1</ElctrncSeqNb><CreDtTm>2024-02-03T06:00:00+01:00</CreDtTm>'
             f'<Acct><Id><IBAN>{IBAN_NL_A}</IBAN></Id><Ccy>EUR</Ccy></Acct>'
             + _camt_balance('OPBD', '1000.00', 'CRDT', '2024-01-31') + _camt_balance('CLBD', '1090.25', 'CRDT', '2024-02-02')
             + '<TxsSummry><TtlNtries><NbOfNtries>3</NbOfNtries></TtlNtries></TxsSummry>'
             + _camt_entry('100.00', 'EUR', 'CRDT', '<Dt>2024-02-01</Dt>', '<Dt>2024-02-01</Dt>', ref='R1',
                           tx=_camt_tx('Dbtr', 'Payer Example', IBAN_NL_B, 'Invoice 1'))
             + _camt_entry('9.75', 'EUR', 'DBIT', '<DtTm>2024-02-02T10:15:00+01:00</DtTm>', '<DtTm>2024-02-02T00:00:00</DtTm>',
                           ref='R2', tx=_camt_tx('Cdtr', 'Shop Example', IBAN_NL_B, 'Coffee'))
             + _camt_entry('50.00', 'EUR', 'DBIT', '<Dt>2024-02-02</Dt>', '<Dt>2024-02-02</Dt>', status='PDNG', ref='R3')
             + '</Stmt>')
    stmt2 = ('<Stmt><Id>STMT-2</Id>'
             f'<Acct><Id><IBAN>{IBAN_NL_A}</IBAN></Id></Acct>'
             + _camt_balance('OPBD', '1090.25', 'CRDT', '2024-02-02') + _camt_balance('CLBD', '1200.00', 'CRDT', '2024-02-05')
             + '<TxsSummry><TtlNtries><NbOfNtries>2</NbOfNtries></TtlNtries><TtlCdtNtries><NbOfNtries>1</NbOfNtries>'
               '<Sum>100.00</Sum></TtlCdtNtries></TxsSummry>'
             + _camt_entry('100.00', 'EUR', 'CRDT', '<Dt>2024-02-05</Dt>', '<Dt>2024-02-05</Dt>', ref='R4',
                           tx=_camt_tx('Dbtr', 'Batch One', IBAN_NL_B, 'Part 1', amount='60.00')
                           + _camt_tx('Dbtr', 'Batch Two', IBAN_NL_B, 'Part 2', amount='40.00'))
             + _camt_entry('5.00', 'EUR', 'DBIT', '<Dt>2024-02-05</Dt>', '<Dt>2024-02-05</Dt>', reversal='true', ref='R5')
             + '</Stmt>')
    doc = (f'<?xml version="1.0" encoding="UTF-8"?>\n<Document xmlns="{CAMT_NS2}"><BkToCstmrStmt>'
           '<GrpHdr><MsgId>MSG-1</MsgId><CreDtTm>2024-02-05T07:00:00Z</CreDtTm></GrpHdr>'
           f'{stmt1}{stmt2}</BkToCstmrStmt></Document>\n')
    v8 = (f'<?xml version="1.0" encoding="UTF-8"?>\n<Document xmlns="{CAMT_NS8}"><BkToCstmrStmt><GrpHdr><MsgId>M8</MsgId></GrpHdr>'
          f'<Stmt><Id>S8</Id><Acct><Id><Othr><Id>ACCOUNT-0000-1</Id></Othr></Id></Acct>'
          + _camt_balance('PRCD', '10.00', 'DBIT', '2024-03-01') + _camt_balance('CLBD', '15.00', 'CRDT', '2024-03-02')
          + '<Ntry><Amt Ccy="EUR">25.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>'
            '<BookgDt><Dt>2024-03-02</Dt></BookgDt><ValDt><Dt>2024-03-02</Dt></ValDt><NtryDtls>'
          + _camt_tx('Dbtr', 'Nested Party', IBAN_PL_B, 'Nested name form', nested=True)
          + '</NtryDtls></Ntry></Stmt></BkToCstmrStmt></Document>\n')
    entity = ('<?xml version="1.0"?>\n<!DOCTYPE Document [<!ENTITY x "boom">]>\n'
              f'<Document xmlns="{CAMT_NS2}"><BkToCstmrStmt>&x;</BkToCstmrStmt></Document>\n')
    notification = ('<?xml version="1.0"?>\n<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.054.001.02">'
                    '<BkToCstmrDbtCdtNtfctn><GrpHdr><MsgId>N</MsgId></GrpHdr></BkToCstmrDbtCdtNtfctn></Document>\n')
    out = {}
    for name, text in (('two_statements', doc), ('v8', v8), ('entity', entity), ('notification', notification)):
        out[name] = _write(root / f'camt053_{name}.xml', text, 'utf-8')
    return out


MT940_ONE = """{1:F01EXAMPLEXXXXX0000000000}{2:I940EXAMPLEXXXXXN}{4:
:20:STMT-2025-12
:25:@ACCOUNT@
:28C:00012/001
:60F:C251230EUR1000,00
:61:2512311231D25,50NTRFNONREF//BANKREF1
SUPPLEMENTARY DETAILS
:86:/EREF/E2E-1/NAME/Shop Example/IBAN/@IBAN_B@/REMI/Groceries
:61:2512310102C100,NMSCREF2
:86:?00Przelew?20Zwrot za u?21sluge?32Firma Przyk?33lad?38@IBAN_PL@
:61:2601020102RD5,00FCHGNONREF
:86:Free text line one
continuing line two
:61:2507010101C1,00NTRFAMBIG
:86:ambiguous entry date
:62F:C260102EUR1080,50
-}
:20:STMT2
:25:@ACCOUNT@
:28C:13/1
:60F:C260102EUR1080,50
:61:260105D80,50NTRFX
:61:260105D80,50NTRFX
:62F:C260105EUR950,00
-
:20:STMT3
:25:@ACCOUNT@
:60M:C260105EUR950,00
:61:THIS IS NOT A STATEMENT LINE
:62M:C260105EUR950,00
-
"""


def _mt940(root: Path) -> dict:
    text = (MT940_ONE.replace('@ACCOUNT@', IBAN_NL_A).replace('@IBAN_B@', IBAN_NL_B).replace('@IBAN_PL@', IBAN_PL_B)
            .replace('\n', '\r\n'))
    return {'path': _write(root / 'statement.sta', text, 'ascii'),
            'no_fields': _write(root / 'not_mt940.txt', 'just a note\n', 'ascii')}


APPLE_DTD = """<!DOCTYPE HealthData [
<!ELEMENT HealthData (ExportDate,Me,(Record|Correlation|Workout|ActivitySummary)*)>
<!ATTLIST HealthData locale CDATA #REQUIRED>
<!ATTLIST Record type CDATA #REQUIRED unit CDATA #IMPLIED value CDATA #IMPLIED sourceName CDATA #REQUIRED device CDATA "DEFAULT-DEVICE-FROM-DTD">
]>"""


def _apple_record(kind, start, end, value=None, unit=None, source='Example Phone', device=None, extra='', inner=''):
    attrs = f'type="{kind}" sourceName="{source}" sourceVersion="17.0"'
    if device:
        attrs += f' device="{device}"'
    if unit:
        attrs += f' unit="{unit}"'
    attrs += f' creationDate="{end}" startDate="{start}" endDate="{end}"'
    if value is not None:
        attrs += f' value="{value}"'
    return f' <Record {attrs}{extra}>{inner}</Record>\n' if inner else f' <Record {attrs}{extra}/>\n'


SLEEP = 'HKCategoryTypeIdentifierSleepAnalysis'
WATCH = '&lt;&lt;HKDevice: 0x0&gt;, name:Example Watch, manufacturer:Example&gt;'


def apple_export(extra_records: str = '') -> str:
    v = 'HKCategoryValueSleepAnalysis'
    body = [
        ' <ExportDate value="2024-04-01 12:00:00 +0200"/>\n',
        ' <Me HKCharacteristicTypeIdentifierDateOfBirth="" HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexNotSet"/>\n',
        _apple_record('HKQuantityTypeIdentifierStepCount', '2024-03-30 10:00:00 +0100', '2024-03-30 10:05:00 +0100', '120', 'count'),
        _apple_record('HKQuantityTypeIdentifierHeartRate', '2024-03-31 01:59:00 +0100', '2024-03-31 03:01:00 +0200', '61.5',
                      'count/min', 'Example Watch', WATCH,
                      inner='<MetadataEntry key="HKMetadataKeyHeartRateMotionContext" value="0"/>'),
        _apple_record('HKQuantityTypeIdentifierBodyMass', '2024-03-30 08:00:00 +0100', '2024-03-30 08:00:00 +0100', '70.25',
                      'kg', inner='<MetadataEntry key="HKWasUserEntered" value="1"/>'),
        _apple_record('HKQuantityTypeIdentifierRestingHeartRate', '2024-03-30 00:00:00 +0100', '2024-03-30 23:59:00 +0100',
                      '55', 'count/min', 'Example Watch', WATCH),
        _apple_record('HKQuantityTypeIdentifierStepCount', '2024-03-30 12:00:00 +0100', '2024-03-30 11:00:00 +0100', '5', 'count'),
        _apple_record('HKQuantityTypeIdentifierStepCount', 'yesterday', '2024-03-30 11:00:00 +0100', '5', 'count'),
        # night 1, watch: stages across the DST change (02:00 +01:00 -> 03:00 +02:00)
        _apple_record(SLEEP, '2024-03-30 23:00:00 +0100', '2024-03-31 07:30:00 +0200', f'{v}InBed', None, 'Example Watch', WATCH),
        _apple_record(SLEEP, '2024-03-30 23:20:00 +0100', '2024-03-31 01:50:00 +0100', f'{v}AsleepCore', None, 'Example Watch', WATCH),
        _apple_record(SLEEP, '2024-03-31 01:50:00 +0100', '2024-03-31 03:40:00 +0200', f'{v}AsleepREM', None, 'Example Watch', WATCH),
        _apple_record(SLEEP, '2024-03-31 03:40:00 +0200', '2024-03-31 05:00:00 +0200', f'{v}AsleepDeep', None, 'Example Watch', WATCH),
        _apple_record(SLEEP, '2024-03-31 05:00:00 +0200', '2024-03-31 05:05:00 +0200', f'{v}Awake', None, 'Example Watch', WATCH),
        _apple_record(SLEEP, '2024-03-31 05:05:00 +0200', '2024-03-31 07:20:00 +0200', f'{v}AsleepCore', None, 'Example Watch', WATCH),
        # night 1, phone: overlapping in-bed estimate from a second device
        _apple_record(SLEEP, '2024-03-30 23:10:00 +0100', '2024-03-31 07:00:00 +0200', f'{v}InBed', None, 'Example Phone'),
        # night 2, watch: separated by more than the session gap
        _apple_record(SLEEP, '2024-03-31 23:30:00 +0200', '2024-04-01 06:30:00 +0200', f'{v}AsleepUnspecified', None,
                      'Example Watch', WATCH),
        ' <Correlation type="HKCorrelationTypeIdentifierBloodPressure" sourceName="Example Cuff" startDate="2024-03-30 09:00:00 +0100" endDate="2024-03-30 09:00:00 +0100">\n'
        + _apple_record('HKQuantityTypeIdentifierBloodPressureSystolic', '2024-03-30 09:00:00 +0100', '2024-03-30 09:00:00 +0100', '120', 'mmHg', 'Example Cuff')
        + _apple_record('HKQuantityTypeIdentifierBloodPressureDiastolic', '2024-03-30 09:00:00 +0100', '2024-03-30 09:00:00 +0100', '80', 'mmHg', 'Example Cuff')
        + ' </Correlation>\n',
        ' <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30.5" durationUnit="min" totalDistance="5.01" '
        'totalDistanceUnit="km" totalEnergyBurned="320" totalEnergyBurnedUnit="kcal" sourceName="Example Watch" sourceVersion="10.1" '
        f'device="{WATCH}" creationDate="2024-03-30 18:31:00 +0100" startDate="2024-03-30 18:00:00 +0100" endDate="2024-03-30 18:30:30 +0100">\n'
        '  <MetadataEntry key="HKIndoorWorkout" value="0"/>\n'
        '  <WorkoutEvent type="HKWorkoutEventTypePause" date="2024-03-30 18:10:00 +0100" duration="0.5" durationUnit="min"/>\n'
        '  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="2024-03-30 18:00:00 +0100" endDate="2024-03-30 18:30:30 +0100" average="150.25" minimum="120" maximum="171" unit="count/min"/>\n'
        '  <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" startDate="2024-03-30 18:00:00 +0100" endDate="2024-03-30 18:30:30 +0100" sum="320" unit="kcal"/>\n'
        '  <WorkoutRoute sourceName="Example Watch"><FileReference path="/workout-routes/route_2024-03-30.gpx"/></WorkoutRoute>\n'
        ' </Workout>\n',
        ' <Workout workoutActivityType="HKWorkoutActivityTypeYoga" duration="20" durationUnit="min" sourceName="Example Phone" '
        'creationDate="2024-03-31 19:00:00 +0200" startDate="2024-03-31 18:40:00 +0200" endDate="2024-03-31 19:00:00 +0200">'
        '<MetadataEntry key="HKWasUserEntered" value="1"/></Workout>\n',
        ' <ActivitySummary dateComponents="2024-03-30" activeEnergyBurned="512.5" activeEnergyBurnedGoal="500" '
        'activeEnergyBurnedUnit="kcal" appleExerciseTime="35" appleExerciseTimeGoal="30" appleStandHours="11" appleStandHoursGoal="12"/>\n',
        extra_records,
    ]
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + APPLE_DTD + '\n<HealthData locale="en_NL">\n' + ''.join(body) + '</HealthData>\n'


def _health(root: Path) -> dict:
    import zipfile
    apple = root / 'apple'
    apple.mkdir(exist_ok=True)
    out = {'export': _write(apple / 'export.xml', apple_export(), 'utf-8')}
    with zipfile.ZipFile(apple / 'export.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('apple_health_export/export.xml', apple_export())
        archive.writestr('apple_health_export/export_cda.xml', '<ClinicalDocument/>')
    out['zip'] = str(apple / 'export.zip')
    out['entity'] = _write(apple / 'entity.xml', '<?xml version="1.0"?>\n<!DOCTYPE HealthData [<!ENTITY lol "lol">]>\n'
                           '<HealthData locale="x">&lol;</HealthData>\n', 'utf-8')
    out['external_dtd'] = _write(apple / 'external.xml', '<?xml version="1.0"?>\n<!DOCTYPE HealthData SYSTEM '
                                 '"http://example.org/health.dtd">\n<HealthData locale="x"/>\n', 'utf-8')
    fit = root / 'Takeout' / 'Fit'
    (fit / 'All Sessions').mkdir(parents=True, exist_ok=True)
    (fit / 'Daily activity metrics').mkdir(parents=True, exist_ok=True)
    sleep_session = {'fitnessActivity': 'sleep', 'startTime': '2024-03-30T22:00:00.000Z', 'endTime': '2024-03-31T05:30:00.000Z',
                     'duration': '27000s', 'segment': [
                         {'fitnessActivity': 'sleep.light', 'startTime': '2024-03-30T22:00:00.000Z', 'endTime': '2024-03-31T01:00:00.000Z'},
                         {'fitnessActivity': 'sleep.deep', 'startTime': '2024-03-31T01:00:00.000Z', 'endTime': '2024-03-31T05:30:00.000Z'}],
                     'aggregate': [{'metricName': 'com.google.calories.expended', 'floatValue': 0.1}]}
    walk = ('{"fitnessActivity": "walking", "startTime": "2024-03-30T17:00:00.000Z", "endTime": "2024-03-30T17:30:03.512Z", '
            '"duration": "1700s", "aggregate": [{"metricName": "com.google.calories.expended", "floatValue": 65.12345678901234567}, '
            '{"metricName": "com.google.step_count.delta", "intValue": 3012}], "application": {"packageName": "com.example.fit"}}')
    google = {
        'sleep': _write(fit / 'All Sessions' / '2024-03-30T23_00_00+01_00_SLEEP.json', json.dumps(sleep_session), 'utf-8'),
        'walk': _write(fit / 'All Sessions' / '2024-03-30T18_00_00+01_00_WALKING.json', walk, 'utf-8'),
        'daily': _write(fit / 'Daily activity metrics' / 'Daily activity metrics.csv',
                        'Date,Move Minutes count,Calories (kcal),Distance (m),Low latitude (deg),Low longitude (deg),Step count\n'
                        '2024-03-30,45,1850.123,3500.5,52.0000,4.0000,6012\n2024-03-31,,1700,,,,\n', 'utf-8'),
        'intervals': _write(fit / 'Daily activity metrics' / '2024-03-31.csv',
                            'Start time,End time,Move Minutes count,Step count\n'
                            '01:45:00.000+01:00,02:00:00.000+01:00,1,20\n03:00:00.000+02:00,03:15:00.000+02:00,,15\n', 'utf-8'),
    }
    samsung_dir = root / 'Samsung Health'
    samsung_dir.mkdir(exist_ok=True)
    prefix = 'com.samsung.health.sleep.'
    samsung = {
        'sleep': _write(samsung_dir / 'com.samsung.shealth.sleep.20240401120000.csv',
                        'com.samsung.shealth.sleep,6315006,3\n'
                        f'{prefix}start_time,{prefix}end_time,{prefix}time_offset,{prefix}deviceuuid,{prefix}datauuid,efficiency,sleep_score,\n'
                        '2024-03-30 22:05:00.000,2024-03-31 05:25:00.000,UTC+0100,device-0001,uuid-0001,91.5,82,\n'
                        '2024-03-31 21:40:00.000,2024-04-01 04:50:00.000,UTC+0200,device-0001,uuid-0002,88,,\n'
                        '2024-04-01 21:40:00.000,2024-04-02 04:50:00.000,CET,device-0002,uuid-0003,,,\n', 'utf-8'),
        'exercise': _write(samsung_dir / 'com.samsung.shealth.exercise.20240401120000.csv',
                           'com.samsung.shealth.exercise,6315003,5\n'
                           'com.samsung.health.exercise.start_time,com.samsung.health.exercise.end_time,'
                           'com.samsung.health.exercise.time_offset,com.samsung.health.exercise.exercise_type,'
                           'com.samsung.health.exercise.duration,com.samsung.health.exercise.distance,'
                           'com.samsung.health.exercise.calorie,com.samsung.health.exercise.deviceuuid,'
                           'com.samsung.health.exercise.datauuid\n'
                           '2024-03-30 17:00:00.000,2024-03-30 17:30:00.000,UTC+0100,1002,1800000,5010.5,320.25,device-0001,uuid-e1\n',
                           'utf-8'),
        'steps': _write(samsung_dir / 'com.samsung.shealth.step_daily_trend.20240401120000.csv',
                        'com.samsung.shealth.step_daily_trend,6315002,4\n'
                        'day_time,count,distance,calorie,deviceuuid,datauuid\n1711756800000,6012,4500.25,250.5,device-0001,uuid-s1\n',
                        'utf-8'),
        'unmapped': _write(samsung_dir / 'com.samsung.health.weight.20240401120000.csv',
                           'com.samsung.health.weight,6315001,1\nstart_time,weight\n2024-03-30 07:00:00.000,70.1\n', 'utf-8'),
        'not_samsung': _write(samsung_dir / 'plain.csv', 'a,b\n1,2\n', 'utf-8'),
    }
    fitbit_dir = root / 'Takeout' / 'Fitbit' / 'Global Export Data'
    fitbit_dir.mkdir(parents=True, exist_ok=True)
    sleep_logs = [
        {'logId': 1001, 'dateOfSleep': '2024-03-31', 'startTime': '2024-03-30T23:10:00.000', 'endTime': '2024-03-31T07:00:30.000',
         'duration': 28230000, 'minutesToFallAsleep': 0, 'minutesAsleep': 420, 'minutesAwake': 50, 'timeInBed': 470,
         'efficiency': 92, 'type': 'stages', 'infoCode': 0, 'logType': 'auto_detected', 'mainSleep': True,
         'levels': {'summary': {'deep': {'count': 3, 'minutes': 80}},
                    'data': [{'dateTime': '2024-03-30T23:10:00.000', 'level': 'wake', 'seconds': 600},
                             {'dateTime': '2024-03-30T23:20:00.000', 'level': 'light', 'seconds': 3600}],
                    'shortData': [{'dateTime': '2024-03-31T02:00:00.000', 'level': 'wake', 'seconds': 60}]}},
        {'logId': 1002, 'dateOfSleep': '2024-03-31', 'startTime': '2024-03-31T14:00:00.000', 'endTime': '2024-03-31T14:30:00.000',
         'duration': 1800000, 'minutesAsleep': 30, 'type': 'classic', 'logType': 'manual', 'mainSleep': False, 'levels': {}},
    ]
    exercise_logs = ('[{"logId": 2001, "activityName": "Walk", "activityTypeId": 90013, "startTime": "03/30/24 18:00:00", '
                     '"duration": 1800000, "activeDuration": 1800000, "logType": "auto_detected", "steps": 3000, '
                     '"calories": 150, "distance": 2.12345678901234567, "distanceUnit": "Kilometer", "averageHeartRate": 101}, '
                     '{"logId": 2002, "activityName": "Yoga", "startTime": "03/31/24 07:00:00", "duration": "oops", '
                     '"logType": "manual"}]')
    fitbit = {'sleep': _write(fitbit_dir / 'sleep-2024-03-30.json', json.dumps(sleep_logs), 'utf-8'),
              'exercise': _write(fitbit_dir / 'exercise-0.json', exercise_logs, 'utf-8')}
    return {'apple': out, 'apple_expect': {'records': 16, 'workouts': 2, 'sleep_samples': 8, 'sleep_sessions': 3},
            'google_fit': google, 'samsung': samsung, 'fitbit': fitbit}


def build(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    tables = root / 'tables'
    tables.mkdir(exist_ok=True)
    statements = root / 'statements'
    statements.mkdir(exist_ok=True)
    health_root = root / 'health'
    health_root.mkdir(exist_ok=True)
    manifest = {
        'bank_pl': _bank_pl(tables), 'bank_nl': _bank_nl(tables), 'bank_dc': _bank_dc(tables),
        'transit': _transit(tables), 'rides': _rides(tables), 'camt': _camt(statements), 'mt940': _mt940(statements),
        'health': _health(health_root),
        'ibans': {'nl_a': IBAN_NL_A, 'nl_b': IBAN_NL_B, 'pl_a': IBAN_PL_A, 'pl_b': IBAN_PL_B, 'invalid': IBAN_INVALID},
    }
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding='utf-8')
    return manifest

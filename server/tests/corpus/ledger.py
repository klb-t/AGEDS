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


def build(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    tables = root / 'tables'
    tables.mkdir(exist_ok=True)
    manifest = {
        'bank_pl': _bank_pl(tables), 'bank_nl': _bank_nl(tables), 'bank_dc': _bank_dc(tables),
        'transit': _transit(tables), 'rides': _rides(tables),
        'ibans': {'nl_a': IBAN_NL_A, 'nl_b': IBAN_NL_B, 'pl_a': IBAN_PL_A, 'pl_b': IBAN_PL_B, 'invalid': IBAN_INVALID},
    }
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding='utf-8')
    return manifest

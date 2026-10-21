"""Isolated synthetic fixture for the /ledger browser script (task L3); never uses an existing database.

Generated ledger corpus (bank CSV, CAMT.053, MT940, transit taps, Apple Health with
the per-run opt-in) plus one bank row whose counterparty and description are
markup-like text that must render as text.
"""
import json
import os
import tempfile
from pathlib import Path

LITERAL = '<img src=x onerror="window.__ageds_ledger_xss=1">'


def main():
    with tempfile.TemporaryDirectory(prefix='ageds-ledger-browser-') as directory:
        root = Path(directory)
        os.environ.update(EW_DATA_DIR=str(root / 'data'), EW_DB_PATH=str(root / 'data' / 'evidence.sqlite'),
                          EW_STORE_DIR=str(root / 'data' / 'store'))
        from server.app import db
        from server.app.importers import camt053, health_apple, mt940, tabular_profiles
        from server.tests.corpus import ledger as corpus
        import uvicorn
        db.init_db()
        manifest = corpus.build(root / 'corpus')
        tabular_profiles.import_tabular(Path(manifest['bank_pl']['files']['cp1250']))
        markup = root / 'corpus' / 'markup.csv'
        markup.write_text('Data operacji;Data księgowania;Kontrahent;Rachunek kontrahenta;Tytuł;Kwota;Waluta\n'
                          f'01.02.2024;01.02.2024;{LITERAL};{corpus.IBAN_PL_B};<script>window.__ageds_ledger_xss=2</script>;-1,00;PLN\n',
                          encoding='utf-8')
        tabular_profiles.import_tabular(markup)
        tabular_profiles.import_tabular(Path(manifest['transit']['taps']))
        camt053.import_camt053(Path(manifest['camt']['two_statements']))
        mt940.import_mt940(Path(manifest['mt940']['path']))
        health_apple.import_apple_health(Path(manifest['health']['apple']['export']), enable_health=True)
        print(json.dumps({'iban': corpus.IBAN_PL_B, 'literal': LITERAL}), flush=True)
        uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')


if __name__ == '__main__':
    main()

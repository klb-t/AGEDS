# Wave 12 — server CSV physical-line repair

N73, 2026-10-01. `scanner.py` is version **1.1.1**; the manifest schema is unchanged.

## Reproduction and correction

The former parser built raw-record boundaries with `str.splitlines(keepends=True)`, while `csv.reader` used `StringIO` line numbers. These disagree on Unicode/control separators. The synthetic UTF-8 input `name,value\nalpha\u2028beta,x\nsecond,y\n` produced correct cell values but the wrong raw records: row two captured only `alpha\u2028`, and row three captured `beta,x\n`. Both records claimed completeness; file, table and global coverage were complete with no issues. Source bytes remained unchanged.

The scanner now builds untranslated physical lines using `io.StringIO(text, newline="")` and passes that same line sequence to `csv.reader`. CR, LF and CRLF determine physical line numbers; U+2028, U+2029, NEL, vertical tab, form feed and record-separator characters remain literal cell data. Raw decoded records are sliced from the exact sequence consumed by the CSV reader. Bare-CR input now parses correctly, and the existing Excel `sep=` preamble recognizes bare CR as well as LF/CRLF.

Quoted multiline content retains original decoded line endings. Error fragments begin after the final successfully consumed record. Existing encoding selection, character/row/file budgets, raw-record truncation flags and source read-only behavior are preserved. This change does not alter WAV or XLSX parsing and does not claim original encoded bytes are included in decoded `raw_record` fields.

## Verification

New `server/tests/test_scanner_csv_physical_lines.py`: **5 tests**, including subcases for eight nonphysical separator characters and four physical newline patterns. It checks exact cells/raw records/start lines, UTF-16 with bare-CR preamble and quoted mixed newlines, malformed fragment boundaries, fragment/raw-record budget truncation, explicit completeness and unchanged source bytes.

Combined with the existing scanner suite: **26 tests passed** in 0.498 seconds:

```sh
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:$PWD python -m unittest server.tests.test_scanner_csv_physical_lines server.tests.test_scanner -q
```

Independent adversarial tests and actual CLI smoke have separate N75/N77 owners. No dependencies, commits, external actions or source corpus were used. No open defect remains within the owner regression scope.

## Frozen hashes

- `server/app/scanner.py`: `711488e3e79f3dfc8f886f5a8a30da5865275677e55f6640ff90ca887d4dae95`
- `server/tests/test_scanner_csv_physical_lines.py`: `4a3a281323572ced38457073c25d2e187f95e856aebdadf9701ce4dbac042794`

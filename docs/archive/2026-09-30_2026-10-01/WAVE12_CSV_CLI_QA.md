# Wave 12: actual CSV CLI acceptance (N77)

Executed `scripts/csv_cli_smoke.py` after the N73 scanner 1.1.1 source freeze.
Both real `python -m server.app.cli scan ... --output ...` subprocesses passed:
20 exact record checks in the complete scan and 8 in the deliberately limited
scan. Evidence is recorded in [WAVE12_CSV_CLI_RECEIPT.json](WAVE12_CSV_CLI_RECEIPT.json),
including the interpreter, executed commands, source snapshots, output hashes,
and SHA-256 fingerprints of the scanner, CLI, WAV import dependency and harness.
Those code fingerprints were checked again after execution.

Reproduce from the repository root with the existing Python interpreter:

```sh
python scripts/csv_cli_smoke.py --receipt /tmp/ageds-csv-cli-receipt.json
```

The compact fixtures are generated in a temporary source directory. A separate
sibling metadata directory receives the actual CLI exports; the subprocess runs
with the source directory as its working directory and bytecode writes disabled.
No scanner functions are imported into the test process. Temporary inputs and
manifests are removed after assertions; the receipt retains hashes and checks,
not a second copy of all parsed source records.

## Inputs and assertions

Four synthetic files cover UTF-8 CSV, UTF-8 BOM TSV, UTF-16 little-endian BOM CSV,
and UTF-16 big-endian BOM TSV. Each has five records. CSV fixtures include a
`sep=;` preamble terminated by a bare CR; TSV uses tabs directly. Records mix
CR, LF and CRLF endings, with no terminator on the last record. A quoted field
contains all three physical newline forms, another contains an escaped double
quote and the delimiter. Quoted and unquoted fields retain U+2028, U+2029, NEL,
VT, FF, FS, GS and RS literally.

Expected decoded values, exact `raw_record` strings and physical `start_line`
numbers are hand-authored in the harness; the oracle does not call `csv.reader`
or `splitlines`. BOM removal is the existing decoding contract, so raw records
are decoded strings, not claims of byte-identical source slices. File hashes
independently cover the original encoded bytes including the BOM.

The complete run checks all table, raw-record and cell completeness flags,
encoding/delimiter/preamble metadata, empty issues and full manifest coverage.
The second run uses `--max-rows-per-file 2`: exit zero means the partial manifest
was exported successfully. It checks false table/manifest/stdout completeness,
one `row_limit` issue per file, and complete exact records for the admitted
prefix. Truncation is not represented as a complete scan.

Both runs verify original source SHA-256, size and nanosecond modification time
against snapshots, matching manifest hashes/mtime, no extra source files,
`read_only=true`, `source_copies=false`, `source_bytes_written=false`, and output
outside the source directory. This does not assert access-time preservation;
ordinary reads can update filesystem access time.

## Scope

This is executed local CLI/parser/export evidence on small synthetic fixtures.
It adds no private corpus access, network requests, database restore, source
writes, production edits or dependencies. It does not establish Android SAF
runtime behavior, spreadsheet phonetic handling, semantic truth, authorship,
ASR alignment, or equivalence between Android and local scanner policies.

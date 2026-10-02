# N52 — actual CLI WAV scan and metadata export

**PASS**, 2026-10-01 00:58 UTC. `scripts/server_wav_scan_smoke.py` generated a new,
synthetic WAV tree and invoked the real `python -m server.app.cli scan --output`
command in a subprocess. The command exported a 17,237-byte metadata manifest
outside the source directory. This exercises the CLI's scan/export path, not a
mocked scanner or a database metadata export.

The final receipt is `SERVER_WAV_SCAN_SMOKE_RECEIPT.json`. Parser and scanner
owners froze their files before execution; all four recorded fingerprints
(CLI, scanner, WAV parser and smoke script) matched after execution.

## Observed fixtures

| Synthetic source | Actual bytes | `parse_status` | Header status | Declared duration | Header bytes read | Whole-file hash |
|---|---:|---|---|---|---:|---|
| Valid mono PCM16/16 kHz | 16,044 | `metadata_only` | `observed` | 0.5 s | 16,044 | Present, matches independent hash |
| Header only, declaring 16,000 data bytes | 44 | `failed` | `malformed` | None | 44 | Present; does not validate the declaration |
| Valid large declaration with sparse zero-filled data | 4,194,348 | `metadata_only` | `observed` | 131.072 s | 65,536 | Unknown: exceeds configured hash budget |
| 9,000 JUNK chunks before format/data | 72,060 | `limited` | `partial` | None | 65,536 | Present, separate from header limit |
| Unsupported format code 6 | 76 | `unsupported` | `unsupported` | None | 76 | Present; does not establish codec support |

Every header observation records `scope=header_prefix_only`,
`body_validated=false`, and `source=same_descriptor_riff_header`. Size comes from
`fstat` on the same descriptor, and descriptor metadata was unchanged across each
scan. Supported durations retain the explicit basis
`declared_data_bytes_divided_by_header_byte_rate`; they are declarations, not
measured playback times or successful decoding claims.

## Bounds and source preservation

The manifest declares the effective limits: **65,536 bytes per WAV header**,
**8 MiB aggregate WAV headers**, **1 MiB per full-file hash**, **2 MiB aggregate
hashing**, and the CLI invocation limits output to **256 KiB**.

Observed header reads totaled **147,236 bytes**, separately from **88,224 hash
bytes**. Four files received full hashes and the oversized sparse file did not
receive a partial digest disguised as a full hash. These byte counters include
separate reads of the same source bytes; they are not unique coverage measures.
Overall `coverage.complete` remains **false**. CLI exit 0 means the partial
manifest was successfully exported, not that every source was fully understood.

The harness compared source names, sizes, SHA-256 hashes and modification times
before and after, including the source directory's modification time. All matched.
Before writing its own receipt, the only file created outside the source tree in
the isolated run was `metadata/manifest.json`. No audio copy, database or cache
file appeared; the deliberately designated application-data directory was not
created. The exported manifest declares `read_only=true`, `source_copies=false`
and contains no sample tables. Access times are deliberately not claimed
unchanged because reads may update them.

## Reproduce

```sh
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:/workspace/scratch/773725428b88/AGEDS \
python3 scripts/server_wav_scan_smoke.py --work-dir /path/to/new/isolated-directory
```

The directory must not exist. The accepted local run is
`/workspace/scratch/773725428b88/review/server-wav-scan-wave8`. Its receipt preserves
the exact subprocess command/stdout/stderr, source snapshots, each file's header
metadata, effective limits, coverage, output hash and executed-code fingerprints.
A failed run retains its exception/traceback and exits nonzero.

Elapsed harness time was approximately 0.116 seconds; this small synthetic run
is not a performance benchmark. No ASR, model download, dependency installation,
private source tree, device interaction, acoustic alignment or human-audio quality
assessment occurred. Header provenance and whole-file hashes do not establish
source authenticity or the truth of a recording.

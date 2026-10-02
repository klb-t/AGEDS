# N65 — actual CLI acceptance of bounded inert archive verification

**PASS**, 2026-10-01 01:26 UTC. The new
`scripts/archive_budget_cli_smoke.py` executed **nine real CLI subprocesses** against
compact synthetic fixtures. The source script, CLI, package validator, archive,
verification-budget helper and citation-projection fingerprints all matched the
final frozen code after execution.

Evidence: `ARCHIVE_BUDGET_CLI_SMOKE_RECEIPT.json` records exact commands,
stdout/stderr, return codes, input/output hashes and modification times, code
fingerprints, limits and acceptance checks. No production files were edited by
this task; no dependencies or models were downloaded.

## Actual subprocess outcomes

| Case | CLI operation | Observed outcome |
|---|---|---|
| Valid pinned citation package with opaque deeply nested metadata text | `metadata-verify` | Exit 0, valid |
| Valid package → new inert SQLite archive | `metadata-archive-import` | Exit 0 |
| Archive → metadata JSON | `metadata-archive-export` | Exit 0; exact canonical roundtrip |
| Hidden depth in pinned `segments_json` | `metadata-verify`, archive import | Exit 1 structured `verification_limit_exceeded`; exit 2 refusal, no output |
| Hidden depth in pinned `selector_json` | `metadata-verify`, archive import | Exit 1 structured `verification_limit_exceeded`; exit 2 refusal, no output |
| Digest-consistent inert archive containing the hidden-depth segments | Archive export | Exit 2 `verification_limit_exceeded`, no output |
| Existing valid archive target | Archive import | Exit 2 `FileExistsError`; existing archive unchanged |

The valid JSON fixture is **3,243 bytes**, its inert archive **16,384 bytes**, and
the exported canonical JSON **3,243 bytes**. Invalid JSON fixtures are **3,426
bytes each**. No large fixture or resource-exhaustion experiment was needed.

## Semantic boundary exercised

All three JSON packages have outer wire depth **4**. The invalid inputs hide **70
nested arrays** inside a JSON string that must actually be decoded to verify the
pinned citation: either its transcript's `segments_json` or anchor's
`selector_json`. Both package digests remain valid. They therefore exercise the
semantic depth budget, not a broken digest or an oversized outer JSON document.
The malicious archive fixture is constructed separately with consistent package
and table digests, so its rejection also exercises semantic verification on export.

By contrast, the valid package's artifact `metadata_json` contains **80 nested
arrays as opaque text**. It is retained byte-for-byte as a string throughout the
roundtrip. Arbitrary metadata text is not silently interpreted merely because it
resembles JSON. The pinned quote, selector, whitespace, Unicode, annotation text,
opaque metadata and historical job state all remain in the exact canonical
package.

Existing CLI options were used: **65,536 input bytes**, **1 MiB archive bytes**,
and **100 rows**. The production default semantic depth remains **64**. No new CLI
flag or production API was introduced by the harness.

## Preservation and publication

Each subprocess compares the supplied read-only input's SHA-256, size and
modification time before/after. The original JSON fixtures, synthetic source
sentinel, valid archive and deliberately hostile archive remained unchanged.
Invalid archive import/export created neither their intended output nor its
parent directory. No `.ageds-*` temporary publication remained. The existing
archive was not overwritten.

The accepted archive has only `archive_envelope` and `archive_records` tables.
CLI reports retain `inert=true`, `jobs_resumed=false` and
`live_restore_supported=false`. A historical `running` job stays historical
metadata; the deliberately designated live database/store directories were never
created. This is **not** live restore, task replay, a source-media copy or a signed
chain of custody. The harness itself hashes its generated source sentinel for
preservation checks; no private source is used.

## Reproduce

```sh
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:/workspace/scratch/773725428b88/AGEDS \
python3 scripts/archive_budget_cli_smoke.py --work-dir /path/to/new/isolated-directory
```

The output directory must be new. The accepted run remains in
`/workspace/scratch/773725428b88/review/archive-budget-cli-wave10`. Failures retain
an error/traceback receipt and exit nonzero. Total harness elapsed time was
approximately **1.005 seconds**; this is functional acceptance, not a performance
benchmark. No ASR, device interaction, acoustic alignment or human-corpus quality
assessment was performed.

# Wave 8 independent Python WAV stream QA — N54

Claim `80bc13e`; task `AGEDS-20261001-N54`. Production scanner integration belongs
to N50 and the pure parser to N49. N51 separately tests header semantics and
false-complete layouts. N54 owns only the new
`server/tests/test_wav_stream_acceptance.py` and this report.

## Measurement and scope

The 15 synthetic tests call the actual `scan_sources` entry point with real
filesystem files and a small wrapper around its `os.fdopen` handle. The parser
and scanner are not mocked. The wrapper records requested read lengths, bytes
returned, seeks and closure. Hash and header phases are measured separately on
the same descriptor, with the rewind marking the header phase.

These counters describe logical bytes returned to the scanner. They do not
measure disk sectors, kernel read-ahead, network/provider traffic or physical
I/O. The production WAV descriptor is unbuffered, but that does not establish a
lower-layer physical-I/O budget.

| Check | Acceptance |
|---|---|
| Header budget | Default 64 KiB and smaller per-file caps; aggregate remainder across three files; no read/sentinel after exhaustion |
| Hash separation | Full hash bytes and header bytes remain distinct metrics on one acquired descriptor |
| Partial failure | An 8-byte read followed by an exception consumes 8 bytes, leaving only the aggregate remainder for the next file |
| EOF | Short EOF charges actual returned bytes rather than requested capacity; exact cap performs no extra EOF read |
| Mutation | Growth and same-size changes during header reads invalidate hash/completion; earlier entry-stat observations are preserved when a change precedes the first descriptor-stat |
| Descriptor lifecycle | Normal/error closure, failure while wrapping an acquired descriptor, failure during close |
| Read-only source policy | No reopen for header parsing, no copied audio, same file bytes/mtime after an ordinary scan |

The mutation fixtures deliberately write to their own synthetic files from the
test hook; ordinary read-only acceptance checks compare bytes/mtime and directory
contents. Such fixture writes are not scanner writes.

## Findings and observed runs

The initial 12-case run confirmed the coordinator's suspected stale
`coverage.files_hashed` counter: mutation cleared the file hash but left the
accepted-hash count at one. After its correction, a 14-case run passed 12 and
reproduced two remaining failures:

- Same-inode content change between enumeration and first descriptor-stat lacked
  an explicit `source_changed_before_read` observation.
- `os.fdopen` failure after successful `os.open` leaked the acquired descriptor;
  an independent `fstat` still succeeded. The failing test cleaned up that FD.

Both were reported to the production owner. The final fifteenth case checks
close-error invalidation of the accepted hash count and derived duration. Raw
header/duration observations may remain, but must be explicitly unstable or
unreadable, with derived seconds withheld. They are not silently corrected or
presented as stable results.

Final post-fix execution passed **36 tests**, zero failures (0.325 s): all
15 new independent stream cases plus 21 existing scanner regression cases.
Command: `python -m unittest server.tests.test_wav_stream_acceptance server.tests.test_scanner -v`
with the existing `review/deps` and repository on `PYTHONPATH`.

The production owner fixed the accepted-hash counter, first-stat comparison and
FD wrapping cleanup, and centralized invalidation for mutation and read/close
errors. The new close-error case also passed. QA changed no production code.
No Gradle, new dependency, model/corpus download or commit was performed by N54.

## Limits

This is actual local Python scanner execution over synthetic files, not Android
SAF, remote-provider acceptance or acoustic playback. Header inspection and full
hashing have separate budgets and claims. A complete hash identifies the bytes
read; it does not prove a stable filesystem snapshot, authorship, true duration,
or playable/valid sample data. The existing hash-reader policy is separate from
the new header-read cap. Header-format coverage and independent parser fixtures
are recorded by N49/N51, not added to N54's test count.

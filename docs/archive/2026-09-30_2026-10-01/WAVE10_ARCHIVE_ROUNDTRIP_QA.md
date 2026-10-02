# N64 — independent semantic roundtrip acceptance

**10 tests and 4 subtests passed** in
`server/tests/test_archive_semantic_roundtrip.py` against the shared semantic
budget and archive-forwarding implementation. This task changed only the new
acceptance tests and this report; it did not modify production validators.

The fixture authors its tables, selectors, quotes and canonical SHA-256 digests
independently of the package exporter and citation projection helpers. It contains
three transcript versions of one artifact, five different pinned segment/word
citations, failed processing-run provenance, historical running-job metadata,
literal Unicode/whitespace, opaque malformed auxiliary JSON and an unanchored
malformed deeply nested segment string. The latter deliberately must not become a
new decoding obligation merely because semantic budgets were introduced.

## Observed behavior

| Case | Acceptance result |
|---|---|
| Exact inert import → read → export | Canonical exported bytes exactly equal the independently canonicalized input package. Original payload/table digests, export time, all raw strings and pinned citation identities survive. |
| Opaque historical fields | Invalid artifact/run/audit/error strings and duplicate keys inside opaque metadata remain literal. Unanchored malformed segment text is not decoded. |
| Historical NaN in unused word data | A valid segment citation roundtrips with the original `segments_json` unchanged. A new word selector using that NaN timestamp is rejected; no word precision is fabricated. |
| Multiple versions, same artifact | Citations preserve their pinned version. Changing a version reference and recomputing all digests does not make the old quote valid against another version. |
| Different selectors, same version | Each selector is checked independently. Substituting another anchor's selector while keeping the original quote is detected. |
| Cache lifetime after successful decoding | A later call with changed raw segments at the same version ID is rejected; a subsequent original package succeeds. No cross-call successful decode cache is reused. |
| Cache lifetime after failed decoding | A malformed pinned version fails; restoring its raw bytes at the same ID succeeds and roundtrips. Failed decode state does not poison another call. |
| Duplicate keys in executable selector JSON | Rejected even though duplicate keys in an opaque metadata string are preserved. |
| Import under reduced shared-node budget | Outer JSON alone is proven to fit. Decoded pinned data exceeds the limit, so import fails before creating a new output directory and preserves an existing inert archive byte-for-byte. |
| Read/export under reduced shared-node budget | An archive produced under the default budget remains readable under that budget, but low-limit read/export fails. The archive and any prior export remain byte-for-byte unchanged; no partial new output appears. |

The reduced-limit fixture puts 5,000 nodes in an unused field of an **anchored**
segment structure. It is intentionally decoded during citation validation, unlike
opaque auxiliary metadata or unanchored segment strings. A 1,500-node outer-only
parse succeeds, demonstrating that archive rejection exercises the forwarded
semantic budget rather than a trivial top-level JSON size/shape failure.

Source locators remain inert. The roundtrip path runs while ordinary implicit
file opens are blocked; archive publication and bounded archive reading still use
their explicit descriptor-based paths. Reports retain `jobs_resumed=false`,
`live_restore_supported=false`, `source_bytes_included=false`,
`replay_supported=false` and `signed=false`. No historical job is resumed and no
referenced audio is opened or authenticated.

## Reproduction

From the repository root with the already installed test dependencies:

```sh
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:$PYTHONPATH \
  python -m pytest server/tests/test_archive_semantic_roundtrip.py -q
```

Observed result: `10 passed, 4 subtests passed`.

N63 owns general node/depth/cache-count adversarial acceptance; N66 owns projection
work accounting. These tests exercise independent semantic preservation and
archive entry-point behavior, not an exhaustive CPU benchmark. Per-version JSON
caching alone is not described as a bound on all projection work. The evidence is
synthetic; it does not establish signature authenticity, truth, ASR alignment,
source-media integrity, live restoration or partner-system compatibility.

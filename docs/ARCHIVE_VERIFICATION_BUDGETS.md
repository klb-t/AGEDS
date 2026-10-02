# Archive semantic verification budgets (N62)

All inert archive import, read and export paths now forward `ArchiveLimits` to
the same metadata-package semantic verifier. The archive format, canonical JSON,
literal stored strings, IDs and digests are unchanged. There is no live restore,
source dereference, background task or new dependency.

## Limits and accounting

Existing `max_nodes` (2,000,000), `max_depth` (64) and `max_input_bytes` (32 MiB)
apply to verification together with the additive
`max_projection_visits` limit (2,000,000). All limits remain positive integers;
booleans are rejected. Existing constructors and CLI invocations use compatible
defaults. This increment adds no CLI flags.

Nodes are values and containers, not object keys. The package root is depth 0,
including scalar-leaf depth in the bound. The outer package and semantic decoded
projection inputs share one node/depth budget during each validation call:

- Pinned `segments_json` is decoded once per transcript version per validation
  call; selectors are accounted for each anchor.
- Their decoded roots start at depth 4, the position of the stored row field in
  the outer package, rather than resetting depth to zero.
- Lexical byte/node/depth checks precede nested JSON allocation. The strict
  decoder still rejects malformed grammar and duplicate keys.
- Projection visits conservatively account for anchors, selector entries,
  touched segments/words and candidate text characters before actual projection.
  They are bounded work units, not CPU instructions or a time guarantee.

Budget exhaustion returns `verification_limit_exceeded` from package validation;
archive validation turns that result into a `ValueError` before publication.
Intake structural exhaustion also raises a `ValueError` describing the limit.
A rejected import/export creates no output and preserves any existing target.

## SQLite reconstruction

Archive envelope and row JSON fragments share an independent intake budget,
preventing every row from resetting its outer-node allowance. The envelope is
counted at depth 0, the reconstructed tables object and arrays at depth 1/2, and
rows at depth 3. No table scaffold or row is counted twice in that intake budget.
The assembled package then receives a fresh full semantic verification budget.

Import validates the supplied package and separately verifies its reconstructed
inert archive before publication. These are independently bounded validation
phases, not one whole-operation counter. The extra roundtrip check and exact
canonical comparison remain in place. `load_package`/`strict_json` perform bounded
JSON intake; semantic validity is established by the archive/package verifier.

Outer object shape checks use iterator-based traversal, retaining state by depth
instead of allocating a pending list proportional to all siblings before the
node limit can fire. Duplicate outer-key diagnostics include only a bounded
excerpt; the literal stored payload is never shortened or rewritten.

## Historical semantics and validation

Arbitrary historical metadata/error strings remain opaque, even if they resemble
malformed or deeply nested JSON. Unreferenced transcript strings remain literal.
Selected transcript projections retain the existing treatment of unused legacy
nonfinite word fields; selector JSON must remain finite and canonical. These
limits do not correct raw ASR, repair historical metadata, or broaden quote
precision.

`test_archive_verification_budget.py` supplies seven owner tests: forwarding work
limits through import/read/export, aggregate SQLite intake, exact outer-node
boundary without duplicate charging, interpreted-depth versus opaque-string
behavior, unchanged inputs/targets on rejection, bounded duplicate-key errors,
and additive limit validation. Seven owner tests passed; an earlier combined
run of the first six plus existing archive tests passed 24 tests and 26 subtests.
Independent semantic/projection tests, actual CLI roundtrips and the final full
backend receipt are recorded separately by the coordinator.

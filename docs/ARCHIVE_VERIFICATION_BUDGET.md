# Bounded interpreted metadata verification

Metadata archives retain opaque raw strings, but verifying a saved citation must
interpret its selector and pinned transcript segments. Previously those nested
JSON strings escaped the archive's node/depth limits and the same version was
decoded again for each anchor. A 63,768-byte synthetic package could pass limits
of 500 nodes/depth 10 while its pinned segments contained 10,001 entries and an
unused field nested 80 levels. Repeated anchors also repeated full word-list
validation even if decoding were cached.

`VerificationBudget` in `server/app/verification_budget.py` now bounds these
operations. `validate_metadata_package(package, *, budget=None)` retains its
existing report shape; no supplied budget creates a fresh default budget. An
exceeded limit emits `verification_limit_exceeded`, makes the report invalid and
returns immediately. Exhaustion is sticky and cannot be swallowed as an ordinary
bad-selector error followed by further expensive anchors.

## Resource contract

Defaults are 2,000,000 semantic nodes, depth 64, 2,000,000 projection visits and
32 MiB for an individual interpreted JSON string. Archive integration passes its
existing input-byte/node/depth limits and `max_projection_visits`. All configured
ceilings are positive integers; booleans and coerced numeric strings reject.
They are processing limits, not claims of transcript completeness or confidence.

Node accounting preserves the existing `ArchiveLimits` convention: each value
and container counts once; object **keys do not count as nodes**. The package root
has depth zero, including leaf depth. The package's outer structure is charged
once, followed by each interpreted selector and each unique pinned transcript's
segments. Decoded JSON starts at depth **4**, corresponding to
`package(0) → tables(1) → table array(2) → row(3) → JSON field(4)`. This semantic
expansion is additional node work; the raw outer string was already counted as
one value. It does not change the bytes retained for that field.

`preflight_json(raw, start_depth=...)` scans the bounded JSON text before
`json.loads`: UTF-8 byte count, token nodes and depth are charged before the
recursive parser allocates the expanded structure. This lexical resource check
is followed by strict JSON parsing, including duplicate-key rejection; it does
not replace grammar validation. `check_structure` uses an iterator stack sized
by depth rather than materializing all sibling nodes at once.

Archive intake may use a separate fresh safety budget while assembling envelope
and row fragments, preventing many individually small rows from accumulating
past an aggregate bound. Full semantic validation then uses its own fresh budget
once. These are sequential safety passes, not double charges against the same
validation allowance. Every new validation/import/export invocation has local
budget state; no process-global transcript cache is introduced.

## Local cache and raw preservation

Within one package validation call, pinned transcript decode results are cached
by concrete version ID, including failed decodes. Each anchor still has its own
selector decoding, projection, quote and provenance checks. A later call or
another version cannot reuse this cache. Duplicate-key diagnostics use a static
short message so a huge untrusted key is not replicated into many anchor errors.
Raw source strings remain untouched.

Only fields actually needed by anchored verification are interpreted. Arbitrary
`metadata_json`, provenance/parameter/error strings and **unanchored** transcript
segments remain opaque—even when malformed or deeply nested as text. Historical
nonfinite values in unused word fields remain preserved in raw `segments_json`;
segment selection does not validate or charge word projection it never uses.
Selectors require finite canonical JSON, and selected text/times retain existing
strict citation semantics. This limit work does not invent alignment or reject
all legacy nonfinite strings indiscriminately.

## Repeated projection work

Caching JSON decoding alone does not prevent repeated scans of a long word list
for many anchors. `_charge_projection_work` conservatively charges every anchor
**before** calling the existing real citation projection functions. No changes
to `citations.py` are needed. Work units are:

- One per attempted anchor and one per selector-list entry.
- Segment selector: one per valid selected segment index, plus that segment's
  text character count.
- Word selector: once per referenced segment in that anchor, one segment visit,
  its text length, its word count and the sum of stored word-text lengths.
- Each valid selected word reference additionally charges its selected word-text
  length.

For example, four one-character words in a four-character segment, selecting one
word, cost `1 + 1 + 1 + 4 + 4 + 4 + 1 = 16` visits per anchor. Three anchors cost
48 even though the transcript is decoded once. Invalid selectors may be charged
conservatively before their normal error is discovered; the contract does not
promise free validation of malformed inputs. Word-list length is charged before
the preflight iterates its text lengths. Segment selectors ignore unused word
lists for projection accounting; decoded semantic nodes still count.

`nodes` and `projection_visits` expose attempted accounting, including the
refused increment that exceeded a ceiling. They are not completed-work timings,
heap measurements or exact CPU instruction counts. The model bounds repeated
structural/text traversal under the named limits; it is not a deadline guarantee.

## Acceptance and unchanged scope

`test_verification_budget.py` covers lexical/tree accounting, rejection before
JSON decoding and real projection, attached field depth, opaque/unanchored raw
preservation, unused NaN word fields, sticky failure, constructor types and
bounded repeated duplicate-key diagnostics. Independent suites separately count
actual decode/projection calls, check per-call resets and exact boundaries, and
perform canonical archive roundtrips and real CLI acceptance.

The package remains unsigned metadata. No source locator is opened, no live DB
is restored, and no jobs run. Canonical serialization, original export time,
IDs, raw error strings and digest meanings remain unchanged for accepted input.
Exceeding a processing limit fails explicitly rather than skipping anchors,
repairing raw evidence or producing a partial verified archive.

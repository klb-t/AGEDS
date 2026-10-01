# Wave 13 native selector wire review

## Scope

This is an independent review of the response check between the Python citation
producer and the Kotlin client. It covers the canonical selector emitted by
`server/app/citations.py`, its `JsonObject` representation in `Citation`, and the
frozen selector constructed by `CitationSelection`. It does not claim a device,
HTTP integration or acoustic-alignment test.

## Wire representation

`EvidenceApi` decodes `Citation.selector` directly as a kotlinx.serialization
`JsonObject`. Object member order is therefore not semantic. Array order remains
semantic for `indices` and `word_refs`. The native expected selector has exactly
the keys emitted by the server:

- segment: `kind`, ordered `indices`, `text_join`, `time_unit`,
  `stored_time_unit`, `rounding`, and `precision`;
- word: the same fixed metadata with ordered `word_refs`, plus `source_start`,
  `source_end`, and `alignment_verification`.

The matcher requires equal object key sets at every object level. Missing and
additional keys are refused. Strings are compared as strings and booleans as
booleans; neither is coerced to a number. Arrays require equal length and order.
`null` only equals `null`.

## Numeric semantics zależne od roli pola

Numeric token spelling cannot be required to match. Stored Python transcript
times may be integral JSON numbers such as `0`, while Kotlin deserializes the
same field into `Double` and constructs `JsonPrimitive(0.0)`. Those values
describe the same JSON number and must compare equal. Equivalent decimal forms
such as `1.25`, `1.2500`, `125e-2`, and negative zero are accepted.

The matcher is deliberately path-aware. Integer identity fields (`indices`,
`segment_index`, and `word_index`) are canonicalized as exact decimal tokens,
without parsing through binary `Double`. Consequently adjacent large integers,
including values above JavaScript's safe integer range, do not collapse to one
value. By contrast, `source_start` and `source_end` have already been decoded
into the client's `Double` transcript model. They are compared as equal finite
IEEE-754 values. This accepts Python's `5e-324` and the JVM's `4.9E-324` as two
shortest spellings of the same smallest positive `Double`. A string `"0"` and
boolean `false` remain distinct from numeric zero. Programmatically constructed
non-finite `JsonPrimitive` values are rejected.

Exact-decimal exponent processing is bounded to a signed `Long`. A syntactically
valid integer token with a larger exponent is conservatively unequal. Source
bounds instead use the finite `Double` branch and do not rely on this decimal
canonicalization.

## Exactness and remaining boundary

The frozen selector, positive returned ID, artifact ID, transcript version,
exact quote and rounded millisecond range are all checked before a response can
be accepted. The selector's `precision` member covers the separate preview
precision. The server-owned quote hash is not recomputed by this client check;
the server already builds it from the stored projection, and this change is a
response-consistency guard rather than a new authentication mechanism.

The server can represent integral seconds above `2^53`, while the Android model
cannot retain every such integer in a `Double`. This remains fail-closed: the
server's millisecond range then differs from the locally frozen range and the
outer `startMs`/`endMs` check rejects the response. It is not full numeric-range
parity; achieving that would require retaining the raw decimal token or
narrowing the server contract.

The tests in `CitationSelectorWireCompatibilityTest` independently cover
decimal/exponent/trailing-zero/negative-zero equivalence for raw time,
Python/JVM subnormal spelling equivalence, exact preservation of large integer
indices, rejection of non-finite primitives, exact key sets, and rejection of
string/boolean numeric lookalikes. The integrated build receipt is owned by the
coordinator; this review alone does not claim those tests ran.

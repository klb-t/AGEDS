# N28 — browser identity precision boundary

Scope: `server/app/static/citations.mjs` and nine Node boundary tests in
`server/tests/js/id-boundaries.test.mjs`.

SQLite permits positive identifiers through 9223372036854775807; JavaScript
Number represents every integer only through 9007199254740991. This browser
workspace now explicitly refuses identifiers outside its exact supported range.
The server's legacy numeric JSON contract is unchanged. This is a client
capability limit, not a migration or a claim that larger database IDs are invalid.

## Implemented checks

- IDs from HTML datasets and version options must be canonical positive decimal
  strings. The exact integer is checked before conversion to Number. Whitespace,
  exponent notation, leading zeroes, signs, malformed values and unsafe integers
  are refused without rounding.
- IDs returned in JSON must be positive safe integer **numbers**. Strings,
  booleans, null, fractional numbers and unsafe decoded integers are refused.
- A transcript response must match both the requested artifact and the captured
  version. A citation save response must contain a safe citation ID and match both
  the captured artifact and pinned version before it can produce playback or
  packet-export controls.
- Fetch, save and preview playback check the current identity before acting.
  Existing saved citation playback checks artifact, citation and transcript IDs.
  Packet links must have an exact canonical local route matching the checked
  artifact and citation. Unsupported saved controls display an explicit Polish
  refusal; unsupported packet links lose their navigation target.
- A save response arriving after a version switch may append its correctly
  pinned older citation within the same artifact, after identity and exact quote
  validation. It does not replace the newer version's status. This preserves the
  previously accepted behavior; a version switch does not change a saved citation.
  A changed artifact identity prevents insertion.

No server/template/schema migration, raw material mutation, or change to native
server-rendered annotation forms is included. Original media URLs are emitted as
exact server-rendered strings; the guarded behavior here is citation-workspace
selection, range playback and packet export. API response values outside the
safe Number range are refused even if a JSON parser has already rounded them.

## Validation observed

Command:

```sh
node --test server/tests/js/id-boundaries.test.mjs server/tests/js/range-player.test.mjs
```

Result: **23 tests passed, 0 failed** (9 new ID-boundary tests and 14 existing
range-player tests). `node --check server/app/static/citations.mjs` passed.

Fixtures cover safe endpoints, 2^53 and 2^53+1, SQLite's signed-64 maximum,
unsafe raw JSON numbers, string/boolean/null coercions, malformed and
noncanonical decimal strings, mismatched transcript/citation ownership, and
invalid or mismatched packet routes. Tests execute the exported guards used by
the UI; they do not substitute for actual DOM interaction.

Actual Chromium acceptance is owned by the coordinator/independent browser QA
and is not claimed in this report. No phone, acoustic alignment or private corpus
acceptance was performed.

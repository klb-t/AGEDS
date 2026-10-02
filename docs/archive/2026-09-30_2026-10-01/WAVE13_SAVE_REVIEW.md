# Wave 13 — independent browser save review

Reviewed the working browser change against `ab89133`, after reading `AGENTS.md`,
`HANDOFF.md`, the current coordination records and architecture/ecosystem rules.
Reviewer: `save_review`; no implementation changes made.

**Result: no concrete correctness defect found within the browser save scope.**

- The expected artifact, transcript, exact text, ordered occurrence references,
  canonical selector metadata and rounded range are copied and recursively frozen
  before the asynchronous POST. Mutable controls and input units do not alias it.
- Response validation precedes success status, history admission and construction
  of playback/export controls. Failed admission does not reserve the response ID.
- The millisecond conversion matches Python's binary multiplication followed by
  ties-to-even rounding within JavaScript's explicitly supported safe integers.
- The selector comparison preserves array ordering and scalar types, ignores
  object-key order and accepts equivalent JSON numeric spellings.
- Existing history reads retain their prior, less restrictive contract. The
  exact canonical selector is required only for a newly created response.
- A late valid response may enter the same artifact's history with its original
  transcript ID. It does not replace the status or selection of a newer version.
  A late invalid response cannot enter history. Artifact identity is checked again
  before publication to the view.

Independently ran the four new Node test files:

```sh
node --test server/tests/js/citation-save-snapshot.test.mjs \
  server/tests/js/citation-save-contract.test.mjs \
  server/tests/js/citation-python-parity.test.mjs \
  server/tests/js/citation-save-handler.test.mjs
```

Observed **36 passing tests, 0 failures, 0 skipped**. This includes an actual
Python subprocess comparison at 3,008 seeded numerical boundaries and two
canonical projections. The handler tests execute the production module with a
minimal synthetic DOM, synthetic audio and controlled fetch responses. They are
not Chromium, phone, acoustic alignment or actual backend failure tests.

Reviewed production SHA-256:
`190d34fd5979e2f7273edd2162c92a490892e3af197e7b10aede15e4173a6885`
(`server/app/static/citations.mjs`).

Limits: this review does not approve native changes, prove source authenticity,
verify returned quote hashes, or assert that the backend ever produced the
simulated mismatched responses. Actual browser runtime remains a separate gate.

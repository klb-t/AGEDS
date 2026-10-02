# Wave 13 — independent browser create-response contract acceptance

Task: N82. Synthetic data only; Node v24.19.0. Production owner and independent
acceptance author were separate agents. This receipt concerns pure browser
contract functions; DOM/race integration and actual Chromium have separate
receipts.

## Reproduced baseline

Before the new helpers were installed, an actual Node invocation called
`validateCitationIdentity` with artifact `12`, version `17`, exact quote `yes`,
and response time `0–500 ms`. The selected occurrence was word `(0,0)`; the
response contained `(1,0)` at the same interval. The call accepted the response.
The create handler used this validator. This demonstrates insufficient client
response consistency checking, **not** a backend-generated corrupt citation.

The legacy identity validator remains intentionally available for history rows.
New create acceptance uses the independently captured projection.

## Executed acceptance

Command:

```sh
node --test server/tests/js/citation-save-contract.test.mjs
```

Result: **23 tests passed, 0 failed**, no skipped or cancelled tests.

The fixture's expected selectors are written independently, not copied from the
production helper. Word and segment positive controls preserve exact whitespace
and newlines. Negative cases cover:

- Another occurrence with identical text, version and rounded interval; reversed,
  duplicate, shortened or extended selections.
- Every canonical selector key omitted or set to null; incorrect kind, join,
  units, rounding, precision, raw word bounds or alignment status; extra keys in
  selector and individual word reference.
- Boolean/string/coerced or unsafe IDs, references, time values; mismatched
  artifact/version; altered raw text or rounded interval.
- Input mutation after capture and recursive freezing of the detached snapshot
  and request. Modifying the original picked units cannot alter expectations.
- Object key reordering and JSON numeric `0`/`0.0` equivalence, while quoted zero
  is refused.
- Ties-to-even at `0.0005`, `0.0015`, `0.0025`, `1.0005`, `1.0015` seconds,
  adjacent non-ties, unsafe millisecond ranges, and raw bounds retained separately
  from their rounded projection.
- Historical rows with an opaque legacy selector still pass history validation,
  while the same row cannot pass the stronger create-response check.

An additional integration invocation of `node --test server/tests/js/*.test.mjs`
at this checkpoint passed **79 tests**, including the existing history/range/ID
suite, the owner's four tests, these 23 tests and root's three Python parity
checks. Other agents may subsequently add tests; this is an observed count, not
the final wave total.

Inputs at the 23-test acceptance:

| File | SHA-256 |
|---|---|
| `server/app/static/citations.mjs` | `190d34fd5979e2f7273edd2162c92a490892e3af197e7b10aede15e4173a6885` |
| `server/tests/js/citation-save-contract.test.mjs` | `51a51376963759c7c33737ef73e031dc26d24368b9df69ca538ed883fed42bb5` |

No network service, GitHub Actions, paid model or private corpus was used. This
receipt does not establish backend honesty, audio alignment, ASR accuracy,
phone behavior or partner integration. It verifies that a returned selector
agrees with the captured client selection under the tested JSON contract.

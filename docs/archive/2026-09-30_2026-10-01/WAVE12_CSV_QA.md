# Wave 12 — independent CSV raw-record fidelity

N75 adds seven tests in `server/tests/test_csv_raw_record_fidelity.py`, exercising 57 synthetic scans through the production `scan_sources` entrypoint. Production scanner changes belong to N73; this acceptance work changes only its new test and this receipt.

## Results

```sh
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:$PWD python -m unittest server.tests.test_csv_raw_record_fidelity server.tests.test_scanner -q
```

Observed: **28 tests passed**, zero failures/errors, in 0.647 seconds: seven new independent tests plus 21 existing scanner tests. No source bytes or modification times changed, and scans created no files in the source directories.

The main cross-product covers U+2028, U+2029, NEL, VT, FF, and U+001C/U+001D/U+001E as quoted and unquoted field content under each physical CR, LF, and CRLF ending (48 cases). Every case asserts exact `raw_record`, physical `start_line`, cell content, and complete table/scan coverage. Final records deliberately omit a line terminator.

Additional cases cover UTF-8 BOM and UTF-16 BOM, a bare-CR `sep=;` preamble, quoted records containing mixed physical endings and nonphysical separators, TSV content, and unclosed trailing quotes after a valid multiline record. Malformed fragments retain the exact decoded source suffix and its physical starting line; failed parsing remains explicitly incomplete. Row-budget and character-budget omissions preserve exact admitted prefixes and mark incomplete raw records/fragments rather than asserting full fidelity.

No production defect remains demonstrated by these cases. The first test run exposed a test assumption about the raw-record allowance: that allowance is the smaller of remaining aggregate characters and per-cell limit times column limit, not the per-cell limit alone. The test now explicitly constrains the aggregate budget to exercise real raw-record truncation.

## Scope

These are bounded temporary-file Python scanner tests with the actual CSV parser. Assertions compare decoded Unicode raw-record strings and unchanged original bytes separately. They do not equate a decoded string with byte identity, validate inferred column semantics, or claim Android/device execution. No dependencies, production edits, commits, Gradle builds, or Actions were used by N75.

# Wave 4 independent native XLS CONTINUE QA — N26

Task `AGEDS-20261001-N26`, claim
`d7f03661ca7177ac2bfef549b71b32fdae77d2d0`.
QA owns only `SourceXlsContinuationAdversarialTest.kt` and this report. The N25
owner implements the parser; QA did not alter production or earlier tests.

## Fixture contract and coverage

The 17 independent JUnit cases generate SST and CONTINUE payloads in memory.
They reuse the pre-existing generic `XlsFixture` CFB envelope, not the new
owner's SST fixture builder. Every fixture compares input bytes before/after
parsing and requires the `xls_projection` partial-coverage diagnostic.

| Area | Acceptance |
|---|---|
| Character continuation | Compressed→wide→compressed, wide→compressed high Latin bytes, exact whitespace and Unicode |
| Legal boundaries | Full header at a new record, character data starting in a continuation, empty string followed by a new header |
| UTF-16 | A surrogate pair may span complete code units; a single code unit split is rejected without repair |
| Metadata | Rich-text tail wholly inside the final character record is skipped without consuming the following string; tail crossing records is explicitly omitted |
| Malformed input | Truncated final characters, missing/invalid continuation flags, split fixed headers, malformed later strings |
| Budgets | Declared cell-character length, declared shared-string count, cumulative decoded-character budget |
| Provenance | Raw LABELSST payload remains index bytes with original cell/record locator; malformed SST produces unresolved raw index rather than fabricated decoded text |

The fixed-field and character-boundary choices were independently checked
against Microsoft's [XLUnicodeRichExtendedString specification](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xls/173d9f51-e5d3-43da-8de2-be7f22e119b9)
and [Continue specification](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xls/999fae21-d3d9-42e8-8290-639782460c67).
The fixed string header stays in one record; wide character data is split only
between complete code units. Rich/phonetic tails spanning records remain an
explicit implementation omission, not a claim that those inputs are invalid.

## Execution

Cached Kotlin 2.4.20/JUnit 4.13.2 direct execution passed all **17 independent
N26 cases** (0.08 s). The final combined parser regression run then passed
**55 tests, zero failures** (0.223 s): N26 independent 17 + N25 owner 12 +
legacy parser 14 + prior independent XLS adversarial 12. These overlapping runs
are not additive. The real parser and source models were compiled, with the
existing cached toolchain and no Android runtime stubs.

No Gradle was launched by QA. Android sources/tests are frozen for the
coordinator's serial integrated build; that later build is separate evidence.

No production defect was found in the initial independent run. Review confirmed
that the new parser stages the entire SST before publishing entries, and uses
one strict UTF-16 decode after collecting code units across continuation records.
The obsolete previous test expecting blanket CONTINUE omission was reported to
the production owner/root and updated by that owner under root authorization.

## Limits

These are synthetic byte fixtures and JVM parser tests. They do not prove full
BIFF8 interoperability, fidelity for all historical workbooks, Android/SAF runtime
behavior, style/date/formula evaluation, or complete workbook reconstruction.
Rich/phonetic metadata crossing records stays unsupported. No source corpus,
network spreadsheet download, paid service or device/emulator was used.

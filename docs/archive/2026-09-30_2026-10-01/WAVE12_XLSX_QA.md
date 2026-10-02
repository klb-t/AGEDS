# Wave 12 independent XLSX phonetic projection QA

Scope: `SourceWorkbookPhoneticAdversarialTest.kt` exercises the actual native workbook parser using synthetic ZIP packages generated in memory. It does not mock XML parsing, use a private workbook, or claim Android SAF/device execution.

The independent cases cover shared-string reuse with unchanged raw indices; Unicode and whitespace across rich runs; inline raw/value projection; phonetic-only strings with an empty base; isolation from subsequent strings and cached formula values; ordinary strings without a new loss issue; combined base plus omitted-phonetic character limits, including multiple runs; and concrete invalid nested/misplaced `rPh` scopes. One fixture also checks that the input ZIP bytes remain unchanged.

Pronunciation hints are omitted from projected cell text and reported through `xlsx_phonetic_omitted`. This suite checks that diagnostic; engine/cache partial-coverage propagation is checked separately by the Wave 12 engine/cache suite. It does not require retention of omitted hints, support for phonetic properties, or complete OOXML schema validation.

The format distinction is documented by Microsoft's [PhoneticRun reference](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.phoneticrun?view=openxml-3.0.1): `rPh` is a phonetic run associated with base string text, rather than an additional base-text run. Fixtures place valid hints under shared `si` or inline `is` containers.

Validation uses the existing cached Kotlin 2.4.20 compiler, JDK 21, and JUnit 4.13.2, with no Gradle invocation or dependency download. Initial execution passed all eleven phonetic behavior cases; the ordinary-string control exposed an independently authored fixture missing a cell reference. That fixture was corrected to an explicit `B1` reference, without changing production code or weakening its expected diagnostic list. Final direct-run result is recorded below.

Final direct execution: **12 tests passed, zero failures**, JUnit-reported time 0.158 seconds. Test source SHA-256: `be244043808dfeedf58d4c409d2321228842db52ff30275c2a68dde7295a082d`. No production defect was found by this suite. Full Android/desktop integration remains the root agent's serial-build responsibility.

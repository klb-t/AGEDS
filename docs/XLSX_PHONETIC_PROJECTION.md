# XLSX phonetic hints and base text

The native XLSX parser previously appended every `<t>` beneath a shared-string
`<si>` or inline-string `<is>`. This merged phonetic `rPh` annotations into the
actual cell text. Before the fix, a direct JVM test using the production parser
failed with expected `[東京, 東京]` and actual
`[東京とうきょう, 東京とうきょう]` for a shared and an inline cell.

Microsoft's primary Open XML documentation describes `rPh` as pronunciation
hints associated with base text and identifies the relevant `si`/`is` parents:

- [PhoneticRun documentation](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.phoneticrun?view=openxml-3.0.1)
- [Spreadsheet rPh element](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.linq.x.rph?view=openxml-3.0.1)

Those sources establish the distinction between annotations and base text. The
executed synthetic test, rather than the documentation alone, establishes the
previous implementation error.

`SourceWorkbookParser` now tracks phonetic context separately for shared and
inline strings. Base `<t>` and ordinary rich-text-run text retain exact decoded
characters and whitespace. Phonetic text does not enter cell values or inline
stored-text projections. Shared-string raw indices, formula text and cached
formula values remain unchanged. A phonetic-only string projects empty base text;
it does not substitute its pronunciation hint for missing base text.

Observed phonetic runs emit `xlsx_phonetic_omitted` with their shared-string or
cell locator. The existing diagnostic cap remains 100 issues plus one omission
sentinel. Existing engine coverage rules therefore report partial coverage even
when base cell text was extracted correctly. No new model field stores phonetic
payload, offsets or formatting: that information is omitted explicitly, not
silently represented as complete raw XML. Original input bytes remain unchanged.

Base and omitted phonetic characters **together** consume the existing
`maxCellChars` budget per shared string or inline cell, including multiple runs.
The parser counts omitted characters without retaining their text. Reaching a
bound still yields `cell_chars_limit`. A phonetic run must be a direct child of
the relevant shared/inline string; nested or misplaced runs and formula/value
fields masquerading as phonetic text fail with `invalid_workbook`. This is a
scoped context guard, not complete OOXML schema validation or validation of
phonetic `sb`/`eb` attributes.

`SourceWorkbookPhoneticTest` covers the executed regression, rich-text whitespace,
formula caches, phonetic-only text, exact combined character bounds, malformed
contexts, state reset and bounded diagnostics with unchanged input bytes. Legacy
workbook/encoding tests, independent phonetic fixtures and production engine/cache
tests are separate regression evidence. Host JVM acceptance does not claim SAF
provider or physical-device execution.

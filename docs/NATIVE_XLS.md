# Native Android XLS projection (N9)

`SourceXlsParser.parse(bytes, locator, limits, checkCancelled)` returns the existing
`ParsedSourceRows` contract without writing to the source or evaluating formulas.
This is a small dependency-free Android/JVM adapter, **not full XLS support**.
`xls_projection` is always present so the scanner reports partial coverage.

## Supported subset

- CFB version 3, 512-byte sectors, header-resident DIFAT (up to 109 FAT sectors),
  root-level `Workbook`/`Book` stream, normal FAT and 64-byte MiniFAT streams.
- BIFF8 workbook globals and BoundSheet8 worksheet references; sheet ordinal,
  raw decoded sheet name, row index and BIFF stream offset in row locators.
- NUMBER, RK, MULRK, LABELSST with a noncontinued shared-string table, BOOLERR,
  BLANK, and FORMULA numeric/Boolean/error/empty cached results.
- BIFF8 compressed Unicode and UTF-16LE strings, retaining whitespace and
  original decoded text. Rich-text run/phonetic metadata is not projected.
- Formula tokens are retained as **opaque hexadecimal bytes**, never executed
  or converted into an Excel formula expression. Cached values may be stale.

`SourceCell.raw` is lowercase hex of the **entire original BIFF record payload**,
including row/column/style fields; it is not displayed-number text. `value` is
an explicit decoded scalar projection, with `error:<code>` for error values.
`sourceType` identifies the BIFF record. `sourceReference` holds the A1 address
and logical Workbook-stream record-header offset. This offset is not a physical
CFB-file offset. MULRK cells share the record payload but have distinct addresses.
Numeric strings use JVM double conversion, without date/style/locale interpretation.
Exact binary number bits remain available in raw. Duplicate cell records remain
separate observations, in stream order; no resolution or repair is attempted.

## Explicit limits and exclusions

CFB v4, extended DIFAT, encrypted workbooks and non-BIFF8 workbooks are unsupported.
Malformed chains, overlap of consumed sectors, invalid directory references,
truncated records, invalid sheet offsets and malformed supported scalar records
report `invalid_xls`. This is not a full CFB conformance validator: unrelated streams,
nested storage, directory sorting/color constraints and unconsumed allocation are
not validated. Chart/macro/non-worksheet streams are omitted with an issue.

Continued SST data is not decoded: all related LABELSST cells retain their index
as value plus an unresolved-string diagnostic. Formula string caches are omitted
with an issue; the formula's raw payload and tokens remain. LABEL, MULBLANK and
RSTRING cell records are explicitly reported as omitted. Other BIFF records,
styles, comments, dates, links, drawing objects, macros and formatting are outside
the projection. Source bytes remain the basis for any later fuller interpretation.

Budgets cover input bytes, cumulative consumed CFB bytes, directory entries,
worksheet count, shared-string count, rows, cells, per-cell characters (including
hex expansion), cumulative projected raw/value/formula characters and 100,000
BIFF records. Cumulative projected characters use `maxExpandedBytes` as an
additional budget. Diagnostics are capped at 100 plus one omission marker.
MULRK hex is memoized; the character budget still counts each output reference so
serialized output cannot amplify without bound. Chain visits and record iteration
check cancellation. Zero padding is checked once with periodic cancellation.
Budget/error results retain only previously admitted cells and explicit issues.

## Validation

`SourceXlsParserTest` generates normal FAT and MiniFAT containers in memory:
scalars, Unicode, raw bytes/locators, formulas, unsupported continuations/encryption,
malformed/cyclic/out-of-range chains, invalid BIFF framing, limits, cancellation and
unchanged input. `SourceAdversarialXlsTest` (N15 owner) adds independent boundary
regressions. No private corpus or binary fixtures are checked in. The current parser and source models were compiled with the cached Kotlin 2.4.20
compiler on JDK 21, then all **34 JUnit tests passed** (14 N9 and 20 N15; 0.367 s).
This standalone run avoided a shared Gradle cache lock. It does not validate
Android packaging or generated serialization code; the coordinator records the
separate integrated Android build receipt.
An independent `xlwt==1.3.0` writer smoke also passed against the current compiled
parser: five cells (`" 001 +48 ć "`, `-1.23`, `true`, an unevaluated formula's empty
cache, `42.125`), with only `xls_projection`. The optional generator below reproduces
that synthetic workbook in scratch after installing xlwt in an isolated test environment:

```python
import xlwt
w = xlwt.Workbook()
s = w.add_sheet("Zażółć")
s.write(0, 0, " 001 +48 ć ")
s.write(0, 1, -1.23)
s.write(1, 0, True)
s.write(1, 1, xlwt.Formula("1+2"))
s.write(2, 0, 42.125)
w.save("/tmp/ageds-xlwt-smoke.xls")
```

Synthetic acceptance does not establish compatibility with every historical XLS
writer or SAF behavior on a physical Android device.

The adapter is an intentionally narrow reversible default. Extend it only with
format-specific generated fixtures and boundedness tests when an actual unsupported
source pattern is needed. A maintained larger dependency remains an alternative;
no POI or spreadsheet execution engine was added in this increment.

## Format references

Microsoft's specifications checked during implementation:

- [MS-CFB header and sector fields](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-cfb/05060311-bfce-4b12-874d-71fd4ce63aea)
- [MS-CFB MiniFAT](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-cfb/c5d235f7-b73c-4ec5-bf8d-5c08306cd023)
- [MS-XLS RK encoding](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xls/04fa5340-122f-49db-93ea-00cc75501efc)
- [Microsoft Excel 97–2007 Binary File Format specification](https://download.microsoft.com/download/5/0/1/501ED102-E53F-4CE0-AA6B-B0F93629DDC6/Office/Excel97-2007BinaryFileFormat%28xls%29Specification.pdf)

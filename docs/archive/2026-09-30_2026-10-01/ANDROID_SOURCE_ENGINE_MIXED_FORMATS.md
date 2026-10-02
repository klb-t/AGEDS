# N58 — mixed-format production scanner engine acceptance

`SourceScanEngineMixedFormatsTest` adds five host-JVM pipeline tests. They call
`SourceScanEngine.scan` with a synthetic read-only document provider and the actual
production CSV/TSV, XLSX, XLS and WAV parsing paths. No parser, hash function or
scan result is mocked. All fixture bytes are small and generated in memory.

## Covered behavior

| Pipeline case | Evidence asserted |
|---|---|
| Five formats through one engine run | UTF-16LE CSV BOM and quoted raw `"Zażółć"`, decoded text and `007`; UTF-16BE TSV BOM, explicit tab basis, `+001` and preserved spaces/comma; XLSX sparse column, raw cached `999` and unevaluated `1+1`; XLS exact raw BIFF payload, cached `999.0` and opaque token `1e0700`; WAV declared duration with unvalidated audio body. |
| Same names under two directories | Both exact synthetic URIs and relative paths survive, with distinct rows/hashes and an explicit collision containing both locators. No filename-based merge occurs. |
| Unsupported material | UTF-16 without BOM, unsupported CFB version, and unsupported WAV format remain explicit issues/partial coverage. No table rows or audio duration are invented; complete-byte hashes remain independent of parser support. |
| Per-file row limits | CSV, XLSX and XLS each retain one permitted row, preserve full-byte hashes, and report truncation/partial coverage. |
| Byte and hash scope | Unknown-size truncated CSV yields no rows/full hash; bounded WAV prefix keeps declared-header provenance without a full hash; known oversized table is not opened. Actual provider byte returns equal the engine's reported read count. |

The mixed fixture intentionally disagrees with formula results: XLSX stores `999`
next to `1+1`, and XLS stores `999.0` next to an opaque integer-7 token. Keeping those
cached values proves that the pipeline does not silently evaluate or repair them.
The XLS fixture reuses the existing `XlsFixture` byte writer without API changes;
the small XLSX ZIP writer is attributed to `SourceWorkbookParserTest`.

Source byte arrays are checked unchanged. Opened streams and directory cursors
are observed closing. Row locators retain the provider URI. Overall and per-file
coverage remain partial where the format projection or byte/row budgets are
partial; a complete-file hash does not establish full semantic interpretation.

## Runtime boundary

These tests exercise the production orchestration and parsers on the host JVM.
The provider supplies in-memory cursors/streams and synthetic `content://` locators;
Android `ContentResolver`, actual SAF permissions, provider processes, pickers and
phone interaction are **not executed**. Existing SAF instrumentation remains a
separate device acceptance step. There are no dependency downloads, ASR calls,
private files, production source writes or parser/engine edits in this task.

Independent QA executed the final sources, including the exact XLS raw-payload
assertion: **25 tests passed, 0 failed** in 0.435 seconds, comprising these five
mixed-format tests and 20 lifecycle tests. Actual engine/parser sources were
compiled. `git diff --check` passed. The final integrated Android build remains
a separate coordinator gate. See `WAVE9_LIFECYCLE_QA.md` for the aggregate run.

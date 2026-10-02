# Wave 12: XLSX phonetic omission through engine and cache

`SourceScanXlsxPhoneticCacheTest` contains three host-JVM acceptance tests using the production `SourceScanEngine`, `SourceWorkbookParser`, source models, Kotlin serialization, and `BoundedMetadataCache`. Each test builds a compact synthetic ZIP/XLSX, reads the original through a read-only provider, writes metadata to a separate directory through the real bounded cache, then decodes a typed `SourceScanResult` and checks exact equality with the scanned result.

The tests cover:

- Inline base and rich text retain their exact spaces in both raw and value fields. `rPh` text is excluded, `xlsx_phonetic_omitted` survives cache serialization, and file plus overall coverage remain partial.
- Shared rich text retains base text while the cell's raw shared-string index remains `0`. An intentionally inconsistent cached formula value (`1+1` with stored value `99`) remains unchanged: the scanner does not evaluate it. Phonetic omission remains explicit after typed readback.
- Ordinary rich text without phonetic annotations retains its text and has only the existing `xlsx_projection` issue; phonetic loss is not invented.

Every case checks the original source URI, full source SHA-256, total source bytes read, original bytes, original modification time, and unchanged source-directory membership. Cache files live outside the source directory; only the final metadata JSON remains in the cache directory. The distinctive phonetic payload is absent from cached metadata in both omission cases. The small ZIP builder follows the pattern in `SourceWorkbookParserTest`; no office package, formula engine, download, or private corpus is involved.

Execution: all three `SourceScanXlsxPhoneticCacheTest` tests passed in the first integrated Wave 12 build. That build passed 379 JVM tests (268 Android-module tests and 111 desktop tests), completed 57 tasks, recorded 91 input hashes, and reported signature verification exit code 0. The build evidence is recorded in [`ANDROID_WAVE12_BUILD_RECEIPT.json`](ANDROID_WAVE12_BUILD_RECEIPT.json). These tests exercise real parser/engine/cache code through a synthetic provider; they do not demonstrate Android SAF, ViewModel lifecycle, or device UI behavior.

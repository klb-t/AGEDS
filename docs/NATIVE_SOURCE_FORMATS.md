# Native source formats — wave 2

The Android Sources workspace scans the selected SAF tree without a seed. It
reads source bytes and stores bounded metadata separately. It does not repair,
rename, move, copy or delete the scanned corpus. Sending selected audio to a
server remains a separate explicit acquisition operation.

## Text projection

| Input | Interpretation | Uncertainty / exclusion |
|---|---|---|
| UTF-8 BOM | Strict UTF-8, consume one transport BOM | A second U+FEFF remains source text |
| UTF-16LE/BE BOM | Strict declared byte order | UTF-16 without BOM is not guessed |
| No BOM | Strict UTF-8 default | Valid decoding is not proof of author-intended encoding |
| CSV | Sample comma, semicolon, tab and vertical bar using quote-aware parsing | A unique uniform candidate is a hypothesis, not source-supplied dialect metadata |
| Ambiguous/ragged/single-column CSV | Provisional comma projection with explicit issue and partial coverage | Candidates and choice remain visible; no silent certainty |
| TSV | Tab chosen from file extension | No CSV dialect inference |

Malformed byte sequences, UTF-32, unsupported legacy code pages and NUL text
produce explicit decoding failure. Text is not replaced with lossy characters.
Raw tokens preserve quotes, leading zeros, whitespace and embedded newlines;
values do not automatically become dates, phone numbers or numeric types.

Dialect inference examines at most 65,536 characters and 32 records, additionally
subject to row/cell budgets. `textFormat` records encoding, BOM size, selection
basis, delimiter candidates, ambiguity and whether sampling was truncated.
The source detail dialog explains these choices. Earlier cached scans without
this optional field still decode; their current bytes/access are not revalidated
merely by displaying the cache.

## Workbook projection

XLSX accepts strict UTF-8 and BOM UTF-16 XML with encoding-declaration agreement.
DTD/entity guards apply after decoding, before SAX parsing. Nested row/cell
structures are rejected and row bounds checked again at emission. ZIP entry,
expansion, cell, row, character and diagnostic budgets remain enforced.
Styles, date interpretation and formula evaluation are outside the projection.

Legacy XLS has a dependency-free bounded CFB v3 / BIFF8 subset described in
[NATIVE_XLS.md](NATIVE_XLS.md). Raw is BIFF record payload hex; decoded values
and cached formula values are projections. **Every XLS result remains partial**,
even for a supported fixture. Unsupported structures and unresolved strings are
visible, and original bytes remain authoritative for fuller future processing.

WAV header metadata and other-file inventory retain the prior bounded behavior.
Provider completeness remains unknown; a scan is not an atomic snapshot of a
changing remote tree. Automatic phone/date/recording correlation is not added.

## Evidence boundaries

The independent N15 receipt records malformed-input, retention and unchanged-byte
checks. The wave-2 build receipt identifies the exact compiled sources and JVM
test results. Instrumentation fixtures compile separately; no Android runtime is
present in this environment, so SAF, picker and physical-phone acceptance remain
pending. Native and server parser policies differ; equivalent file extensions do
not establish cross-platform semantic equivalence.

## Wave 7: bounded WAV header observation

The native WAV path now retains at most 64 KiB, probes known oversized files
within actual file/total read budgets and streams complete-file hashing only
when EOF is observed. Duration is a supported header declaration, not measured
playback or validation of audio samples. Provider/actual EOF size conflicts and
unsupported layouts stay explicit. See `WAV_HEADER_PROBE.md`,
`WAVE7_STREAM_QA.md` and `ANDROID_WAVE7_BUILD_RECEIPT.json`.

## Fala 12: tekst bazowy XLSX i adnotacje fonetyczne

Rzeczywisty fixture ujawnił, że rPh było wcześniej doklejane do tekstu
bazowego w sharedStrings i inlineStr. Parser oddziela teraz wskazówki wymowy,
zachowuje bazowy/rich text, whitespace, indeks shared string i cache formuły.
Adnotacje fonetyczne nie są zachowywane w projekcji: jawny
`xlsx_phonetic_omitted` powoduje częściowe pokrycie. Oryginał pozostaje
niezmieniony i wskazywany przez URI oraz hash odczytanych bajtów. Zliczanie
znaków obejmuje także pomijany tekst, aby nie osłabiać dotychczasowego limitu.
Szczegóły i reprodukcja: `XLSX_PHONETIC_PROJECTION.md`; niezależny odbiór:
`WAVE12_XLSX_QA.md` i `WAVE12_XLSX_ENGINE_QA.md`.

# Features and boundaries

AGEDS implements an evidence-review workflow. The source, each acquisition, processing attempt, transcript version and interpretation remain separately addressable.

| Area | Implemented behavior | Boundary |
|---|---|---|
| Source preservation | Ingestion stores content by SHA-256 and retains acquisition context, including repeated ingestion of the same bytes. | Application and filesystem integrity checks do not establish physical WORM, authorship or truth. |
| Communication imports | SMS Backup & Restore XML and WhatsApp text imports retain raw fields and repeated occurrences, with idempotent import positions. | Timezone, date-order and historical provenance ambiguity remain explicit. |
| Transcription | Local faster-whisper worker; atomic claim, renewable lease, recovery and fenced publication; separate run for each attempt and version for each success. | A working synthetic smoke test is not recognition-quality evidence for a personal corpus or language. |
| Review and citations | Browser and Android version selection, annotations, contiguous segment/word citations, pinned quote text and timing; saved-citation range playback. | Word selection requires stored ASR timestamps and consistent text. Citation validation does not verify listening, speaker identity or acoustic alignment. |
| Search and history | Literal SQLite FTS5 search with typed validation and work limits; descending-ID pages and bounded client history. | Completeness describes a bounded query, not the completeness of imported or indexed sources. Continuation cursors do not provide MVCC across requests. |
| Server/local scan | Read-only bounded CSV/TSV, XLS, XLSX and WAV inspection, inventory, conflicts and coverage reporting. | Unsupported formats, damaged files and resource exhaustion remain issues. No formula execution, original correction or automatic ingestion. |
| Android source scan | SAF directory access through the production scan engine, exact URI identity, raw fields, format hypotheses and app-private cache. | XLS is a narrow partial BIFF8 projection; native and server parsers have different policies. Host tests do not execute Android provider/grant behavior. |
| WAV observations | Bounded RIFF/WAVE header declarations and labeled duration projection, with separate source size and complete-stream hash scope. | Header duration is not measured playback or decoded sample validation. Unsupported containers/encodings are explicit. |
| Media reads | Initial whole-file integrity verification and checked descriptor chunks for playback and decoder reads. | Files remain mutable outside AGEDS; limits and platform descriptor requirements apply. |
| Metadata transfer | Package digests and relationship checks; exact canonical import/export through a new inert SQLite archive. | No source media, signature, live restore, replay or job execution. |
| Citation transfer | Bounded version-pinned citation packet with a standalone independent consumer. | Locators are inert and packets are not anonymized. Recorded source hashes cannot be recomputed without source bytes. |

## Evidence rules

1. Preserve source bytes and source-native metadata.
2. Keep normalized records, parser observations, model output and human annotations separate.
3. Preserve conflicting observations with their locators rather than silently selecting a truth.
4. Attach a citation to the selected transcript version and occurrence.
5. Represent missing provenance and unsupported precision explicitly.
6. Keep deterministic handling of identifiers, dates, numbers, hashes and selectors outside model interpretation.

The current service uses SQLite and local content storage. Broader legal-analysis layers, OCR, diarization, semantic search, cloud connectors, signed exports and integrations with other ecosystem projects are future directions. Their presence in architectural notes does not imply an available adapter.

[Architecture](ARCHITECTURE.md) describes the project boundary; [Architecture rules](ARCHITECTURE_RULES.md) record the authority and semantics of design decisions. [Documentation index](README.md) links the detailed contracts and acceptance records.

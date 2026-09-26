# AGEDS architecture

## Boundary

AGEDS is an auditable-artifact system. `evidence-workbench` is its first practical module, not a separate conceptual product.

The mobile app is a **projection/client**. The evidence model is platform-neutral.

```text
source bytes / source-native records
             |
             v
        immutable artifact ---- SHA-256 / source locator / timestamps
             |
             +--> normalized events
             +--> correlations (confidence + rationale)
             +--> transcripts/OCR/parsers (model + parameters + version)
             +--> human annotations
             +--> semantic/LLM analyses
                         |
                         v
              claims / timelines / legal analysis / exhibits
```

No arrow mutates the source node.

## Platform strategy

- `core`: Kotlin Multiplatform. No Android APIs. Domain objects and deterministic scheduling/rules.
- `androidApp`: first UX surface. File picker, import queue, transcript review and annotations.
- later: desktop/JVM and iOS clients consume the same JSON/domain contracts; web can use JS/Wasm or the HTTP API.
- `server`: authoritative evidence store + workers. PostgreSQL/pgvector/object store can replace SQLite/local files without changing the domain boundary.

## Transcription strategy

Current: server-side faster-whisper, because it gives a working batch pipeline immediately and does not force one inference runtime into every client platform.

Future provider interface can add:
- on-device Whisper/whisper.cpp
- GPU worker
- external transcription providers
- diarization post-pass

Each run is a new `derived_text` version. Model/version/language/segments stay attached to it.

## Priority semantics

Queue priority is operational, not evidentiary. Manual selection dominates deterministic hints such as duration, known counterparty, conflict marker or legal tag. An LLM may suggest priorities later, but its score must be separately attributed and never presented as evidentiary weight.

## AGEDS extension

The same artifact graph can later support:
- minimum required metadata
- artifact classification
- Live Explainer projection
- chain-of-custody / signed manifests
- cross-validation across independent sources
- Rumsfeld-style disclosure: known / uncertain / absent / unknowable
- legal claims with supporting, contradicting and merely contextual evidence

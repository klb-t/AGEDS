# Architecture notes

## Design constraints

1. **Raw evidence is immutable by convention.** Import creates a content-addressed stored copy and SHA-256. Correction never edits the source row; it creates normalized/derived facts.
2. **Provenance is first-class.** Every source-native object retains source locator, timestamps, identifiers, import time and raw metadata.
3. **Assertions have confidence.** Contact resolution, filename correction, message/thread association and transcript output may be probabilistic; the model must store that instead of collapsing it into a single truth field.
4. **Time is multi-valued.** Source timestamp, filesystem timestamp, normalized timestamp and inferred timestamp must remain distinct. Timezone conversion is metadata, not destructive rewriting.
5. **Reproducibility.** Parser/transcriber model/version and parameters belong on the derived object. Re-running creates another version.
6. **Human annotation is separate from machine output.** Notes can point to an exact artifact/event/transcript interval.
7. **Cross-source correlation is a relation, not a mutation.** E.g. recorder filename `X` can be linked to call-log event `Y` with confidence + rationale while retaining the conflicting filename.

## Next production step

SQLite is deliberately used in this starter so it runs immediately on one VM. Production path:

- PostgreSQL + pgvector
- object store (local immutable disk, GCS/S3 or WORM target)
- background workers and durable queue
- Google OAuth connector service
- per-source incremental cursors
- stronger identity/contact graph
- diarization/OCR/embeddings as optional derived pipelines
- export signer + Merkle/manifest verification

This preserves optionality: no UI or evidence model depends on a specific transcription or LLM provider.

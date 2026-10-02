# Evidence Workbench architecture

The service owns the preserved content store and SQLite catalog. Android and browser clients are review surfaces over its HTTP API; source scans are a separate read-only observation flow.

## Implemented layers

| Layer | Modules and behavior |
|---|---|
| Acquisition | `evidence.py` stores ingested bytes by SHA-256 and retains separate acquisition observations for repeated imports. Source locators, original names and raw metadata remain distinct from normalized values. |
| Communication import | `importers/sms_backup.py` and `importers/whatsapp.py` retain raw fields, import positions and explicit time ambiguity. |
| Processing | `jobs.py` and `worker.py` use transactional claims, renewable leases and fenced publication. Each attempt has a processing run; each successful run creates a transcript version. |
| Review | `citations.py` pins a quote to an exact transcript and selector. Annotations do not overwrite source or model text. Browser and Android clients check save responses against the selected occurrence. |
| Query | `search.py` preserves literal FTS5 syntax with typed input/work failures. `read_pages.py` exposes bounded history pages; client retention budgets are separate. |
| Content access | `verified_media.py` checks the retained descriptor and outgoing chunks. `verified_reader.py` provides checked seek/read access for the ASR decoder, with final verification before publication. |
| Source observation | `scanner.py` reads bounded CSV/TSV, XLS, XLSX and WAV profiles and records coverage, issues and conflicts. Scanning neither ingests files nor repairs originals. |
| Transfer | `packages.py` verifies metadata snapshots; `archive.py` stores exact canonical packages in inert archives. `exchange.py` exports a pinned citation; `exchange_consumer.py` independently inspects it without following source locators. |

SQLite and local content storage are the implemented runtime. The service currently has no built-in authentication or tenant isolation. HTTP source scans are disabled until an operator configures allowed roots.

## Preservation and interpretation

Original bytes, acquisition context, parser observations, processing runs, transcript versions and human interpretation remain separate. A scanner observation is not an acquisition record. Contradictory fields remain addressable; normalized dates or filename-derived correlations do not replace raw values.

A SHA-256 identifies content and supports integrity comparisons. It does not prove authorship, time of acquisition or factual truth. Descriptor verification prevents unchecked changed chunks from being returned; it does not turn the store into physical WORM storage.

ASR text and timings are model output. Exact citation validation proves consistency with the stored version and selected occurrence, without establishing acoustic alignment, speaker identity or recognition quality. Missing historical model or acquisition metadata remains unknown. Queue priority is an operational choice rather than evidentiary weight.

## Operational boundaries

The service and worker must use the same catalog and content-store configuration. Before an upgrade, stop old workers and the service, back up both database and store, and verify the backup. Startup applies additive migrations; already loaded old worker code does not acquire new lease checks automatically.

Metadata archives and citation packets are bounded inert data. They contain no source media and provide no live restore, task resumption, inference replay or cryptographic signature. Packet locators remain literal metadata. Imported metadata is not anonymized merely because server storage paths are omitted.

Detailed limits and failure contracts are indexed in [technical documentation](../docs/README.md). [Local validation](../docs/DEVELOPMENT.md) separates host tests, actual browser execution, real synthetic ASR inference and Android device acceptance.

## Future extensions

PostgreSQL or a different content store, cloud incremental connectors, OCR, diarization, embeddings, identity resolution, signed exports and broader legal-analysis workflows remain possible extensions. They require concrete contracts and acceptance tests; they are not prerequisites for the implemented local workbench or currently available service adapters. Background workers and the durable transcription queue are already implemented.

Server-side faster-whisper is the current transcription path. The Android JNI/whisper.cpp sources are an unconnected prototype: Gradle does not configure their native build and the client has no Kotlin native bridge. On-device inference is not an implemented feature.

# AGEDS documentation

## Start and use

- [Getting started](GETTING_STARTED.md): local service, worker, Android, scanning and export.
- [Features and boundaries](FEATURES.md): implemented behavior and precise limitations.
- [Development](DEVELOPMENT.md): reproducible local checks, acceptance runners and upgrades.
- [Corpus import](CORPUS_IMPORT.md): optional private catalog workflow.
- [Change history](../CHANGELOG.md): functional development increments.

## Architecture and evidence contracts

| Topic | Documents |
|---|---|
| Project boundary and decision semantics | [Architecture](ARCHITECTURE.md), [Service architecture](../server/ARCHITECTURE.md), [Architecture rules](ARCHITECTURE_RULES.md), [Ecosystem](../ECOSYSTEM.md). |
| Search, pagination and client limits | [Literal FTS search](SEARCH_CONTRACT.md), [Read pages](READ_PAGES.md), [Client history budget](CLIENT_HISTORY_PAYLOAD_BUDGET.md). |
| Citation transfer | [Evidence exchange](EVIDENCE_EXCHANGE.md), [Independent consumer](EVIDENCE_CONSUMER.md). |
| Metadata archives | [Inert archive](METADATA_ARCHIVE.md), [Interpreted verification](ARCHIVE_VERIFICATION_BUDGET.md), [Archive budgets](ARCHIVE_VERIFICATION_BUDGETS.md). |
| Verified media and decoding | [Media serving](VERIFIED_MEDIA.md), [Decoder reader](VERIFIED_READER.md). |
| Native source formats | [Format profiles](NATIVE_SOURCE_FORMATS.md), [BIFF8 XLS](NATIVE_XLS.md), [XLSX phonetic hints](XLSX_PHONETIC_PROJECTION.md). |
| WAV observations | [Common-core header probe](WAV_HEADER_PROBE.md), [Server probe](SERVER_WAV_HEADER.md), [Server scan integration](SERVER_WAV_HEADER_SCAN.md), [Android display](ANDROID_WAV_HEADER_UI.md). |
| Source scan and cache lifecycle | [Production engine](SOURCE_SCAN_ENGINE.md), [Android adapter](ANDROID_SOURCE_SCAN_ADAPTER.md), [Publication gate](SOURCE_SCAN_PUBLICATION_GATE.md), [Guarded cache](SOURCE_SCAN_CACHE_PUBLICATION.md), [ViewModel integration](ANDROID_SCAN_PUBLICATION_INTEGRATION.md). |
| Device acceptance | [SAF provider harness](ANDROID_SAF_PROVIDER_ACCEPTANCE.md). |

## Development state and records

[HANDOFF.md](../HANDOFF.md) and [coordination](../coordination/README.md) identify current work and acceptance criteria. [AGENTS.md](../AGENTS.md) defines contributor responsibilities and invariants.

[Current acceptance records](validation/README.md) index the latest local checks and their limitations. Preserved implementation notes, experiment outputs, initial failures and build receipts from the earlier increments have a separate [historical index](archive/2026-09-30_2026-10-01/README.md) and [path/hash manifest](archive/2026-09-30_2026-10-01/manifest.json). Archived snapshots describe the revisions they tested; consult the handoff for current acceptance.

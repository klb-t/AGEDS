# AGEDS — current handoff

Recovery and repository cleanup: **2026-10-02**. Read
[CLAUDE.md](CLAUDE.md), [current coordination](coordination/state.json) and
[validation index](docs/validation/README.md) before continuing.

The earlier PR #4 line is preserved at `b6abe86`; it contains 49 commits beyond
the previously published main `8c3efb9`. The foundation branch is already
integrated. The standalone branch's functional changes were incorporated by
`1e71cf8`; it needs no duplicate cherry-pick. Keep these branches as history.
Merged into `main` through PR #5 (`b861757`). The checked source tree is
unchanged by the merge. [Publication mapping](coordination/publication-20261002.json)
resolves local commit IDs in receipts to their published equivalents.
Current publication status is recorded in `coordination/state.json`.

## Implemented workflow

| Stage | Behavior |
|---|---|
| Discover | Android SAF or server/local read-only scan of CSV/TSV, bounded BIFF8 XLS, XLSX and WAV; inventory other files. Limits, ambiguity and omissions remain visible. |
| Preserve | Content-addressed original bytes, separate acquisition observations, raw source fields and additive database migrations. |
| Process | Renewable leased transcription jobs; one processing run per attempt; fenced publication and a new text version per successful run. |
| Review | Search, bounded version histories, separate annotations, segment/word citation selection and range playback. |
| Cite | Version, occurrence, exact raw text, hash and selector remain pinned; returned save data must match the frozen selection before success/history updates. |
| Exchange | Metadata and citation packages with digests and graph checks; exact canonical roundtrip through a separate inert SQLite archive. |

A private corpus seed is optional. Case-specific defaults now come from the
seed's `defaultPresetIds`, filtered to existing preset IDs. Legacy seeds without
this field start with no selected groups; the user can select them manually.
The app contains no hardcoded case identities.

## Recovery and acceptance

The overnight ledger had 82 tasks: 78 accepted and four blocked on test/runtime
execution. The native implementation was already saved; it was not absent.
All four blocked tasks now have actual acceptance. The recovery re-runs the concrete acceptance instead of implementing it twice.

| Check | Current evidence |
|---|---|
| Backend | 429 tests + 588 subtests passed; generated fixtures and fresh Python environment. |
| JavaScript | 85 tests passed, including exact occurrence and Python numeric parity. |
| Browser | 25 real Chromium scenarios passed, including all four previously unexecuted N83 cases, real WAV/HTTP playback and history budgets. |
| Native/JVM/APK | Pinned Gradle 9.7.0/JDK 21/Kotlin 2.4.20/AGP 9.4/SDK 37: 411 JVM tests passed (135 core, 276 Android), APK signature verified; test APK compiled, zero SAF runtime. |
| ASR environment | Fresh installation of nine declared pins, 14 imports and `pip check` passed. |
| Real ASR | New tiny.en CPU/int8 inference through production VerifiedReader passed; word citation and inert archive roundtrip retained exact output. |
| CLI | Real subprocess acceptance for CSV raw records, bounded WAV scanning and archive budgets passed without changing synthetic sources. |
| History | Byte-preserved documentation/coordination snapshots and their original-path/hash manifests. |

All receipts and commands are in [validation](docs/validation/README.md).
Failures from earlier attempts remain in the [historical documentation
index](docs/archive/2026-09-30_2026-10-01/README.md). Full task history is in
[historical coordination](coordination/archive/2026-09-30_2026-10-01/README.md).
These snapshots can contain old paths, expired claims and superseded next steps.

## Concrete remaining boundaries

- SAF device/provider execution has not happened. Six instrumentation tests
  are prepared; a successful APK or instrumentation compilation does not pass
  them. Follow [the device harness](docs/ANDROID_SAF_PROVIDER_ACCEPTANCE.md) on an
  available Android runtime, then separately test picker and persisted grants.
- Synthetic English ASR does not validate Polish speech quality or word
  alignment. Keep the uncorrected recognition result and use a separate consented
  corpus for a quality experiment.
- API authentication is not implemented; operate the current service on a
  trusted/private network. Hash agreement is not authorship, truth or custody.
- The inert archive has no source media, live restore, job replay or signature.
- On-device JNI is an unconnected prototype, not a functioning ASR provider.
- Full five-layer legal analysis, Live Explainer, signed custody and partner
  integrations remain future work. Do not equate a retained design with a
  deployed feature or manufacture numerical certainty.
- Current files were cleared of case-specific identities. Those identifiers
  already exist in the repository's public Git history; this work preserves
  history and does not claim to erase that earlier exposure.

## Continue without restarting completed work

Use local checks from [Development](docs/DEVELOPMENT.md). GitHub Actions now runs
only by explicit manual dispatch. No Actions jobs or paid model calls were used
for recovery. Never restore an expired overnight claim or infer an active agent
from a historical schedule. Record a new task ID, file owner, source revision,
acceptance and result in the current ledger. Preserve rejected attempts and
experiments off the main documentation path.

Before upgrading a live deployment, stop old workers and back up SQLite and the
content store. Existing loaded workers do not gain lease fencing from edited
files on disk.

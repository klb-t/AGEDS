# N40: aggregate client history payload budget

Transcript-version, citation and annotation collections each retain at most
1,000 rows and 4,194,304 bytes of encoded row payload in the browser and Android
clients. This adds an aggregate bound to the existing per-response server budget;
count-only limits could otherwise retain hundreds of megabytes of quote or note
text. Each collection has its own budget. The limits do not mutate server records,
quotes, selectors or the contents of an admitted row.

This is the sum of serialized row UTF-8 byte lengths, not a heap or process-memory
claim. Array/envelope framing, client UI objects, string representations, transient
HTTP responses, serialization buffers, selected full transcript and duplicate
rendered strings are outside this measurement. The server's 2 MiB response budget
is a separate boundary. Protecting the transport from an arbitrarily large
malicious HTTP body is outside this change.

## Measurement and admission

The browser uses `historyRowBytes(row)`, defined as
`new TextEncoder().encode(JSON.stringify(row)).byteLength` after row validation.
It counts JSON escaping and multibyte Unicode. `createHistoryPager` exposes
`retainedPayloadBytes`, `reachedPayloadLimit` and `initialItems`. Its optional
`maxPayloadBytes` parameter can lower but cannot raise the 4 MiB production cap.

Kotlin uses public `serializedHistoryRowBytes(serializer, value)` with
`Json { encodeDefaults = true }` and default explicit null encoding, followed by
`encodeToByteArray().size`. This measures the actual retained typed model, including
its default/null fields; it does not approximate JSON with `toString()` or string
character counts. `CitationWorkspace` supplies concrete serializers for
`TranscriptVersion`, `Citation` and `Annotation` to every production `CitationPages`
instance. The common `ArtifactPageChain` accepts an injected positive `rowSizeOf`
contract. Its old generic constructor remains available without measurement for
compatibility tests; `payloadBudgetEnabled=false` makes that explicit. Production
controllers cannot omit the measurement function.

Every incoming page's identities, cursor/snapshot constraints and all row costs
are checked before admission. Only the longest fitting prefix is admitted. The
first unfit row stops that collection; it is never silently skipped in order to
continue with smaller older rows. The retained cursor does not advance beyond the
last admitted row. If no row fits, the collection can retain zero new rows and
still reports omission. A complete result exactly at the byte boundary remains
complete; exact boundary plus older availability prevents another request.

`reachedPayloadLimit` distinguishes payload omission from server history end and
from the 1,000-row count limit. Browser and Android coverage messages report the
encoded byte count, the 4 MiB budget, omitted history, stopped loading and the
explicit refresh option. Refresh starts new accounting and a new snapshot.

## Save and asynchronous boundaries

Browser local citation saves use the same encoded row cost. A save that succeeds
on the server but does not fit is acknowledged, and the row is not appended
outside accounting. An older response arriving after a local save is checked
against current remaining bytes and can admit only a prefix. Stale/closed request
responses and late saves after closure cannot mutate retained accounting. Existing
1,000-row concurrent-save overflow remains an atomic rejection with a bounded
smaller retry.

Android already refreshes the relevant collection after a successful citation or
annotation save. Reset clears its rows, byte count and budget flag; cancellation
and request-epoch fencing prevent a delayed older page from reappearing in the
refreshed collection. `CitationPages` exposes measured bytes and the reached flag
for UI and independent tests.

Browser initial HTML remains server-bounded. If the client admits only an initial
prefix, omitted initial rows/options are removed, while all retained literal text
stays unchanged. The inert bootstrap JSON element is removed after initialization.
Adding older version summaries preserves the selected transcript and annotation
version; saved quotation playback/export remain pinned to the stored citation.

## Validation scope

Owner tests: `history-payload-owner.test.mjs` verifies actual UTF-8/escaping cost,
first-row overflow with no later-row substitution and closed-view late saves.
Together with the eleven prior paging tests, these fourteen tests pass. The three
new common Kotlin tests in `ArtifactPayloadBudgetOwnerTest.kt` cover real serializer
prefix admission, invalid measured costs even beyond a cutoff, and explicit
count-only compatibility behavior. The coordinator subsequently ran all three
successfully within the 207-test integrated JVM build; the owner ran no Gradle.
See `archive/2026-09-30_2026-10-01/ANDROID_WAVE6_BUILD_RECEIPT.json`.

Independent N41 budget tests and the coordinator's real Chromium large-row case
provide additional acceptance separately. This document does not substitute code
review or Node tests for those execution receipts, device runtime or heap profiling.

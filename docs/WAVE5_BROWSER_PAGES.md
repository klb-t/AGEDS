# Wave 5 browser history pages

N35 changes `server/app/static/citations.mjs` and `artifact.html`. Server-rendered
history starts with bounded pages (normally 50 each) of transcript summaries,
citations and annotations. Each list has a separate explicit “Wczytaj starsze…”
control and a status reporting its loaded count, older availability, snapshot end
or the 1,000-item browser limit. Reaching exactly 1,000 complete records reports
snapshot end rather than claiming omitted records. New saved citations count
against the same limit; an already completed save is acknowledged even when its
row cannot be added to a full view.

Initial data uses Jinja `tojson` in an inert `application/json` element. Initial
source text uses ordinary autoescaped template values; dynamically added text
uses `textContent`. No source string is interpreted as markup. Transcript history
items contain ID/model/language/run/date summaries, not all transcript bodies or
segments. The selected version's full text is loaded through the existing pinned
transcript endpoint and displayed as literal text in an expandable view. Older
version pages append options without changing either the active citation version
or annotation version; selecting a version remains an explicit operation.

Each independent pager validates the exact envelope fields, supported numeric
IDs (positive safe JavaScript integers), owning artifact, relevant row types,
strict descending IDs, no repeated IDs, fixed snapshot, requested limit and
exclusive continuation cursor before changing its state or appending rows.
`hasMore` allows a short page because the server's byte budget can return fewer
rows than the requested count. Empty snapshots use zero. Nonterminal cursors must
match the last supplied ID; terminal cursors are null. Source time/quote values
are not normalized or inferred. Unsafe IDs are refused rather than rounded.

Only one request per list is active. An immutable request token binds its cursor
and snapshot; stale, forged, duplicate or closed requests cannot append content.
Navigation aborts outstanding fetches and closes the pagers even if a transport
ignores abort. Failed validation leaves cursor and row state unchanged, allowing
a bounded retry. A concurrent new citation save cannot push a pending older page
past the cap; that page is rejected atomically and a retry requests only remaining
capacity. The API still owns snapshot semantics: the browser does not assert a
cryptographically authenticated snapshot or immutable annotation history.

Saved citation playback and download links continue to use the citation's own
pinned transcript ID, independently of the currently selected version. Both
initial and newly added packet links are checked against the artifact/citation
IDs. New citations retain their captured version even if selection changes while
the save completes. These actions do not verify ASR acoustic alignment or truth.

The other page sections retain explicit bounded previews: up to 50 nontranscript
texts, 4,096 characters per text, and up to 100 events with 4,096-character body/
subject previews. Truncation and additional omitted rows are identified in the
page. Those previews are separate from transcript version paging; they are not
silently presented as complete source content.

## Local checks

`server/tests/js/history-pages.test.mjs` contains 11 passing tests. They cover all
three row shapes, literal text, empty snapshots, short byte-limited pages, malformed
envelopes, ownership/unsafe IDs, cursor/snapshot/ordering checks, atomic retries,
stale/forged/closed requests, exact cap boundary and concurrent-save cap behavior.
The existing nine ID-boundary and fourteen range-player tests also pass (34 Node
tests total). The twelve existing API tests pass after template integration.

Actual Chromium integration and additional browser scenarios are owned by the
coordinator's acceptance run; the unit checks above do not substitute for browser
execution, physical-device checks or acoustic verification. No private corpus was
read and no source records were changed by this work.

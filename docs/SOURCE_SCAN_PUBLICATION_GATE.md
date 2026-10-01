# Source-scan publication ordering

`SourceScanPublicationGate` orders scan-generation invalidation against the
**final atomic cache publication action** for one owner. An earlier token check
followed by an unguarded file move leaves a cancellation/supersession race. The
gate checks the token and runs that final move while holding the same monitor
used by invalidation and generation replacement.

## API

The plain JVM class is in `dev.klbt.ageds`:

```kotlin
val gate = SourceScanPublicationGate()
val token = gate.begin()
val current = gate.isCurrent(token)
val published = gate.publishIfCurrent(token) { /* final atomic file move only */ }
gate.invalidate()
```

`begin()` creates a new identity token and replaces the prior current token.
There is no numeric generation counter to wrap or be reused. A token from another
gate does not match. `invalidate()` clears the current token. `isCurrent()` is a
locked snapshot check useful for subsequent UI decisions; it cannot replace
guarded publication.

`publishIfCurrent` returns false without calling the action when the token is
stale. Otherwise it runs the action under the lock and returns true. Publication
exceptions propagate and release the lock; they are not falsely reported as
success. The token remains current after either successful publication or an
action exception, until begin/invalidate changes it. This supports later UI
checks and does not promise one publication per token.

## Linearization and ownership boundary

The final action and current-token check form one critical section relative to
begin/invalidate:

- If invalidation or a new begin acquires the gate first, an old token cannot
  execute a subsequent publication action.
- If publication acquires it first, invalidation waits for that final action.
  The already-published cache may legitimately remain afterward. Cancellation
  does not undo a move that won the ordering race.

Encoding, serialization, temporary-file creation, writes and fsync must all occur
**outside** the critical section. Only the final atomic move belongs inside.
The cache caller owns staging, unique temporary names, stale-result cleanup and
move-error cleanup. Callers must avoid holding another cache lock while waiting
for this gate; this small class does not coordinate a broader lock hierarchy.

A single owner, such as one ViewModel's scan lifecycle, must retain and use one
gate for its begin/cancel/publish path. The gate is not cross-process locking,
coordination across multiple ViewModels, a distributed lease or a source-data
lock. It grants no provider permissions and performs no source writes. Existing
cache contents can remain when a new scan is canceled; they must retain their
own metadata rather than be mislabeled as a completed new scan.

## Verification

`SourceScanPublicationGateTest` checks token supersession, invalidation before
publication, foreign-owner tokens, move exceptions and subsequent recovery, and
publication holding the gate while invalidation waits. Independent tests exercise
the actual guarded cache move and typed scan-result roundtrip using deterministic
staging/publication barriers. These host tests establish ordering for this
in-process owner; they do not claim Android lifecycle or multiple-process
acceptance. No new dependencies, GitHub Actions or paid execution are required.

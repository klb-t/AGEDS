# Coordination

[state.json](state.json) is the current task and acceptance index. Start with
[HANDOFF.md](../HANDOFF.md) and [CLAUDE.md](../CLAUDE.md).

Each task records a stable ID, owner, file scope, source revision, acceptance
criterion and result. Results distinguish code review, compiled tests, actual
runtime execution and publication. A local commit is not a GitHub checkpoint.

Use `ready → in_progress → done → accepted`, or `blocked` with the concrete
condition needed to proceed. Integration and edits to this index are sequential.
Parallel workers must have disjoint file ownership or coordinate a handover.
Existing routine decisions remain autonomous; new material costs, commitments
or irreversible operations outside the mandate require separate authorization.

Historical foundation and overnight task ledgers are kept in
[archive/2026-09-30_2026-10-01](archive/2026-09-30_2026-10-01/README.md). They preserve
all 82 overnight tasks and their evidence, including expired claims and initial
failures. They are not the current queue or an active scheduler.

Communication between separate conversations requires the recipient to read the
published commit and acknowledge the task/result ID. Search results alone do not
prove delivery, execution, current state or completion.

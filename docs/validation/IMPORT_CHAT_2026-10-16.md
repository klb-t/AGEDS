# Chat importers — validation 2026-10-16

Task `AGEDS-TASK-20261016-B` part 1, branch `wt-B`, tested head `d8fa22b`
(machine-readable: [receipt JSON](IMPORT_CHAT_2026-10-16.json); feature:
[chat importers](../IMPORTERS_CHAT.md)).

| Check | Result |
|---|---|
| Full backend suite `pytest server/tests -q` | 460 passed, 1 skipped (opt-in scale), 588 subtests, 0 failed |
| Chat + importer + foundation tests | 45 passed, 1 skipped |
| Opt-in scale run, 120 MB target | 125,831,832-byte export, 2,313 conversations, 57,825 events, 43.3 s, peak RSS +19.5 MB, coverage complete |
| Real CLI subprocesses on the synthetic corpus | all exit 0; re-import output identical; 8 `event_reobserved`; 16 corpus files byte-identical before/after |
| `scripts/verify_repository.py` | pass |

**Runtime-executed:** CLI imports/report in a separate data directory, upload
endpoints via TestClient, the 126 MB streamed import. **Unit-tested only:**
TZ independence, mapping-file reinterpretation, limit handling. **Not run:**
real vendor exports (none available), JS/browser/Android (unchanged),
concurrent imports, Gemini conversations (track B2).

Boundaries: absence is "not observed", never deletion; RSS includes the
process baseline and is reduced by releasing scanned mmap pages on Linux;
`.dat` media types are extension guesses; new tables are not yet in metadata
packages.

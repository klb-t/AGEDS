# Wave 10 — independent archive semantic-budget acceptance

N63, 2026-10-01. New independent tests in `server/tests/test_archive_semantic_budget_adversarial.py` exercise the public archive import/read/export paths and the package verifier. All inputs are synthetic; no source corpus, dependency changes, production edits, or commits are involved.

## Reproduced old bypass

Before N61/N62 integration, a valid package fixture was changed only by adding twelve nested lists in an unused field of its pinned transcript's `segments_json`, then recalculating its recorded integrity digests. Calling public `import_metadata_archive` with `ArchiveLimits(max_depth=8)` succeeded and published a 16,384-byte SQLite archive. The outer package was shallow, while the JSON string decoded during anchor verification exceeded the configured depth. This is a structural-work bypass, not a quote-tampering or authenticity claim.

## Independent assertions

The suite models node counts independently: containers and values count, object keys do not. It adds the outer package, each unique pinned transcript's decoded structure, and each anchor's decoded selector. Identical JSON in two transcript versions is charged twice; repeated anchors pinned to one version share its successful or failed decode only within one verification.

Tests cover hidden transcript and selector depth, hidden wide arrays, exact decoded-field depth at its outer field position, exact shared node allowance and one-node-short rejection, multiple versions, successful/failed decode call counts, fresh budgets on repeated verification, and rejection through public archive read/export without output publication. Budget-rejected import must not even create the output parent directory. Rejected read/export must preserve archive bytes.

## Verification result

**10 independent tests passed** against the landed helper and archive integration. Combined with the unchanged `test_metadata_archive` and `test_packages` suites, **47 tests passed** in 0.599 seconds:

```sh
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:$PWD python -m unittest server.tests.test_archive_semantic_budget_adversarial server.tests.test_metadata_archive server.tests.test_packages -q
```

The tests use real parsing and projection; decode spies wrap production functions rather than supplying canned results. No open blocker was found within this scope. Projection-work charging and broader roundtrip interoperability have separate acceptance owners; this receipt does not claim those tests.

Tested file SHA-256:


- `server/app/verification_budget.py`: `c66e5bce855d4727d7a58eb8b0bb49a735bd38534d1fb43b06da63ed29c795d5`
- `server/app/packages.py`: `28f9425c4bd609e668b122cad3f9c16f45f0655c5afb04254b32401df8cd6ba7`
- `server/app/archive.py`: `20d7388f389b5bd501a1d085f36ffbf4c54252f839c47205794be378423662ec`
- `server/tests/test_archive_semantic_budget_adversarial.py`: `24a5bbc615b3e5adb4860c0ef4e7c72aa8df508797ed2256fcc891355fe671b4`

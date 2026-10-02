# N21 — independent citation-exchange acceptance

Task: `AGEDS-20261001-N21`; claim/base `e208310073933ac8d29576de7d17fe98b75de4b3`.
Test date: 2026-10-01 overnight batch. New independent test owner: `exchange_qa`.
Production changes were made by their producer, consumer and coordinator owners.

## Executed result

```
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:. python -m pytest server/tests/test_exchange*.py -q
37 passed, 65 subtests passed, 3 warnings
```

The independent `server/tests/test_exchange_acceptance.py` contributes **18 tests
and 27 subtests**. The combined result includes producer and consumer owner tests.
Warnings are existing Starlette/AnyIO and FastAPI startup deprecations. This is a
local working-tree receipt; publication and integration are separate coordinator
steps. It is not an Android/device, live ASR, remote server or partner-project test.

## Evidence covered

- A synthetic SQLite fixture has two transcript versions, an old-version word
  selection spanning two segments, two acquisition observations, raw malformed
  metadata, a recorded run, combining Unicode, newlines and preserved errors.
- The HTTP packet route and subprocess `citation-export` return the same packet;
  subprocess `citation-inspect` independently verifies its included pinned version.
  An independently implemented digest formula matches the producer digest.
- Export leaves database file bytes unchanged. Patched Python file-opening APIs
  reject any producer attempt to read source paths. Literal file/content/HTTP URIs,
  traversal text and shell-command-looking strings survive as metadata. No source
  media is read or fetched. Default export omits `stored_path` and the lease token.
- The independent consumer runs with server projection helpers disabled; a fresh
  subprocess proves it imports no producer, citations, database or server config
  module and creates no live data directory.
- Digest corruption and unknown schemas fail. Recomputing the envelope digest
  does not bypass validation of quote, version, artifact/run/acquisition references,
  word selectors, projected time, or false restore capability claims.
- Duplicate envelope and nested raw JSON keys, nonfinite wire values, overflowed
  JSON numbers, invalid UTF-8, byte/depth/node limits, segment and observation caps
  are rejected. A 50,000-key auxiliary object verifies object keys count toward
  the shared 100,000-node budget. Oversized stored TEXT is rejected on export;
  implementation review confirms SQL byte-size/count preflight before row fetch.
- A raw unused NaN word timestamp remains unchanged and permits a valid segment
  packet; selecting that invalid word is refused. Failed historical run status and
  raw error text remain available, without treating them as completed work.
- Missing DB export does not create a database or publish a packet; existing output
  is not overwritten. Invalid inspection creates no live storage. The file consumer
  refuses symlinks and FIFOs through no-follow/nonblocking regular-file checks.
- HTTP invalid IDs return 422, a missing anchor returns 404, malformed stored data
  returns 409, and packet limits return 413, without partial packet bodies.

## Findings fixed during acceptance

1. The first consumer rejected NaN anywhere in the preserved raw transcript string,
   including irrelevant word timing under an otherwise valid segment selection.
   The consumer now preserves auxiliary raw errors while checking selected fields.
2. The first consumer required a `done` run although the producer preserved an
   attached historical run's status/error. Both now preserve the record and make
   unsuccessful/unconfirmed processing explicit, per coordinator decision.
3. Initial producer and consumer structural accounting differed: dictionary keys
   and empty deeply nested containers could pass producer limits but fail consumer
   limits. Producer accounting now matches consumer keys/value and container-depth
   conventions. Independent real-boundary regressions cover both cases.

## Boundaries of the evidence

This is internal metadata consistency, not authenticity, authorship, truth,
acoustic alignment, verified listening, reconstruction of source bytes, replay or
live restore. Rehashing a completely self-consistent fabricated packet is possible;
there is no signature or trust anchor. Locators and names remain potentially
sensitive literal metadata even when the server's stored path is omitted.

The profile is bounded to a 2 MiB envelope, 32 container levels, a shared 100,000
node budget including object keys, 10,000 transcript segments and 1,000 acquisition
observations. Raw segments and selector JSON are parsed within that budget;
auxiliary metadata strings stay opaque. The read-only snapshot does not migrate
or update records. Tests use TestClient plus real local CLI subprocesses, not a
network deployment. No other ecosystem repository or private corpus was accessed.

# Independent citation packet inspection

`server/app/exchange_consumer.py` is a standalone Python standard-library program.
It imports no AGEDS producer, database, citation or domain modules. It can be
copied alone and invoked outside the repository:

```sh
python -I /path/to/exchange_consumer.py /explicit/path/packet.json
```

`inspect_packet_bytes(raw)` accepts UTF-8 bytes or text and returns a JSON-ready
inspection report. `inspect_packet_file(path)` reads only the explicitly supplied
regular packet file, using a bounded descriptor with `O_NOFOLLOW` and
`O_NONBLOCK`; symlinks, directories, FIFO inputs and oversized files fail.
Platforms without no-follow descriptors reject file inspection; callers can still
supply already obtained bytes. `report_json(report)` returns JSON text with literal
Unicode retained and HTML delimiters, terminal control and bidi characters escaped.
JSON decoding recovers the exact quote, including spacing and combining characters.
This output is data, not trusted HTML; a downstream UI must render strings as text.
Exit status is zero on inspection success and two on validation/read failure.

The consumer independently verifies the `ageds.citation-evidence/v1` envelope,
domain-separated canonical SHA-256 digest, identity references, declared content
hash/size agreement, included pinned transcript, exact segment or contiguous word
selector, quote bytes/hash, source seconds and rounded milliseconds. It does not
substitute another transcript version. Python ties-to-even rounding and canonical
JSON number serialization are explicitly part of v1, not a language-neutral
canonicalization standard. Every source locator remains an inert string; no URL,
source file, media, database or embedded command is accessed.

Bounds are 2 MiB input and canonical envelope, nesting depth 32, and 100,000 nodes
aggregated across envelope plus decoded `segments_json` and `selector_json`.
Every scalar, container and object key counts as one node. The root container has
depth one. Segment count is at most 10,000, acquisition observations 1,000 and
selected words 10,000. Duplicate keys, invalid UTF-8, escaped lone surrogates,
unknown schema and nonfinite outer/selector numbers are rejected. The raw stored
`segments_json` can retain legacy NaN/Infinity in unused fields; selected segment
times and every word of a word-selected segment must be finite and consistent.
Thus a broken unused word projection does not invalidate an independent valid
segment citation. Other raw metadata JSON strings remain opaque, even malformed.

Envelope, payload, integrity, scope, projection and selector fields are exact v1
contracts. Row dictionaries preserve additional inert metadata fields, while
identity-bearing fields and relationships receive explicit checks. A new field
cannot alter selection or grant additional verification semantics under v1.
Changed semantics require a new supported schema, not inference from extra fields.

The report separates declared unknowns from independently observed missing
provenance and warns when an included run does not declare completion. It retains
that run's literal status/error. Success means internal consistency with the
included transcript. Anyone can recompute this unsigned digest after changing
metadata. It proves neither authenticity, authorship, truth, real source bytes,
ASR quality nor acoustic alignment. Acquisition source IDs can differ from the
artifact's source; omitted source records are not silently fetched. The full
transcript text and raw metadata are retained declarations, not reconstructed from
the selected quote. No live import, replay, restored jobs or partner integration
is performed.

## Validation evidence

`server/tests/test_exchange_consumer.py`: 12 tests and 26 subtests pass. Independent
hand-authored Unicode segment/word packets cover redigested semantic tampering,
invalid wire JSON and resource limits, provenance gaps, failed runs, legacy unused
NaN and strict word rejection. Real producer interoperability covers both selector
kinds, version pinning despite a newer transcript, and both stored-path options.
A producer packet at exactly 100,000 aggregate nodes is accepted by the independent
consumer; the 100,001-node version is rejected by both. A real isolated `python -I`
subprocess with no repository import path verifies standalone operation; file
checks cover symlink, directory, FIFO and oversized regular files. Inputs are
synthetic. This is a portable consumer fixture inside AGEDS, not evidence of an
adapter running in another project.

# Citation evidence exchange v1

This bounded AGEDS packet carries one saved citation and its exact transcript
version. It supports an independent, inert consumer; it is not a partner adapter,
full archive, signature, source-byte verification, live restore or task import.
No source or media bytes are included. Neither producer nor consumer follows
locators. Source-content SHA256 is a recorded assertion copied from the database;
without source bytes the consumer cannot recompute it. The envelope digest checks
packet consistency against an independently trusted digest, not authorship,
truth, acquisition time or acoustic alignment. An attacker can rehash an edit.

## Producer and wire profile

`server.app.exchange.export_citation_packet(artifact_id, anchor_id,
include_stored_path=False)` returns a dict. Both IDs must be positive SQLite
integers (booleans are rejected). It opens the existing database with SQLite
`mode=ro`, uses one read transaction and never migrates or audits the export.
Missing requested artifact/anchor pairs raise `PacketNotFound(ValueError)`;
limits raise `PacketLimitError(ValueError)`; inconsistent stored citation or
references raise `ValueError`. Database access errors remain `sqlite3.Error`.

`canonical_packet_bytes(packet)` serializes and checks outer byte/structure
bounds; it does not verify packet semantics or the digest. Independent verification
is the standalone consumer's responsibility.

The exact envelope is:

```json
{
  "schema": "ageds.citation-evidence/v1",
  "payload": {
    "artifact": {},
    "source": null,
    "source_observations": [],
    "processing_run": null,
    "derived_text": {},
    "anchor": {},
    "projection": {},
    "unknowns": [],
    "scope": {
      "source_bytes_included": false,
      "media_bytes_included": false,
      "live_restore_supported": false,
      "tasks_imported": false,
      "locators": "literal_inert_metadata",
      "stored_path_included": false
    }
  },
  "integrity": {
    "algorithm": "sha256",
    "canonicalization": "python-json-sorted-keys-compact-utf8-no-nan/v1",
    "domain": "ageds.citation-evidence/v1\n",
    "payload_sha256": "64 lowercase hexadecimal characters"
  }
}
```

Empty objects above stand for records, not valid minimal specimens:

| Payload field | Exact data and intentional omissions |
|---|---|
| `artifact` | Selected `artifacts` row. `sha256` identifies recorded source content and `size_bytes` counts source bytes, each nullable for unknown legacy content. `stored_path` omitted by default. |
| `source` | Artifact's referenced `sources` row, or null when source ID is null. |
| `source_observations` | All selected artifact's acquisition rows in ascending ID order; not scanner field observations. An observation's source ID may differ from the artifact's source ID. Those other source records are outside this packet. |
| `processing_run` | Referenced run row, or null when absent; `lease_token` always omitted. Job IDs are inert historical references; jobs are not included or resumed. |
| `derived_text` | Full pinned transcript row including exact `text`, `segments_json`, `metadata_json`, ID and nullable `run_id`. The latest version is never substituted. |
| `anchor` | Selected evidence-anchor row including exact raw `selector_json`, quote, SHA256 and millisecond boundaries. |
| `projection` | Canonical selector plus `quote_text`, `quote_sha256`, `start_ms`, `end_ms` rebuilt using existing citation validation. |
| `unknowns` | Declared missing-data markers, not proof of complete provenance. Consumers derive relevant missingness independently. |

Canonical bytes are Python `json.dumps(value, ensure_ascii=False, sort_keys=True,
separators=(',', ':'), allow_nan=False).encode('utf-8')`. No Unicode or line-ending
normalization occurs. This is a named Python JSON wire profile, **not RFC 8785**
or a promise that arbitrary languages serialize floating-point numbers identically.
The digest is lowercase hexadecimal SHA256 of UTF-8 domain bytes
`ageds.citation-evidence/v1` followed by one LF byte, followed by canonical bytes
of **payload only**. Envelope schema/domain/canonicalization/algorithm must match
exactly. The integrity field is not included in its own digest. Export has no
new timestamp; unchanged selected state yields identical packet bytes.

Limits are 2,097,152 canonical envelope bytes, 10,000 transcript segments,
1,000 acquisition observations, depth 32 and 100,000 nodes (including object keys). The node budget
covers the outer packet plus separately parsed segment/selector values. Depth
counts nested object/array containers, with the root container at depth one. SQLite
counts rows and sums byte lengths before materializing each selected table's
rows. Final serialization checks escaping overhead and aggregate envelope size.
Exceeding any bound fails explicitly; there is no partial/truncated packet.

## Raw data, units and epistemic limits

`segments_json`, `selector_json`, all metadata/provenance/parameter JSON strings
and raw run errors remain exact stored strings. Malformed auxiliary metadata is
not repaired, parsed into assertions, or discarded. Segment/selector duplicate
keys reject export because their interpretation is ambiguous. Selected times and
text must pass existing strict citation rules. Historical nonfinite values in
unused segment fields remain inside the raw JSON string; they never become
nonfinite numbers in the outer JSON and cannot grant word precision.

Segment selectors require contiguous ascending indices and concatenate exact
segment text. Word selectors require contiguous recorded word indices, validated
word ordering/times and exact whole-segment word concatenation; no inferred words
or forced alignment are introduced. Their canonical selector must match every
field, including precision and units. Quote SHA256 hashes the exact quote's UTF-8
bytes, without normalization. Full transcript `text` is preserved separately;
it is not assumed to equal concatenated segment text.

ASR segment/word times are numeric **seconds**. Anchor/projection boundaries are
integer **milliseconds**, obtained with Python `round(seconds * 1000)` (ties to
even under the represented numeric value). Stored provenance timestamps remain
literal text; unspecified timezone, format or actual acquisition time is not
inferred. A legacy snapshot is explicitly partial acquisition history. Missing
run/model/version information remains unknown. A retained run status or error
is historical metadata, not authorization to execute or evidence of successful
inference. Validation confirms a citation's consistency with its stored version,
not truth, speaker identity, listening or acoustic alignment.

Default removal of `stored_path` only minimizes one server storage location.
Source locators, filenames, account fields and arbitrary raw metadata can still
contain private paths or personal information. This is **not anonymization**.
Including `stored_path` is an explicit producer option, always literal metadata.

## Transformation and acceptance

Preserved: selected version, full raw transcript representation, literal source
content identity and acquisition/run metadata, exact anchor and derived selector.
Omitted: source/media bytes, other transcripts/anchors, unrelated tables, lease
secrets and (by default) the server storage path. Added: deterministic projection,
bounded scope declarations and a domain-separated digest. The packet cannot
reconstruct the database or original audio. No shared ecosystem ontology is
introduced; IDs are scoped to this packet's originating AGEDS database.

Producer coverage is `server/tests/test_exchange_producer.py`: pinned older
version, literal paths with source IO blocked, read-only SQL, database bytes
unchanged, malformed/NaN raw preservation, altered selected version rejection,
explicit storage-path option, unknown provenance, omitted lease tokens and limits.
Independent consumer and adversarial HTTP/CLI coverage are maintained separately.

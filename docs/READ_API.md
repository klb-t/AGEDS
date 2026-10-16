# Read API v1 and local tokens

`/api/read/v1/*` is a versioned, read-only contract for ecosystem clients
(ChatADHD, Loom, scripts). It projects database rows only: no source bytes, no
media, no `stored_path`, no private seed. Every route requires a local bearer
token. The existing unauthenticated routes (`/api/search`, `/api/artifacts/...`,
`/api/timeline`) are unchanged by default because the Android client depends on
them; an operator can enforce tokens there too with `EW_REQUIRE_TOKEN=1`.

Code: `server/app/integrations/` (`tokens.py`, `read_api.py`, `intents.py`,
`proposals.py`, `loom_graph.py`), routes in `server/app/api/integrations.py`,
schema versions 70–72 in `server/app/schema/integrations.py`, CLI in
`server/app/commands/integrations.py`. Partner-level description, data-access
scopes and benefit tests: [Ecosystem adapters](ECOSYSTEM_ADAPTERS.md).

## Tokens

```sh
python -m server.app.cli token-create --label "ChatADHD laptop" --scope read --scope propose --client-id chatadhd --expires-in-days 90
python -m server.app.cli token-list
python -m server.app.cli token-revoke 3 --note "laptop lost"
```

`token-create` prints `{"token": "ageds_<key>.<secret>", ...}` exactly once. The
database stores `token_key` (public lookup id), a random salt and
`sha256(salt || secret)`; the secret is never stored, logged or shown on the
`/integrations` page. Verification recomputes the digest and compares it with
`hmac.compare_digest`. Scopes are `read` (all `/api/read/v1/*` and listing
routes) and `propose` (submitting annotation proposals and relation
hypotheses). Revocation and expiry are timestamps; rows are kept.
Verification of a valid token is read-only (review finding 8): `last_used_at` and
`use_count` are written at most once per minute per token and server process
(`TOUCH_INTERVAL_SECONDS`); uses in between are counted in memory and added on the
next write, so `use_count` is a lower bound (uses since the last write are lost if the
process stops) and a busy database never fails a valid request.

`audit_log` records `token_create`, `token_revoke` and rejections. Rejections are
counted in `api_token_rejections` (schema 73) per UTC hour, `token_key` and reason
(`unknown_key`, `secret_mismatch`, `revoked`, `expired`, `scope`) with first/last
time; only the first one of each writes a `token_rejected` audit row. After 25 keys in
one hour further keys are pooled under `*`, so junk bearer tokens cannot grow the
tables per request. Requests without a token or with a malformed token are refused
before any database access and are not audited. There is no rate limiting; the
service remains intended for a trusted/private network.

### Authentication and error model

Send `Authorization: Bearer ageds_<key>.<secret>`. Error bodies on these routes
are `{"detail": {"code": "<stable_code>", "message": "<text>"}}`:

| Status | Codes |
|---|---|
| 401 (`WWW-Authenticate: Bearer`) | `missing_token`, `malformed_token`, `unknown_token`, `revoked_token`, `expired_token` |
| 403 | `insufficient_scope` |
| 404 | `not_found` |
| 409 | `stored_data_invalid` (rows cannot form the declared projection; originals unchanged) |
| 413 | `read_limit` (bounded read profile exceeded) |
| 422 | `invalid_input`, `invalid_intent`, `invalid_interpretive_answer`, `invalid_proposal`, `invalid_hypotheses` |
| 503 | `work_limit` (SQLite VM budget exhausted; no partial result), `database_unavailable` |

### Opt-in enforcement everywhere (`EW_REQUIRE_TOKEN=1`)

With `EW_REQUIRE_TOKEN=1` (also `true`/`yes`/`on`) a pure-ASGI middleware
requires a valid, unrevoked, unexpired token on **every** `/api/*` and `/export/*`
route and on **every state-changing request** (any method other than
GET/HEAD/OPTIONS, e.g. `/upload`, `/import/*`, the `/integrations` and `/media`
forms) — review finding 6; scopes are not checked there. The token is accepted as
`Authorization: Bearer …` or as the browser session cookie `ageds_token` (HttpOnly,
SameSite=Strict, path `/`) that the form `POST /integrations/session` (field
`token`) sets after verifying it; `POST /integrations/session/logout` clears it.
The cookie does not grant the scoped `/api/read/v1/*` and `/api/integrations/v1/*`
routes, which still need the bearer header. Read-only HTML pages (`/`,
`/artifact/...`, `/integrations`, …) stay open so the login form is reachable. The
switch is read per request, so it can be toggled without code changes. The
middleware is installed through the `install_app` hook that
`server/app/api/__init__.py` calls for every router module.

### Cross-site request guard (always on)

Independently of `EW_REQUIRE_TOKEN`, the same middleware refuses (403,
`cross_site_request`) a state-changing request without an `Authorization` header
when `Sec-Fetch-Site: cross-site` is sent, or when `Origin` (if absent: `Referer`)
names another origin than the request's `Host` (default ports ignored; `Origin:
null` is refused). Reverse proxies can list public origins in `EW_TRUSTED_ORIGINS`
(comma-separated, e.g. `https://evidence.example.org`). Requests without these
headers (CLI, curl, scripts, the Android client) and bearer requests are unaffected:
a cross-site page cannot send `Authorization` without a CORS preflight, which this
server never grants.

Migration notes for an existing installation:

1. Create a token for each client (`token-create --label "Pixel 7"`), deliver it
   out of band, and configure it in the client (the Android app has no token
   setting in this revision; keep `EW_REQUIRE_TOKEN` off until it does or
   front the service with a proxy that injects the header).
2. Start the service with `EW_REQUIRE_TOKEN=1`; check `/integrations` for
   rejected attempts (`token_rejected` audit rows name the `token_key`).
3. Rolling back is unsetting the variable; tokens stay valid for the
   `/api/read/v1/*` routes either way.

## Routes

| Route | Scope | Result |
|---|---|---|
| `GET /api/read/v1/contract` | read | Contract descriptor: endpoints, limits, error model, exclusions, time semantics. |
| `GET /api/read/v1/search?q=&limit=` | read | FTS page with `has_more`/`complete` (same semantics as [search](SEARCH_CONTRACT.md)). |
| `GET /api/read/v1/timeline?from=&to=&event_type=&phone=&direction=&thread_id=&artifact_id=&source_id=&limit=&cursor=` | read | Event window with keyset cursor and recorded `event_times` candidates. |
| `GET /api/read/v1/artifacts/{id}` | read | Artifact metadata, source, acquisition observations, content group (same sha256), transcript versions, counts. |
| `GET /api/read/v1/artifacts/{id}/citations/{anchor_id}/packet` | read | Byte-identical `ageds.citation-evidence/v1` packet ([exchange](EVIDENCE_EXCHANGE.md)). |
| `GET /api/read/v1/reports` | read | Report register (newest first, at most 500, `has_more`); private output paths withheld ([reports](REPORTS.md)). |
| `GET /api/read/v1/graph/export?from=&to=&include_text=&max_nodes=&max_edges=` | read | `ageds.graph-export/v1` (see adapters doc). |
| `GET /api/read/v1/intents/schema` | read | `INTENT_SCHEMA` (JSON Schema 2020-12). |
| `POST /api/read/v1/intents/execute` | read | Anchored items for one intent; never prose. |
| `POST /api/read/v1/intents/interpretive/validate` | read | Labelled interpretive answer with `unsupported` flags; nothing stored. |
| `POST /api/integrations/v1/proposals` | propose | Annotation proposal stored as `proposed`. |
| `GET /api/integrations/v1/proposals?status=` | read | The caller's own proposals (by `client_id`). |
| `POST /api/integrations/v1/hypotheses` | propose | Relation hypotheses quarantined; `links_written` is always 0. |
| `GET /api/integrations/v1/hypotheses?status=` | read | The caller's own hypotheses. |

Human decisions (accept/reject proposals, promote/reject hypotheses, revoke
tokens) are on the `/integrations` page and in the CLI (`proposal-accept`,
`proposal-reject`, `hypothesis-promote`, `hypothesis-reject`).

## Limits

| Read | Bound |
|---|---:|
| Search `limit` | 1–200 (default 100); query ≤ 4,096 UTF-8 bytes; 128 tokens; VM budget 2,000,000 steps |
| Timeline `limit` | 1–500 (default 100); `from`/`to` ≤ 64 chars; text filters ≤ 512 chars; cursor ≤ 512 chars |
| Event `subject`/`body` | ≤ 16,384 chars returned, `*_truncated` + full `*_sha256` otherwise |
| `event_times` per event | ≤ 50 (`event_times_truncated`) |
| Artifact lists (observations, content group, versions) | ≤ 200 each with `*_truncated` |
| Citation packet | 2 MiB, 10,000 segments, 1,000 observations (producer limits) |
| Intent items | ≤ 200 per result; ≤ 20 segments per transcript hit; ≤ 10,000 segments scanned per transcript |
| Interpretive answer | prose ≤ 65,536 chars; ≤ 500 claims; ≤ 10,000 known item ids |
| Graph export | ≤ 50,000 nodes and 100,000 edges by default (`truncation` flags); caller may raise to 10,000,000 |
| Hypotheses import | ≤ 100,000 items, 32 MiB |

Every read runs in one `mode=ro` transaction under the SQLite progress budget;
exhaustion is 503 `work_limit` with no partial result.

## Time semantics (timeline window)

The window compares the stored `events.ts_start` **text** lexicographically.
SMS rows carry `+00:00`, WhatsApp rows carry a local wall-clock time without a
zone, so `'2026-03-01 10:15'` (space) sorts before `'2026-03-01T08:00:00+00:00'`
(`T`). The read API does not invent an instant: `display_time` is always
`null`, `display_rule` says `not_applied`, and the recorded `event_times`
candidates (kind, raw, utc, local, `tz_status`, precision, source) are returned
as stored. The display/sort rule between candidates is the timeline area's
versioned projection; clients that need instants must apply it themselves or
wait for that projection. Untimed events sort first when no window is given and
are counted in `untimed_events_excluded_by_window` when one is.

## Response schemas

All objects below are JSON Schema 2020-12 fragments; unlisted properties are
present as described in the prose and may be extended additively.

```json
{"$id": "ageds.read/v1#error", "type": "object", "required": ["detail"],
 "properties": {"detail": {"type": "object", "required": ["code", "message"],
   "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}}}
```

```json
{"$id": "ageds.read/v1#search", "type": "object",
 "required": ["schema", "kind", "query", "limit", "has_more", "complete", "items"],
 "properties": {"schema": {"const": "ageds.read/v1"}, "kind": {"const": "search"},
   "query": {"type": "string"}, "limit": {"type": "integer"}, "has_more": {"type": "boolean"}, "complete": {"type": "boolean"},
   "items": {"type": "array", "items": {"type": "object", "required": ["object_kind", "object_id", "title", "snippet", "rank"],
     "properties": {"object_kind": {"enum": ["artifact", "event", "derived_text"]}, "object_id": {"type": "integer"},
       "title": {"type": "string"}, "snippet": {"type": "string", "description": "plain source text; escape before rendering"}, "rank": {"type": "number"}}}}}}
```

```json
{"$id": "ageds.read/v1#timeline_window", "type": "object",
 "required": ["schema", "kind", "items", "has_more", "complete", "next_cursor", "display_rule", "window_semantics"],
 "properties": {"kind": {"const": "timeline_window"}, "next_cursor": {"type": ["string", "null"]},
   "untimed_events_excluded_by_window": {"type": ["integer", "null"]},
   "items": {"type": "array", "items": {"type": "object",
     "required": ["id", "event_type", "ts_start", "body", "body_truncated", "body_sha256", "event_times", "display_time", "time_status"],
     "properties": {"id": {"type": "integer"}, "ts_start": {"type": ["string", "null"]}, "display_time": {"const": null},
       "time_status": {"enum": ["stored_ts_start_text", "untimed"]}, "metadata": {"type": "object"},
       "event_times": {"type": "array", "items": {"type": "object", "required": ["id", "kind", "raw", "utc", "local", "tz_status", "precision", "source"],
         "properties": {"tz_status": {"enum": ["explicit_offset", "utc_epoch", "assumed_zone", "unknown"]}}}}}}}}}
```

```json
{"$id": "ageds.read/v1#artifact", "type": "object",
 "required": ["schema", "kind", "artifact", "source", "observations", "content_group", "transcript_versions", "counts", "content_bytes_included", "stored_path_included"],
 "properties": {"kind": {"const": "artifact"}, "content_bytes_included": {"const": false}, "stored_path_included": {"const": false},
   "artifact": {"type": "object", "required": ["id", "sha256", "original_name", "mime_type", "size_bytes", "source_locator", "metadata", "content_available"],
     "not": {"required": ["stored_path"]}},
   "content_group": {"type": "object", "required": ["sha256", "other_artifacts", "truncated"]},
   "counts": {"type": "object", "required": ["events", "annotations", "citations"]}}}
```

```json
{"$id": "ageds.read/v1#reports", "type": "object", "required": ["schema", "kind", "available", "items", "has_more", "reason", "output_location_included"],
 "properties": {"kind": {"const": "reports"}, "available": {"type": "boolean"}, "has_more": {"type": "boolean"},
   "output_location_included": {"const": false}, "reason": {"type": ["string", "null"]},
   "items": {"type": "array", "items": {"type": "object", "required": ["id", "report_uid", "report_type", "template_id", "params", "content_sha256", "manifest_sha256", "created_at"],
     "not": {"required": ["output_dir"]}}}}}
```

### Intents (`POST /api/read/v1/intents/execute`)

The request body must satisfy `INTENT_SCHEMA` (`GET /api/read/v1/intents/schema`):

| `kind` | Fields | Items |
|---|---|---|
| `search` | `q` (1–4096 bytes), `limit` (1–200) | `transcript_segment` (anchored), `event` (exact body), `artifact_reference`, `derived_text_reference` (FTS hit that no single segment explains; `anchor: null`, `reason`) |
| `timeline_window` | `from`, `to`, `event_type`, `phone`, `direction`, `thread_id`, `artifact_id`, `source_id`, `limit`, `cursor` | `event` |
| `artifact` | `artifact_id` | `artifact` (metadata projection) |
| `citation` | `artifact_id`, `anchor_id` | `citation` (quote, selector, packet digest, unknowns) |
| `quote` | `artifact_id`, `derived_text_id`, `segment_indices` (contiguous) | `transcript_selection` |

```json
{"$id": "ageds.intent-result/v1", "type": "object",
 "required": ["schema", "mode", "intent", "items", "count", "has_more", "complete", "truncated", "prose"],
 "properties": {"schema": {"const": "ageds.intent-result/v1"}, "mode": {"const": "anchored"}, "prose": {"const": null},
   "items": {"type": "array", "items": {"type": "object", "required": ["item_id", "kind", "verification"],
     "properties": {"item_id": {"type": "string", "pattern": "^(segment|event|artifact|citation|derived_text|quote):[0-9:-]+$"},
       "kind": {"enum": ["transcript_segment", "transcript_selection", "event", "citation", "artifact", "artifact_reference", "derived_text_reference"]},
       "artifact_id": {"type": "integer"}, "derived_text_id": {"type": "integer"},
       "selector": {"type": "object", "description": "exact citations selector; segments kind in this revision"},
       "quote_text": {"type": "string"}, "quote_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
       "start_ms": {"type": "integer"}, "end_ms": {"type": "integer"},
       "audio_verification": {"const": "not_performed"}}}}}}
```

Segment location inside a transcript hit uses the locator rule
`nfkd_strip_marks_strokes_casefold_substring/v1` over the literal query terms
(phrases kept, operators and column filters dropped). It folds at least as much
as FTS5 `unicode61 remove_diacritics 2` so every FTS hit can be located; the
quote itself is always the exact stored segment text and its hash equals the
`quote_sha256` of a citation pinned on the same selector. A hit that no single
segment explains (phrase across segments, title-only match) is returned as a
reference with a reason, never as a guessed quote.

### Interpretive answers (`POST /api/read/v1/intents/interpretive/validate`)

```json
{"$id": "ageds.interpretive-answer/v1#request", "type": "object", "required": ["mode"],
 "properties": {"mode": {"const": "interpretive"}, "provider": {"type": "string"}, "model": {"type": "string"}, "run": {"type": "string"},
   "parameters": {"type": "object"}, "prose": {"type": "string", "maxLength": 65536},
   "item_ids_available": {"type": "array", "items": {"type": "string"}},
   "claims": {"type": "array", "maxItems": 500, "items": {"type": "object", "required": ["text"],
     "properties": {"text": {"type": "string", "maxLength": 4096}, "item_ids": {"type": "array", "items": {"type": "string"}}}}}}}
```

```json
{"$id": "ageds.interpretive-answer/v1", "type": "object",
 "required": ["schema", "mode", "label", "prose", "claims", "unsupported_count", "supported_count"],
 "properties": {"mode": {"const": "interpretive"},
   "label": {"type": "object", "required": ["mode", "provider", "model", "run", "generated_by", "ageds_verified"],
     "properties": {"mode": {"const": "interpretive"}, "generated_by": {"const": "external_client"}}},
   "claims": {"type": "array", "items": {"type": "object", "required": ["index", "text", "item_ids", "unknown_item_ids", "support"],
     "properties": {"support": {"enum": ["referenced", "unsupported"]}}}}}}
```

A claim is `referenced` only when it cites at least one item id and every id is
in `item_ids_available`; otherwise it is `unsupported`. Missing provider, model
or run are recorded as `unknown`. AGEDS verifies references, not truth, and
stores nothing; a client that wants the answer kept submits an annotation
proposal with `mode: interpretive` and the cited item ids as `basis`.

### Proposals (`POST /api/integrations/v1/proposals`)

```json
{"$id": "ageds.annotation-proposal/v1#request", "type": "object", "required": ["artifact_id", "body"], "additionalProperties": false,
 "properties": {"artifact_id": {"type": "integer", "minimum": 1}, "derived_text_id": {"type": "integer", "minimum": 1},
   "kind": {"type": "string", "default": "note"}, "label": {"type": "string", "maxLength": 200}, "body": {"type": "string", "maxLength": 65536},
   "start_ms": {"type": "integer", "minimum": 0}, "end_ms": {"type": "integer", "minimum": 0},
   "basis": {"type": "array", "maxItems": 200, "items": {"type": "string", "pattern": "^(segment|event|artifact|citation|derived_text|quote):[0-9:-]+$"}},
   "mode": {"enum": ["anchored", "interpretive"], "default": "anchored"}, "provider": {"type": "string"}, "model": {"type": "string"}, "metadata": {"type": "object"}}}
```

The same rules as the native annotation API apply (nonblank body, paired range,
a timed proposal needs a transcript version of the artifact; the range is
checked against that version again at acceptance). An interpretive proposal
must cite at least one item id. The response is the stored proposal with
`status: "proposed"`. Acceptance (`/integrations` page or
`proposal-accept ID --by NAME`) inserts the `annotations` row and writes an
`annotation_from_proposal` audit entry with proposal id, client id, token id,
mode, provider, model and basis; the proposal row keeps `annotation_id`.
The `annotations` table itself has no provenance column in this revision; the
proposal row and the audit entry are the provenance record.

### Relation hypotheses (`POST /api/integrations/v1/hypotheses`, `loom-import-hypotheses FILE`)

See [Ecosystem adapters](ECOSYSTEM_ADAPTERS.md#loom-adapter) for the document
schema. `links_written` is always `0` in the import result.

## What this contract does not do

- It does not authenticate the HTML UI, limit request rates or protect the
  database file; run the service on a trusted network and keep tokens private.
- It does not return media or source bytes, and `content_available` is not a
  custody claim. Hash agreement proves byte identity, not authorship or truth.
- It does not resolve display time, merge duplicate counterparties or verify
  acoustic alignment; `audio_verification` stays `not_performed`.
- `annotation_proposals`, `hypothesis_imports` and `relation_hypotheses` travel in
  the metadata package's declared `extension_tables` section (without
  `annotation_proposals.token_id`); `api_tokens` is explicitly excluded as a
  credential table ([reports](REPORTS.md#extension-tables-in-the-metadata-package)).

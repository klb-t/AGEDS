# Ecosystem adapters: ChatADHD and Loom

AGEDS exposes evidence to partner tools through the [read API](READ_API.md)
and takes their results back only through quarantine. Rules A08/A09 from
[architecture rules](ARCHITECTURE_RULES.md) apply: a mechanism is shared only
where semantics agree, every adapter passes a small benefit test, no shared
database, no imposed ontology, explicit data-access scope. Nothing here makes
a model call; providers and models appear only as recorded labels.

## Data access scope per adapter

| Adapter | Reads from AGEDS | Writes into AGEDS | Never flows |
|---|---|---|---|
| ChatADHD (conversation client, `engine/ageds_adapter.py` stub in that repo) | Search pages, timeline windows, artifact metadata, citation packets, anchored intent results; with `propose`: its own proposal list | Annotation **proposals** (`proposed`, human decides); interpretive answers only as proposals with cited item ids | Source bytes, media, `stored_path`, private seed, other clients' proposals, token secrets, model prose into `annotations` without a human |
| Loom (graph/epistemic tool) | `ageds.graph-export/v1` (events, counterparties as hypotheses, transcripts, annotations, citations, links; claims explicitly empty), optional text-free mode | Relation **hypotheses** (`quarantined`; promotion writes one `links` row with audit) | Source bytes, media, identities (counterparties are observed labels), automatic writes into `links`, Loom's claim semantics (not merged) |
| Any client | Contract descriptor, intent schema | Nothing without `propose` | — |

Access is a token with scopes created by the operator (`token-create`); a
client has no access by presence in the ecosystem alone. Client settings
(endpoint, token) live in the client, off by default.

## What flows each way

**AGEDS → ChatADHD.** The client's model parses the user's question into a
typed intent (`search`, `timeline_window`, `artifact`, `citation`, `quote`).
AGEDS validates it against the published JSON Schema and executes it
deterministically; the result is anchored items (artifact id, transcript
version id, selector, exact quote, `quote_sha256`, times) with `prose: null`.
The client renders quotes; its model may write prose only in the separately
labelled interpretive mode (`mode: interpretive`, provider/model/run recorded
or `unknown`, every claim without known item references auto-flagged
`unsupported`).

**ChatADHD → AGEDS.** A proposal (`POST /api/integrations/v1/proposals`) with
body, optional transcript range, cited item ids and mode. It is stored as
`proposed` and shown on `/integrations`; a human accepts or rejects it. The
accepted annotation carries provenance (proposal id, client id, token id,
mode, provider, model, basis) in the proposal row and in `audit_log`.

**AGEDS → Loom.** `loom-export OUTPUT [--from --to] [--no-text]` or
`GET /api/read/v1/graph/export` writes a typed graph. Every node and edge has
`provenance: {source_table, row_id, locator, run_id, epistemic_status, loom}`:

| Node | `epistemic_status` | Loom vocabulary hint (`evidence_class`/`origin`) |
|---|---|---|
| `artifact:<id>` | `observed` | observed / archive |
| `event:<id>` | `parser_output` | derived / system |
| `transcript:<derived_text_id>` (with run tool/model or `unknown`) | `model_output` | derived / archive (derivation: processing run) |
| `annotation:<id>`, `citation:<anchor_id>` (quote stays `model_output`) | `human_annotation` | user / user |
| `counterparty:<16 hex>` (normalized label/address; `identity_claim: false`) | `hypothesis` | inferred / system |
| edge from `links` | `hypothesis` (+ `promoted_from` when a human promoted it) | inferred / system |

`claims` is `{available: false, items: [], reason}`; AGEDS stores no claims.
The mapping is a hint for a Loom importer; the file is **not** a validated
`loom.graph_packet/1` (`loom_packet_compat: unknown`), because that codec needs
Loom's exact entity/claim field sets and content digests, and merging the two
claim models would conflate semantics (A08).

**Loom → AGEDS.** A document `ageds.relation-hypotheses/v1`:

```json
{"schema": "ageds.relation-hypotheses/v1", "origin": "loom", "run": {"tool": "loom", "version": "..."},
 "items": [{"external_id": "h1", "left": "event:12", "right": "event:15", "relation": "same_thread",
            "confidence": 0.7, "rationale": "same counterparty within 2h", "provenance": {}}]}
```

References are addresses (`event:`, `artifact:`, `transcript:`, `annotation:`,
`citation:`) or `{kind, id}`. Items whose references do not resolve are kept as
`rejected` rows (`unresolved_reference`); items that relate to a
`counterparty:` are rejected as `unsupported_kind_counterparty` because a
counterparty is a label hypothesis without a row, not an identity. Everything
else is `quarantined`. `hypothesis-promote ID --by NAME` (or the page) inserts
exactly one `links` row and writes `link_from_hypothesis` to `audit_log`;
`hypothesis-reject` keeps the row. `loom.graph_packet_diff/1` is not accepted;
a converter can be written once Loom's diff shape is pinned (format_compat
`unknown`, same pattern as the UCAF decision in the plan).

## Benefit tests (A09)

| Adapter | Test | Status |
|---|---|---|
| ChatADHD | `test_integrations_intents.py::test_chatadhd_client_benefit_anchored_quote_verifies_against_citation_packet`: a client with a `read` token executes a parsed `search` intent, recomputes `quote_sha256` from the returned text, fetches the citation packet for the human-pinned anchor and finds the same quote, hash, selector and version; no annotation was created and no source bytes crossed. | unit-tested, runtime-executed through FastAPI TestClient |
| ChatADHD | Proposal lifecycle: submit with `propose` scope → `proposed` → human accept via `/integrations` → annotation + provenance audit; reject keeps the row; read-only token gets 403. | unit-tested |
| Loom | Export: every node/edge has provenance and a known epistemic status; counterparties are hypotheses with `identity_claim: false`; claims empty with reason; text-free mode; truncation flags. Import: quarantine, unresolved/unsupported kept as rejected, promote writes one link with audit, reject keeps the row. Real CLI subprocesses for `loom-export` (new file only) and `loom-import-hypotheses`. | unit-tested, CLI runtime-executed |
| ChatADHD repo | `engine/tests/test_ageds_adapter.py` in the ChatADHD worktree: the stub sends the bearer header, refuses to run when disabled, returns anchored items only, verifies a quote hash against a packet, and labels interpretive answers — with a fake transport, no network. | unit-tested there |

Not verified: a live ChatADHD or Loom process talking to a live AGEDS service;
Loom's import of the export file; any real model call.

## Proposals to maximize functionality

Each item: value, cost, risk, and whether this increment implemented it.

| # | Proposal | Value | Cost | Risk | Implemented |
|---|---|---|---|---|---|
| 1 | `quote` intent: anchored quote for explicit `segment_indices` of a version | Lets a conversational client cite a known passage with a verifiable hash without search | small | none | yes |
| 2 | Serve `INTENT_SCHEMA` at `/api/read/v1/intents/schema` | Clients validate locally and generate tool definitions from the contract | tiny | none | yes |
| 3 | Content group in the artifact read (all artifact rows with the same sha256) | Dedup view without the destructive `UNIQUE(sha256, source_locator)` change | small | none | yes |
| 4 | Text-free graph export (`--no-text`: hashes only) | Relation analysis on structure without moving message text to another tool | small | hashes still leak length/equality | yes |
| 5 | Token expiry, `client_id`, per-client listing of proposals/hypotheses | Operators scope access by client and time | small | none | yes |
| 6 | `/integrations` human page with accept/reject/promote and token revocation | Decisions stay human and auditable without a CLI | small | none | yes |
| 7 | Interpretive validation endpoint (labelling without storage) | Clients can show "unsupported" flags before a user believes prose | small | none | yes |
| 8 | Store interpretive answers as labelled `model_output` records with run metadata | Keeps the audit of what a model said about evidence | 1 table + route | prose in the evidence DB may be mistaken for evidence; needs a hard label and exclusion from packages | no — propose after the reports area decides what reports cite |
| 9 | Word-level anchored items (`words` selector) when word timings exist | Finer citations from search | medium | alignment caveat must stay visible | no |
| 10 | Validated `loom.graph_packet/1` emitter as an optional adapter (`unavailable` without the Loom codec) | Direct import into Loom's KnowledgeStore | medium | semantic conflation of claims/confidence | no — mapping hint only |
| 11 | MCP tool-source shim over the read API (stdio JSON-RPC) | Loom consumes AGEDS through its existing `tool_sources` mechanism; every call confirmed by the user per Loom's rules; no C++ | small–medium | protocol version drift | no — recommended next for the ChatADHD side |
| 12 | Artifact/collection-scoped tokens | Share one recording's transcripts with a partner, not the whole case | medium (search index is not scoped; results would need filtering) | leakage through FTS snippets if filtering is incomplete | no — highest-value next step |
| 13 | Change notifications (SSE "evidence changed") | Clients refresh without polling | small | none | no |
| 14 | Rate limiting and retention policy for `token_rejected` audit rows | Hardening on untrusted networks | small | none | no — documented limitation |
| 15 | Extension tables in the metadata package (`api_tokens` without digests, proposals, hypotheses) | Archives keep the decision trail | small, reports area owns `packages.py#extension_tables` | token digests must stay out | no — contract described |
| 16 | Android setting for the local token (EA-AGEDS-008 already asks for an endpoint setting) | Enables `EW_REQUIRE_TOKEN=1` for the phone | small Kotlin change | none | no — Android out of this scope |
| 17 | In-app: Polish/Dutch/English labels on `/integrations` through the workbench i18n catalog | Consistency with the workbench area | tiny once H lands | none | no |

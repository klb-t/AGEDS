# AI chat export importers and logical dedup across exports

Task `AGEDS-TASK-20261016-B` part 1 (stage 2.1 chat importers and the chat
part of stage 1.4). Verification: [receipt](validation/IMPORT_CHAT_2026-10-16.md).

The importers read **ChatGPT** (`conversations.json` and
`conversations-NNN.json` shards) and **Claude.ai** (`conversations.json`)
exports. They keep the whole message tree, separate reasoning/tool content
from answers, record every observed time with its zone status, and track the
same conversation/message across several exports of one account.

Gemini conversations exist in Google Takeout only as My Activity records
(HTML/JSON). Those belong to the communications track (B2); there is no
separate `gemini.py` here. The chatbot identity `gemini` and the event contract
below are available for that importer to join the same timeline lanes.

## Use

```bash
python -m server.app.cli import-chatgpt EXPORT_DIR_OR_FILES... [--export-label L] [--collection ID] [--mapping FILE]
python -m server.app.cli import-claude  EXPORT_DIR_OR_FILE...  [--export-label L] [--collection ID] [--mapping FILE]
python -m server.app.cli chat-dedup-report [--namespace chatgpt|claude] [--conversation ID] [--changes-only] [--limit N] [--offset N] [--output NEW_FILE]
```

Limits are flags too (`--max-conversation-bytes`, `--max-file-bytes`, …; see
below). Each command prints one JSON object; partial coverage is also announced
on stderr. HTTP: `POST /api/imports/chatgpt` and `POST /api/imports/claude`
(multipart `files`, optional `export_label`, `collection_id`) and
`GET /api/imports/chat/dedup-report` (same parameters as the CLI).

Inputs are read-only. A directory contributes the conversation files at its
top level and, for ChatGPT, every other file below it (bounded walk, no
symlinks) as an attachment candidate. Explicitly listed files are classified
by name; nothing outside the given paths is consulted. Uploads are copied to a
private temporary directory and imported under the locator
`upload:<namespace>:<file name>`.

## What is stored

| Layer | Where | Notes |
|---|---|---|
| Preserved bytes | `artifacts` + `source_observations` via `ingest_file` | One artifact per conversation file/shard (source per file locator); referenced attachment files are ingested as raw artifacts too. Parsing reads the content-store copy, never the original again. |
| Logical export | `record_exports`, `record_export_files`, `record_export_parses` | An export is a set of files with a label. Without `--export-label` the label is `derived:<16 hex>` of the conversation files' SHA-256 values, so an unlabelled re-import is idempotent. Each distinct parse result (interpretation, limits, coverage, bounded issues) is kept. |
| Parser observations | `events` | `chat_conversation` per conversation, `chat_message` per ChatGPT node with a message / per Claude message, `chat_message_part` per Claude non-text block or attachment extract. |
| Times | `event_times` | `created`/`updated` (messages and conversations), `start`/`end` (Claude blocks). |
| Logical identity | `logical_records`, `logical_occurrences` | `(namespace, conversation id, record id)` with one occurrence per export/artifact/locator/interpretation. |
| Attachment references | `chat_attachment_refs` | Every reference with its resolution against the files available in that import. |

All new tables (migrations 21–23 in `server/app/schema/records.py`) are
append-only. `add_event` gained an optional caller transaction so one
conversation is written atomically (all its rows or none).

### Event fields

| Column | Value |
|---|---|
| `thread_id` | conversation id as given (`conversation_id`/`id`, Claude `uuid`) |
| `external_id` | `<parser>@<version>/map<mapping version>:<mapping sha256[:12]>:<conversation>/<node or message>[~dupN]`; `~occN` marks a conversation repeated in one file |
| `sender` | role as given (`user`, `assistant`, `tool`, `system`, `human`); non-string roles stay in metadata only |
| `recipients` | ChatGPT `recipient` (e.g. `python`) |
| `direction` | from the mapping's `direction_by_role` (owner perspective; `system` → null) |
| `contact_label` | chatbot lane: `chatgpt` / `claude` |
| `subject` | conversation title in that export |
| `body` | visible text of that node/message/part; bounded at 4,000,000 characters with `metadata.body.truncated`, full length and SHA-256 |
| `ts_start` / `ts_end` | UTC of the `created` (or block `start`/`end`) candidate when the source defines it; otherwise null |
| `confidence` | null (parsing is not a probability of truth) |

`metadata.chat` carries `chatbot`, `conversation_id`, `node_id`/`message_id`,
`role`, `author_name`, `model` (per message), `default_model`,
`content_type`, `content_kind`, `hidden` + `hidden_reasons`,
`conversation_title` and `tree` (`parent`, `children`, `depth`, `in_cycle`,
`on_current_path`, `is_current_node`, `has_message`, `duplicate_index`).
`metadata.locator` has `artifact_file`, `element_index`,
`element_byte_range`, `element_pointer`, `json_pointer`, `duplicate_index` and
the text pointers. The raw node is recoverable exactly with
`chat_common.read_raw(stored_path, element_byte_range, element_pointer, duplicate_index)`.
Raw fields kept in metadata (`raw.message_metadata`, `raw.content` for every
non-plain-text kind, `raw.conversation_fields`, Claude `raw.message_fields` and
`raw.block_fields`) are bounded at 16 KiB each; larger values keep size,
SHA-256 and keys.

`content_kind`: `text`, `multimodal`, `reasoning` (ChatGPT `thoughts`,
`reasoning_recap`; Claude `thinking`), `tool_call`, `tool_output`, `context`
(`user_editable_context`), `error`, `empty` (null content), `unknown`
(unmapped type, raw content kept), `attachment_extract` (Claude
`extracted_content`: text produced by the service, not the file).

The ChatGPT tree keeps **every** node, including branches off the
`current_node` path; the message-less root is a structural logical record (no
event). Claude branches come from `parent_message_uuid`; the export declares no
current leaf, so `on_current_path` stays `null`. Older Claude exports without
the field keep `parent_declared: false` and the list predecessor, not an
inferred parent.

## Interpretation: mapping files and versions

Key paths live in data, not code:
[`chatgpt_mapping.v1.json`](../server/app/importers/data/chatgpt_mapping.v1.json) and
[`claude_mapping.v1.json`](../server/app/importers/data/claude_mapping.v1.json).
Paths are JSON pointers; a `*` token expands every list item (and object value).
Text specs are `"/ptr"` (strings only), `{"each": "/ptr/*", "fields": [...]}`
(per element; `""` is the element) and `{"json": "/ptr"}` (canonical JSON of any
value). The files also hold content-type → kind tables, hidden-flag rules,
role → direction, time formats and placeholder policy, file-name patterns,
the asset-pointer pattern and the attachment file-name match rule.

An interpretation is `parser@version/map<version>:<mapping sha256[:12]>/<normalization>`.
Importing the same file with another mapping (`--mapping`) creates new events
under the new identity; earlier events stay (tested). Payload-shaping bounds
(body length, raw-field size, listed children) are constants of the parser
version, not run options, so a run limit never changes an existing payload.

`content_sha256` hashes a canonical JSON projection (`chatgpt-content-v1`:
role, content type, extracted text with CRLF→LF, asset pointers, attachment ids,
plus the raw content hash for unknown types; `claude-content-v1`: role, text
field, per-block type and text, attachment name/size/type/extracted-text hash,
files). Metadata, status, weight, model and times are not part of it.
Structure is compared as parent + set of children.

## Identity and idempotency

* Artifact identity is the repository's `(sha256, source_locator)`; the CLI
  locator is the absolute file path.
* Re-importing the same file creates no new artifact, event, time candidate,
  logical record, occurrence, export, parse row or attachment reference; each
  event gets an `event_reobserved` audit row and the acquisition itself is a new
  `source_observations` row (an observation of the same bytes, by design).
* A different parser version or mapping is a different event identity, so it
  never trips `add_event`'s payload-conflict rule.
* The same file listed under two export labels shares its events; it gets one
  occurrence per export.
* An export label is bound to one collection; a conflicting `--collection` fails
  before anything is ingested.

## Dedup report

`chat-dedup-report` (schema `ageds.logical_dedup_report.v1`) groups by namespace
and interpretation. Exports are in import order, which is **not** the date an
export was produced. Per conversation: `present_in`, `not_observed_in`, title
variants, `current_node` variants, node-set changes and every occurrence (file,
shard, artifact, event, locator). Per record: `present_in`, `not_observed_in`
(reason `record_not_observed_in_conversation` or `conversation_not_observed`,
with that export's coverage), `content_variants` (hash, exports, event ids),
`structure_variants` (parent, children, exports), `on_current_path` per export
and `changes` (`content_variants`, `structure_variants`, `current_path_changed`,
`not_observed_in_some_exports`, `multiple_occurrences_in_export`).

Absence means "not observed in that export under this interpretation". It is
never reported as deletion: an export can be partial, filtered, produced at a
different moment or parsed with limits. Exports never parsed under an
interpretation are listed as `not_parsed_under_interpretation` instead of absent.

## Time zones

| Source value | Candidate |
|---|---|
| ChatGPT epoch seconds | `utc_epoch`, raw spelling kept exactly (`1700000000.5`) |
| ChatGPT `0` | raw `0`, no UTC (`zero_placeholder_not_projected`, mapping policy) |
| ChatGPT `null`/missing | no candidate; `create_time_missing_or_null` issue |
| Claude ISO with `Z`/offset | `explicit_offset` with that offset |
| Claude ISO without offset | local wall time, `tz_status='unknown'`, `*_without_offset` issue |
| Anything else (text, `1e400`, wrong type) | raw kept, `parse_status='unparsed'`, no UTC |

The local machine zone is never consulted (tested under `TZ=Pacific/Kiritimati`).

## Limits and memory

The top-level JSON array is split by a byte scanner (strings with escapes and
bracket nesting) over a read-only `mmap` of the stored copy; each element is
decoded on its own. Already scanned pages are released (`MADV_DONTNEED`, Linux).
Peak memory is therefore bounded by the largest decoded conversation
(≈ several × `max_conversation_bytes` in Python objects), not by the file size.
Measured: a 126 MB synthetic export (2,313 conversations, 57,825 events)
imported in 45.7 s with a peak RSS increase of 19.1 MB (receipt).

| Run limit (flag) | Default | Effect when reached |
|---|---|---|
| `max_file_bytes` | 8 GiB | file not ingested, `file_too_large` |
| `max_conversation_bytes` | 64 MiB | element skipped, `conversation_bytes_limit_exceeded` with byte range |
| `max_conversations_per_file` | 1,000,000 | scan stops, `conversation_limit_reached` |
| `max_nodes_per_conversation` | 200,000 | first N nodes, `node_limit_reached` |
| `max_input_files` / `max_walk_depth` | 100,000 / 8 | discovery stops / prunes |
| `max_attachment_bytes` | 4 GiB | reference `not_ingested_limit` |
| `max_issues_listed` | 1,000 | list truncated; per-code counts stay exact |

Coverage per file is `complete`, `partial` (some content not interpreted:
limits, truncated or malformed JSON, non-object elements, an unrecognised
conversation shape, a conversation that could not be recorded) or `failed`
(empty file, not an array, UTF-16/32, too large). Informational issues
(cycles, missing parents, unknown content types, duplicate ids, lone
surrogates, …) do not reduce coverage because the data is still recorded with
the issue.

Robustness cases covered by tests: cycles in the parent graph (depth `null`,
`in_cycle`, `descends_from_cycle`; current-path walk terminates), missing
parents/children, parent/child mismatches, duplicate JSON keys (first in file
order is the plain locator, later copies `duplicate_index` 1…), a conversation
repeated in one file, non-object nodes, null content, non-string roles,
unknown content and block types, invalid UTF-8 (replaced, reported), lone
surrogate escapes (U+FFFD in stored text, original hash kept), non-BMP and RTL
text, a 4.2-million-character message, truncated files, a wrong-vendor file
(`conversation_shape_not_recognized`, nothing recorded) and non-standard
`NaN`/`Infinity` (the element is reported malformed).

## Attachments

ChatGPT `metadata.attachments[].id` and every `asset_pointer`
(`file-service://file-…`, `sediment://file_…`) become references. They are
matched against the files of the same import by the mapping rule
`{file_id}(?:[-_.].*)?` on the file name: one match → `resolved` (ingested as a
raw artifact, file role `attachment`), several → `ambiguous` (all ingested and
listed, none chosen), none → `unresolved`. The media type of a `.dat` file is an
extension guess only (signature sniffing is the catalog track's job). Claude
exports contain no attachment bytes: their `attachments`/`files` stay
`unresolved`; `extracted_content` is a separate part event.

## Transformation contract

| Input → result | Kept | Lost | Added | Reversibility / provenance |
|---|---|---|---|---|
| export file → artifact | bytes, SHA-256, acquisition observation | — | export label/collection binding | exact bytes |
| conversation element → events | ids, tree links, roles, models, text, times (raw), bounded raw fields, locator | exact JSON serialization of the event payload; raw fields above 16 KiB (hash kept); body beyond 4 M chars (hash kept) | content kind, hidden flags, direction, UTC projections, issues | raw element recoverable from artifact + byte range + pointer |
| occurrences → dedup report | presence, hashes, structure per export | — | absence as "not observed", variant grouping | recomputed from stored rows; no stored conclusion |

## Contract for other tracks

* Timeline (C): chat lanes by `contact_label`/`metadata.chat.chatbot`; kind via
  `metadata.chat.content_kind`; parallel conversations overlap through
  `event_times` (`created`/`updated` on `chat_conversation`). A display rule may
  hide `chat_message_part` and `hidden` messages, but they exist.
* Communications (B2): a Gemini My Activity importer can emit `chat_message`
  events with `contact_label='gemini'` and register logical records through
  `server.app.records` (`ensure_export`, `add_occurrence`) if a stable record id
  exists in that format.
* Reports/packages (F): the new tables are not in `packages.TABLES`; they need an
  explicit extension declaration to be exported.

## Prompt vs repository invariants

* The prompt lists "deletions" among cross-export differences; the report says
  "not observed in export X" because absence does not prove deletion.
* The prompt asks for the full raw node in metadata; it is bounded instead, and
  the exact node is recoverable from the preserved artifact and its locator.

## Not done

* ZIP export input (unpack first or use the catalog's archive locators), a
  browser view of the report, Gemini conversation exports, signature-based media
  types for attachments, and `projects.json`/`users.json` contents (listed as
  candidates, not parsed).
* Real exports were not available; the formats follow observed public export
  layouts and are encoded as editable mapping files.

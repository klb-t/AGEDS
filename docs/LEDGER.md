# Ledger: bank statements, transport and health/fitness records

Task `AGEDS-TASK-20261017-L3` of the [digital traces wave](plans/DIGITAL_TRACES_2026-10-17.md).
Code: `server/app/ledger/` (exact money, text decoding, format-driven dates, append-only store,
health mechanics, queries), importers `server/app/importers/{tabular_profiles,camt053,mt940,health_apple,health_google_fit,health_samsung,health_fitbit}.py`
with their data files, schema version 120 (`server/app/schema/ledger.py`), routes
`server/app/api/ledger.py` (page `/ledger`, nav „Rejestry”), CLI `server/app/commands/ledger.py`,
import-plan adapters `server/app/importplan/adapters_ext/ledger.py` and detector rules
`server/app/importplan/data/detectors.d/ledger.v1.json`. Receipt:
[LEDGER_2026-10-17](validation/LEDGER_2026-10-17.md).

## 1. Common contract

| Rule | Implementation |
|---|---|
| Acquisition | Every input goes through `evidence.ingest_file` (`comm_common.acquire`, optional `collection_id`); parsing reads the stored copy. Originals are read only (tests hash the corpus before and after). Uploads are private temporary copies (`temporary_copy_acquisitions`). |
| Layers | Source rows are parser observations. Provider inferences (sleep stages, workouts, a transit operator's assembled journey, a ride app's trip record, daily aggregates) are labelled `provider_inference`; records a person entered by hand in the app are `provider_user_edit`; our own derivations (tap pairing, sleep grouping) carry a versioned rule. Nothing becomes a probability. |
| `metadata.signal` | `{class, basis, provider, device_or_account}` on every event: bank rows and taps `active/raw_sample`; operator journeys and ride trips `active/provider_inference`; tap pairs `active/derived` (rule `ledger.tap_pairing.v1`); sleep `passive/provider_inference`; workouts `active/provider_inference`, manual entries `declared/provider_user_edit`, Fitbit auto-detected `passive`. |
| Exact numbers | Amounts are decimal **text** (`amount_exact`, scale as written), ISO currency, and integer minor units computed by code with the ISO 4217 exponent ([data](../server/app/ledger/data/currencies.v1.json)); unknown exponent → minor units `null`, never guessed. `amount_exact` is signed for the account holder (negative = money out). Health values keep their text (`value_raw`, `value_exact`); JSON numbers are parsed with `parse_float=str`. Floats never enter a stored value; XLSX/XLS numeric cells (which the reader returns as floats) are flagged `amount_from_spreadsheet_float_cell_repr`. |
| Time | Every timestamp is a `TimeCandidate` in `event_times`. The `timeinfo` kind list is closed, so the source's date kind is kept as `metadata.date_kind` and mapped: `transaction_date`/`check_in`/`tap`/`start` → `start`, `booking_date`/`request` → `created`, `value_date` → `other`, `check_out`/`end` → `end`. Dates are `precision='day'`, wall clocks without an offset stay `unknown`; an explicit offset (`+0100`, `Z`, `DtTm` with offset) is `explicit_offset`. `events.ts_start/ts_end` hold only source-fixed UTC instants. |
| Identity and idempotence | One record per source position (table row, `Stmt[n]/Ntry[k]`, `msg[n]/61[k]`, `Record[n]`). Identical rows at different positions are **different records** (both kept); `content_key` + `content_occurrence` is a deterministic aid for later comparison of overlapping exports, never a merge. Re-importing the same file under the same reading reuses every event, candidate and row (`row_sha256` must match; a changed interpretation under the same parser version is an `IntegrityError`). A different reading (profile, encoding, delimiter, date-order or decimal policy, MT940 year base, sleep gap) has another `reading_key` and becomes a new result next to the old one. |
| Bounds | `CommLimits`: `max_records` (rows, entries, health elements), `max_json_bytes` (whole-file reads; a CSV over the bound is cut at a line and marked partial), `max_raw_chars` (raw cells/records), `max_issues`. Reaching a bound makes `coverage.complete=false`. |
| Audit | One `audit_log` row (`import_run`) per run with importer, version, options (including `enable_health`), counts, coverage and issue counts. |
| Search and dossier | Each event is indexed like any event. Since 2026-10-27 its search row also carries the parsed amount (signed and unsigned), currency, flow, counterparty and account of its ledger rows, or the sample types and units of a health session. The text is appended once and stays behind the event's `read_text` gate. A dossier event item can pin one row by `ledger_entry_id` / `health_sample_id` and its `row_sha256` ([hypotheses](HYPOTHESES.md)). Events imported before that date are searchable by their body and title only. |

## 2. Generic tabular profiles (`tabular_profiles.py`)

Profiles are data: [`tabular_profiles.v1.json`](../server/app/importers/data/tabular_profiles.v1.json)
holds **examples only** with neutral ids (PL-style and NL-style bank CSV, a debit/credit-column
bank table, transit journeys per row, transit taps per row, ride-hailing trips). None is verified
against, or claims to match, a bank's, operator's or app's export. Copy one into a private file and
pass `--profiles`. The header and day/month mechanics of the operator-billing profiles were moved
here unchanged (`_norm`, `_resolve_columns`, `_date_parts`, `_time_parts`, `infer_date_order`);
`billing.py` imports them and its version-1 output is byte-identical
(`test_ledger_billing_golden.py`, digest from base `790d386`).

| Profile field | Meaning |
|---|---|
| `record_kind` | `financial_transaction`, `travel_segment` (journey per row), `travel_taps` (tap per row), `ride` |
| `read.encoding` | `auto` (hypothesis) or `utf-8`, `utf-8-sig`, `utf-16`, `cp1250`, `iso-8859-2`, `cp1252`; `encoding_preference` breaks ties |
| `read.delimiter` | `;`, `,`, `\t`, `|` or `auto` (each candidate is tried until the header matches; recorded as `header_match_under_hypothesis`) |
| `read.header` | `names` (header row found within `header_search_rows`; lines before it are the preamble, kept raw in the result) or `none` with `skip_lines` and 1-based `{"column": n}` positions |
| `columns` | field → header aliases (NFKC, case-folded, whitespace-normalized) or `{"column": n}` |
| `times` | named time points: `kind`, `date`/`time`/`datetime` fields, explicit `formats`/`date_formats`/`time_formats` ([tokens](../server/app/ledger/timeparse.py)) or a day/month `order` (`day_first`, `month_first`, `infer_from_file`); `rollover_from` (a check-out earlier than the check-in on one date is the next day, flagged); `times_by_row_kind` for rows of another kind (a top-up row in a transit history) |
| `number` | `decimal` (`,`, `.`, `auto`), allowed `thousands` separators (groups of three are checked), `negative` notations (`leading_minus` incl. U+2212, `trailing_minus`, `parentheses`) |
| `amount` | `mode`: `signed`, `debit_credit` (two columns; both non-zero → conflict, nothing derived) or `indicator` (`indicator_map`, e.g. `Af`/`Bij`, `D`/`C`; a sign that contradicts the indicator → no amount); `positive_is: debit` for fare columns; `default_currency` (recorded as `profile_default_currency`) |
| `assumed_zone` | optional: adds a separate `assumed_zone` candidate (rule `tabular_profile:<id>`), never a replacement |
| `two_digit_year_base` | century for `YY`; without it a two-digit year gets no candidate |

**Encoding hypotheses** ([`textio.py`](../server/app/ledger/textio.py)). A BOM decides; valid non-ASCII
UTF-8 decides; otherwise every single-byte candidate is decoded and scored (letters inside words for,
symbols inside words and C1 controls against). Candidates whose text equals the chosen one are
reported as equivalent; a tie with different text is `ambiguous` and broken by the profile's
preference. An explicit encoding is strict (no replacement characters) and its failure is named in
the error. Decoding is recorded on every table result.

**Amount inference.** With `decimal: auto` a file-level decision is taken only from values that one
reading alone accepts (`1,5`, `1.234,56`); an undecidable value such as `1,234` keeps both readings and
no amount.

**Transit.** Journey rows give a `travel_segment` with both candidates and a duration
(`utc_difference`, or `wall_clock_difference_zone_unknown` with a DST caveat); a row with only one
endpoint is a `travel_tap` (`pairing.reason` `missing_check_out`/`missing_check_in`). Tap rows are
paired by rule `ledger.tap_pairing.v1` (per card, chronological; an entry pairs with the next exit
within `max_pair_gap_s`, default 18 000 s); unpaired taps stay explicit with a reason
(`next_tap_was_check_in`, `no_preceding_check_in`, `gap_exceeds_max_pair_gap`, `tap_type_unmapped`,
`time_not_comparable`). **Rides:** request (`created`), begin (`start`), dropoff (`end`); distance as
written plus exact metres when the unit is stated by the profile or the header (`1 mi = 1609.344 m`);
a contradictory `+0100 UTC` label is flagged; trips without begin/dropoff are `ride_request` events.

**Bank tables** also check consecutive `balance_after` values (forward or newest-first order) and
report inconsistencies (`running_balance_check`, never corrected); counterparty account numbers are
kept raw with an ISO 13616 mod-97 observation (`checksum_valid`/`checksum_invalid`, not a claim that
the account exists).

## 3. CAMT.053 (`camt053.py`) and MT940 (`mt940.py`)

**CAMT.053**: streaming expat reader ([`xmlstream.py`](../server/app/ledger/xmlstream.py)), any
`camt.053.001.xx` namespace by local names (recorded). Per `Stmt`: account (`IBAN` or `Othr/Id`),
stated balances (`OPBD`, else `PRCD`, as opening; `CLBD` closing), `TxsSummry` (count/sum check);
per `Ntry`: `Amt@Ccy`, `CdtDbtInd` (the booking direction, also for reversals; `RvslInd` kept), status
(`BOOK`/`PDNG`/`INFO`, version-2 plain or version-8 `Sts/Cd`), `BookgDt`/`ValDt` as `Dt` (day,
zone unknown) or `DtTm` (explicit offset when present), references, bank transaction code and all
`TxDtls` (related parties incl. the v8 `Pty/Nm` form, remittance). A batch entry with several
`TxDtls` keeps them all and assigns no counterparty. Documents with DOCTYPE/ENTITY declarations are
refused; other ISO 20022 messages (e.g. camt.054) are refused as not CAMT.053.

**MT940**: fields per message (SWIFT `{1:}{2:}{4:` envelopes and `-}`/`-` terminators tolerated;
a new `:20:` after statement lines starts the next statement). `:61:` grammar: value date `YYMMDD`,
optional entry date `MMDD`, mark `C`/`D`/`RC`/`RD` (`RC` reduces and `RD` increases the balance),
optional funds code (checked against the statement currency's third letter), amount with a decimal
comma (`1000,` is valid), type, references and supplementary details. The **entry date has no year**:
it takes the nearest of the value date's previous, same and next year; crossing a year boundary is
flagged; when the nearest reading is more than 92 days away or a second reading is within 31 days of
it, both readings are kept as separate candidates and flagged. `two_digit_year_base` (default 2000) is
a recorded reading parameter. `:86:` is kept raw; numbered subfields (`?20`, `<20`) or slash codes
(`/NAME/…/REMI/`) are parsed with the example hints in
[`mt940_details.v1.json`](../server/app/importers/data/mt940_details.v1.json).

**Balance check** ([`bank.py`](../server/app/ledger/bank.py)): stated opening + signed sum of booked
entries vs stated closing, exact decimals, computed by code; `match`, `mismatch` (with the exact
difference) or `not_checkable` (missing balance, differing currencies, or booked entries left out —
unparsed amount or another currency). Not-booked entries are excluded and counted. Nothing is
corrected (`corrected: false`).

## 4. Health and fitness (special category data)

Every health importer **refuses to run and acquires nothing** without `enable_health=True` for that
run (CLI `--enable-health`, HTTP form/JSON flag, import-plan opt-in); the opt-in is recorded in the
run audit and on every session (`metadata.opt_in`). There is no network code. Samples go to
`health_samples`; sessions are `health_session` events. Mappings are example data files.

| Importer | Input | Notes |
|---|---|---|
| `health_apple` | `export.xml` or the export zip (member streamed, not extracted) | expat streaming with bounded memory (tested: 4× input, peak memory not doubled); Apple's internal DTD is accepted (ELEMENT/ATTLIST only; ENTITY, NOTATION, SYSTEM/PUBLIC refused; ATTLIST defaults never appear as data). `Record` → sample (offsets such as `+0100` kept, DST spans exact in UTC); computed types are `provider_inference`, `HKWasUserEntered` → `provider_user_edit`. Sleep samples are grouped per source **and** device by rule `ledger.sleep_session.v1` (`sleep_session_gap_s`, default 3600): two devices' overlapping nights stay two sessions. `Workout` → session with declared duration and span, statistics/events/routes linked. `ActivitySummary` → daily samples (day, zone unknown). `Me` is not imported. |
| `health_google_fit` | Takeout `All Sessions/*.json`, `Daily activity metrics.csv`, per-day `YYYY-MM-DD.csv` | session + segments + aggregates; declared vs span duration compared; latitude/longitude columns are **not** imported (location belongs to the opt-in location importers); interval files take the date from the file name (`date_basis: file_name`). |
| `health_samsung` | per-table CSV (first line names the table) | `time_basis` is explicit in the mapping: `utc_wall_clock_with_offset_column` (default example) reads the clock as UTC and `time_offset` (`UTC+0100`) as local offset; an unparsable offset leaves the UTC instant with `tz='UTC'`; unmapped tables are acquired and reported. |
| `health_fitbit` | `sleep-*.json`, `exercise-*.json` | times without zone (`unknown`); stage ends are projections `start + seconds`; exercise start `MM/DD/YY` read with the mapping's recorded `two_digit_year_base`; end = start + duration (projection); `logType` maps to class/basis. |

## 5. Time budget

Sessions and travel segments are evidence of **provider-inferred durations**: in the budget
([TIME_BUDGET](TIME_BUDGET.md) §4) an event whose `metadata.signal.basis` is in
`inferred_duration_bases` (default since `time_budget.v5`: `provider_inference`,
`provider_user_edit`, `derived`) contributes its span to the probable variant, never to the lower
bound, unless the run sets `inferred_duration_layer: lower`. Manual entries (`provider_user_edit`)
are self-reports, not independently confirmed time, so they are estimates by default; a run that
treats them as declarations removes that basis from the list (recorded in the run). The L3 branch
had them confirmed by default without a rule version; the integration chose the conservative
default (decision 2026-10-21, reversible per run). Category rules can use
`event_type` (`health_session`, `travel_segment`, `financial_transaction`) and the new kind
`signal_basis`. Fitbit and other zone-less sessions are excluded as unknown-zone unless the run names
an `assume_zone` with an assumption rule. Shared edit (E1): `analysis/source.py` exposes
`item.signal`; `analysis/budget.py` and `budget_defaults.json` add the two parameters;
`analysis/categories.py` adds `signal_basis`. Events without `signal` are computed exactly as before.

## 6. Schema (version 120, append-only)

| Table | Content |
|---|---|
| `ledger_statements` | statement key, account, stated opening/closing (raw + exact), entry count, `balance_check` and its computation, locator |
| `ledger_entries` | event id, record kind, parser/version/profile/reading key, source position, exact amount, currency, minor units and exponent, flow, counterparty name/account, description, duration, content key/occurrence |
| `health_samples` | provider type and class, value raw/exact, unit, start/end raw/UTC/local/zone status, precision, source/version/device raw, basis, session event |

All three are exported in the metadata package (`packages.EXTENSION_TABLES`); the package is private
metadata and contains these special-category rows when health data were imported.

## 7. Use

```bash
python -m server.app.cli ledger-profiles [--profiles private.json]
python -m server.app.cli ledger-import statement.csv [--profile ID] [--profiles private.json] [--encoding cp1250] [--delimiter ';']
python -m server.app.cli camt-import camt053.xml
python -m server.app.cli mt940-import statement.sta [--two-digit-year-base 2000] [--encoding cp1250]
python -m server.app.cli health-import export.zip --enable-health [--format auto|apple|google-fit|samsung|fitbit] [--sleep-session-gap-s 3600]
python -m server.app.cli ledger-entries [--artifact-id N] [--mask-accounts]
python -m server.app.cli ledger-statements | health-sessions
```

Common options: `--label`, `--collection-id`, `--source-locator`, `--max-records`.
HTTP: `POST /api/ledger/import/{tabular|camt053|mt940|health}` (upload) and `…/path`
(`{rootId, relativePath, …}` inside `EW_SCAN_ROOTS`); `GET /api/ledger/{overview,profiles,entries,totals,statements,health/sessions}`
(`entries?mask_accounts=true`). 403 without the health opt-in, 409 conflict, 422 invalid input. The
page `/ledger` masks account numbers for display by default (`?mask=0` shows them raw; stored values
are always raw). Import plan: rules propose `ledger_tabular` (profile chosen at approval), `camt053`,
`mt940` (file names `*.sta|*.940|*.mt940|*.swi` or statement-like `.txt`; the SWIFT envelope makes the
catalog sniff such files as JSON) and the four health importers (blocked until `enable_health`); a
private profile file is the plan option `ledger_profiles_path`.

## 8. Transformations (A03)

| Input → result | Preserved | Lost | Added | Reversibility |
|---|---|---|---|---|
| table → entries/events | raw cells and record (bounded), row/line locators, preamble lines, decoding hypothesis | cell formatting; XLSX numeric text (float repr) | exact amounts, minor units, candidates, flows, pairs, content keys | re-parse with another profile/reading (new result) |
| CAMT.053 → statements/entries | raw `Ntry` subtree (bounded), stated balances | XML serialization details | signs, candidates, checks | re-parse from the stored XML |
| MT940 → statements/entries | raw `:61:`/`:86:` text, non-entry fields | — | entry-year readings, parsed `:86:` structure | re-parse with another year base |
| health export → samples/sessions | type, value text, unit, raw times, source/device text, metadata entries (bounded) | `Me` characteristics, ECG/route files (route paths listed) | sessions by versioned rules, spans | re-parse with another gap/mapping |

## 9. Decisions and conflicts

* Time kinds stay the closed `timeinfo` list (shared contract of F0/C); the bank date kind is
  `metadata.date_kind`. A display rule version could rank `booking_date` vs `value_date` explicitly.
* Fares and ride amounts use `positive_is: debit` so every `amount_exact` has one sign convention.
* Daily aggregates (Google Fit, Samsung, Apple `ActivitySummary`) are `provider_inference` (the
  contract has no separate aggregate basis).
* The prompt asks for health samples "not one event per sample": sessions are events, every sample
  (including sleep stages) is a row; a session links its samples.
* Detector rules use the existing probe kinds only (`text_markers`, `json_keys`); the `xml_root`
  probe does not see past an internal DTD subset (Apple exports), so the Apple rule matches the text
  `<HealthData`. A generic probe hook in `importplan/detect.py` would allow header matching through
  the profiles themselves (contract proposal, not implemented).

## 10. Not done / boundaries

* **No real exports were available**: every layout follows public observations; all profiles,
  detector markers and mappings are examples. Check them against a real export (changes are data).
* XLS/XLSX numeric amounts come from the spreadsheet reader as floats (flagged); dates in typed
  cells are trusted as written by the spreadsheet.
* Apple ECG CSVs, workout-route GPX files (L1's area), `export_cda.xml` and clinical records are not
  parsed; Fitbit heart-rate/steps minute files, Samsung tables not in the mapping and Google Fit raw
  data points are reported, not interpreted.
* No currency conversion, no merchant categorization, no reverse geocoding of stops or cities.
* The import-plan parameter `profile_id` of `ledger_tabular` is validated when the item runs, not
  when the plan is decided (billing has a plan-time check in `plan.py`).

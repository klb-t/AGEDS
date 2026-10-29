# Export format variants through the owner's upload path (2026-10-30)

**All fixtures are synthetic.** They are built in
[`server/tests/test_real_export_formats.py`](../../server/tests/test_real_export_formats.py) and
modelled on publicly documented or commonly observed layouts: WhatsApp "Export chat" on
Android and iOS in several locales, Google Takeout Mail, and `.eml` files saved by Outlook and
Thunderbird. No real export, name, number, address or message was used or read. Every name and
address in them is invented (`example.invalid`).

Path exercised for every variant: `POST /start/upload` (TestClient, owner token) → catalogue
scan → import plan → bulk approve → `POST /api/import-plans/{id}/runs` (sync) →
`owner.keep_originals`. The test then asserts what was detected and what was imported.

## Results

"Before" is the behaviour at the worktree base (`a598f9e`). Each fix has a regression test of
the same name.

### WhatsApp line formats

| Variant | Before | After / fix |
|---|---|---|
| Android NL `14-10-2026 10:42 - Name: text`, `.txt` alone named `WhatsApp-chat met X.txt` | **not detected** (`no_detector_matched`); in a zip it was found | Detector chat-name pattern accepts `WhatsApp-chat met` (hyphen). Times parse; day-first is now confirmed by the file (`14`). `test_android_nl_dash_24h_as_plain_txt` |
| Android PL `29.10.2026, 21:41 - `, `.txt` alone named `Czat WhatsApp z X.txt`, `<Pominięto multimedia>`, `(załączono plik)` | **not detected** (`structure_probe_did_not_match`) | Chat-name pattern accepts `Czat WhatsApp z` (and FR/ES/PT forms). 3 messages timed; omitted and missing media references. `test_android_pl_dot_24h_…` |
| Android EN `29/10/2026, 21:41 - `, system message without sender, multi-line body with an empty line, `<Media omitted>` | ok | unchanged; sender `None` for the system line, body joined with line breaks, `omitted_by_export`. |
| Android `29.10.26, 21:41 - ` (two-digit year) | untimed `unknown_century` | unchanged by design: the century is not assumed. |
| US 12h `10/29/26, 9:41 PM - ` with U+202F before PM | untimed, but reported `invalid` (read day-first: month 29) | **Parser `whatsapp_txt` v3**: the date order comes from the file (a second field above 12 shows month-first). Now `unknown_century`, `date_order: month_first`. `test_us_12h_month_first_…` |
| Month-first with four-digit year `10/29/2026, 9:41 PM` | **invalid or wrong**: day-first applied (`11/3/2026` became 11 March, flagged only as ambiguous) | v3 reads month-first: `2026-10-29T21:41`, `2026-11-03T09:42`. `test_month_first_four_digit_year_…` |
| 12h with `p.m.`/`a.m.` (day-first, four-digit year) | ok | unchanged; `12:05 a.m.` → `00:05`. |
| A file with both a first field and a second field above 12 | day-first applied; the month-first lines reported `invalid` | v3: every line `date_order_conflict`, untimed. `test_conflicting_date_orders_…` |
| BOM at file start and U+200E before lines | ok | unchanged (detector line pattern now also allows several marks / U+200F). |
| iOS `[29.10.26, 21:41:05] Name: ` with `‎<attached: …>` in a zip with the photo | ok, untimed (two-digit year) | unchanged; reference `present` with a child artifact. |
| iOS US `[10/29/26, 9:41:05 PM]` | untimed, reported `invalid` | v3: `unknown_century`, `month_first`. |
| iOS PL `[29.10.2026, 21:41:05]` with BOM and `<załączono: …>` | ok | unchanged; both messages timed, media `present`. |

All WhatsApp times are phone-local as written, `timezone_status: unknown`. No zone is assumed.

### WhatsApp export layouts

| Variant | Before | After / fix |
|---|---|---|
| `(file attached)` EN, `(bestand bijgevoegd)` NL, `(załączono plik)` PL, `<Media weggelaten>`, a named file that is not in the zip | ok | unchanged: 3 `present`, 1 `omitted_by_export`, 1 `missing_from_export`. |
| Zip with the chat `.txt` and media inside a subfolder | ok (a directory unit inside the archive) | unchanged; media `present`. Needs a chat-like file name (see below). |
| Several chat `.txt` files uploaded in one batch (Android "without media" exports) | **blocked**: one unit, `parameter_missing:chat_member`; at most one chat could ever be imported | Plan no longer blocks; the `whatsapp_export` adapter imports each detected chat in turn, and passes the other chat texts as `other_chats` so they are never acquired as media of a chat. `test_several_chat_texts_uploaded_together_…` |
| A zip holding two chat texts | **blocked** as above | both imported; choosing `chat_member` still imports only that one. `test_zip_with_two_chats_…` |
| Re-import of a chat recorded by parser version 2 | — | keeps version 2 (no payload conflict). `test_a_chat_imported_by_version_2_keeps_version_2` |

### Gmail Takeout and `.eml`

| Variant | Before | After / fix |
|---|---|---|
| Takeout split into `takeout-…-001.zip` (mbox + `archive_browser.html`) and `-002.zip` (Drive only) | ok | unchanged: one `email_mbox` item with context `google_takeout`; both zips and the Drive file are kept as originals. |
| mbox with CRLF line endings | ok | unchanged: 2 messages, subjects and labels, no issues. |
| `X-Gmail-Labels` raw UTF-8 (`Ważne`, `"Sąd, pisma"`, `Kategoria: Społeczności`) | ok | unchanged. |
| `X-Gmail-Labels` as RFC 2047 encoded-word (`=?UTF-8?Q?Wa=C5=BCne?=`) or folded over two lines | **mis-parsed**: label kept as `=?UTF-8?Q?…?=`; a folded label kept a line break | **Parser `email_rfc5322` v4**: unfold, decode encoded-words, then split. Raw value still kept. A mailbox already recorded by v3 keeps v3. `test_non_ascii_gmail_labels_…`, `test_a_mailbox_imported_by_version_3_keeps_version_3` |
| Very long header lines (400 `References` ids, a 20 000-character header, 500 labels, one 1.5 MiB header line) | ok | unchanged: both messages imported; strings and lists bounded with `truncated` marks; `header_complete: false` when the header block exceeds `max_header_bytes` (1 MiB). |
| Mailbox with zero messages (empty mbox in a Takeout zip, a second empty label mbox, a bare empty `.mbox`) | ok | unchanged: three items done, 0 events, each run audited with `messages: 0`, coverage complete. |
| Outlook `.eml`: CRLF, UTF-8 Q-encoded `Subject`/`From`, quoted-printable body with a soft line break, `winmail.dat` (`application/ms-tnef`) | ok | unchanged: subject `Zażółć gęślą jaźń`, UTC time from `+0000`, `winmail.dat` extracted as an attachment (TNEF itself is not decoded). |
| Thunderbird `.eml` starting with `From - Thu Oct 29 …` and `X-Mozilla-Status` | **ambiguous**: the From line made it sniff as mbox, so `email_mbox` and `email_eml` were both primary; "approve all" skipped it | Detector table v2: a `.eml` that sniffs as mbox goes to `email_eml` (mbox offered as alternative). `test_thunderbird_eml_…` |

## Fix summary

* `server/app/importplan/data/detectors.v1.json` → table version 2: WhatsApp chat names in
  NL/PL/FR/ES/PT wording, several bidi marks before a chat line, rule
  `eml_with_mbox_from_line`.
* `server/app/importers/whatsapp.py` → `whatsapp_txt` version 3 (file-level date order).
* `server/app/importers/whatsapp_media.py` → `other_chats`; `server/app/importplan/adapters.py`
  and `plan.py` → several chats imported in turn; `templates/importplan.html` says so.
* `server/app/importers/email_mbox.py` → `email_rfc5322` version 4 (label decoding).

## Limits the owner should know before uploading (not changed)

* A loose chat `.txt` is recognised by its name only. Keep the name the phone gave it. Inside
  a zip, any root `.txt` with chat-like lines is found.
* Every other file uploaded in the same batch as a loose chat `.txt` is offered to that chat as
  possible media, and unreferenced ones are recorded as such. Upload recordings separately.
* Two-digit years leave messages untimed. A month-first file whose days are all ≤ 12 cannot be
  told apart from day-first: it is read day-first and every such line is flagged
  `date_order_ambiguous`.
* A system line containing `: ` is read as a message from the text before the colon (format
  limitation of the export; raw line kept).
* Takeout: a member above 4 GiB is not hashed by the catalogue (`max_member_hash_bytes`) and so
  not imported; one mbox import stops at 200 000 messages (`max_records`, coverage marked
  incomplete); the upload limit defaults to 8 GiB per batch (settable on `/settings`). A member
  that compresses better than 200:1 is not expanded (zip-bomb guard); ordinary mail does not
  reach that.
* `winmail.dat` and other TNEF contents are not decoded.

## Checks

Full suite at the commit: 1617 passed, 3 skipped, 2629 subtests passed; `scripts/verify_repository.py` passed. The new file alone:
`server/tests/test_real_export_formats.py` — 25 tests, all passed.

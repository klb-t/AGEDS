# The owner's path in the browser

Everything the owner needs from upload to report, without the CLI (`server/app/owner.py`,
`server/app/api/owner.py`). The pages are owner-only: reads need installation `read_text`,
actions need `administer`, so a recipient gets 403.

| Page | What it does |
|---|---|
| `/start` | States the limits first. Then upload one batch of files: export zips (Takeout mail, WhatsApp), `.mbox`/`.eml`, recordings, images, PDFs. The batch is written to `<data dir>/incoming/<batch>/` (files 0400, never changed), gets a collection (`acquisition_method=browser_upload`), is catalogued read-only and gets an import plan. |
| `/start/batch/<id>` | Refreshes itself every 10 s while a job is queued or running. Five steps: uploaded files with SHA-256; plan summary with a link to `/import` to review, approve and run; files no plan item claims, to keep as originals; originals with their text versions and jobs, plus a button to queue ASR/OCR with the default presets; link to the report. |
| `/report` | The pilot and coverage report from the database: originals and acquisitions; model output labelled as such (model, timing, alignment, superseded versions); costs; mail and chat coverage per import and per month, with gaps; the list of gaps. |
| `/settings` → Recipients | Invite a recipient such as an advocate (account, package of chosen cases as a snapshot or live, grant, optional read-aloud and file access, validity in days). The link is shown once, on a `no-store` / `no-referrer` page, with the token in the URL fragment. Grants and tokens can be revoked; the owner's own tokens cannot be revoked here. Disabled with an explanation while the installation runs `legacy_open`, where grants are not enforced. Same function as the CLI `authz-invite` (`sharing.invite_recipient`). |
| `/settings` | Consents for Vertex AI and Cloud Text-to-Speech (record with their basis, revoke); the provider on or off; the daily budget and today's spend; read-aloud on or off; default ASR and OCR presets (stored as `media.default_recipe.asr` / `.ocr`, ahead of `EW_ASR_RECIPE` / `EW_OCR_RECIPE`). |

The owner is told before use:
* File times on the server are upload times. The browser's `lastModified` per file is kept
  apart in the collection as declared by the browser, not verified.
* Vertex transcription and OCR need a consent and budget; a recording sent inline is limited
  to 15 MB.
* Model output is model output.
* Password-protected archives are kept but not imported.
* Direct Gmail access needs the separate authorisation in [GMAIL_API](GMAIL_API.md).

Upload safety (`UploadSurfaceTests` in `server/tests/test_owner_web.py`):
* **Names:**
  * a stored name is a plain basename: no path parts, `\` and `:` replaced, control and
    format characters (NUL, RTL override) dropped, leading dots removed, NFC-normalized;
  * case-insensitive collisions in a batch get ` (2)`, ` (3)`;
  * files are created with `O_EXCL` and set to 0400, so nothing is ever overwritten.
* **Archives** are never extracted into the batch. The catalog reads them in place; ingest
  writes a member only to a fixed temporary name (`payload`), so `../`, absolute and
  symbolic-link members cannot write outside. A link member is its own bytes, never its
  target.
* **Zip bombs** are cut by the catalog limits: a member over the 200:1 compression ratio (from
  1 MiB) is not hashed, the scan is marked incomplete (`limit_hits`), and the member cannot be
  ingested.
* **Size:** the limit per batch is a setting on `/settings` (preset `EW_MAX_UPLOAD_BYTES`,
  8 GiB). A batch over the limit is removed whole.
* **CSRF:** a cross-site POST, also with Basic credentials, is refused (403) before anything is
  written.

Presets are checked against this installation (`owner.preset_status`, probed locally without
network or inference):
* Vertex presets need an active Vertex consent.
* Local ASR needs faster-whisper.
* Tesseract OCR needs the binary.
* Forced alignment needs torch/torchaudio.

`/settings` disables an unusable preset with the reason, marks a partial one (for example the
hybrid preset without torch: its fill step is skipped), refuses to save an unusable choice and
warns when the current default cannot run. The batch page repeats the warning, and processing
does not queue files whose preset cannot run.

Without a stored choice and with no `EW_OCR_RECIPE`, OCR falls back to the Tesseract preset,
which the production image lacks ([DEPLOY_DEPS](validation/DEPLOY_DEPS_2026-10-30.md)). There the
owner picks the Vertex OCR preset in `/settings` after recording the consent.

Bytes that already have a transcript or OCR text under another acquisition (the same files
uploaded again) are not sent again. The page names the existing version instead.
`scripts/pilot_local.sh serve` starts a worker next to the server, so queued jobs run without
a command; in Docker the `worker` service does this.

Verified on 2026-10-27:
* **Synthetic data, real Chromium:** `server/tests/browser/owner.cjs`, 7 steps — settings,
  upload of a WhatsApp zip with WAV and PNG, plan approved and run on `/import`, originals
  kept, ASR/OCR finished by the worker on a test-only fake Vertex transport, report, phone
  width, no script errors. Backend: `server/tests/test_owner_web.py`.
* **Real pilot files, real Chromium,** on a throwaway copy of the pilot database (deleted
  afterwards):
  * report and settings rendered the real pilot;
  * the 7 real files were uploaded; the plan proposed 7 units (EXIF, recording names), which
    acquire the files, so nothing was left over;
  * the plan ran `completed`;
  * "process" queued nothing, because the same bytes already had text, and no provider call
    was made.

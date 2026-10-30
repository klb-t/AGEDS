# Design-philosophy review, 2026-10-30, and what was fixed

One read-only agent (Sonnet, Explore type: no write or edit tools) read the code at `6d74af6`
and wrote the findings below. They are model output. Line numbers refer to `6d74af6`.

## Status after the fixes (2026-10-30, branch `claude/rzecznik-design`)

Checks at `6b4fc97`, all on synthetic test data:
* `pytest server/tests`: 1635 passed, 3 skipped;
* `node --test`: 137 passed;
* `scripts/browser_tests.sh` in real Chromium: 14 suites, 141 steps.

| Finding | Status | Commit | Test (exact value) |
|---|---|---|---|
| 2.1 bulk transcription re-bills transcribed audio | fixed | `3b7436c` | `test_jobs.py::test_bulk_enqueue_skips_recordings_that_already_have_a_transcript` |
| 5.1 schema forbids `contradicts` | fixed | `3924493` | `test_advocate.py::test_the_response_schema_allows_every_status_the_prompt_asks_for` (fails on the old enum) |
| 2.2 provider switch looks like it stops paid media | fixed: the switch is labelled as the advocate's, and the paid-media state is shown | `d68d804` | `test_owner_web.py::test_settings_says_the_provider_switch_does_not_stop_paid_media` |
| 6.1 expired leases loop for ever | fixed: `EW_JOB_MAX_ATTEMPTS` (3) | `14fc2db` | `test_jobs.py::test_a_job_whose_lease_keeps_expiring_fails_after_the_attempt_limit` |
| 3.1 consent `data_policy` never read | fixed: a consent covers calls only while its policy equals `providers.PROVIDER_DATA_POLICY` | `5e388c3` | `test_providers.py::test_a_consent_covers_calls_only_with_the_transports_data_policy`, `test_owner_web.py` (settings) |
| 2.5 read-aloud is re-billed on every replay | fixed: `advocate_speech` cache (migration 140) | `de87dc6` | `test_voice.py::test_a_replay_is_served_from_the_cache_without_a_second_call` |
| 6.2 queued jobs cannot be cancelled | fixed for queued jobs; running jobs are not cancelled (their call may already be paid) | `6b4fc97` | `test_media_queue.py::test_a_queued_job_can_be_cancelled_and_is_never_claimed`, `test_media_api.py::test_cancel_over_http_and_from_the_queue_page` |
| 3.2 `/settings` on/off drops model, location and project set from the CLI | **confirmed in code, not fixed** (`api/owner.py` `settings_provider` / `settings_voice` write a fixed dict) | — | — |
| all others | **not verified**: check each one against the code before fixing | — | — |

Consequence of 3.1 for an existing installation: any Text-to-Speech consent recorded before
`5e388c3` has `provider_cache: enabled` stored, which no longer matches, and must be granted
again. Vertex consents created on `/settings` or with the CLI still match.

---

## Original report

### Summary

| Cat | Topic | Findings | High | Medium | Low |
|---|---|---|---|---|---|
| 1 | Text, prompts and policy wording in Python | 6 | 1 | 3 | 2 |
| 2 | Silent or unchecked paid or engine fallback | 7 | 2 | 3 | 2 |
| 3 | Settings and env written but not read, or read but not settable | 5 | 2 | 2 | 1 |
| 4 | Fixed choices in code instead of presets | 7 | 1 | 4 | 2 |
| 5 | Closed lists that silently drop or refuse | 5 | 1 | 2 | 2 |
| 6 | States with no way out | 6 | 0 | 4 | 2 |
| 7 | Tests that assert only existence or truthiness | 6 | 0 | 4 | 2 |
| Total | | 42 | 7 | 22 | 13 |

The most important findings, in order:
1. **2.1** `/transcribe/all` and the per-artifact queue can re-bill every audio file on each click.
2. **5.1** The advocate's structured-output schema forbids the "contradicts" status that the system prompt requires.
3. **3.1** The consent `data_policy` is recorded but never read or enforced.
4. **2.2** The `/settings` provider switch does not stop paid ASR or OCR.
5. **3.2** The `/settings` toggles overwrite CLI-set provider and voice configuration.
6. **4.1** Vertex OCR and ASR recipe fields are not applied, while the prompts and some limits are baked into code.

---

### Category 1 – Text, prompts and policy wording hard-coded in Python

**1.1 [medium-high] `server/app/media/vertex.py:36-51` and `server/app/providers.py:40-55, 311-312`**
- What is wrong: the ASR, OCR, STT and advocate system prompts are inline Polish string constants.
- The ASR prompt names the languages "polski, niderlandzki, angielski lub inny". The recipe already has `allowed_languages` and `languages`, and the prompt ignores them.
- The `OCR_CHAT` fragment is also inline.
- Why it matters: the recipe is the stated home for model behaviour, but it holds no prompt. `recipe_sha256` is therefore identical for runs whose behaviour differs after a code edit.
- `describe()` and the call hash record the prompt per run, which partly mitigates this. There is no prompt id or version.

**1.2 [medium] `server/app/advocate.py:446`**
- What is wrong: the summary request to the model is built inline as `f'Streść, co udostępniony materiał mówi o: {topic}'`.
- Why it matters: this is a second hard-coded model prompt, separate from the system prompt, and it is unversioned.

**1.3 [medium] `server/app/providers.py:219-221`**
- What is wrong: the passage labels sent to the model (`transkrypcja`, `tekst`, `wiadomość`, `interpretacja w sprawie`) are hard-coded, and so are the `(brak)` and `Fragmenty:` and `Pytanie:` frames.
- `server/app/static/advocate.mjs:6-18` hard-codes the same claim-status and kind labels again, in JS.
- Why it matters: decision-bearing wording (what "proven" means to the lawyer) exists in two code copies. `server/app/static/i18n/*.json` is the established catalogue and these labels are not in it.

**1.4 [medium] `server/app/api/owner.py:24-30`**
- What is wrong: the nav labels (`NAV`) and the consent provider descriptions (`CONSENT_PROVIDERS`: "Vertex AI (Gemini): transkrypcja, OCR, odpowiedzi rzecznika") are Python literals. `server/app/main.py:34` appends `('/advocate', 'Rzecznik')`.
- Many user-facing Polish error and refusal strings are also inline: `api/owner.py:131-135, 306-345`, `owner.py:93-100`, and `owner.py:288-322` (the `preset_status` reasons).
- Why it matters: `owner_settings.html` hard-codes the provider name ("Vertex AI (Gemini)", line 20) instead of taking it from the provider config.
- The owner pages are Polish by design, so the issue is the split between the template and Python homes, not the language.

**1.5 [low] `server/app/analysis/eventstudy.py:447-537`, `analysis/theses.py:25-50`, `analysis/budget.py:94`**
- What is wrong: the statistical caveat texts (pl and en pairs) sit inside `_warnings` and `_deviation_warning`.
- Why it matters: `server/app/reports/labels.py` and `reports/disclaimers.py` give these a hashed-label home with an id. The caveats are not in it, so they have no label id or sha256.
- The "active" decision for several caveats is also a code constant (see 4.5).

**1.6 [low] `server/app/api/integrations.py:251`, `server/app/catalog/export.py:258`**
- What is wrong: inline HTML pages with Polish text and `lang="pl"` are built in Python.
- Why it matters: the pages are template-shaped content outside `templates/`.

---

### Category 2 – Silent or unchecked paid or engine fallback

**2.1 [high] `server/app/transcription.py:64-68`, `server/app/main.py:208-212, 368-377`, `server/app/templates/index.html:6`**
- What is wrong: `queue_all_audio()` queues ASR for every artifact whose mime starts with `audio/` or `video/`. It does not check for an existing transcript.
- Under the default dedup policy `reuse`, completed jobs never block a new attempt (`server/app/media/queue.py` docstring). One click on "transcribe all" therefore re-submits every file.
- With `EW_ASR_RECIPE=asr-vertex-gemini-aligned` (the `docker-compose.vm.yml` default) that is a paid Gemini call, and possibly a Speech-to-Text call, per file.
- There is no cost estimate and no confirmation.
- The owner path (`owner.process_new`) does skip files that already have text. `/transcribe/all` and `/api/artifacts/{id}/transcribe` do not.
- Why it matters: this is a paid, repeated call with no explicit per-action consent. Only the daily budget stops it.

**2.2 [high] `server/app/templates/owner_settings.html:19-21`, `server/app/owner.py:299-303`, `server/app/media/vertex.py:73-80`**
- What is wrong: `/settings` shows "Dostawca modelu: wyłączony" with a "Wyłącz" button for `advocate.provider`. That setting governs only the advocate.
- Vertex ASR and OCR still run with the preset values whenever a consent exists, even when the provider is "off".
- The only note is a code comment ("the provider switch governs the advocate only").
- Why it matters: the owner can believe the paid provider is off while the batch page keeps spending.

**2.3 [medium] `server/app/providers.py:121`, `server/app/api/owner.py:199`, `server/app/cli.py:189`**
- What is wrong: if `advocate.daily_budget_eur` was never set, the budget silently defaults to 1.0 EUR in three separate places. `/settings` displays "1.0 €" as if the owner had chosen it.
- Why it matters: a paid call is authorised by an implicit hard-coded budget, not by a recorded owner decision.
- There is also no way to return to "unset", only to enter another number (`api/owner.py:306-315`).

**2.4 [medium] `server/app/media/alignment.py:194-206`, `server/app/media/vertex.py:84-90`**
- What is wrong: the `asr-vertex-gemini-chirp` timing reference (Google Speech-to-Text v2, Chirp) sends the audio to a different Google service.
- It is booked as `provider='vertex'` and covered only by the `vertex` consent checked in `bind`.
- `providers.py:303-309` states that Cloud TTS "is another Google service … needs its own consent", but STT v2 gets no such treatment.
- The price conversion `ref.get('eur_per_usd', 1.0)` also ignores the configured `eur_per_usd`.
- Why it matters: the data goes to a second service without its own consent record.

**2.5 [medium] `server/app/main.py:803-818`, `server/app/static/advocate.mjs:115-130`**
- What is wrong: read-aloud is a GET (`/api/advocate/answers/{uid}/speech`) that makes a paid Text-to-Speech call on every request. `Cache-Control: no-store` means each replay is re-billed.
- Why it matters: a GET with a side effect that costs money, with no confirmation, no cache and no cost shown.
- Any reader with `chat_with_advocate` can spend the owner's budget this way. Calls are booked with the recipient's principal, but no per-principal budget is set by default (see 2.7).

**2.6 [medium] `server/app/media/recipe.py:196-215`**
- What is wrong: `default_recipe_id` wraps the settings lookup in a bare `except Exception: pass`. On any DB error it silently falls back to `EW_*_RECIPE`, and then to `legacy` (ASR) or Tesseract (OCR).
- `effective_recipe` provenance (lines ~620-640) records `preset_origin` and hash, but not whether the recipe was requested, taken from the owner setting, taken from the env, or the built-in fallback.
- Why it matters: a switch between engines is not recorded as a switch.

**2.7 [low-medium] `server/app/media/vertex.py:98-105`, `server/app/media/alignment.py:194`, `server/app/providers.py:110-126`**
- What is wrong: all media calls are reserved with `principal_id=None`, so `advocate.daily_budget_per_principal_eur` never applies to them.
- Queuing paid presets needs only the `process` capability (`main.py:208, 368`; `api/media.py:113`), not `administer`. The caller may pick any preset by id (`JobIn.recipe`).
- Why it matters: a non-owner with `process` can drive paid calls against the owner's daily budget.

**2.8 [low-medium] `scripts/pilot_local.sh:30-34`**
- What is wrong: `init` records an installation-wide Vertex consent automatically (basis "R100 owner 2026-10-25; pilot 2026-10-27", `granted_by=cli`). It also sets the budget to `${AGEDS_PILOT_BUDGET_EUR:-1.0}` and makes the Vertex presets the default.
- Why it matters: the script grants consent on the owner's behalf, with the date hard-coded.

---

### Category 3 – Settings and env written but not read, or read but not settable

**3.1 [high] `server/app/api/owner.py:30, 290-293`, `server/app/cli.py:179`, `server/app/providers.py:77-85`**
- What is wrong: `provider_consents.data_policy_json` is written as `{'public': False, 'training': False, 'provider_cache': 'enabled'}`. A grep finds no reader. Nothing in `vertex_transport`, `tts_transport` or the request bodies enforces or even transmits it.
- The values are a hard-coded constant in two places (owner.py and cli.py), not chosen by the owner at consent time. They are also stamped onto the `google_tts` consent.
- Why it matters: the consent record asserts terms (no training, cache on) that the code neither checks nor controls. That is a decision-bearing fact recorded as if it were chosen and enforced.

**3.2 [high] `server/app/api/owner.py:316-330` vs `server/app/cli.py:155-167`**
- What is wrong: `/settings/provider` writes exactly `{'type':'vertex_gemini','name':'vertex'}` and `/settings/voice` writes exactly `{'enabled': True}`.
- This overwrites any `model`, `location`, `project` or `tts_voice` set earlier via `advocate-provider` or `advocate-voice`. The `set_setting` audit entry shows the change but the UI does not warn.
- Why it matters: re-enabling from the web silently changes which model or project is used for paid calls.
- The UI also offers no way to choose model, location, project or voice, so these are "settable only by CLI".

**3.3 [medium] `server/app/api/owner.py:119`, `server/app/templates/owner_batch.html`**
- What is wrong: `vertex_consent` is passed to the batch template and never used. The consent status is therefore not shown on the page that starts paid processing.
- Only `preset_status` is shown, and its consent check ignores scope (see 5.4).

**3.4 [medium] `server/app/config.py:11`, `server/app/db.py:757`**
- What is wrong: `EW_CASE_NAME` is read only to seed the first case at database creation. Changing it later has no effect, and it is not documented as one-shot (`docker-compose.vm.yml:10` sets it as if it were live).
- `EW_SCAN_ROOTS` is likewise env-only, with no setting or UI.
- Why it matters: operator-visible configuration appears live but is not.

**3.5 [low] `scripts/deploy_vm.sh:83, 87`**
- What is wrong: every deploy runs `authz-setting --key http.allowed_hosts --value-json "[PUBLIC_HOST]"` and `--key http.legacy_open_clients --value-json "[GATEWAY/32]"`. These replace the whole list and drop anything the owner added.
- The migration check at line 99 hard-codes `{1,...,6}` and ignores the extension migrations registered in `server/app/schema/__init__.py`.

---

### Category 4 – Fixed choices in code instead of presets

**4.1 [high] `server/app/media/ocr.py:312`, `server/app/media/vertex.py:78-79, 390`, `server/app/media/recipes/ocr-vertex-gemini*.json`**
- What is wrong:
  - OCR passes `max_output_tokens=32768` as a literal, but the OCR recipe has no such field.
  - The Vertex OCR recipe fields `limits.timeout_s`, `limits.max_words`, `limits.max_page_image_bytes` and `pdf.max_pages` (200 or 50) are never read on the Vertex path. They are used only on the Tesseract path, `ocr.py:388-413`.
  - `_config` forces `temperature=0.0` and a timeout of at least 600 s, overriding the stored config (`vertex.py:78-79`).
  - `1300` tokens per page, `32` audio tokens per second and `3.0` chars per token are code constants. `providers.VERTEX_PRESET.chars_per_token` exists and is ignored.
- Why it matters: the recipe states limits that the pinned run does not apply (a 500-page PDF goes whole to the model). That makes the recorded recipe untrue for these fields.

**4.2 [medium] `server/app/media/recipes/asr-vertex-gemini*.json` vs `server/app/media/vertex.py`**
- What is wrong: `allowed_languages: ["pl","nl","en"]` is validated by `recipe.py` and used by the local per-segment strategy. The Vertex strategy never reads it. The prompt names its own languages and the model's `language` field is stored without any check against the list.
- Why it matters: a recipe field that looks like a constraint is not one.

**4.3 [medium] `server/app/providers.py:30-36, 303-310`, `server/app/main.py:792`, `server/app/providers.py:363-364`**
- What is wrong:
  - `VOICE_PRESET` carries `max_audio_bytes` (10 MiB), but the endpoint re-hard-codes `10 * 1024 * 1024 + 1`.
  - The STT call uses a literal `maxOutputTokens: 1000` and `temperature: 0.0`.
  - `history[-6:]` is hard-coded.
  - `VOICE_PRESET['location']` overrides the stored provider's `location`, because it comes later in `{**text.config, **VOICE_PRESET, **voice}` (`providers.py:434`).
  - The UTC day boundary for the budget is fixed.
- Why it matters: the presets exist, and the code paths bypass or override them.

**4.4 [medium] `server/app/media/ocr.py:301`, `server/app/media/ocr.py:218-227`**
- What is wrong: a PDF page counts as "has a text layer" at 20 characters or more. Such pages are taken from the text layer and not sent to the model. The side thresholds (0.45 and 0.55) and `gap_factor=0.8` for the chat-screenshot heuristic are also constants.
- Why it matters: the 20-character threshold decides which text is parser output and which is model output, and it is not in the recipe.

**4.5 [medium] `server/app/analysis/eventstudy.py:82-83, 483`**
- What is wrong: `SEASONALITY_TV_THRESHOLD=0.2` and `DEPENDENCE_CV_THRESHOLD=1.5` decide whether a caveat is flagged as active.
- Why it matters: the other thresholds (`flag_threshold`, `density_ratio_warning`) are in the preregistered params, but these are not. A re-registered analysis cannot vary or record them.

**4.6 [low] `server/app/advocate.py:32-36, 132, 119`**
- What is wrong: `MAX_PASSAGES=8`, `chosen[:3]` per text, a 360-character window, and the Polish stemming rule (`len>5` then `len-3`, with `ł->l`) are code constants.
- Why it matters: the recorded retrieval stores only a passage count. It does not say that results were capped at 8 or 3, so an answer can omit relevant material without saying so.

**4.7 [low] `server/app/media/alignment.py:240`, `server/app/media/alignment.py:175`**
- What is wrong: the Speech transport timeout is a literal 300 s. `SPEECH_SYNC_SECONDS=60` and `fuzzy_ratio=0.8` are code constants, not recipe fields.

---

### Category 5 – Closed lists that silently drop or refuse

**5.1 [high] `server/app/providers.py:48-50` vs `server/app/providers.py:61`**
- What is wrong: `SYSTEM_PROMPT` tells the model to return status `"contradicts"` for material that contradicts the thesis ("Materiału sprzecznego z tezą pytania nie pomijaj"). `RESPONSE_SCHEMA` constrains the status enum to `['proven','claim','hypothesis','unknown']`.
- With structured output the model cannot emit `contradicts`. `advocate.CLAIM_STATUSES` and the UI labels support it, but the schema does not.
- Why it matters: conflicts are meant to be preserved, and the answer path structurally cannot produce the conflict status.

**5.2 [medium] `server/app/owner.py:29-31, 205-215, 229-230`**
- What is wrong: `processing_kind()` accepts only `audio/*`, `video/*`, `image/*` and `application/pdf`. Anything else, such as an `application/octet-stream` recording, is skipped with a bare `continue` and not added to the `skipped` list.
- Why it matters: the owner presses "transcribe" and a file with an unrecognised mime gets neither a job nor a note.

**5.3 [medium] `server/app/media/ocr.py:37, 255-256, 329-331`, `server/app/media/vertex.py:33`**
- What is wrong: `detect_input` has no signature for HEIC/HEIF, although `GEMINI_DOCUMENT` lists those types.
- `MIME_FOR_KIND` omits tiff, bmp and gif, which Tesseract accepts (`IMAGE_KINDS`).
- Those inputs raise `UnsupportedInput` for the Vertex preset.
- Model output is also dropped silently:
  - OCR pages whose number repeats a page already read from a text layer.
  - Non-dict items in the ASR segment list (`vertex.py:281`).
  - Non-dict OCR message items.
- A non-integer page number is replaced by `len(pages)+1` (a guess).
- The failures that are raised are explicit. The silent drops and the guess are not.

**5.4 [low-medium] `server/app/owner.py:290-295`**
- What is wrong: `preset_status` treats any active `vertex` consent (`SELECT 1 … LIMIT 1`) as sufficient. It ignores scope coverage, which the actual call enforces.
- Why it matters: a case-scoped consent shows the preset as available, and then jobs for other artifacts fail at `bind`.

**5.5 [low] `server/app/providers.py:313, 399`, `server/app/reports/labels.py:5`**
- What is wrong: `AUDIO_TYPES` is a closed list. The refusal is stated ("unsupported or oversized recording"), but there is no recorded reason for the choice.
- `max_tts_chars` truncates the answer text sent to TTS (`[:max_tts_chars]`) without telling the listener.
- Report labels exist only for `pl` and `en`, although the UI catalogue has `nl`, and an unknown language falls back to Polish (`reports/disclaimers.py:58`).

---

### Category 6 – States with no way out

**6.1 [medium] `server/app/jobs.py:41-52, 78-86`**
- What is wrong: an expired lease re-queues the job indefinitely. `attempts` is incremented but never consulted.
- Why it matters: a poison job, for example a very large file that kills the worker, loops forever. Each cycle with a paid recipe makes another Vertex reservation and possibly another paid call.

**6.2 [medium] `server/app/jobs.py`, `server/app/api/media.py:125`, `server/app/templates/media_queue.html`**
- What is wrong: queued or running jobs can be reprioritised but not cancelled (grep finds no cancel route). A queued paid job can only be neutralised by revoking consent, and then it fails.
- A failed job has no retry route. A new job is created through enqueue.

**6.3 [medium] `server/app/integrations/proposals.py:158` (`decide_proposal`), `server/app/integrations/loom_graph.py:361-393` (`decide_hypothesis`)**
- What is wrong: `annotation_proposals` and `relation_hypotheses` move proposed to accepted or rejected, and promoted to a `link`, but the row then refuses any further decision ("already …").
- No revoke exists. By contrast `timeline/correlation.py:27-29` and `location/correlate.py:31-32` have `revoked` and reverse transitions.
- No `UPDATE annotations` or `DELETE FROM annotations` exists anywhere, so an accepted annotation or link created by a proposal cannot be retracted.
- Why it matters: a human decision made by mistake cannot be undone, only audited.

**6.4 [medium] `server/app/api/owner.py:81-103`, `server/app/owner.py:162-176`**
- What is wrong: if `owner.start_batch` fails after `create_collection` (scan or plan error), the collection and the files remain, but the web UI offers no re-scan, re-plan or discard.
- The batch page has only "originals" and "process" actions.
- The error redirects to `/start` with a message, and the batch id is not shown.

**6.5 [low-medium] `server/app/providers.py:129-133`**
- What is wrong: the reservation row in `provider_calls` is never updated. Settlement is a separate row, so the original stays `status='reserved'` for ever.
- A crash between `reserve` and `_book` leaves worst-case cost counted for that UTC day with no marker that it was never settled.
- The `/settings` call list (`api/owner.py:200-202`) also shows these settlement deltas, which can be negative, as ordinary "calls".

**6.6 [low] `server/app/connectors/gmail.py`, `server/app/commands/connectors.py:12`**
- What is wrong: only `gmail-auth-url`, `gmail-auth-complete` and `gmail-import` exist. There is no command to forget or revoke the stored OAuth refresh token (`gmail_token.json`). The only advice is to revoke the app at myaccount.google.com.

---

### Category 7 – Tests that assert only existence, truthiness or "some error"

**7.1 [medium] `server/tests/test_owner_web.py:106, 122, 124, 134, 137, 141, 264, 270, 301, 307, 317`**
- What is wrong: the settings and invite checks use `assertIn('error=', location)`. The expected error text is known: Polish messages in `api/owner.py`, or the consent-missing reason from `preset_status`.
- Lines 311-317 loop over five different invalid invite inputs and accept any error for each.
- Why it matters: any error satisfies the test. A refusal for the wrong reason passes, for example "vertex consent missing" versus "recipe unknown" at line 137.

**7.2 [medium] `server/tests/test_providers.py:145`, `server/tests/test_voice.py:87`**
- What is wrong: `assertGreater(actual[0], 0)` and `assertGreater(...['actual_eur'], 0)`. The worst-case cost is a function of the preset and the body size (`providers._cost`), so the value is knowable.
- Why it matters: these tests exist to prove "unknown usage is booked at worst case, not zero". A positive number does not prove worst case, and a wrong price conversion would pass.

**7.3 [medium] `server/tests/test_verified_asr_acceptance.py:64-65`**
- What is wrong: `assertTrue(job['error'])` and `assertTrue(run['error'])`. The failure message is known per scenario.
- Why it matters: the test verifies that a failure was recorded, but not which failure.

**7.4 [medium] `server/tests/test_cli.py:73, 76, 92, 94`, `server/tests/test_exchange_acceptance.py:126, 132, 277`, `server/tests/test_http_authz.py:312`**
- What is wrong: `assertNotEqual(returncode, 0)`. The CLI sets 1 for validation failures (`test_cli.py:90` asserts `1` elsewhere), and a specific stderr reason is known.
- Why it matters: a crash and a validation refusal are indistinguishable.

**7.5 [low] `server/tests/test_smoke.py:4-8`**
- What is wrong: the only smoke test asserts that two table names exist.

**7.6 [low] `server/tests/test_advocate.py:75, 168`, `server/tests/test_traces_plan_api.py:162`, `server/tests/test_reports_interfaces.py:177`, `server/tests/test_read_pages_acceptance.py:178`, `server/tests/test_integrations_tokens.py:23`**
- What is wrong: `assertTrue(answer['basis'])`, `assertTrue(...['items'])`, `body['items']` and `assertNotEqual(row['salt'], '')` assert non-emptiness only.
- Why it matters: the expected passages, items and salt length are knowable from the fixtures. The neighbouring assertions in the same tests check exact values, which makes these the weak spots.

---

### Other observations (outside the seven categories)

**Reflected error text.** `GET /start?error=…` and `GET /settings?error=…` render an arbitrary query string as a message (`api/owner.py:55, 192`). Jinja escapes it, but anyone who can lure the owner to a crafted link can show chosen text inside the app's own error banner. Low.

**Empty model output.** An empty `segments` list from the model publishes an empty transcript as `ok` (`media/vertex.py:269-281`). `finishReason` is not recorded. Low confidence: the agent did not trace whether `publish_transcript` rejects empty text.

**Env-var check.** The environment variables documented in `docs/*.md` and read in `server/app` line up with code, except `EW_DIARIZATION_RECIPE`. It is read in `media/recipe.py:41-43` but not documented. The `AGEDS_*` test and browser variables are documented in docs and used only by tests, which is acceptable.

### Items checked and found sound (no finding)

- Paid Vertex calls take a consent and budget reservation. `bind` checks artifact coverage before ASR, and `require_consent` runs before OCR (`media/ocr.py:279`).
- The advocate's provider-unavailable path falls back to retrieval-only and records `provider_fallback` in the verification (`advocate.py:347-366`).
- Recipes pin the effective payload per job.
- HTTP clients cannot override engine, model or limits (`recipe.py:77-90`, with a stated reason).
- Gmail uses a read-only scope and reports selection truncation (`select()` returns a `complete` flag).
- Importers read their closed lists from config (`photos_takeout` `config[...]`).
- Timeline zone assumptions are explicit parameters.
- Import-plan attempts and catalog scans have recovery and abandon paths.

# Multilingual transcription, OCR, diarization and the media queue

Task `AGEDS-TASK-20261016-D` (Etap 3). Code: `server/app/media/`, `server/app/worker.py`,
`server/app/jobs.py`, `server/app/transcription.py`, migrations 40–43 in
`server/app/schema/media.py`. Receipt: [MEDIA_2026-10-16](validation/MEDIA_2026-10-16.md).

**Evidence boundary.** Every adapter in this document was exercised with injected fakes
(a fake faster-whisper module, fake OCR engine, fake diarizer) on generated data. No real
Whisper, Tesseract or pyannote inference ran in this increment: faster-whisper is not
installed in the test environment, Hugging Face downloads are blocked, Tesseract and
pyannote are absent. Poppler (`pdftoppm`, `pdfinfo`) is present and was executed for PDF
rasterization. Nothing here is a claim about recognition quality on Polish, Dutch or
English speech.

## 1. ASR recipes are pinned data

A recipe is a JSON preset (`server/app/media/recipes/<id>.json`, or a private directory
named by `EW_MEDIA_RECIPE_DIR`; private ids cannot shadow built-in ones). `effective_recipe()`
is the single resolver used by enqueue, worker, comparison and export:

1. load the preset; reject unknown/missing keys (strict schema per kind);
2. apply explicit overrides (deep merge; `schema`, `id`, `kind`, `revision` are immutable);
3. resolve `{"$setting": "whisper_model"}` placeholders from `EW_WHISPER_*` settings;
4. hash: SHA-256 of canonical JSON (`sort_keys`, no whitespace, UTF-8, finite numbers) of
   the execution-relevant fields; `description` and provenance are outside the hash.

| Preset | Strategy | Hash with default settings (small/cpu/int8) |
|---|---|---|
| `legacy` | `auto_per_file` — the pre-recipe call | `78921f93a049506cbeab800d125a8e08d7d94e3477554592061485dcd0678548` |
| `per-segment-pl-nl-en` | `per_segment_language`, allowed `pl, nl, en` | `b5530b0e5f3dac066426e869c7f718287df413416236d5162d7a14bed7149171` |

`legacy` reproduces exactly `WhisperModel(model, device=..., compute_type=...)` and
`model.transcribe(audio, word_timestamps=True, vad_filter=True)` (tested by capturing both
calls with and without the recipe). The default model stays the small local model; a
larger one is an override (`{"model": "large-v3"}`) and therefore a different hash.

**Job pinning.** `queue_transcription()` / `enqueue_media_job()` write
`jobs.payload_json = {"schema": "ageds.job_payload", "recipe": {id, kind, sha256,
effective, provenance}, "dedup_policy", "pinned_at"}`. The worker executes the pinned
`effective` copy after re-validating it and recomputing its hash (a mismatch fails the
attempt). Changing presets, `EW_ASR_RECIPE` or `EW_WHISPER_*` later never changes a
queued job.

**Dedup policy** (`EW_MEDIA_DEDUP_POLICY`, per call `dedup=`): `reuse` (default) returns
an active job with the same kind, artifact and recipe hash and applies a requested
priority in place; completed jobs do not block a new attempt (behaviour before recipes).
`reuse_completed` also returns a completed job with the same hash. `duplicate` always
creates a job. A different hash is always a separate job, run and version.

**Pre-recipe jobs** (empty payload) run as `legacy` resolved at run time and are recorded
with `pin_status: unpinned_pre_recipe_job`, `historical_recipe: unknown`. Their payload is
not backfilled and earlier runs are not attributed a recipe. While active, such a job is
matched by a `legacy` request so an upgrade does not duplicate queued work.

**Run record.** `processing_runs.provenance_json.recipe` holds `{id, kind, sha256,
effective, pin_status, application, provenance}` for every attempt; recipe-built adapters
also put the full recipe and hash in `parameters_json`. `application` says whether the
adapter was built from the pin or injected (`injected_adapter_not_bound_to_recipe` is
recorded honestly). Each transcript version's metadata names the recipe and whether word
timestamps were requested; a recipe with `word_timestamps: false` marks citations of that
version as limited to segment selectors. `GET /api/media/runs/{id}/recipe` recomputes the
hash (`hash_matches`). The metadata export already carries `jobs.payload_json` and
`processing_runs.provenance_json` (tested).

## 2. Per-segment language strategy (code-switching)

`CodeSwitchingAdapter` (separate adapter, separate run), written against the
faster-whisper 1.2.0 API (signatures read from the pinned wheel's source):

1. `faster_whisper.decode_audio(verified_stream, sampling_rate=16000)`;
2. `faster_whisper.vad.get_speech_timestamps(audio, VadOptions(**params))` (Silero) →
   regions clipped, sorted and merged without inventing speech;
3. each region split into the fewest equal windows ≤ `max_window_s` (default 6 s, max 30);
4. `WhisperModel.detect_language(audio=window, language_detection_segments=1, ...)`;
   the report is restricted to `allowed_languages` and renormalized; ties break by the
   order of the allowed list. Stored per window: raw top-k as reported (including
   disallowed languages), the allowed distribution, the chosen language;
5. `WhisperModel.transcribe(window, language=chosen, word_timestamps=..., vad_filter=False,
   condition_on_previous_text=False)`;
6. segment and word times are offset by the window start (sample index / 16000) to file
   time; raw model times are otherwise unchanged.

One `derived_text` version of kind `transcript` results (so the existing citation code
applies). Each segment carries `language`, `language_probability` (reported, for the chosen
language), `language_probability_allowed` (renormalized), `language_alternatives`,
`language_probability_meaning: "language identification probability; not transcript
correctness"`, `window {index, start, end}` and `strategy`. Version language is the single
language or `mul`. If no allowed language is reported the window falls back to model
detection, marked `model_auto_fallback` / `outside_allowed`. `limits.max_windows` gives
explicit partial coverage.

Word timestamps satisfy `citations.validated_segment_words`; word citations across
segment and window boundaries were created on a code-switching version in tests.

**Limits (stated in every version's metadata):** a switch shorter than a window is not
resolved — the whole window is decoded under one forced language (the synthetic fixture
plants exactly this case: the 5–9 s window holds 1 s of the `pl` tone and 3 s of `nl`);
VAD can drop quiet or overlapped speech (planted: a quiet segment produces no words);
language probability is not quality; very short windows identify language unreliably;
window boundaries can split words.

## 3. Comparing runs

`compare_versions(a, b)` (API `GET /api/media/compare?a=&b=&normalize=`, page
`/media/compare`, CLI `transcript-compare A B [--output FILE]`) for two versions of the same
artifact and kind:

* word-level diff with `difflib.SequenceMatcher(autojunk=False)` over stored ASR words
  (or whitespace tokens when word timings are invalid) — counts are not WER and neither side
  is reference truth; optional normalization `ageds-asr-eval-norm/1`;
* per-segment language aligned by time overlap: pairs, seconds same/different/unknown,
  `A->B` transitions; the auto-per-file run has version-scoped language;
* timing: segment start/end deltas (each A segment with its largest-overlap B segment) and
  matched-word start deltas.

The JSON includes `content_sha256`; identical inputs give identical output (tested).

## 4. Benchmark tool (no quality claims)

```
python -m server.app.cli asr-benchmark --manifest corpus.json --output /private/dir \
    [--recipe legacy --recipe per-segment-pl-nl-en] [--hypotheses hyp.json] [--max-cells N]
```

The manifest (`ageds.asr_benchmark_manifest` v1) lists audio paths (relative to the
manifest) and reference text, optionally reference segments with time and language.
Recipes run through the worker adapter interface; `--hypotheses` scores precomputed text
without inference. WER/CER are Levenshtein counts computed by code (`textmetrics.py`),
tie-break substitution > deletion > insertion. Normalization `ageds-asr-eval-norm/1`: NFC,
casefold, NFC; punctuation → space except an apostrophe between letters and `, . :` between
digits; Cf/Cc removed; whitespace collapsed; **diacritics kept**; no number or spelling
normalization. An empty reference has no rate (`null`) and reports insertions; corpus
aggregates are micro averages that include those insertions. Results include per item,
per item language, per reference-segment language and language-label agreement (labels
only). Output is written exclusively (never overwritten) only into the given directory;
paths inside the repository, the content store or any directory holding manifest audio
(review U2) are refused. Source audio is opened read-only. The result carries a scope statement that it is not a quality claim.

## 5. Diarization (optional post-pass)

Job kind `diarize`, recipe `diarization-pyannote`. `PyannoteDiarizer` uses
`pyannote.audio` 3.x (`Pipeline.from_pretrained(...)` on the verified stream) only if the
package is importable; otherwise the job fails with `diarization unavailable: pyannote.audio
is not importable`. The real pyannote path has **not** been run. Turns are validated (finite,
ordered, nonblank label; rejected turns are counted with reasons, not repaired) and stored in
`speaker_turns` linked to their run — hypotheses, never merged into transcript text.

Human speaker assignment is a dedicated append-only table `speaker_assignments` (decision:
the generic `annotations` table cannot reference a diarization run and label structurally).
Scopes: one transcript segment, or a diarization label of one run. Display precedence:
manual segment → manual label mapping → model hypothesis; all three are returned together
(`GET /api/media/artifacts/{id}/speakers?derived_text_id=`, page `/media/artifact/{id}/speakers`).
A correction is a new row; the latest row of a scope is displayed and history is counted.

## 6. OCR

Job kind `ocr`, recipes `ocr-tesseract-pol-nld-eng` (document) and
`ocr-chat-screenshot-pol-nld-eng`. Engine: `tesseract stdin stdout -l pol+nld+eng --psm N tsv`
(`EW_TESSERACT` may name the executable); languages are configurable by override. Inputs are
detected by signature (PDF, PNG, JPEG, TIFF, BMP, GIF, WebP). PDF pages are rasterized one by
one with `pdftoppm -f N -l N -r DPI -png -` (stdin/stdout; no temporary input copy);
`pdf.max_pages` gives partial coverage. Without poppler a PDF is `unsupported here`. PDF text
layers are not extracted (recorded `not_extracted`; that would be parser output, not OCR).
Output: `derived_text` kind `ocr` with line segments `{page, line_index, text, box (px),
words [{text, box, engine_confidence}]}`; text joins words by one space, lines by newline,
pages by form feed. Engine confidence is stored as reported and labelled not correctness.

With layout `chat_screenshot`, `chat-screenshot-layout/1` groups lines into bubbles by vertical
gap and side, and writes `event_candidates` rows (status always `candidate`, append-only):
raw `sender_text` (only a `Name:` prefix), `time_text` (raw OCR time string, last of several,
all kept in metadata; date and zone unknown), `body_text`, `layout_side` (a layout hypothesis,
not identity) and box locators. Candidates are never events; nothing promotes them.

## 6a. Vertex Gemini engines and the timing reference

`asr-vertex-gemini`, `ocr-vertex-gemini` and `ocr-vertex-gemini-chat` send the material to
Gemini on Vertex AI under a recorded provider consent covering the artifact and within the daily
budget (reservation, then settlement in `provider_calls`). Gemini returns text, speaker
hypotheses and per-segment language, but only **estimated** segment times; a PDF uses its text
layer first and the model only for pages without one.

Word times come from a separate **timing reference** named in the recipe's
`model_options.timing_reference` and switched on by `word_timestamps: true`
(`server/app/media/alignment.py`):

* `asr-vertex-gemini-aligned` (pilot default): local faster-whisper with word timestamps
  (model and revision pinned, CPU, no network call once the model is cached) on the same bytes
  that were sent to Gemini.
* `asr-vertex-gemini-chirp`: Speech-to-Text v2 (Chirp 2) word offsets; paid per audio
  second and booked like the Gemini call; synchronous recognition only, so at most 60 s
  (a longer recording keeps the model estimates and records why). Price is an unverified preset.

* `asr-vertex-gemini-forced`: forced alignment of the Gemini text itself to the audio
  (torchaudio MMS_FA, CTC; weights pinned by SHA-256; optional `server/requirements-align.txt`).
  Every word gets a time and an acoustic score. A segment is word-citable only when its mean
  score is ≥ 0.7. Placeholders such as `[niezrozumiałe]` and digits are aligned as wildcards.
  Wildcards between segments absorb speech the model did not transcribe.

Comparison on the three pilot phone calls (real data, 2026-10-27; offline on the stored Gemini
text, no model call):

| Preset | Segments word-citable (artifacts 4, 5, 6) |
|---|---|
| `asr-vertex-gemini-aligned` | 28/55, 23/46, 26/46 (77/147) |
| `asr-vertex-gemini-forced` at score 0.5 | 23/55, 0/46, 4/46 (27/147) |

Forced alignment failed on the quieter remote speaker of two calls, and loudness normalisation
did not help. Where both presets time a segment, segment ends agree to about 0.05 s and starts
to about 1 s (median). The aligned preset stays the default. A real end-to-end run of the forced
preset (artifact 4, throwaway copy) made 22/60 segments citable.

* `asr-vertex-gemini-hybrid` (optional): the aligned preset, then segments it did not align
  are filled from forced alignment (score ≥ 0.7) when they stay between the neighbouring
  timed segments. Filled segments carry `timing_source: forced_alignment_unconfirmed` and
  `independent_confirmation: false`. On the pilot (offline, real data) it filled 13, 0 and 0
  segments: 41/55, 23/46, 26/46 word-citable.

**Time notation of the model estimates.** Gemini sometimes switches to minutes.seconds
written as a decimal (1.26 for 86 s). `metadata.timing_alignment.notation` records the
detection. From the first drop in the raw estimates, if the minutes.seconds readings run
forward, those segments get `timing_notation_hypothesis`. Only when the reference confirms
it (≥ 80 % of the checked segments within ±2 s of their aligned start, at least 3 checked)
do they also get the derived `model_estimate_reinterpreted`. `model_estimate` always keeps
Gemini's values. On pilot recording 6 only 7 of 12 checked segments fit, so it stays a
hypothesis.

Gemini's raw words (joining exactly to the stored segment text) are matched in order to the
reference words (difflib on case/diacritic-folded words; `exact`, or `fuzzy` ≥ 0.8 inside a
mismatched block). A matched word takes the reference interval. Unmatched words between two
matched ones share that gap in equal parts (`interpolated`, the whole gap kept as `bound`).
A segment receives words, and becomes word-citable, only when at least `min_matched_ratio`
(default 0.5) of its words matched. Otherwise it keeps the model estimate with an
`alignment_note`. Every aligned segment keeps `model_estimate`.

The comparison is stored in the version's `metadata.timing_alignment`:
* the reference transcript;
* counts of exact, fuzzy, interpolated and untimed words;
* text agreement;
* per-segment deltas between the estimate and the aligned interval.

If the reference fails, the paid Gemini text is still published with
`timing_alignment.status = unavailable` and the reason. Two engines agreeing is not proof of
what was said, and a low agreement is a reason to listen, not a verdict on either engine.

## 7. Queue, estimates, recovery

Media jobs use the existing leased queue: a run per attempt, publication only by the owner of
the unexpired lease via `jobs.publish_result` (derived text, event candidates and speaker turns
roll back together with the completion). `finish_job(..., 'done')` cannot complete `ocr` or
`diarize` jobs. `claim_job(kinds=...)` and `EW_WORKER_KINDS` let a worker claim only kinds it
can run. Crash recovery is the existing lease expiry; it was tested for the new kinds (late OCR,
diarization and code-switching workers cannot publish; the replacement runs the same pin).

`GET /api/media/queue` (page `/media/queue`, CLI `media-queue`) lists running then queued jobs
in claim order with estimates: work amount (audio seconds from an earlier run on the same
content, else the WAV header declaration; pages for OCR images) × measured wall seconds per
unit of completed runs with the **same job kind and recipe hash**, reported as min/median/max
with sample size. Run wall time includes model load and verification. Without measured runs
or a known work amount the estimate is `unknown` with reasons. A cumulative median through each
queued job is shown only while all previous estimates are known. Priority can be changed for
queued jobs (`POST /api/media/jobs/{id}/priority`, audited).

## 8. Interfaces

HTTP (`server/app/api/media.py`; navigation "Transkrypcje", "Kolejka"):
`/api/media/capabilities`, `/api/media/recipes`, `POST /api/media/artifacts/{id}/jobs`,
`/api/media/queue`, `POST /api/media/jobs/{id}/priority`, `/api/media/artifacts/{id}/versions`,
`/api/media/compare`, `/api/media/runs/{id}/recipe`, `/api/media/artifacts/{id}/speakers`,
`POST /api/media/artifacts/{id}/speaker-assignments`, `/api/media/artifacts/{id}/event-candidates`;
pages `/media`, `/media/artifact/{id}`, `/media/compare`, `/media/queue`,
`/media/artifact/{id}/speakers`. CLI: `asr-recipes`, `media-enqueue`, `media-queue`,
`transcript-compare`, `asr-benchmark`, `media-capabilities`. API authentication is opt-in
(`EW_REQUIRE_TOKEN`, see [read API](READ_API.md)); otherwise use on a trusted network.

**HTTP overrides are limited (review finding 3, 2026-10-16).** `POST …/jobs` accepts only
decoding/strategy overrides (`recipe.CLIENT_OVERRIDE_KEYS`): ASR `strategy`, `vad`,
`word_timestamps`, `language`, `allowed_languages`, `windows`, `detection`,
`transcribe_options`; OCR `languages`, `psm`, `oem`, `layout`, `pdf.dpi`, `pdf.max_pages`;
diarization `num_speakers`, `min_speakers`, `max_speakers`. `engine`, `model`, `device`,
`compute_type`, every `model_options` key, the diarization `pipeline` and all `limits` choose
code, files or network behaviour on the server: they are operator decisions made in a recipe
preset (built-in or `EW_MEDIA_RECIPE_DIR`) or through the CLI (`media-enqueue --override`).
A refused key is a 422 naming the key and the permitted list; the HTML form has no override
field. **HTTP values are bounded too (review 2 finding 11)**, because decoding options set worker
CPU/memory: `transcribe_options` — `beam_size`, `best_of` 1–10; `patience` 0.1–5; `length_penalty`
0–5 or null; `repetition_penalty` 0.1–5; `no_repeat_ngram_size` 0–20; `temperature` a number or 1–10
numbers in 0–1; `compression_ratio_threshold` 0–100, `log_prob_threshold` −100–0, `no_speech_threshold`
0–1 (each or null); `prompt_reset_on_temperature` 0–1; `max_initial_timestamp` 0–30;
`max_new_tokens` 1–448, `chunk_length` 1–30, `hallucination_silence_threshold` 0–30 (or null);
`initial_prompt`/`hotwords` ≤ 1000 characters, `prefix` ≤ 200, punctuation sets ≤ 100;
`suppress_tokens` ≤ 64 ids; booleans must be booleans. `vad.parameters`: `threshold`/`neg_threshold`
0–1, `min_speech_duration_ms`/`min_silence_duration_ms` 0–60 000, `speech_pad_ms` 0–10 000,
`max_speech_duration_s` 0–86 400. OCR `pdf.dpi` 72–600 and `pdf.max_pages` 1–500; diarization speaker
counts 1–50. The tables are `recipe.CLIENT_*_BOUNDS`; operator presets and the CLI keep the full
recipe validation (e.g. `pdf.max_pages` up to 10 000). Reversal: extend `CLIENT_OVERRIDE_KEYS` (code) — an operator who wants another model
over HTTP adds a private preset and selects it by id.

## 9. Not done / open

* Real inference: faster-whisper per-segment path, Tesseract and pyannote were not executed.
  First real run should use a consented local corpus with the benchmark tool.
* New tables (`speaker_turns`, `speaker_assignments`, `event_candidates`) are not in the
  metadata package's fixed table list; the export task must declare them explicitly.
* Event candidate review/promotion, OCR of PDF text layers, intra-window language switches,
  overlapped-speech handling and a GPU/model-size benchmark are future work.
* `model_options.download_root` (if a user sets it) is a local path that appears in run
  provenance and exports.

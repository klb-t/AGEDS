# N11 — real local ASR smoke

The opt-in `scripts/real_asr_smoke.py` runs the production
`FasterWhisperAdapter` through queue → claim → worker → publication, using a
new isolated database. FFmpeg/flite generates an English sentence locally.
The harness selects the first three actual ASR words as an exact version-pinned
citation, validates metadata export, and compares the inert archive roundtrip.
A fake adapter is never substituted. Private source directories are never read.

## Reproduce

Install `server/requirements.txt` and `server/requirements-whisper.txt` in an
isolated environment (including explicit `requests` and a compatible PyAV pin;
see dependency findings below). FFmpeg must include the `flite` filter and `slt` voice.
Acquire these files from `Systran/faster-whisper-tiny.en`, revision
`0d3d19a32d3338f10357c0889762bd8d64bbdeba`: `config.json`, `tokenizer.json`,
`vocabulary.txt`, `model.bin`. Store them outside the checkout. The model card
identifies an English CTranslate2 conversion of OpenAI tiny.en, MIT licensed,
with FP16 stored weights. This smoke requests CPU/int8 at load time.

```sh
python scripts/real_asr_smoke.py \
  --model-dir /path/to/local/tiny.en \
  --model-revision 0d3d19a32d3338f10357c0889762bd8d64bbdeba \
  --work-dir /path/to/new/isolated-run
```

The work directory must not exist. The harness sets `HF_HUB_OFFLINE=1` and uses
an explicit local model directory. It writes generated WAV, isolated live DB,
metadata JSON, inert SQLite archive and receipt there. The receipt records
all model file hashes, dependency versions, effective transcription parameters,
raw transcript/segments/words, elapsed runtime, run/job rows and citation/export
checks. `--model-revision` is an externally supplied acquisition observation;
file hashes establish the actual bytes used. It does not rewrite the production
adapter's honest `unknown` model revision/version fields.

A failed run exits nonzero and records its stage/error. Missing model files or
an existing output directory are rejected before creating the run. Do not
interpret `actual_inference_completed: true` alone as downstream acceptance;
the receipt must also have `status: passed`.

## Scope and limits

One generated voice establishes execution of the local model and downstream
contracts. It does not establish recognition quality on human, Polish, noisy,
telephone, multi-speaker or private audio, and does not verify acoustic alignment.
The spoken text is recorded separately from raw ASR output; punctuation/case or
word errors are not repaired. No numerical confidence in transcript correctness
is inferred from language identification probability. Runtime includes model
loading and worker work; it is not a controlled performance benchmark.
The archive carries metadata, not source/model bytes, replay, live restore or a
signature. Source checksum and modification time are checked after processing.

## Primary references checked 2026-09-30 UTC

- https://huggingface.co/Systran/faster-whisper-tiny.en — model identity,
  conversion, license and stored precision.
- https://huggingface.co/api/models/Systran/faster-whisper-tiny.en — resolved
  revision; pinned revision endpoint used for acquisition.
- https://github.com/SYSTRAN/faster-whisper — local CPU/int8, word timestamps,
  VAD and generator consumption documented by the implementation publisher.

Observed receipt and runtime outcome are in `REAL_ASR_SMOKE_RECEIPT.json`.

## Dependency findings from actual execution

`REAL_ASR_INITIAL_FAILURES.json` preserves three initial failures, with complete
tracebacks. A fresh requirements-whisper install resolved faster-whisper 1.2.0,
huggingface-hub 1.33.0 and PyAV 19.0.0:

1. faster-whisper imports `requests` in `utils.py:8` but does not declare it.
   The resolved hub no longer brings it transitively. Added requests 2.34.2 to
   the isolated environment.
2. PyAV 19 rejects `metadata_errors` in `av.open`, used by faster-whisper's
   `audio.py:46`. These attempts did not complete inference.

3. With requests 2.34.2 and PyAV 16.1.0, actual inference completed but
   word-timing metadata incorrectly reported unavailable. The adapter supplied
   NumPy float64 scalars; the timing validator requires Python int/float. A
   second actual invocation confirmed raw capability unavailable while its
   exact JSON roundtrip was available. This is a representation-boundary bug,
   not missing ASR timing. Raw transcript and all returned words are preserved
   in the failed receipt.

The final receipt identifies the accepted combination and exact code hashes.

## Observed result

PASS on 2026-09-30 23:17 UTC, after the coordinator's explicit dependency pins
and numeric-scalar adapter fix. A 5.43-second generated WAV completed the real
worker in 4.655 seconds (5.485 seconds total harness time). One segment contains
17 saved ASR words. The exact first-three-word citation is ` The quick brown`,
0–1020 ms, pinned to transcript 1. Export validation and canonical inert archive
roundtrip passed; generated source hash and modification time remained unchanged.

Raw output, without correction:

> The quick brown fox jumps over the lazy dog, this is a test of locals' peach recognition.

The synthesis input ends in “local speech recognition.” That recognition error
remains in the stored result. The receipt also retains ASR probabilities, while
transcript correctness confidence remains `unknown`.

The final run reused the isolated dependency directory created from repository
requirements, supplemented by requests 2.34.2 and replacing PyAV 19 with 16.1.0.
Those versions are now explicitly pinned by the coordinator in repository
requirements. A second wholly fresh dependency installation was **not** performed
because the shared environment was short of disk space. This is a tested runtime
combination, not a claim that all platform/dependency resolutions were tested.
The production adapter's NumPy-scalar conversion is covered by the coordinator's
separate offline regression. This task's evidence is actual inference plus the
end-to-end receipt; no additional synthetic test count is claimed here.

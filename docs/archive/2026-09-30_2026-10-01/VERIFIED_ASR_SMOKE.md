# N39 — actual offline ASR through the verified reader

**PASS**, 2026-10-01 00:30 UTC: the production queue, claim, worker and
`FasterWhisperAdapter` completed real local tiny.en CPU/int8 inference using the
production `VerifiedReader`. The resulting exact word citation and canonical
inert metadata archive roundtrip passed. No fake adapter, download, dependency
installation, remote ASR service or private corpus was used.

The final evidence is `VERIFIED_ASR_SMOKE_RECEIPT.json`. The earlier passing run
is retained as `VERIFIED_ASR_SMOKE_INITIAL_RECEIPT.json`; the final run was repeated
in a new directory after the reader owner added sticky integrity-failure handling.
All eight captured code/dependency-file fingerprints matched the final working
files when checked after execution. The earlier N11 reports/receipts are unchanged.

## Observed production read path

The new opt-in `scripts/verified_asr_smoke.py` retains the earlier smoke's generated
English flite input, isolated store/database, queue → worker → citation → archive
flow. During actual worker execution, Python profiling observes the installed
faster-whisper decoder entry and the production reader methods. It does not replace
the adapter, model, decoder or reader.

- `faster_whisper.audio.decode_audio` received a
  `server.app.verified_reader.VerifiedReader` object, rather than a pathname.
- Ten actual `VerifiedReader.read` calls used that same object identity. Requests
  were 32,768 or 32,846 bytes, below the reader's 1 MiB per-read limit.
- Test-only descriptor inspection recorded the retained descriptor's store target,
  device, inode and 173,838-byte size. It did not expose a production path or file
  descriptor API, reopen the media for decoding, or substitute media bytes.
- One final `verify_unchanged` call/return occurred before successful publication.
  The retained descriptor was closed after the worker returned.
- The processing-run provenance and transcript metadata both recorded
  `input_mode=verified_seekable_filelike`, initial SHA-256 verification `passed`,
  final same-descriptor verification `passed`, and
  `read_policy=verify_covering_chunks_before_return`. The nested verification
  metadata explicitly records no worker pathname reopen and no worker media copy.

Observed read lengths include repeated decoder reads; they are not a measure of
unique-byte coverage or proof that every input sample influenced the transcript.
Production metadata correctly retains `decoder_read_coverage=not_measured`.

## Output and limits

The 5.43-second synthetic English utterance produced one segment with **17 saved
ASR words**. The exact first-three-word citation remains ` The quick brown`, pinned
to transcript 1 at **0–1020 ms**. Export validation passed and canonical metadata
before/after the inert SQLite archive roundtrip was equal. The generated source's
hash/modification time and model weight hash remained unchanged. All four local
model file hashes match the previously accepted N11 model bytes.

Raw ASR output was preserved:

> The quick brown fox jumps over the lazy dog, this is a test of locals' peach recognition.

The synthesis text says “local speech recognition.” This recognition error was
not corrected. No listening, acoustic alignment verification, human/Polish/noisy
corpus quality assessment, or transcript-correctness confidence is claimed.

Final worker elapsed time was **3.690 seconds**, and the harness measured **4.560
seconds** overall. This includes model loading and profiling; it is not a controlled
performance benchmark. Verified byte reads do not establish source authenticity,
filesystem immutability, truth of speech or model accuracy. The inert archive
contains metadata, not media/model bytes, a replay environment or live restore.

## Reproduce with already available dependencies/model

```sh
PYTHONPATH=/workspace/scratch/773725428b88/review/asr-deps:/workspace/scratch/773725428b88/review/deps:/workspace/scratch/773725428b88/AGEDS \
HF_HUB_OFFLINE=1 python3 scripts/verified_asr_smoke.py \
  --model-dir /workspace/scratch/773725428b88/review/asr-model \
  --model-revision 0d3d19a32d3338f10357c0889762bd8d64bbdeba \
  --work-dir /path/to/a/new/isolated-output-directory
```

The output directory must not exist. Final local output was
`/workspace/scratch/773725428b88/review/verified-asr-wave6-final`; the initial output
remains in `review/verified-asr-wave6`. The receipt records exact dependency versions,
model hashes, source hash, run/job rows, raw text/segments/words, reader observations,
citation selectors, archive verification and code fingerprints. A failed invocation
retains its error/traceback receipt and exits nonzero. Read this as actual inference
plus downstream contract acceptance; it is separate from synthetic unit tests.

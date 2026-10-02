# N38 — independent verified ASR input acceptance

Base/claim: `c8cfdbdfe89652e4a43d9f32773ad4445be37648`.
New independent tests: `server/tests/test_verified_asr_acceptance.py`.
Reader and worker production code were changed by their assigned owners.

## Reproduced old failure

Before the worker change, a synthetic adapter renamed the original source away,
placed different bytes at the original pathname, read those bytes during its
`transcribe` call, and restored the original before returning. The worker's
before/after pathname hashes both passed, yet the observed result was:

```
{"published_text":"WRONG SYNTHETIC!!","original_restored":true,"job_status":"done"}
```

This is a concrete wrong-input publication reproduction, not a hypothetical
race or a real ASR quality observation. Only temporary synthetic files were used.

The new regression repeats the adapter-only replacement with a supplied verified
file-like reader. The adapter receives the original descriptor's verified bytes;
the published result and input SHA refer to those original bytes. It receives no
pathname or public descriptor interface.

## Executed results

```
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:$PWD \
python -m pytest server/tests/test_verified_reader.py \
  server/tests/test_verified_worker.py server/tests/test_verified_asr_acceptance.py -q
28 passed, 3 subtests passed
```

The independent adversarial suite contributes **12 tests**. The reader and worker
owner suites contribute eight tests each. A separate compatibility run covering
jobs, word-transcription metadata, synthetic night integration and the independent
suite passed **46 tests and 19 subtests**, with three existing Starlette/AnyIO and
FastAPI startup deprecation warnings.

## Independent evidence

- Replacing the pathname only during adapter processing no longer substitutes
  decoder input. Exact verified bytes remain available and the original content
  digest is retained in the published transcript metadata.
- A same-inode change fails before changed bytes return to the adapter. A changed
  unread tail is detected by final full verification; no transcript is published.
  Run provenance records initial verification passed and final verification failed.
- An adapter cannot swallow a verified-read integrity error, restore source bytes
  and return a result successfully: the sticky failed state blocks final verification
  and publication. The source can be restored while the failed run remains evidence.
- Failed adapter execution, bad input digest, unavailable size, invalid store scope
  and final verification failures retain failed job/run records and errors while
  leaving `derived_text` empty. Streams close on success and all exercised failures.
- An outside-store path is rejected before adapter invocation or any `pread`, even
  when the synthetic outside file has exactly the expected hash and size.
- After reader construction, patched file-opening APIs prove reads, seeks and final
  checks do not reopen a pathname. `fileno()` is unsupported and `os.fspath(reader)`
  is rejected; no path fallback is exposed by the public input protocol.
- A cross-chunk `readinto` failure leaves both caller buffer and read position
  unchanged. Bounded reads, seek-from-end, EOF and final position preservation are
  exact. Read-budget failure becomes sticky and cannot be treated as successful
  decoder input afterward.
- A lease reclaimed after a verified read still prevents the old worker from
  publishing. The old run stays abandoned, the replacement run remains running,
  and the stale worker does not overwrite its job error/state.

## Legacy fixture compatibility

Per coordinator authorization, minimal updates were made to `test_jobs.py`,
`test_word_transcription.py` and `test_night_integration.py`: synthetic artifacts
now record their accurate size; worker settings use their isolated content store;
the integration adapter reads the supplied stream instead of reopening a path.
The changed-input regression now corrupts bytes without changing length and
asserts the typed SHA256 integrity failure, preserving its original semantic gate.
No production behavior was weakened to accommodate obsolete fixture assumptions.

## Scope and limits

All adapters in this receipt are synthetic. There are no model downloads, private
recordings, production media copies or source repairs. Real offline model/runtime
compatibility is a separate owner receipt, not established by these fake adapters.

The reader reuses the descriptor/chunk verification primitive, retaining one
read-only no-follow store descriptor. Its default aggregate read cap is 1 MiB;
full media/chunk-digest bounds remain 4 GiB, 256 KiB chunks and 16,384 digests.
Large decoder reads fail explicitly, with no temporary-file or pathname fallback.

This is a trusted Python adapter contract, not a sandbox against introspection or
an attestation that a model actually consumed the supplied input. Initial and
final checks do not promise filesystem immutability after return. Transcripts
remain model outputs with unknown correctness confidence; verifying input bytes
adds no claim of factual truth, authorship or acoustic alignment. Existing lease
fencing remains a separate publication guarantee. This local result is not a
commit, deployment or remote execution claim.

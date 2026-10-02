# N51 — independent server WAV-header acceptance

Wave 8 claim: `80bc13e`. Owned additions:
`server/tests/test_wav_scan_acceptance.py` and this receipt. Probe and scanner
production changes belong to N49/N50. Read-count, short-read, cancellation and
mutation instrumentation is a separate independent suite; this report does not
claim those results as its own.

## Concrete old failures preserved

Before this wave, the actual `scan_sources()` pipeline was exercised on two
small synthetic files, with a counting wrapper around its already-open handle:

| Fixture | Previous observed result |
|---|---|
| 44-byte RIFF/WAVE header declaring 16,000 data bytes, with no body | `frames=8000`, `duration_seconds=1.0`, `hash_status=complete`, `parse_status=metadata_only`, global `coverage.complete=true`, no issues |
| 80,044-byte WAV with 10,000 zero-length JUNK chunks, hashing skipped with `max_hash_bytes=1` | 20,009 Python handle `read()` calls returning 80,044 bytes; `parse_status=metadata_only`, `hash_status=unknown`; only `hash_size_limit` issue |

The first fixture is exactly:

```python
fmt = b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 8000, 16000, 2, 16)
raw = b'RIFF' + struct.pack('<I', 16036) + b'WAVE' + fmt + b'data' + struct.pack('<I', 16000)
```

The second inserts `b'JUNK\x00\x00\x00\x00' * 10000` before fmt, uses a
zero-length data chunk and the correct RIFF extent. These counts measure Python
read calls/returned bytes, not physical storage-device I/O. Both original
experiments left source bytes unchanged. Their new regression tests call the
actual scanner, not a mocked parser result.

## Executed acceptance

```
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:$PWD \
python -m pytest server/tests/test_wav_scan_acceptance.py \
  server/tests/test_wav_header.py server/tests/test_wav_scanner_integration.py -q
34 passed, 76 subtests passed
```

The independent N51 suite contributes **17 tests and 56 subtests**. The combined
result also includes 12 producer-owner tests with 20 subtests and five scanner
integration-owner tests. No warnings or failures occurred in this run.

## What changed in the observed results

- The 44-byte false-complete fixture now fails header interpretation explicitly.
  Its exact file hash remains valid as a hash of those 44 bytes; descriptor size
  44, RIFF extent 16,044, declared data size 16,000 and byte rate 16,000 remain
  preserved in `header_probe`. There is no derived duration or duration observation,
  and global scan coverage is incomplete.
- The 10,000-JUNK fixture now yields a partial header and `parse_status=limited`;
  it cannot reach later fmt/data headers outside the bounded prefix and claim
  successful metadata parsing. Actual read-budget instrumentation is reported by
  its separate owner.
- Normal PCM retains legacy scalar values, including duration, while declaring
  same-descriptor header provenance, `fstat` size basis, header-only scope and
  `body_validated=false`. Duration observations explicitly state their arithmetic
  basis. Even ordinary valid-header WAV scans disclose incomplete global coverage
  because audio-body validation was not performed.
- Large valid PCM and IEEE-float headers can supply declared-duration hypotheses
  without reading/decoding the entire body. A duplicate fmt outside an unread data
  payload remains explicitly unknown through `trailing_chunks_uninspected`; it
  is not asserted absent or silently treated as fully validated structure.
- Visible duplicate fmt/data chunks, inconsistent geometry, partial sample frames,
  absent odd padding, empty files and descriptor/RIFF extent contradictions suppress
  duration. Raw inconsistent byte-rate fields remain unchanged.
- Unsupported codecs/extensions and RIFX/RF64/other containers remain explicit.
  Valid odd-chunk padding advances to the proper next header. Existing normal-PCM
  scalar values remain compatible without restoring the old false-complete claim.
- Pure-probe checks cover every basic-header truncation, unknown provider length
  versus observed EOF, maximum unsigned 32-bit extents without wrapping, exact
  64 KiB header boundary versus a crossing header, and strict input types. Supplying
  more than the prefix cap cannot promote effective EOF or full validation.
- Every actual-scanner fixture verifies source bytes and modification timestamp
  remain unchanged. No private corpus, model, source repair or media copy occurs.

## Review boundaries

The probe reports declarations, not measured playback duration, successful decode,
full-container validity, attribution or factual content. Whole-file SHA completion
and header interpretation are separate observations. Correct geometry plus matching
`fstat` size still does not validate audio samples; even a fully supplied small file
retains `body_validated=false`.

The scanner's header budget is independent of hashing/table budgets and uses the
already-safe descriptor. This suite checks semantic outcomes and raw provenance;
its sibling independent suite measures actual reads and mutation behavior. Kotlin/
Python comparison, real CLI export and independent manifest consumption are separate
receipts. This reviewer changed no production code, existing tests, Gradle setup or
commits. No acceptance blocker was found in the exercised scope.

# Server WAV header probe

`server.app.wav_header.probe_wav_header(prefix, provider_size_bytes=None,
end_of_input=False)` is a pure bytes-to-observation function. It does no source
I/O, media decoding, full-content hashing or corpus mutation. It inspects at most
**65,536 bytes**. The separate scanner integration owns safe reads, stream EOF,
actual byte accounting and any full-content digest.

This closes a concrete semantic gap in header interpretation: a 44-byte WAV
header declaring 16,000 data bytes does **not** establish one second of complete
audio. With observed EOF at byte 44, the probe returns `malformed`, preserves
both declared sizes and reports no duration. With an unknown unread tail, an
otherwise consistent header can yield a explicitly labeled duration declaration,
with `body_validated: false`. Neither case establishes complete audio content.

## Python API and model

`prefix` must be exactly `bytes`. Provider size is `None` or a nonnegative signed
64-bit integer; booleans, floats, strings and negative values reject.
`end_of_input` must be a boolean. Invalid API parameters raise `ValueError`;
malformed source bytes instead produce an observation. A supplied bytes object
larger than the prefix budget is not copied wholesale: a bounded memoryview is
used, excess bytes are ignored, `prefix_limit` is recorded and effective EOF is
false. Allocation of the caller's input bytes is outside this pure function.

All observations contain these snake_case fields:

| Fields | Meaning |
|---|---|
| `status`, `coverage` | Status described below; coverage is always `header_prefix_only`. |
| `bytes_inspected`, `prefix_limit_bytes`, `end_of_input` | Supplied bounded prefix window and whether that window reaches observed EOF. This is not decoded-payload coverage. |
| `provider_size_bytes` | Provider-reported size, retained separately or null when unknown. |
| `riff_declared_bytes` | Total declared RIFF extent: unsigned size field at offset 4 plus eight bytes. |
| `format_code`, `channels`, `sample_rate_hz`, `byte_rate`, `block_align_bytes`, `bits_per_sample` | Raw scalar format declarations, null when not observed. |
| `data_declared_bytes` | Raw first observed data-chunk length. |
| `declared_duration_sec`, `duration_basis` | Optional finite arithmetic declaration and its explicit basis. |
| `body_validated` | Always false. |
| `issues` | Bounded distinct issue codes with message and inert `wav-prefix#byte=N` locator. No raw media bytes. |

Statuses are `observed`, `partial`, `malformed`, `unsupported` and
`size_mismatch`, with malformed taking precedence over unsupported and provider
size mismatch when several issues coexist. All issues remain reported. A status
of observed means required header declarations were internally consistent under
this narrow profile; it is not successful playback, waveform validity or proof
of absent unobserved duplicate chunks.

## Narrow declaration profile

The supported container is little-endian RIFF/WAVE. Basic `fmt ` lengths 16 and
18 (the latter only with zero extension length) support PCM format 1 at
8/16/24/32 bits and IEEE float format 3 at 32/64 bits. Positive channels, rate,
byte rate and alignment must satisfy `blockAlign = channels * bits / 8` and
`byteRate = sampleRate * blockAlign`. Data length must divide into whole sample
frames. Compressed formats, WAVE extensible, RF64/RIFX and other extensions are
explicitly unsupported.

RIFF/chunk arithmetic uses unsigned header values promoted to Python integers;
chunk payloads and required odd-byte padding must fit the declared RIFF extent.
The parser scans every visible chunk header, including after a fully visible
first data chunk. Duplicate fmt/data headers and data-before-fmt are rejected.
Unknown chunks can be skipped only when their declared next header lies within
the bounded prefix. Their unread tail remains partial. Large unread data payloads
are distinguished from missing required headers; they can preserve the header
hypothesis while explicitly stating that body/trailing chunks were not examined.

Only internally consistent required declarations without error or incomplete
header state yield:

`declared_duration_sec = data_declared_bytes / byte_rate`

The basis string is `declared_data_bytes_divided_by_header_byte_rate`.
Zero-byte data yields zero; a zero/inconsistent byte rate is invalid rather
than divided by. All integer extents and validated rates bound the result to a
finite value. Error states preserve raw observed declarations but suppress both
duration and basis. Provider-size disagreement is not silently reconciled.

## EOF and native comparison

`end_of_input=True` must mean the supplied prefix itself reaches observed EOF.
A complete larger stream represented by only a retained 64 KiB prefix must use
false here. Its reader may separately compare actual EOF byte count with RIFF
extent and report a mismatch without overwriting the provider's original size.
A full-file digest requires reading/hashing the complete stream; the probe's
header observation cannot grant it.

The native common-core `WavHeaderProbe` supplies a useful second implementation
of these declaration semantics. It is **not identical I/O**: server file access
and Android SAF/provider access have separate safety, size and EOF contracts.
The Python API also deliberately rejects invalid argument types/negative
provider sizes, whereas native argument types and malformed-size reporting differ.
Cross-runtime comparison must use valid shared argument shapes and compare pure
observations, not claim interchangeable streams, permissions or cache behavior.

## Evidence and preservation

`server/tests/test_wav_header.py` includes the exact 44-byte truncated regression,
unknown-tail declaration, PCM/float/empty data, conflicting provider size,
visible duplicates/order, unsupported profiles, geometry, odd padding, unsigned
extents, exact prefix boundaries, invalid parameter types, arbitrary finite-JSON
observations and unchanged source bytes. Separate independent tests and a
cross-runtime comparison add different fixtures.

Preserved are raw observed scalar declarations, the independent provider size
and explicit coverage/issues. Added are narrow validation rules and a labeled
arithmetic hypothesis. Omitted are payload bytes, unobserved headers, decoder
results and audio integrity. The observation cannot reconstruct or validate a
recording. Tests use generated bytes only; no real corpus or new dependencies
are required.

# Bounded WAV header observations

`WavHeaderProbe.probe(prefix, providerSizeBytes = null, endOfInput = false)` is a
pure common-core parser. It inspects at most the first **65,536 bytes** supplied
and returns a serializable `WavHeaderObservation`. It never opens a URI, copies
media, computes a full-file hash, decodes audio or changes the source. The
existing full-input `SourceTextParser.wavDuration` is unchanged.

## Supported declaration profile

The narrow profile is little-endian `RIFF` with form `WAVE`, a basic `fmt ` chunk
of 16 bytes (or 18 bytes with zero extension length), followed by a `data` chunk.
Supported encoding declarations are PCM code 1 at 8/16/24/32 bits, and IEEE float
code 3 at 32/64 bits. Channels, sample rate, byte rate and block alignment must be
positive and consistent: `blockAlign = channels * (bits / 8)` and
`byteRate = sampleRate * blockAlign`. Data length must contain whole sample frames.
Compressed formats, extensible WAVE, RF64/RIFX and other structures remain
explicitly unsupported; they do not receive a guessed duration.

Unsigned 32-bit RIFF/chunk lengths are promoted to `Long` before arithmetic.
`riffDeclaredBytes` is the **total declared container extent**, i.e. the stored
RIFF size field plus eight bytes. Payload ends and required odd-byte chunk padding
must fit that extent. Visible unknown chunks are skipped by declared extent;
if their next header lies outside the bounded prefix, coverage is partial.
Visible second `fmt ` or `data` headers are errors even if the first data body
was already encountered. Headers after a large unread data payload cannot be
checked for duplicates; the observation explicitly does not claim otherwise.

When internally consistent `fmt ` and `data` declarations have been observed,
`declaredDurationSec = dataDeclaredBytes / byteRate`, with basis
`declared_data_bytes_divided_by_header_byte_rate`. A complete audio payload is
not required for this **header-derived declaration**. A `data` payload extending
beyond the prefix remains explicitly uninspected. This value is not measured
playback duration, successful decoding, complete sample validation or integrity
verification. Even when all input bytes happen to fit in the prefix,
`bodyValidated` stays false.

## Observation and errors

The observation preserves nullable raw scalar declarations: RIFF total extent,
format code, channels, sample rate, byte rate, alignment, sample width and data
byte count. The provider's reported size is a separate nullable field. Errors do
not repair these values or replace them with a chosen interpretation.

| Status | Meaning |
|---|---|
| `observed` | Supported, internally consistent required declarations were observed. Only header provenance is claimed. |
| `partial` | The bounded input does not establish all required declarations. |
| `malformed` | Visible structure/geometry is inconsistent, duplicated or truncated at an observed EOF. |
| `unsupported` | Visible container/encoding/extension is outside the narrow supported profile. |
| `size_mismatch` | Provider size disagrees with the declared RIFF total extent. |

Multiple issues remain present even when one status takes precedence. Parser
precedence is malformed, unsupported, size mismatch, then partial/observed.
Malformed, unsupported, conflicting-size and incomplete-header observations have
no duration or duration basis. `ScanIssue` codes include the relevant prefix byte
locator; one issue per code bounds repeated-duplicate reporting. These locators
are inert strings, not access permissions or file paths.

`bytesInspected` describes the bounded supplied prefix window, not proof that
all payload bytes in that window were decoded. `coverage` is always
`header_prefix_only`; `prefixLimitBytes` is 65,536. No raw byte array is stored in
the serializable model. `ScannedSourceFile.wavHeader` is integrated separately as
a nullable default, so older cached records remain distinguishable from new
header observations.

## EOF, provider metadata and stream integration

`endOfInput=true` means the **supplied prefix itself reaches observed EOF**.
A short prefix with unknown continuation is partial, while the same prefix
ending before declared content at a known EOF is malformed. If a caller supplies
more than 65,536 bytes, the parser ignores the suffix, records the prefix limit
and treats its bounded window as not reaching EOF. No read beyond the prefix
budget is attempted.

When a stream helper fully reads a larger file but retains only its prefix,
it must pass `endOfInput=false` to this parser. The helper can separately compare
its actual complete-stream byte count to `riffDeclaredBytes`, preserving the
provider's original size metadata. A discrepancy invalidates the declared
duration and is explicitly reported. A full-content hash is meaningful only
when that helper actually observed EOF and hashed every received byte; a header
observation never grants a full-file hash. Stream cancellation, actual byte
budgets, URI access and closure are separate integration responsibilities.

## Preservation, loss and tests

Preserved: raw observed header scalars, independent provider size, byte-window
scope and explicit issues. Added: validation rules and a labeled arithmetic
duration projection. Omitted: raw media payload, unobserved chunks, acoustic
content and decoder validation. Neither the observation nor its serialized cache
can reconstruct the recording. No correction or shared audio ontology is added.

`WavHeaderProbeTest` covers small complete PCM, large declared data without body,
unknown provider size, empty data, float and minimal extensions, compressed
formats, provider conflict, known EOF truncation, visible duplicates, rate/frame
geometry, odd padding, unsigned oversized lengths, exact 64 KiB header placement
and arbitrary prefixes. Independent `WavHeaderProbeAdversarialTest` adds separate
fixtures. Legacy `SourceTextParserTest` remains a regression boundary. Direct JVM
and integrated build receipts are distinct from SAF/provider or phone runtime
acceptance; all parser fixtures are synthetic.

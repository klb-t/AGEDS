# Verified media descriptor serving

The content route previously hashed a pathname, then returned `FileResponse`,
which opened that pathname again. A replacement between those operations could
send bytes never checked against the artifact's recorded SHA256. Holding the
same descriptor removes that replacement race. Because an open regular file can
still be modified in place, descriptor retention alone is insufficient.

`server.app.verified_media` now initially verifies the full descriptor against
the recorded SHA256 **and known byte count**, recording SHA256 digests for fixed
chunks in a compact byte array. Each outgoing chunk is reread from that same
descriptor and verified against its initial chunk digest **before** its immutable
buffer or requested slice is yielded. A concurrent edit after buffering cannot
change that buffer. A concurrent edit before/during reading is rejected unless
those buffered bytes still match the verified digest. This relies on SHA256's
collision resistance; it is not a physical WORM or provenance/authorship claim.

## API and ownership

```python
verified_media_response(
    path, expected_sha256, expected_size,
    media_type=None, filename=None, range_header=None, head=False,
    if_range=None, store_root=None,
)
```

Optional arguments are keyword-only. Invoke this synchronous preparation from a
worker thread, as FastAPI does for synchronous routes. Full-file verification
must not run directly on an asynchronous event loop. Subsequent chunk reads use
AnyIO's thread worker with non-abandoning cancellation behavior.

The resulting `VerifiedMediaResponse` owns its `media` descriptor. Its ASGI
`__call__` has shielded cleanup on completion, failed sends, verification errors
and cancellation. `close()` is available for callers that never hand the
response to ASGI; the media object also has a best-effort finalizer. A caller
retaining an uncalled response must explicitly close it rather than rely on
when garbage collection runs. Construction errors close the descriptor. HEAD
and 416 responses close before returning and own no media descriptor.

Provide `store_root` in the content route. Containment is lexical, rejects `..`,
and walks the absolute configured root and relative parent components using
no-follow directory descriptors. The final open uses
`O_RDONLY | O_NOFOLLOW | O_NONBLOCK`; `fstat` must show a regular file. This avoids
following a final symlink, hanging on a FIFO, or resolving mutable parent
symlinks before reopening a path. Intermediate symlinks, including configured
root components, are unsupported. Without `store_root`, only the final component
is protected from following symlinks; that mode is for explicitly trusted paths.
Unsupported descriptor primitives fail closed. No source writes, temporary media
copies or snapshots are used.

| Outcome | Contract |
|---|---|
| Missing/unsafe/nonregular/outside-store path | `MediaUnavailable(ValueError)`, mapped to HTTP 409 with generic text. |
| Missing/invalid recorded SHA256 or size; initial mismatch | `MediaIntegrityError(ValueError)`, HTTP 409. Unknown historical size is not fabricated. |
| Limit exceeded | `MediaLimitError(ValueError)`, HTTP 413. |
| Changes discovered after response headers | Abort the stream and close; status can no longer change to 409. Already sent chunks were verified; no altered chunk is yielded. |

## Bounds and HTTP behavior

The fixed chunk size is **262,144 bytes**, with at most **16,384 chunks** and
**4,294,967,296 media bytes (4 GiB)**. Chunk hashes occupy at most 524,288 bytes.
Reads and temporary buffers are chunk-bounded. These limits intentionally reduce
the prior route's unlimited file-size scope; larger content requires a separately
reviewed serving policy. Every request, including ranges and HEAD, verifies the
entire initial file, which costs time proportional to its size. Streaming does
not block the event loop, but full verification occupies a worker thread.

GET without Range returns 200 and the exact content length. One `bytes` range is
supported: closed (`bytes=10-19`), open (`bytes=10-`) or suffix (`bytes=-10`).
Valid ranges return 206, `Content-Range` and the selected length. A range may
span chunks; every entire covering chunk is checked before slicing. Malformed,
unsupported-unit, empty, multiple or unsatisfiable ranges return 416 with
`Content-Range: bytes */<size>`. Range input is capped at 256 characters. Empty
media supports ordinary 200 with zero length; all byte ranges are unsatisfiable.

HEAD ignores Range and returns 200/full length without a body. Responses advertise
`Accept-Ranges: bytes`, `Cache-Control: no-store` and strong
`ETag: "sha256:<recorded lowercase digest>"`. If-Range honors a range only for an
exact matching strong ETag; weak tags, dates and other values fall back to full
200. No Last-Modified timestamp or date equivalence is inferred. MIME header
controls/non-ASCII are rejected; filenames are UTF-8 percent-encoded in
`filename*`, so literal control characters never enter HTTP headers.

## Evidence

`server/tests/test_verified_media.py` uses only generated bytes and covers full
and ranged body equality, pathname replacement, in-place chunk mutation,
whole-covering-chunk verification, initial integrity and bounds, FIFO/symlink
rejection, HEAD/If-Range/416 semantics, failed sends and unsafe headers.
Independent adversarial tests cover the HTTP integration and descriptor lifecycle.
Browser playback is a separate regression receipt; these tests do not establish
acoustic alignment, genuine-recording quality or physical-device behavior.

"""Pure bounded WAV declaration probe; no audio decoding or source access."""
from __future__ import annotations

MAX_PREFIX_BYTES = 65536
MAX_PROVIDER_SIZE = 2**63 - 1
DURATION_BASIS = 'declared_data_bytes_divided_by_header_byte_rate'


def probe_wav_header(prefix: bytes, provider_size_bytes=None, end_of_input=False) -> dict:
    """Inspect at most 64 KiB; EOF describes the supplied prefix, not a hidden tail.

    A duration is arithmetic on header declarations, never body validation.
    Invalid API arguments raise ValueError; malformed source bytes are observations.
    """
    if type(prefix) is not bytes:
        raise ValueError('prefix must be bytes')
    if provider_size_bytes is not None and (type(provider_size_bytes) is not int or
                                           not 0 <= provider_size_bytes <= MAX_PROVIDER_SIZE):
        raise ValueError('provider size must be a nonnegative signed 64-bit integer or None')
    if type(end_of_input) is not bool:
        raise ValueError('end_of_input must be boolean')
    length = min(len(prefix), MAX_PREFIX_BYTES)
    data = memoryview(prefix)[:length]
    eof = end_of_input and len(prefix) <= MAX_PREFIX_BYTES
    result = dict(status='partial', coverage='header_prefix_only', bytes_inspected=length,
                  prefix_limit_bytes=MAX_PREFIX_BYTES, end_of_input=eof,
                  provider_size_bytes=provider_size_bytes, riff_declared_bytes=None,
                  format_code=None, channels=None, sample_rate_hz=None, byte_rate=None,
                  block_align_bytes=None, bits_per_sample=None, data_declared_bytes=None,
                  declared_duration_sec=None, duration_basis=None, body_validated=False, issues=[])
    flags = dict(malformed=False, unsupported=False, size_mismatch=False, partial=False)
    fmt_seen = data_seen = False

    def issue(code, message, offset=0):
        if not any(item['code'] == code for item in result['issues']):
            result['issues'].append(dict(code=code, message=message, locator=f'wav-prefix#byte={offset}'))

    def bad(code, message, offset=0):
        flags['malformed'] = True
        issue(code, message, offset)

    def unavailable(message, offset):
        if eof:
            bad('truncated_header', message, offset)
        else:
            flags['partial'] = True
            issue('header_prefix_incomplete', message, offset)

    def number(offset, count):
        return int.from_bytes(data[offset:offset + count], 'little')

    def finish():
        valid = (not any(flags.values()) and result['format_code'] is not None
                 and result['data_declared_bytes'] is not None and result['byte_rate'] is not None)
        result['status'] = next((name for name in ('malformed', 'unsupported', 'size_mismatch') if flags[name]),
                                'observed' if valid else 'partial')
        if valid:
            result['declared_duration_sec'] = result['data_declared_bytes'] / result['byte_rate']
            result['duration_basis'] = DURATION_BASIS
        return result

    if len(prefix) > MAX_PREFIX_BYTES:
        issue('prefix_limit', 'Only the first 65536 bytes were inspected.')
    if length < 12:
        unavailable('RIFF/WAVE header is incomplete.', 0)
        return finish()
    if data[:4] != b'RIFF':
        flags['unsupported'] = True
        issue('unsupported_container', 'Only little-endian RIFF is supported; RF64/RIFX and other containers are not interpreted.')
        return finish()
    if data[8:12] != b'WAVE':
        flags['unsupported'] = True
        issue('unsupported_wave_type', 'RIFF form is not WAVE.', 8)
        return finish()
    declared = number(4, 4) + 8
    result['riff_declared_bytes'] = declared
    if declared < 12:
        bad('invalid_riff_extent', 'RIFF extent is smaller than its header.', 4)
        return finish()
    if provider_size_bytes is not None and provider_size_bytes != declared:
        flags['size_mismatch'] = True
        issue('provider_size_mismatch', 'Provider byte count differs from RIFF declared extent.', 4)
    if eof and length != declared:
        bad('truncated_or_trailing_container', 'Observed EOF differs from RIFF declared extent.', 4)
    if length > declared:
        bad('trailing_bytes', 'Inspected bytes extend beyond RIFF declared extent.', declared)
    position = 12
    while position < declared:
        if declared - position < 8:
            bad('truncated_chunk_header', 'RIFF ends inside a chunk header.', position)
            break
        if position + 8 > length:
            unavailable('Next chunk header is outside the inspected prefix.', position)
            break
        chunk_id = bytes(data[position:position + 4])
        size = number(position + 4, 4)
        end = position + 8 + size
        next_position = end + (size & 1)
        if end > declared or next_position > declared:
            bad('chunk_extent_outside_riff', 'Chunk payload or required odd-byte padding exceeds RIFF extent.', position)
            break
        if chunk_id == b'fmt ':
            if fmt_seen:
                bad('duplicate_fmt', 'A second visible fmt chunk makes the declaration ambiguous.', position)
            else:
                fmt_seen = True
                if size < 16:
                    bad('invalid_fmt_size', 'fmt payload is shorter than its basic fields.', position)
                elif position + 24 > length:
                    unavailable('Basic fmt fields are outside the prefix.', position)
                    break
                else:
                    format_code, channels = number(position + 8, 2), number(position + 10, 2)
                    sample_rate, byte_rate = number(position + 12, 4), number(position + 16, 4)
                    align, bits = number(position + 20, 2), number(position + 22, 2)
                    result.update(format_code=format_code, channels=channels, sample_rate_hz=sample_rate,
                                  byte_rate=byte_rate, block_align_bytes=align, bits_per_sample=bits)
                    if format_code not in (1, 3):
                        flags['unsupported'] = True
                        issue('unsupported_format', 'Only PCM (1) and IEEE float (3) are supported.', position)
                    if size not in (16, 18):
                        flags['unsupported'] = True
                        issue('unsupported_fmt_extension', 'Only basic 16-byte fmt or 18-byte fmt with zero extension length is supported.', position)
                    if size == 18:
                        if position + 26 > length:
                            unavailable('fmt extension length is outside the prefix.', position)
                            break
                        if number(position + 24, 2) != 0:
                            flags['unsupported'] = True
                            issue('unsupported_fmt_extension', 'Nonempty format extensions are unsupported.', position)
                    if ((format_code == 1 and bits not in (8, 16, 24, 32)) or
                            (format_code == 3 and bits not in (32, 64))):
                        flags['unsupported'] = True
                        issue('unsupported_sample_width', 'Sample width is outside the supported PCM/float profile.', position)
                    if (not all((channels, sample_rate, byte_rate, align, bits)) or bits % 8 or
                            align != channels * (bits // 8) or byte_rate != sample_rate * align):
                        bad('invalid_format_geometry', 'Channels, sample width, alignment, sample rate and byte rate are inconsistent.', position)
        elif chunk_id == b'data':
            if data_seen:
                bad('duplicate_data', 'A second visible data chunk makes the duration declaration ambiguous.', position)
            else:
                data_seen = True
                result['data_declared_bytes'] = size
            if not fmt_seen:
                bad('data_before_fmt', 'This profile requires fmt before data.', position)
            align = result['block_align_bytes']
            if align and size % align:
                bad('unaligned_data_size', 'Declared data bytes do not contain whole sample frames.', position)
        if next_position > length:
            if eof:
                bad('truncated_chunk_payload', 'Observed EOF is before declared payload/padding end.', position)
            elif chunk_id == b'data':
                issue('data_body_uninspected', 'Data payload/padding was not completely read or decoded.', position)
                if next_position < declared:
                    issue('trailing_chunks_uninspected', 'Chunks after the data payload are outside the prefix; duplicates there are unknown.', next_position)
            else:
                flags['partial'] = True
                issue('header_prefix_incomplete', 'A chunk extends beyond the prefix, so later headers are unknown.', position)
            break
        position = next_position
    if not fmt_seen or not data_seen:
        if position >= declared or eof:
            bad('missing_required_chunk', 'A complete visible container lacks fmt or data.', position)
        else:
            flags['partial'] = True
            issue('required_header_unobserved', 'fmt or data header has not been observed in the prefix.', position)
    issue('audio_body_not_validated', 'Duration is a header declaration; audio samples, decoding and complete-content integrity were not validated.')
    return finish()

package dev.klbt.ageds.core

/** Pure bounded RIFF/WAVE header observation. Never changes bytes or decodes the body. */
object WavHeaderProbe {
    const val MAX_PREFIX_BYTES = 65536
    const val DURATION_BASIS = "declared_data_bytes_divided_by_header_byte_rate"

    fun probe(prefix: ByteArray, providerSizeBytes: Long? = null, endOfInput: Boolean = false): WavHeaderObservation {
        val length = minOf(prefix.size, MAX_PREFIX_BYTES)
        val eof = endOfInput && prefix.size <= MAX_PREFIX_BYTES
        val issues = mutableListOf<ScanIssue>()
        var malformed = false
        var unsupported = false
        var mismatch = false
        var partial = false
        var riff: Long? = null
        var format: Int? = null
        var channels: Int? = null
        var sampleRate: Long? = null
        var rate: Long? = null
        var align: Int? = null
        var bits: Int? = null
        var data: Long? = null
        var fmtSeen = false
        var dataSeen = false
        fun issue(code: String, message: String, offset: Long = 0) {
            if (issues.none { it.code == code }) issues += ScanIssue(code, message, "wav-prefix#byte=$offset")
        }
        fun bad(code: String, message: String, offset: Long = 0) {
            malformed = true
            issue(code, message, offset)
        }
        fun unavailable(message: String, offset: Long) {
            if (eof) bad("truncated_header", message, offset)
            else { partial = true; issue("header_prefix_incomplete", message, offset) }
        }
        fun ascii(position: Int) = (position until position + 4).map { prefix[it].toInt().and(255).toChar() }.joinToString("")
        fun u16(position: Int) = prefix[position].toInt().and(255) + (prefix[position + 1].toInt().and(255) shl 8)
        fun u32(position: Int): Long = (0..3).sumOf { prefix[position + it].toLong().and(255) shl (8 * it) }
        fun result(): WavHeaderObservation {
            val valid = !malformed && !unsupported && !mismatch && !partial && format != null && data != null && rate != null
            val duration = if (valid) data!!.toDouble() / rate!!.toDouble() else null
            return WavHeaderObservation(
                status = when { malformed -> "malformed"; unsupported -> "unsupported"; mismatch -> "size_mismatch"; valid -> "observed"; else -> "partial" },
                bytesInspected = length, endOfInput = eof, providerSizeBytes = providerSizeBytes,
                riffDeclaredBytes = riff, formatCode = format, channels = channels,
                sampleRateHz = sampleRate, byteRate = rate, blockAlignBytes = align,
                bitsPerSample = bits, dataDeclaredBytes = data, declaredDurationSec = duration,
                durationBasis = if (duration != null) DURATION_BASIS else null,
                issues = issues.toList(),
            )
        }
        if (prefix.size > MAX_PREFIX_BYTES) issue("prefix_limit", "Only the first 65536 bytes were inspected.")
        if (providerSizeBytes != null && providerSizeBytes < 0) bad("invalid_provider_size", "Provider size is negative.")
        if (length < 12) { unavailable("RIFF/WAVE header is incomplete.", 0); return result() }
        val container = ascii(0)
        if (container != "RIFF") {
            unsupported = true
            issue("unsupported_container", "Only little-endian RIFF is supported; RF64/RIFX and other containers are not interpreted.")
            return result()
        }
        if (ascii(8) != "WAVE") {
            unsupported = true
            issue("unsupported_wave_type", "RIFF form is not WAVE.", 8)
            return result()
        }
        val declared = u32(4) + 8L
        riff = declared
        if (declared < 12) { bad("invalid_riff_extent", "RIFF extent is smaller than its header.", 4); return result() }
        if (providerSizeBytes != null && providerSizeBytes >= 0 && providerSizeBytes != declared) {
            mismatch = true
            issue("provider_size_mismatch", "Provider byte count differs from RIFF declared extent.", 4)
        }
        if (eof && length.toLong() != declared) bad("truncated_or_trailing_container", "Observed EOF differs from RIFF declared extent.", 4)
        if (length.toLong() > declared) bad("trailing_bytes", "Inspected bytes extend beyond RIFF declared extent.", declared)
        var position = 12L
        while (position < declared) {
            if (declared - position < 8L) { bad("truncated_chunk_header", "RIFF ends inside a chunk header.", position); break }
            if (position + 8L > length.toLong()) { unavailable("Next chunk header is outside the inspected prefix.", position); break }
            val p = position.toInt()
            val id = ascii(p)
            val size = u32(p + 4)
            val end = position + 8L + size
            val next = end + (size and 1L)
            if (end > declared || next > declared) { bad("chunk_extent_outside_riff", "Chunk payload or required odd-byte padding exceeds RIFF extent.", position); break }
            when (id) {
                "fmt " -> {
                    if (fmtSeen) bad("duplicate_fmt", "A second visible fmt chunk makes the declaration ambiguous.", position)
                    else {
                        fmtSeen = true
                        if (size < 16L) bad("invalid_fmt_size", "fmt payload is shorter than its basic fields.", position)
                        else if (position + 24L > length.toLong()) { unavailable("Basic fmt fields are outside the prefix.", position); break }
                        else {
                            format = u16(p + 8); channels = u16(p + 10); sampleRate = u32(p + 12)
                            rate = u32(p + 16); align = u16(p + 20); bits = u16(p + 22)
                            if (format !in listOf(1, 3)) { unsupported = true; issue("unsupported_format", "Only PCM (1) and IEEE float (3) are supported.", position) }
                            if (size != 16L && size != 18L) { unsupported = true; issue("unsupported_fmt_extension", "Only basic 16-byte fmt or 18-byte fmt with zero extension length is supported.", position) }
                            if (size == 18L) {
                                if (position + 26L > length.toLong()) { unavailable("fmt extension length is outside the prefix.", position); break }
                                if (u16(p + 24) != 0) { unsupported = true; issue("unsupported_fmt_extension", "Nonempty format extensions are unsupported.", position) }
                            }
                            if ((format == 1 && bits !in listOf(8, 16, 24, 32)) || (format == 3 && bits !in listOf(32, 64))) {
                                unsupported = true; issue("unsupported_sample_width", "Sample width is outside the supported PCM/float profile.", position)
                            }
                            if (channels == 0 || sampleRate == 0L || rate == 0L || align == 0 || bits == 0 || bits!! % 8 != 0 ||
                                align!!.toLong() != channels!!.toLong() * (bits!! / 8) || rate != sampleRate!! * align!!) {
                                bad("invalid_format_geometry", "Channels, sample width, alignment, sample rate and byte rate are inconsistent.", position)
                            }
                        }
                    }
                }
                "data" -> {
                    if (dataSeen) bad("duplicate_data", "A second visible data chunk makes the duration declaration ambiguous.", position)
                    else { dataSeen = true; data = size }
                    if (!fmtSeen) bad("data_before_fmt", "This profile requires fmt before data.", position)
                    if (align != null && align!! > 0 && size % align!! != 0L) bad("unaligned_data_size", "Declared data bytes do not contain whole sample frames.", position)
                }
            }
            if (next > length.toLong()) {
                if (eof) bad("truncated_chunk_payload", "Observed EOF is before declared payload/padding end.", position)
                else if (id == "data") {
                    issue("data_body_uninspected", "Data payload/padding was not completely read or decoded.", position)
                    if (next < declared) issue("trailing_chunks_uninspected", "Chunks after the data payload are outside the prefix; duplicates there are unknown.", next)
                } else { partial = true; issue("header_prefix_incomplete", "A chunk extends beyond the prefix, so later headers are unknown.", position) }
                break
            }
            position = next
        }
        if (!fmtSeen || !dataSeen) {
            if (position >= declared || eof) bad("missing_required_chunk", "A complete visible container lacks fmt or data.", position)
            else { partial = true; issue("required_header_unobserved", "fmt or data header has not been observed in the prefix.", position) }
        }
        issue("audio_body_not_validated", "Duration is a header declaration; audio samples, decoding and complete-content integrity were not validated.")
        return result()
    }
}

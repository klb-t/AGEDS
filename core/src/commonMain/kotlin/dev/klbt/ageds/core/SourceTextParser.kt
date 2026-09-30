package dev.klbt.ageds.core

/** RFC4180-style quoted records; no header, date, number or identity inference. */
object SourceTextParser {
    fun delimited(text: String, delimiter: Char, locator: String, limits: SourceScanLimits,
                  checkCancelled: () -> Unit = {}): ParsedSourceRows = parseDelimited(text, delimiter, locator, limits, true, checkCancelled)

    /** Byte decoding already consumed the transport BOM; leading U+FEFF is cell data. */
    fun decodedDelimited(text: String, delimiter: Char, locator: String, limits: SourceScanLimits,
                         checkCancelled: () -> Unit = {}): ParsedSourceRows = parseDelimited(text, delimiter, locator, limits, false, checkCancelled)

    private fun parseDelimited(text: String, delimiter: Char, locator: String, limits: SourceScanLimits,
                               stripBom: Boolean, checkCancelled: () -> Unit): ParsedSourceRows {
        require(delimiter in listOf(',', '\t', ';', '|'))
        val rows = mutableListOf<SourceRow>()
        val issues = mutableListOf<ScanIssue>()
        fun issue(value: ScanIssue) {
            if (issues.size < 100) issues += value
            else if (issues.size == 100) issues += ScanIssue("diagnostic_limit", "Further CSV diagnostics omitted", locator)
        }
        var pos = if (stripBom && text.startsWith('\uFEFF')) 1 else 0
        var count = 0
        var line = 1
        while (pos < text.length) {
            checkCancelled()
            if (rows.size >= limits.maxRowsPerFile) {
                issues += ScanIssue("row_limit", "Remaining records were not parsed", locator); break
            }
            val startLine = line
            val cells = mutableListOf<SourceCell>()
            var recordDone = false
            while (!recordDone) {
                if (count >= limits.maxCellsPerFile) {
                    issues += ScanIssue("cell_limit", "Remaining cells were not parsed", "$locator#line=$line")
                    return ParsedSourceRows(rows + SourceRow("$locator#line=$startLine", cells), issues)
                }
                val start = pos
                val value = StringBuilder()
                var malformed = false
                if (pos < text.length && text[pos] == '"') {
                    pos++
                    var closed = false
                    while (pos < text.length) {
                        if (pos % 4096 == 0) checkCancelled()
                        val c = text[pos++]
                        if (c == '"') {
                            if (pos < text.length && text[pos] == '"') { value.append('"'); pos++ }
                            else { closed = true; break }
                        } else { value.append(c); if (c == '\n' || (c == '\r' && (pos == text.length || text[pos] != '\n'))) line++ }
                        if (pos - start > limits.maxCellChars) {
                            issues += ScanIssue("cell_chars_limit", "Oversized cell and subsequent records omitted", "$locator#line=$startLine")
                            return ParsedSourceRows(rows + SourceRow("$locator#line=$startLine", cells), issues)
                        }
                    }
                    if (!closed) malformed = true
                    while (pos < text.length && text[pos] != delimiter && text[pos] != '\r' && text[pos] != '\n') {
                        malformed = true; value.append(text[pos++])
                        if (pos - start > limits.maxCellChars) break
                    }
                } else {
                    while (pos < text.length && text[pos] != delimiter && text[pos] != '\r' && text[pos] != '\n') {
                        if (pos % 4096 == 0) checkCancelled()
                        val c = text[pos++]; value.append(c)
                        if (c == '"') malformed = true
                        if (pos - start > limits.maxCellChars) break
                    }
                }
                if (pos - start > limits.maxCellChars) {
                    issues += ScanIssue("cell_chars_limit", "Oversized cell and subsequent records omitted", "$locator#line=$startLine")
                    return ParsedSourceRows(rows + SourceRow("$locator#line=$startLine", cells), issues)
                }
                cells += SourceCell(cells.size + 1, text.substring(start, pos), value.toString())
                count++
                if (malformed) issue(ScanIssue("malformed_csv", "Malformed quoting retained; decoded value is provisional", "$locator#line=$startLine"))
                if (pos >= text.length) recordDone = true
                else if (text[pos] == delimiter) pos++
                else {
                    if (text[pos] == '\r') { pos++; if (pos < text.length && text[pos] == '\n') pos++ }
                    else pos++
                    line++; recordDone = true
                }
            }
            rows += SourceRow("$locator#line=$startLine", cells)
        }
        return ParsedSourceRows(rows, issues)
    }

    /** Basic RIFF PCM/float WAV, no decoding. Compressed/ambiguous formats stay unknown. */
    fun wavDuration(bytes: ByteArray): Double? {
        fun ascii(p: Int, n: Int) = if (p >= 0 && p + n <= bytes.size)
            (p until p + n).map { bytes[it].toInt().and(255).toChar() }.joinToString("") else ""
        fun u16(p: Int) = bytes[p].toInt().and(255) + (bytes[p + 1].toInt().and(255) shl 8)
        fun u32(p: Int): Long = (0..3).sumOf { bytes[p + it].toLong().and(255) shl (8 * it) }
        if (bytes.size < 12 || ascii(0, 4) != "RIFF" || ascii(8, 4) != "WAVE") return null
        val declared = u32(4) + 8
        if (declared != bytes.size.toLong()) return null
        var pos = 12
        var byteRate: Long? = null
        var dataSize: Long? = null
        while (pos.toLong() + 8 <= declared) {
            val size = u32(pos + 4)
            val end = pos.toLong() + 8 + size
            if (end > declared) return null
            when (ascii(pos, 4)) {
                "fmt " -> {
                    if (byteRate != null || size < 16) return null
                    val format = u16(pos + 8)
                    val channels = u16(pos + 10)
                    val sampleRate = u32(pos + 12)
                    val rate = u32(pos + 16)
                    val align = u16(pos + 20)
                    val bits = u16(pos + 22)
                    if (format !in listOf(1, 3) || channels == 0 || sampleRate == 0L || align == 0 ||
                        bits == 0 || bits % 8 != 0 || align != channels * (bits / 8) || rate != sampleRate * align) return null
                    byteRate = rate
                }
                "data" -> { if (dataSize != null) return null; dataSize = size }
            }
            val next = end + (size and 1L)
            if (next > declared) return null
            pos = next.toInt()
        }
        if (pos.toLong() != declared || byteRate == null || dataSize == null) return null
        return dataSize.toDouble() / byteRate.toDouble()
    }
}

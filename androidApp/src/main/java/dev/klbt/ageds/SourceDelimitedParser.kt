package dev.klbt.ageds

import dev.klbt.ageds.core.*
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction

/** No guessed legacy code page. A BOM is required for UTF-16; other input is strict UTF-8. */
internal object SourceTextDecoding {
    data class Decoded(val text: String, val encoding: String, val bomBytes: Int)
    fun decode(bytes: ByteArray): Decoded {
        fun prefix(vararg values: Int) = bytes.size >= values.size &&
            values.indices.all { bytes[it].toInt().and(255) == values[it] }
        require(!prefix(0xff, 0xfe, 0, 0) && !prefix(0, 0, 0xfe, 0xff)) { "UTF-32 is unsupported" }
        val (charset, bom) = when {
            prefix(0xef, 0xbb, 0xbf) -> Charsets.UTF_8 to 3
            prefix(0xff, 0xfe) -> Charsets.UTF_16LE to 2
            prefix(0xfe, 0xff) -> Charsets.UTF_16BE to 2
            else -> Charsets.UTF_8 to 0
        }
        val text = charset.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(bytes, bom, bytes.size - bom)).toString()
        require(!text.contains('\u0000')) { "NUL text is unsupported; UTF-16 requires a BOM" }
        return Decoded(text, charset.name(), bom)
    }
}

/** Bounded dialect hypothesis; lexical cells stay available without numeric/date normalization. */
object SourceDelimitedParser {
    fun parse(bytes: ByteArray, locator: String, limits: SourceScanLimits, tsv: Boolean = false,
              checkCancelled: () -> Unit = {}): ParsedSourceRows {
        checkCancelled()
        val decoded = try { SourceTextDecoding.decode(bytes) }
        catch (e: Exception) {
            return ParsedSourceRows(emptyList(), listOf(ScanIssue("unsupported_text_encoding",
                "Strict text decoding failed: ${e.message?.take(160)}", locator)))
        }
        checkCancelled()
        val sample = decoded.text.take(65536)
        val sampleLimits = limits.copy(maxRowsPerFile = minOf(limits.maxRowsPerFile, 32),
            maxCellsPerFile = minOf(limits.maxCellsPerFile, 1024))
        var sampledRecords = 0
        var truncated = sample.length < decoded.text.length
        val candidates = if (tsv) listOf('\t') else listOf(',', ';', '\t', '|').filter { delimiter ->
            val parsed = SourceTextParser.decodedDelimited(sample, delimiter, locator, sampleLimits, checkCancelled)
            sampledRecords = maxOf(sampledRecords, parsed.rows.size)
            truncated = truncated || parsed.issues.any { it.code.endsWith("_limit") }
            // Incomplete final sampled records must not vote for a separator.
            val records = if (sample.length < decoded.text.length) parsed.rows.dropLast(1) else parsed.rows
            val widths = records.map { it.cells.size }
            widths.isNotEmpty() && widths.first() > 1 && widths.all { it == widths.first() } &&
                parsed.issues.none { it.code == "malformed_csv" || it.code == "cell_chars_limit" || it.code == "cell_limit" }
        }
        val ambiguous = !tsv && candidates.size != 1
        val delimiter = if (candidates.size == 1) candidates.single() else ','
        val format = SourceTextFormat(decoded.encoding, if (decoded.bomBytes > 0) "bom" else "strict_utf8_default",
            decoded.bomBytes, delimiter.toString(), if (tsv) "tsv_extension" else if (ambiguous) "provisional_comma_default" else "inferred_uniform_records",
            candidates.map { it.toString() }, ambiguous, sampledRecords, truncated)
        val parsed = SourceTextParser.decodedDelimited(decoded.text, delimiter, locator, limits, checkCancelled)
        val inferenceIssues = if (ambiguous) listOf(ScanIssue(
            if (candidates.isEmpty()) "delimiter_undetermined" else "delimiter_ambiguous",
            "Separator is not uniquely supported by sampled records; comma projection is provisional", locator)) else emptyList()
        return parsed.copy(issues = inferenceIssues + parsed.issues, textFormat = format)
    }
}

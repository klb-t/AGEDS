package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import org.junit.Assert.*
import org.junit.Test

/** Reproducible BIFF8 records inside the generated CFB; no binary/corpus fixtures. */
class SourceXlsContinuationTest {
    private fun string(value: String, wide: Boolean = false): ByteArray = XlsFixture.le16(value.length) +
        byteArrayOf(if (wide) 1 else 0) + value.toByteArray(if (wide) Charsets.UTF_16LE else Charsets.ISO_8859_1)
    private fun parse(parts: List<ByteArray>, count: Int = 1, limits: SourceScanLimits = SourceScanLimits()): dev.klbt.ageds.core.ParsedSourceRows {
        val sst = XlsFixture.record(0xfc, XlsFixture.le32(count) + XlsFixture.le32(count) + parts.first()) +
            parts.drop(1).fold(ByteArray(0)) { all, part -> all + XlsFixture.record(0x3c, part) }
        val cells = (0 until count).fold(ByteArray(0)) { all, index ->
            all + XlsFixture.record(0xfd, XlsFixture.cellHeader(index, 0) + XlsFixture.le32(index))
        }
        return SourceXlsParser.parse(XlsFixture.cfb(XlsFixture.workbook(cells, sst)), "generated.xls", limits)
    }
    private fun values(result: dev.klbt.ageds.core.ParsedSourceRows) = result.rows.flatMap { it.cells }.map { it.value }

    @Test fun compressedCharacterContinuationPreservesWhitespaceAndRawIndex() {
        val result = parse(listOf(XlsFixture.le16(6) + byteArrayOf(0) + "  ab".toByteArray(), byteArrayOf(0) + " c".toByteArray()))
        assertEquals(listOf("  ab c"), values(result))
        assertEquals("00000000000000000000", result.rows.single().cells.single().raw)
        assertEquals(listOf("xls_projection"), result.issues.map { it.code })
    }
    @Test fun compressionCanChangeAtEveryCharacterContinuation() {
        val result = parse(listOf(XlsFixture.le16(4) + byteArrayOf(0, 65),
            byteArrayOf(1) + "ć".toByteArray(Charsets.UTF_16LE), byteArrayOf(0, 66),
            byteArrayOf(1) + "界".toByteArray(Charsets.UTF_16LE)))
        assertEquals(listOf("AćB界"), values(result))
        assertEquals(listOf("xls_projection"), result.issues.map { it.code })
    }
    @Test fun fullStringHeaderStartsNextRecordWithoutCompressionPrefix() {
        val result = parse(listOf(string("first"), string(" ć ", true)), 2)
        assertEquals(listOf("first", " ć "), values(result))
    }
    @Test fun characterDataCanBeginInNextRecordAfterAtomicHeader() {
        assertEquals(listOf("abc"), values(parse(listOf(XlsFixture.le16(3) + byteArrayOf(0), byteArrayOf(0, 97, 98, 99)))))
    }
    @Test fun unicodeSurrogatePairAcrossRecordBoundaryDecodesOnce() {
        val pair = "😀".toByteArray(Charsets.UTF_16LE)
        assertEquals(listOf("😀"), values(parse(listOf(XlsFixture.le16(2) + byteArrayOf(1) + pair.copyOfRange(0, 2),
            byteArrayOf(1) + pair.copyOfRange(2, 4)))))
    }
    @Test fun richTextTailWithinRecordDoesNotAffectFollowingHeader() {
        val rich = XlsFixture.le16(1) + byteArrayOf(8) + XlsFixture.le16(1) + byteArrayOf(65) + ByteArray(4)
        assertEquals(listOf("A", "next"), values(parse(listOf(rich, string("next")), 2)))
    }
    @Test fun continuedRichTextTailExplicitlyOmittedWithoutPartialText() {
        val rich = XlsFixture.le16(1) + byteArrayOf(8) + XlsFixture.le16(1) + byteArrayOf(65)
        val result = parse(listOf(rich, ByteArray(4)))
        assertEquals(listOf("0"), values(result))
        assertTrue(result.issues.any { it.code == "unsupported_xls" })
        assertTrue(result.issues.any { it.code == "xls_unresolved_shared_string" })
    }
    @Test fun malformedCompressionAndSplitCodeUnitNeverPublishPrefix() {
        val cases = listOf(
            listOf(XlsFixture.le16(2) + byteArrayOf(0, 65), byteArrayOf(2, 66)),
            listOf(XlsFixture.le16(2) + byteArrayOf(1, 65), byteArrayOf(1, 0, 66, 0)),
            listOf(XlsFixture.le16(2) + byteArrayOf(0, 65), byteArrayOf(0)),
            listOf(XlsFixture.le16(2) + byteArrayOf(0, 65), ByteArray(0)),
        )
        for (parts in cases) {
            val result = parse(parts)
            assertEquals(listOf("0"), values(result))
            assertTrue(result.issues.any { it.code == "invalid_xls" })
        }
    }
    @Test fun splitFixedStringHeaderRejected() {
        val result = parse(listOf(byteArrayOf(1), byteArrayOf(0, 0, 65)))
        assertEquals(listOf("0"), values(result))
        assertTrue(result.issues.any { it.code == "invalid_xls" })
    }
    @Test fun lateMalformedStringDiscardsEntireStagedTable() {
        val result = parse(listOf(string("first"), byteArrayOf(1)), 2)
        assertEquals(listOf("0", "1"), values(result))
        assertTrue(result.issues.any { it.code == "invalid_xls" })
    }
    @Test fun continuedStringHonorsCellAndSharedStringBudgets() {
        val parts = listOf(XlsFixture.le16(21) + byteArrayOf(0) + ByteArray(10) { 65 }, byteArrayOf(0) + ByteArray(11) { 66 })
        assertTrue(parse(parts, limits = SourceScanLimits(maxCellChars = 20)).issues.any { it.code == "cell_chars_limit" })
        assertTrue(parse(listOf(string("a"), string("b")), 2, SourceScanLimits(maxCellsPerFile = 1)).issues.any { it.code == "shared_strings_limit" })
    }
    @Test fun repeatedEmptyStringsBoundCountsWithoutContinuationGuessing() {
        val result = parse(listOf(string(""), string("")), 2)
        assertEquals(listOf("", ""), values(result))
        assertEquals(listOf("xls_projection"), result.issues.map { it.code })
    }
}

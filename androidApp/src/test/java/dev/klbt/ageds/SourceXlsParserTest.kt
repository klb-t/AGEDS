package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import org.junit.Assert.*
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.CancellationException

/** All bytes generated in memory; no private or binary fixtures. */
class SourceXlsParserTest {
    private fun parse(bytes: ByteArray, limits: SourceScanLimits = SourceScanLimits()) =
        SourceXlsParser.parse(bytes, "synthetic.xls", limits)

    @Test fun regularFatPreservesNumbersRawPayloadAndOffsets() {
        val bytes = XlsFixture.cfb(XlsFixture.workbook(XlsFixture.number(2, 1, 42.125)))
        val original = bytes.copyOf()
        val result = parse(bytes)
        assertEquals(listOf("42.125"), result.rows.flatMap { it.cells }.map { it.value })
        val cell = result.rows.single().cells.single()
        assertEquals(2, cell.column)
        assertTrue(cell.sourceReference!!.startsWith("B3;stream=Workbook;offset="))
        assertEquals("0200010000000000000000104540", cell.raw)
        assertTrue(result.rows.single().locator.contains("#row=3;offset="))
        assertArrayEquals(original, bytes)
        assertEquals(listOf("xls_projection"), result.issues.map { it.code })
    }
    @Test fun actualBookStreamNameIsPreservedInReference() {
        val bytes = XlsFixture.cfb(XlsFixture.workbook(XlsFixture.number(0, 0, 1.0)))
        val p = 1024 + 128
        "Book\u0000".toByteArray(Charsets.UTF_16LE).copyInto(bytes, p)
        XlsFixture.le16(10).copyInto(bytes, p + 64)
        assertTrue(parse(bytes).rows.single().cells.single().sourceReference!!.contains("stream=Book;"))
    }
    @Test fun miniFatReadsSmallWorkbook() {
        val result = parse(XlsFixture.cfb(XlsFixture.workbook(XlsFixture.number(0, 0, -1.5)), mini = true))
        assertEquals("-1.5", result.rows.single().cells.single().value)
        assertFalse(result.issues.any { it.code == "invalid_xls" })
    }
    @Test fun sharedStringsPreserveUnicodeWhitespaceAndIndexBytes() {
        val sst = XlsFixture.record(0xfc, XlsFixture.le32(1) + XlsFixture.le32(1) + XlsFixture.le16(5) + byteArrayOf(1) + " ąć! ".toByteArray(Charsets.UTF_16LE))
        val cell = XlsFixture.record(0xfd, XlsFixture.cellHeader(0, 0) + XlsFixture.le32(0))
        val result = parse(XlsFixture.cfb(XlsFixture.workbook(cell, sst)))
        assertEquals(" ąć! ", result.rows.single().cells.single().value)
        assertEquals("00000000000000000000", result.rows.single().cells.single().raw)
    }
    @Test fun malformedContinuationNeverPretendsToDecodeSst() {
        val sst = XlsFixture.record(0xfc, XlsFixture.le32(1) + XlsFixture.le32(1)) + XlsFixture.record(0x3c, byteArrayOf(0))
        val cell = XlsFixture.record(0xfd, XlsFixture.cellHeader(0, 0) + XlsFixture.le32(0))
        val result = parse(XlsFixture.cfb(XlsFixture.workbook(cell, sst)))
        assertEquals("0", result.rows.single().cells.single().value)
        // Previously any CONTINUE was unsupported; this unchanged fixture has a truncated string header.
        assertTrue(result.issues.any { it.code == "invalid_xls" })
        assertTrue(result.issues.any { it.code == "xls_unresolved_shared_string" })
    }
    @Test fun formulaReturnsCachedNumberAndOpaqueTokens() {
        val payload = XlsFixture.cellHeader(0, 0) + XlsFixture.double(7.0) + ByteArray(6) + XlsFixture.le16(3) + byteArrayOf(0x1e, 7, 0)
        val result = parse(XlsFixture.cfb(XlsFixture.workbook(XlsFixture.record(6, payload))))
        assertEquals("7.0", result.rows.single().cells.single().value)
        assertEquals("1e0700", result.rows.single().cells.single().formula)
    }
    @Test fun rkMulrkBooleanErrorAndBlank() {
        val cells = XlsFixture.record(0x27e, XlsFixture.cellHeader(0, 0) + XlsFixture.le32((-123 shl 2) or 3)) +
            XlsFixture.record(0xbd, XlsFixture.le16(0) + XlsFixture.le16(1) + XlsFixture.le16(0) + XlsFixture.le32(40 shl 2 or 2) + XlsFixture.le16(0) + XlsFixture.le32(50 shl 2 or 2) + XlsFixture.le16(2)) +
            XlsFixture.record(0x205, XlsFixture.cellHeader(0, 3) + byteArrayOf(1, 0)) +
            XlsFixture.record(0x205, XlsFixture.cellHeader(0, 4) + byteArrayOf(7, 1)) +
            XlsFixture.record(0x201, XlsFixture.cellHeader(0, 5))
        assertEquals(listOf("-1.23", "40.0", "50.0", "true", "error:7", ""), parse(XlsFixture.cfb(XlsFixture.workbook(cells))).rows.single().cells.map { it.value })
    }
    @Test fun cellAndRowBudgetsRetainOnlyAdmittedCells() {
        val bytes = XlsFixture.cfb(XlsFixture.workbook(XlsFixture.number(0, 0, 1.0) + XlsFixture.number(1, 0, 2.0)))
        for ((limits, code) in listOf(SourceScanLimits(maxCellsPerFile = 1) to "cell_limit", SourceScanLimits(maxRowsPerFile = 1) to "row_limit")) {
            val result = parse(bytes, limits)
            assertEquals(1, result.rows.size)
            assertEquals("1.0", result.rows.single().cells.single().value)
            assertTrue(result.issues.any { it.code == code })
        }
        assertTrue(parse(bytes, SourceScanLimits(maxFileBytes = 512)).issues.any { it.code == "file_bytes_limit" })
        assertTrue(parse(bytes, SourceScanLimits(maxExpandedBytes = 100)).issues.any { it.code == "expanded_bytes_limit" })
        assertTrue(parse(bytes, SourceScanLimits(maxCellChars = 4)).issues.any { it.code == "cell_chars_limit" })
    }
    @Test fun rejectsCyclicAndOutOfBoundsFatAndDirectoryChains() {
        val base = XlsFixture.cfb(XlsFixture.workbook(XlsFixture.number(0, 0, 1.0)))
        for ((offset, value) in listOf(512 + 2 * 4 to 2, 512 + 2 * 4 to 10000, 512 + 1 * 4 to 1, 1024 + 128 + 68 to 1)) {
            val bytes = base.copyOf(); XlsFixture.put32(bytes, offset, value)
            assertTrue("$offset", parse(bytes).issues.any { it.code == "invalid_xls" })
        }
    }
    @Test fun rejectsMiniFatCycle() {
        val bytes = XlsFixture.cfb(XlsFixture.workbook(XlsFixture.number(0, 0, 1.0)), mini = true)
        XlsFixture.put32(bytes, 2048, 0)
        assertTrue(parse(bytes).issues.any { it.code == "invalid_xls" })
    }
    @Test fun encryptionAndUnsupportedCfbAreExplicit() {
        val encrypted = XlsFixture.cfb(XlsFixture.workbook(ByteArray(0), XlsFixture.record(0x2f, byteArrayOf(0, 0))))
        assertTrue(parse(encrypted).issues.any { it.code == "unsupported_xls" })
        val v4 = encrypted.copyOf(); v4[26] = 4
        assertTrue(parse(v4).issues.any { it.code == "unsupported_xls" })
        val difat = encrypted.copyOf(); XlsFixture.put32(difat, 72, 1)
        assertTrue(parse(difat).issues.any { it.code == "unsupported_xls" })
    }
    @Test fun badRecordAndSheetOffsetsAreRejected() {
        val wb = XlsFixture.workbook(XlsFixture.number(0, 0, 1.0))
        val truncated = wb.copyOf(); truncated[truncated.size - 2] = 100
        assertTrue(parse(XlsFixture.cfb(truncated)).issues.any { it.code == "invalid_xls" })
        val wrongSheet = wb.copyOf(); XlsFixture.put32(wrongSheet, 24, 1)
        assertTrue(parse(XlsFixture.cfb(wrongSheet)).issues.any { it.code == "invalid_xls" })
    }
    @Test(expected = CancellationException::class) fun cancellationPropagates() {
        SourceXlsParser.parse(ByteArray(0), "cancelled", SourceScanLimits()) { throw CancellationException() }
    }
    @Test fun malformedPrefixesDoNotThrowOrMutate() {
        val bytes = XlsFixture.cfb(XlsFixture.workbook(XlsFixture.number(0, 0, 1.0)))
        for (length in listOf(0, 1, 8, 511, 512, 1024, bytes.size - 1)) {
            val prefix = bytes.copyOf(length); val before = prefix.copyOf()
            assertTrue(parse(prefix).issues.any { it.code == "invalid_xls" })
            assertArrayEquals(before, prefix)
        }
    }
}

/** Small independent byte writer shared with adversarial tests. */
internal object XlsFixture {
    fun le16(n: Int) = byteArrayOf(n.toByte(), (n ushr 8).toByte())
    fun le32(n: Int) = ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(n).array()
    fun double(n: Double) = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putDouble(n).array()
    fun put32(bytes: ByteArray, p: Int, n: Int) { le32(n).copyInto(bytes, p) }
    fun record(id: Int, data: ByteArray) = le16(id) + le16(data.size) + data
    fun cellHeader(row: Int, col: Int) = le16(row) + le16(col) + le16(0)
    fun number(row: Int, col: Int, n: Double) = record(0x203, cellHeader(row, col) + double(n))
    private fun bof(type: Int) = record(0x809, le16(0x600) + le16(type) + ByteArray(12))
    fun workbook(cells: ByteArray, globals: ByteArray = ByteArray(0)): ByteArray {
        val boundSize = 4 + 8 + 5
        val sheetOffset = 20 + boundSize + globals.size + 4
        val bound = record(0x85, le32(sheetOffset) + byteArrayOf(0, 0, 5, 0) + "Sheet".toByteArray())
        return bof(5) + bound + globals + record(0xa, ByteArray(0)) + bof(0x10) + cells + record(0xa, ByteArray(0))
    }
    fun cfb(workbook: ByteArray, mini: Boolean = false): ByteArray {
        require(!mini || workbook.size <= 512)
        val book = if (mini) workbook else workbook.copyOf(maxOf(4096, (workbook.size + 511) / 512 * 512))
        val bookSectors = (book.size + 511) / 512
        val sectorCount = if (mini) 4 else 2 + bookSectors
        require(sectorCount <= 128)
        val bytes = ByteArray((sectorCount + 1) * 512)
        byteArrayOf(0xd0.toByte(),0xcf.toByte(),0x11,0xe0.toByte(),0xa1.toByte(),0xb1.toByte(),0x1a,0xe1.toByte()).copyInto(bytes)
        le16(0x3e).copyInto(bytes, 24); le16(3).copyInto(bytes, 26); le16(0xfffe).copyInto(bytes, 28)
        le16(9).copyInto(bytes, 30); le16(6).copyInto(bytes, 32)
        put32(bytes, 44, 1); put32(bytes, 48, 1); put32(bytes, 56, 4096)
        put32(bytes, 60, if (mini) 3 else -2); put32(bytes, 64, if (mini) 1 else 0); put32(bytes, 68, -2)
        repeat(109) { put32(bytes, 76 + it * 4, if (it == 0) 0 else -1) }
        repeat(128) { put32(bytes, 512 + it * 4, -1) }
        put32(bytes, 512, -3); put32(bytes, 516, -2)
        fun entry(index: Int, name: String, type: Int, start: Int, size: Int, child: Int = -1) {
            val p = 1024 + index * 128
            (name + '\u0000').toByteArray(Charsets.UTF_16LE).copyInto(bytes, p)
            le16((name.length + 1) * 2).copyInto(bytes, p + 64); bytes[p + 66] = type.toByte(); bytes[p + 67] = 1
            put32(bytes, p + 68, -1); put32(bytes, p + 72, -1); put32(bytes, p + 76, child)
            put32(bytes, p + 116, start); put32(bytes, p + 120, size)
        }
        entry(0, "Root Entry", 5, if (mini) 2 else -2, if (mini) (book.size + 63) / 64 * 64 else 0, 1)
        entry(1, "Workbook", 2, if (mini) 0 else 2, book.size)
        book.copyInto(bytes, 1536)
        if (mini) {
            put32(bytes, 520, -2); put32(bytes, 524, -2)
            repeat(128) { put32(bytes, 2048 + it * 4, -1) }
            val miniSectors = (book.size + 63) / 64
            repeat(miniSectors) { put32(bytes, 2048 + it * 4, if (it + 1 == miniSectors) -2 else it + 1) }
        } else repeat(bookSectors) { put32(bytes, 520 + it * 4, if (it + 1 == bookSectors) -2 else it + 3) }
        return bytes
    }
}

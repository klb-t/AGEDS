package dev.klbt.ageds.core

import kotlin.test.*

class SourceTextParserTest {
    private val limits = SourceScanLimits()
    @Test fun quotedMultilineAndDuplicateRecordsRetainRaw() {
        val result = SourceTextParser.delimited("name,value\r\n\"a\n\"\"b\",007\r\n\"a\n\"\"b\",007", ',', "source", limits)
        assertEquals(3, result.rows.size)
        assertEquals("\"a\n\"\"b\"", result.rows[1].cells[0].raw)
        assertEquals("a\n\"b", result.rows[1].cells[0].value)
        assertEquals("007", result.rows[1].cells[1].value)
        assertEquals("source#line=4", result.rows[2].locator)
        assertTrue(result.issues.isEmpty())
    }
    @Test fun trailingEmptyCellAndBomAreHandled() {
        val r = SourceTextParser.delimited("\uFEFFx,y,", ',', "u", limits)
        assertEquals(listOf("x", "y", ""), r.rows.single().cells.map { it.value })
        assertEquals(emptyList(), SourceTextParser.delimited("", ',', "u", limits).rows)
    }
    @Test fun tabsDoNotAlterCommasOrFormulaLikeStrings() {
        val r = SourceTextParser.delimited("+001,2\t=SUM(A1:A2)\t2026-01-02", '\t', "u", limits)
        assertEquals(listOf("+001,2", "=SUM(A1:A2)", "2026-01-02"), r.rows.single().cells.map { it.value })
        assertTrue(r.rows.single().cells.all { it.formula == null })
    }
    @Test fun malformedQuotedInputIsFlaggedAndPreserved() {
        val r = SourceTextParser.delimited("\"not closed", ',', "u", limits)
        assertEquals("\"not closed", r.rows.single().cells.single().raw)
        assertEquals("malformed_csv", r.issues.single().code)
    }
    @Test fun boundsReportPartialRecords() {
        val rows = SourceTextParser.delimited("a\nb\nc", ',', "u", limits.copy(maxRowsPerFile = 2))
        assertEquals(2, rows.rows.size); assertEquals("row_limit", rows.issues.single().code)
        val cells = SourceTextParser.delimited("a,b,c", ',', "u", limits.copy(maxCellsPerFile = 2))
        assertEquals(2, cells.rows.single().cells.size); assertEquals("cell_limit", cells.issues.single().code)
        val chars = SourceTextParser.delimited("abcdef", ',', "u", limits.copy(maxCellChars = 3))
        assertEquals("cell_chars_limit", chars.issues.single().code)
        assertTrue(chars.rows.single().cells.isEmpty())
    }
    @Test fun cancellationPropagates() {
        assertFailsWith<IllegalStateException> {
            SourceTextParser.delimited("a\nb", ',', "u", limits) { error("cancel") }
        }
    }
    @Test fun invalidLimitsFailBeforeProcessing() {
        assertFailsWith<IllegalArgumentException> { limits.copy(maxFiles = 0) }
        assertFailsWith<IllegalArgumentException> { limits.copy(maxFileBytes = Long.MAX_VALUE) }
    }
    private fun wav(): ByteArray {
        val bytes = ByteArray(48)
        fun text(p: Int, s: String) { s.forEachIndexed { i, c -> bytes[p+i] = c.code.toByte() } }
        fun num(p: Int, n: Int, count: Int) { repeat(count) { bytes[p+it] = (n ushr (8*it)).toByte() } }
        text(0,"RIFF"); num(4,40,4); text(8,"WAVE"); text(12,"fmt "); num(16,16,4)
        num(20,1,2); num(22,1,2); num(24,2,4); num(28,4,4); num(32,2,2); num(34,16,2)
        text(36,"data"); num(40,4,4)
        return bytes
    }
    @Test fun wavDurationComesFromValidatedHeader() { assertEquals(1.0, SourceTextParser.wavDuration(wav())) }
    @Test fun inconsistentWavIsUnknown() {
        val b = wav(); b[28] = 9
        assertNull(SourceTextParser.wavDuration(b))
        assertNull(SourceTextParser.wavDuration(wav().copyOf(47)))
        assertNull(SourceTextParser.wavDuration(byteArrayOf(1, 2, 3)))
    }
}

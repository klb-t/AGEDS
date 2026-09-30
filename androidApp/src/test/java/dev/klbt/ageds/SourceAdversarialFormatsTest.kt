package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/** N15 independent synthetic acceptance: source bytes are deliberately hostile or ambiguous. */
class SourceAdversarialFormatsTest {
    private val limits = SourceScanLimits()
    private fun fingerprint(bytes: ByteArray) {
        println("N15_INPUT formats ${MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }} ${bytes.size}")
    }
    private fun delimited(bytes: ByteArray): dev.klbt.ageds.core.ParsedSourceRows {
        fingerprint(bytes)
        return SourceDelimitedParser.parse(bytes, "synthetic", limits)
    }
    private fun workbook(vararg parts: Pair<String, ByteArray>): ByteArray {
        val output = ByteArrayOutputStream()
        ZipOutputStream(output).use { zip ->
            (listOf("[Content_Types].xml" to "<Types/>".toByteArray(),
                "xl/workbook.xml" to "<workbook/>".toByteArray()) + parts).forEach { (name, bytes) ->
                zip.putNextEntry(ZipEntry(name).apply { time = 0L })
                zip.write(bytes); zip.closeEntry()
            }
        }
        return output.toByteArray().also { fingerprint(it) }
    }
    @Test fun onlyOneByteOrderMarkIsConsumed() {
        for (charset in listOf(Charsets.UTF_8, Charsets.UTF_16LE, Charsets.UTF_16BE)) {
            val bom = when (charset) {
                Charsets.UTF_8 -> byteArrayOf(0xef.toByte(), 0xbb.toByte(), 0xbf.toByte())
                Charsets.UTF_16LE -> byteArrayOf(0xff.toByte(), 0xfe.toByte())
                else -> byteArrayOf(0xfe.toByte(), 0xff.toByte())
            }
            val bytes = bom + "\uFEFFa,b\n".toByteArray(charset)
            val before = bytes.copyOf()
            val parsed = delimited(bytes)
            assertEquals("\uFEFFa", parsed.rows.single().cells.first().raw)
            assertEquals("\uFEFFa", parsed.rows.single().cells.first().value)
            assertArrayEquals(before, bytes)
        }
    }
    @Test fun ambiguousUniformSeparatorsStayExplicit() {
        val bytes = "a,b;c|d\n1,2;3|4\n".toByteArray()
        val parsed = delimited(bytes)
        assertTrue(parsed.textFormat!!.delimiterAmbiguous)
        assertEquals(setOf(",", ";", "|"), parsed.textFormat!!.delimiterCandidates.toSet())
        assertTrue(parsed.issues.any { it.code == "delimiter_ambiguous" })
        assertEquals("b;c|d", parsed.rows.first().cells.last().raw)
    }
    @Test fun malformedBomPayloadsNeverBecomeReplacementCharacters() {
        val invalid = listOf(byteArrayOf(0xff.toByte(), 0xfe.toByte(), 0x61),
            byteArrayOf(0xef.toByte(), 0xbb.toByte(), 0xbf.toByte(), 0xc0.toByte(), 0xaf.toByte()),
            byteArrayOf(0, 0, 0xfe.toByte(), 0xff.toByte(), 0, 0, 0, 0x61))
        for (bytes in invalid) {
            val parsed = delimited(bytes)
            assertTrue(parsed.rows.isEmpty())
            assertEquals("unsupported_text_encoding", parsed.issues.single().code)
        }
    }
    @Test fun quotedSeparatorsCannotVoteForDifferentDialect() {
        val parsed = delimited("a;b\n\"1,2|3\";4\n".toByteArray())
        assertEquals(";", parsed.textFormat!!.delimiter)
        assertFalse(parsed.textFormat!!.delimiterAmbiguous)
        assertEquals("1,2|3", parsed.rows[1].cells[0].value)
        assertEquals("\"1,2|3\"", parsed.rows[1].cells[0].raw)
    }
    @Test fun utf16DtdIsRejectedBeforeAnyRowsEscape() {
        val xml = "<?xml version=\"1.0\" encoding=\"UTF-16\"?><!DOCTYPE worksheet [<!ENTITY x SYSTEM \"file:///unreadable-N15-synthetic\">]><worksheet><row><c><v>&x;</v></c></row></worksheet>"
        for (charset in listOf(Charsets.UTF_16LE, Charsets.UTF_16BE)) {
            val bom = if (charset == Charsets.UTF_16LE) byteArrayOf(0xff.toByte(),0xfe.toByte()) else byteArrayOf(0xfe.toByte(),0xff.toByte())
            val bytes = workbook("xl/worksheets/sheet1.xml" to (bom + xml.toByteArray(charset)))
            val before = bytes.copyOf()
            val result = SourceWorkbookParser.parse(bytes, "synthetic", limits)
            assertTrue(result.rows.isEmpty())
            assertEquals("invalid_workbook", result.issues.single().code)
            assertArrayEquals(before, bytes)
        }
    }
    @Test fun zipDirectoryEntriesCountTowardBudget() {
        val bytes = workbook("directory/" to byteArrayOf(), "xl/worksheets/sheet1.xml" to "<worksheet/>".toByteArray())
        val result = SourceWorkbookParser.parse(bytes, "synthetic", limits.copy(maxArchiveEntries = 2))
        assertTrue(result.rows.isEmpty())
        assertEquals("archive_entry_limit", result.issues.single().code)
    }
    @Test fun nestedRowsCannotBypassRetentionBudget() {
        val bytes = workbook("xl/worksheets/sheet1.xml" to "<worksheet><row><row/></row></worksheet>".toByteArray())
        val result = SourceWorkbookParser.parse(bytes, "synthetic", limits.copy(maxRowsPerFile=1))
        assertTrue(result.rows.size <= 1)
        assertTrue(result.issues.any { it.code == "invalid_workbook" || it.code == "row_limit" })
    }
    @Test fun ignoredHighlyCompressedPayloadStillConsumesExpandedBudget() {
        val bytes = workbook("ignored.bin" to ByteArray(20000), "xl/worksheets/sheet1.xml" to "<worksheet/>".toByteArray())
        val result = SourceWorkbookParser.parse(bytes, "synthetic", limits.copy(maxExpandedBytes = 512))
        assertTrue(result.rows.isEmpty())
        assertEquals("expanded_bytes_limit", result.issues.single().code)
    }
}

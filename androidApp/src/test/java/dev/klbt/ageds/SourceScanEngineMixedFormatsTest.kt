package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.security.MessageDigest
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/** Actual production engine/parsers; only the read-only document provider is synthetic. */
class SourceScanEngineMixedFormatsTest {
    private data class Entry(val id: String, val name: String, val bytes: ByteArray,
        val size: Long? = bytes.size.toLong(), val mime: String = "application/octet-stream") {
        fun document() = SourceScanDocument(id, name, mime, size)
    }
    private class Tree(val entries: List<Entry>, val directories: Map<String, List<SourceScanDocument>> =
        mapOf("root" to entries.map { it.document() })) : SourceScanProvider {
        override val rootId = "root"
        override val rootUri = "content://synthetic/tree/root"
        val opened = mutableListOf<String>()
        var bytesReturned = 0L
        var streamsClosed = 0
        var cursorsOpened = 0
        var cursorsClosed = 0
        override fun uri(id: String) = "content://synthetic/tree/root/document/$id"
        override fun children(id: String): SourceScanCursor {
            cursorsOpened++
            val values = directories.getValue(id).iterator()
            return object : SourceScanCursor {
                override fun next() = if (values.hasNext()) values.next() else null
                override fun close() { cursorsClosed++ }
            }
        }
        override fun openRead(id: String): InputStream {
            opened += id
            return object : ByteArrayInputStream(entries.single { it.id == id }.bytes) {
                override fun read(): Int = super.read().also { if (it >= 0) bytesReturned++ }
                override fun read(b: ByteArray, off: Int, len: Int): Int = super.read(b, off, len).also { if (it > 0) bytesReturned += it }
                override fun close() { streamsClosed++; super.close() }
            }
        }
        fun scan(limits: SourceScanLimits = SourceScanLimits()) =
            SourceScanEngine(this).scan(limits, scannedAt = "2026-10-01T00:00:00Z")
    }
    private fun hash(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
    private fun utf16(text: String, little: Boolean) =
        (if (little) byteArrayOf(-1, -2) else byteArrayOf(-2, -1)) + text.toByteArray(if (little) Charsets.UTF_16LE else Charsets.UTF_16BE)

    // Small ZIP fixture pattern adapted from SourceWorkbookParserTest; no parser replacement.
    private fun workbook(twoRows: Boolean = false): ByteArray {
        val rows = "<row r=\"1\"><c r=\"A1\" t=\"inlineStr\"><is><t>+001</t></is></c><c r=\"D1\"><f>1+1</f><v>999</v></c></row>" +
            if (twoRows) "<row r=\"2\"><c r=\"A2\"><v>2</v></c></row>" else ""
        val output = ByteArrayOutputStream()
        ZipOutputStream(output).use { zip ->
            linkedMapOf("[Content_Types].xml" to "<Types/>", "xl/workbook.xml" to "<workbook/>",
                "xl/worksheets/sheet1.xml" to "<worksheet><sheetData>$rows</sheetData></worksheet>").forEach { (path, xml) ->
                zip.putNextEntry(ZipEntry(path)); zip.write(xml.toByteArray()); zip.closeEntry()
            }
        }
        return output.toByteArray()
    }
    // Reuse the existing independent BIFF/CFB byte writer without changing its API.
    private fun xlsFormula(): ByteArray {
        val payload = XlsFixture.cellHeader(0, 0) + XlsFixture.double(999.0) + ByteArray(6) +
            XlsFixture.le16(3) + byteArrayOf(0x1e, 7, 0)
        return XlsFixture.cfb(XlsFixture.workbook(XlsFixture.record(6, payload)))
    }
    private fun wav(dataBytes: Int = 32, format: Int = 1): ByteArray =
        "RIFF".toByteArray() + XlsFixture.le32(36 + dataBytes) + "WAVEfmt ".toByteArray() + XlsFixture.le32(16) +
            XlsFixture.le16(format) + XlsFixture.le16(1) + XlsFixture.le32(16000) + XlsFixture.le32(32000) +
            XlsFixture.le16(2) + XlsFixture.le16(16) + "data".toByteArray() + XlsFixture.le32(dataBytes) + ByteArray(dataBytes)

    @Test fun oneEngineInvokesAllRealParsersAndPreservesRawValuesAndUnevaluatedFormulas() {
        val entries = listOf(
            Entry("csv", "unicode.csv", utf16("name;value\r\n\"Zażółć\";007", true)),
            Entry("tsv", "unicode.tsv", utf16("name\tvalue\r\n+001\t\" a,b \"", false)),
            Entry("xlsx", "book.xlsx", workbook()), Entry("xls", "book.xls", xlsFormula()),
            Entry("wav", "voice.wav", wav(), mime = "audio/wav"),
        )
        val originals = entries.associate { it.id to it.bytes.copyOf() }
        val tree = Tree(entries)
        val result = tree.scan()
        val files = result.files.associateBy { it.name }
        assertEquals(5, files.size)
        assertEquals("2026-10-01T00:00:00Z", result.scannedAt)
        val csv = files.getValue("unicode.csv")
        assertEquals("UTF-16LE", csv.textFormat!!.encoding)
        assertEquals("bom", csv.textFormat!!.encodingBasis)
        assertEquals("\"Zażółć\"", csv.rows[1].cells[0].raw)
        assertEquals("Zażółć", csv.rows[1].cells[0].value)
        assertEquals("007", csv.rows[1].cells[1].value)
        val tsv = files.getValue("unicode.tsv")
        assertEquals("UTF-16BE", tsv.textFormat!!.encoding)
        assertEquals("tsv_extension", tsv.textFormat!!.delimiterBasis)
        assertEquals("+001", tsv.rows[1].cells[0].raw)
        assertEquals(" a,b ", tsv.rows[1].cells[1].value)
        val xlsx = files.getValue("book.xlsx")
        val formula = xlsx.rows.single().cells.last()
        assertEquals(4, formula.column)
        assertEquals("1+1", formula.formula)
        assertEquals("999", formula.raw)
        assertEquals("999", formula.value) // Cached 999 is preserved; 1+1 is never evaluated.
        assertEquals("+001", xlsx.rows.single().cells.first().value)
        val xls = files.getValue("book.xls")
        assertEquals("999.0", xls.rows.single().cells.single().value)
        assertEquals("1e0700", xls.rows.single().cells.single().formula)
        assertEquals("0000000000000000000000388f4000000000000003001e0700", xls.rows.single().cells.single().raw)
        assertTrue(xls.issues.any { it.code == "xls_projection" })
        assertEquals("partial", xls.coverage)
        val audio = files.getValue("voice.wav")
        assertEquals("observed", audio.wavHeader!!.status)
        assertEquals(0.001, audio.wavHeader!!.declaredDurationSec!!, 0.0)
        assertFalse(audio.wavHeader!!.bodyValidated)
        assertEquals("partial", audio.coverage)
        assertEquals("partial", result.coverage)
        entries.forEach { entry ->
            assertArrayEquals(originals.getValue(entry.id), entry.bytes)
            assertEquals(hash(entry.bytes), files.getValue(entry.name).sha256)
            files.getValue(entry.name).rows.forEach { assertTrue(it.locator.startsWith(tree.uri(entry.id))) }
        }
        assertEquals(entries.sumOf { it.bytes.size }.toLong(), result.bytesRead)
        assertEquals(tree.bytesReturned, result.bytesRead)
        assertEquals(5, tree.streamsClosed)
        assertEquals(tree.cursorsOpened, tree.cursorsClosed)
    }

    @Test fun sameNamesInDifferentDirectoriesKeepExactUrisRowsAndDistinctHashes() {
        val left = Entry("left-document", "calls.csv", "id,value\n1,left".toByteArray())
        val right = Entry("right-document", "calls.csv", "id,value\n2,right".toByteArray())
        val tree = Tree(listOf(left, right), mapOf(
            "root" to listOf(SourceScanDocument("left", "left", SOURCE_DIRECTORY_MIME, null), SourceScanDocument("right", "right", SOURCE_DIRECTORY_MIME, null)),
            "left" to listOf(left.document()), "right" to listOf(right.document())))
        val result = tree.scan()
        assertEquals(setOf("left/calls.csv", "right/calls.csv"), result.files.map { it.relativePath }.toSet())
        assertEquals(setOf(tree.uri(left.id), tree.uri(right.id)), result.files.map { it.uri }.toSet())
        assertEquals(setOf("left", "right"), result.files.map { it.rows[1].cells[1].value }.toSet())
        assertEquals(2, result.files.map { it.sha256 }.distinct().size)
        val collision = result.issues.single { it.code == "name_collision" }
        assertTrue(collision.locator!!.contains(tree.uri(left.id)))
        assertTrue(collision.locator!!.contains(tree.uri(right.id)))
        assertEquals(3, result.scannedDirectories)
        assertEquals(3, tree.cursorsClosed)
    }

    @Test fun unsupportedEncodingsContainersAndAudioStayExplicitWithoutInventedRows() {
        val unsupportedXls = XlsFixture.cfb(XlsFixture.workbook(XlsFixture.number(0, 0, 1.0))).also { it[26] = 4 }
        val entries = listOf(Entry("csv", "unknown.csv", "a,b".toByteArray(Charsets.UTF_16LE)),
            Entry("xls", "unsupported.xls", unsupportedXls), Entry("wav", "unsupported.wav", wav(format = 6)))
        val tree = Tree(entries)
        val result = tree.scan()
        val files = result.files.associateBy { it.name }
        assertTrue(files.getValue("unknown.csv").issues.any { it.code == "unsupported_text_encoding" })
        assertTrue(files.getValue("unknown.csv").rows.isEmpty())
        assertTrue(files.getValue("unsupported.xls").issues.any { it.code == "unsupported_xls" })
        assertTrue(files.getValue("unsupported.xls").rows.isEmpty())
        assertEquals("unsupported", files.getValue("unsupported.wav").wavHeader!!.status)
        assertNull(files.getValue("unsupported.wav").audioDurationSec)
        assertTrue(result.files.all { it.coverage == "partial" })
        assertEquals("partial", result.coverage)
        entries.forEach { assertEquals(hash(it.bytes), files.getValue(it.name).sha256) }
    }

    @Test fun rowCapsRemainPerFileAcrossDifferentRealTableParsers() {
        val entries = listOf(Entry("csv", "table.csv", "a,b\nx,y".toByteArray()), Entry("xlsx", "table.xlsx", workbook(twoRows = true)),
            Entry("xls", "table.xls", XlsFixture.cfb(XlsFixture.workbook(XlsFixture.number(0, 0, 1.0) + XlsFixture.number(1, 0, 2.0)))))
        val result = Tree(entries).scan(SourceScanLimits(maxRowsPerFile = 1))
        result.files.forEach { file ->
            assertEquals(1, file.rows.size)
            assertTrue(file.issues.any { it.code == "row_limit" })
            assertEquals("partial", file.coverage)
            assertEquals(hash(entries.single { it.name == file.name }.bytes), file.sha256)
        }
    }

    @Test fun truncatedUnknownSizeTableAndHeaderOnlyWavNeverReceivePartialFileHashes() {
        val csv = Entry("unknown", "unknown.csv", ("a,b\n" + "x,y\n".repeat(30)).toByteArray(), size = null)
        val audio = Entry("audio", "voice.wav", wav(dataBytes = 320), mime = "audio/wav")
        val skipped = Entry("large", "known.csv", ByteArray(100), size = 100)
        val tree = Tree(listOf(csv, audio, skipped))
        val result = tree.scan(SourceScanLimits(maxFileBytes = 64, maxTotalBytes = 256))
        assertEquals(tree.bytesReturned, result.bytesRead)
        assertTrue(result.files.all { it.sha256 == null && it.coverage == "partial" })
        assertTrue(result.files.single { it.name == "unknown.csv" }.rows.isEmpty())
        assertTrue(result.files.single { it.name == "known.csv" }.issues.any { it.code == "bytes_limit" })
        assertFalse(tree.opened.contains("large"))
        val header = result.files.single { it.name == "voice.wav" }.wavHeader!!
        assertFalse(header.bodyValidated)
        assertFalse(header.endOfInput)
        assertEquals("declared_data_bytes_divided_by_header_byte_rate", header.durationBasis)
        assertEquals(2, tree.streamsClosed)
    }
}

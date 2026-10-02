package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import java.io.ByteArrayOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import org.junit.Assert.*
import org.junit.Test

class SourceWorkbookEncodingTest {
    private fun workbook(sheet: ByteArray): ByteArray {
        val out = ByteArrayOutputStream()
        ZipOutputStream(out).use { zip ->
            for ((name, bytes) in mapOf("[Content_Types].xml" to "<Types/>".toByteArray(),
                    "xl/workbook.xml" to "<workbook/>".toByteArray(), "xl/worksheets/sheet1.xml" to sheet)) {
                zip.putNextEntry(ZipEntry(name)); zip.write(bytes); zip.closeEntry()
            }
        }
        return out.toByteArray()
    }
    private fun parse(bytes: ByteArray) = SourceWorkbookParser.parse(workbook(bytes), "u", SourceScanLimits())
    @Test fun bomUtf16XmlAcceptsMatchingDeclarationsAndPreservesCell() {
        for ((charset, bom) in listOf(Charsets.UTF_16LE to byteArrayOf(-1, -2), Charsets.UTF_16BE to byteArrayOf(-2, -1))) {
            val xml = "<?xml version=\"1.0\" encoding=\"UTF-16\"?><worksheet><row r=\"1\"><c r=\"A1\" t=\"inlineStr\"><is><t>Żółć 007</t></is></c></row></worksheet>"
            val result = parse(bom + xml.toByteArray(charset))
            assertEquals("Żółć 007", result.rows.single().cells.single().raw)
            assertEquals(listOf("xlsx_projection"), result.issues.map { it.code })
        }
    }
    @Test fun utf16CannotHideDtdOrEntityDeclarations() {
        val xml = "<?xml version=\"1.0\" encoding=\"UTF-16\"?><!DOCTYPE worksheet [<!ENTITY x SYSTEM \"file:///never-read\">]><worksheet/>"
        for ((charset, bom) in listOf(Charsets.UTF_16LE to byteArrayOf(-1, -2), Charsets.UTF_16BE to byteArrayOf(-2, -1))) {
            val result = parse(bom + xml.toByteArray(charset))
            assertTrue(result.rows.isEmpty()); assertEquals("invalid_workbook", result.issues.single().code)
            assertTrue(result.issues.single().message.contains("DTD/entity"))
        }
    }
    @Test fun declarationMustMatchActualEncoding() {
        for (bytes in listOf("<?xml version=\"1.0\" encoding=\"UTF-16\"?><worksheet/>".toByteArray(),
                byteArrayOf(-1, -2) + "<?xml version=\"1.0\" encoding=\"UTF-8\"?><worksheet/>".toByteArray(Charsets.UTF_16LE),
                "<?xml version=\"1.0\" encoding=\"US-ASCII\"?><worksheet>ż</worksheet>".toByteArray())) {
            assertEquals("invalid_workbook", parse(bytes).issues.single().code)
        }
    }
    @Test fun malformedUtf8AndUtf16AreRejectedBeforeSax() {
        for (bytes in listOf(byteArrayOf(-61, 40), byteArrayOf(-1, -2, 60))) {
            assertEquals("invalid_workbook", parse(bytes).issues.single().code)
        }
    }
}

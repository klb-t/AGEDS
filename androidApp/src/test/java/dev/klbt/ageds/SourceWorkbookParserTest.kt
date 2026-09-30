package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import java.io.ByteArrayOutputStream
import java.util.concurrent.CancellationException
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import org.junit.Assert.*
import org.junit.Test

class SourceWorkbookParserTest {
    private fun workbook(sheet: String, strings: String? = null, extra: String? = null, sheetEncoding: java.nio.charset.Charset = Charsets.UTF_8): ByteArray {
        val out = ByteArrayOutputStream()
        ZipOutputStream(out).use { zip ->
            val parts = linkedMapOf("[Content_Types].xml" to "<Types/>", "xl/workbook.xml" to "<workbook/>", "xl/worksheets/sheet1.xml" to sheet)
            strings?.let { parts["xl/sharedStrings.xml"] = it }; extra?.let { parts["ignored.bin"] = it }
            parts.forEach { (name, text) -> zip.putNextEntry(ZipEntry(name)); zip.write(text.toByteArray(if (name == "xl/worksheets/sheet1.xml") sheetEncoding else Charsets.UTF_8)); zip.closeEntry() }
        }
        return out.toByteArray()
    }
    @Test fun sharedStringsFormulaRawAndSparseColumnArePreserved() {
        val bytes = workbook("<worksheet><sheetData><row r=\"7\"><c r=\"A7\" t=\"s\"><v>0</v></c><c r=\"D7\"><f>1+1</f><v>2</v></c><c r=\"E7\" t=\"inlineStr\"><is><t>+001</t></is></c></row></sheetData></worksheet>", "<sst><si><r><t>A</t></r><r><t> B</t></r></si></sst>")
        val result = SourceWorkbookParser.parse(bytes,"uri",SourceScanLimits())
        val cells = result.rows.single().cells
        assertEquals("0",cells[0].raw); assertEquals("A B",cells[0].value)
        assertEquals(4,cells[1].column); assertEquals("1+1",cells[1].formula)
        assertEquals("2",cells[1].raw); assertEquals("+001",cells[2].value)
        assertTrue(result.rows.single().locator.contains("row=7;ordinal=1"))
        assertEquals(listOf("xlsx_projection"), result.issues.map { it.code })
    }
    @Test fun declaredEntitiesAreRejectedBeforeResolution() {
        val bytes = workbook("<!DOCTYPE worksheet [<!ENTITY x SYSTEM \"file:///etc/passwd\">]><worksheet><row><c><v>&x;</v></c></row></worksheet>")
        val result = SourceWorkbookParser.parse(bytes,"u",SourceScanLimits())
        assertTrue(result.rows.isEmpty()); assertEquals("invalid_workbook",result.issues.single().code)
    }
    @Test fun expansionBudgetAppliesEvenToIgnoredMembers() {
        val bytes = workbook("<worksheet/>",extra = "a".repeat(10000))
        assertEquals("expanded_bytes_limit", SourceWorkbookParser.parse(bytes,"u",SourceScanLimits(maxExpandedBytes=1000)).issues.single().code)
    }
    @Test fun rowAndCellBudgetsAreExplicit() {
        val bytes = workbook("<worksheet><row><c r=\"A1\"><v>1</v></c></row><row><c r=\"A2\"><v>2</v></c></row></worksheet>")
        val result = SourceWorkbookParser.parse(bytes,"u",SourceScanLimits(maxRowsPerFile=1))
        assertEquals(1,result.rows.size); assertEquals("row_limit",result.issues.single().code)
    }
    @Test fun unresolvedSharedStringIsNotInvented() {
        val bytes = workbook("<worksheet><row><c t=\"s\"><v>99</v></c></row></worksheet>")
        val result = SourceWorkbookParser.parse(bytes,"u",SourceScanLimits())
        assertEquals("99", result.rows.single().cells.single().value)
        assertTrue(result.issues.any { it.code == "invalid_shared_string" })
    }
    @Test fun malformedCellReferenceAndTypeRemainAvailable() {
        val bytes = workbook("<worksheet><row><c r=\"0\" t=\"weird\"><v>raw</v></c></row></worksheet>")
        val result = SourceWorkbookParser.parse(bytes,"u",SourceScanLimits())
        assertEquals("0",result.rows.single().cells.single().sourceReference)
        assertEquals("weird",result.rows.single().cells.single().sourceType)
        assertTrue(result.issues.any { it.code == "invalid_cell_reference" })
    }
    @Test fun exoticXmlEncodingsCannotHideEntities() {
        val xml = "<?xml version=\"1.0\" encoding=\"IBM037\"?><!DOCTYPE worksheet [<!ENTITY x \"secret\">]><worksheet><row><c><v>&x;</v></c></row></worksheet>"
        for (charset in listOf(java.nio.charset.Charset.forName("IBM037"), Charsets.UTF_16LE, Charsets.UTF_8)) {
            val result = SourceWorkbookParser.parse(workbook(xml, sheetEncoding = charset),"u",SourceScanLimits())
            assertTrue(result.rows.isEmpty())
            assertEquals("invalid_workbook", result.issues.single().code)
        }
    }
    @Test fun diagnosticsAreBoundedWithoutHidingPartialCoverage() {
        val sheet = "<worksheet><row>" + "<c r=\"0\"><v>x</v></c>".repeat(200) + "</row></worksheet>"
        val result = SourceWorkbookParser.parse(workbook(sheet),"u",SourceScanLimits())
        assertEquals(200,result.rows.single().cells.size)
        assertEquals(101,result.issues.size)
        assertEquals("diagnostic_limit",result.issues.last().code)
    }
    @Test fun cancellationIsNotTurnedIntoParseIssue() {
        try {
            SourceWorkbookParser.parse(workbook("<worksheet/>"),"u",SourceScanLimits()) { throw CancellationException("cancel") }
            fail("Cancellation must propagate")
        } catch (_: CancellationException) { }
    }
}

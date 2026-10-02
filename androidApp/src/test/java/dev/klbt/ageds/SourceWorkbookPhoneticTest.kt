package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

class SourceWorkbookPhoneticTest {
    private fun workbook(sheet: String, shared: String? = null): ByteArray {
        val output = ByteArrayOutputStream()
        ZipOutputStream(output).use { zip ->
            val parts = linkedMapOf("[Content_Types].xml" to "<Types/>", "xl/workbook.xml" to "<workbook/>",
                "xl/worksheets/sheet1.xml" to sheet)
            if (shared != null) parts["xl/sharedStrings.xml"] = shared
            for ((name, xml) in parts) {
                zip.putNextEntry(ZipEntry(name)); zip.write(xml.toByteArray()); zip.closeEntry()
            }
        }
        return output.toByteArray()
    }
    private fun sheet(cells: String) = "<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData><row r=\"1\">$cells</row></sheetData></worksheet>"

    @Test fun sharedAndInlinePhoneticHintsAreNotCellText() {
        val phonetic = "<t>東京</t><rPh sb=\"0\" eb=\"2\"><t>とうきょう</t></rPh>"
        val raw = workbook(sheet("<c r=\"A1\" t=\"s\"><v>0</v></c><c r=\"B1\" t=\"inlineStr\"><is>$phonetic</is></c>"),
            "<sst xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><si>$phonetic</si></sst>")
        val result = SourceWorkbookParser.parse(raw, "synthetic", SourceScanLimits())
        assertEquals(listOf("東京", "東京"), result.rows.single().cells.map { it.value })
        assertEquals(listOf("0", "東京"), result.rows.single().cells.map { it.raw })
        assertTrue(result.issues.any { it.code == "xlsx_phonetic_omitted" })
        assertFalse(result.issues.any { it.code == "invalid_workbook" })
    }
    @Test fun richRunsWhitespaceAndCachedFormulaValuesRemainLiteral() {
        val content = "<r><t xml:space=\"preserve\"> A </t></r><r><t>B</t></r><rPh sb=\"0\" eb=\"3\"><t>hint</t></rPh>"
        val result = SourceWorkbookParser.parse(workbook(sheet("<c r=\"A1\" t=\"inlineStr\"><is>$content</is></c><c r=\"B1\"><f>1+1</f><v>2</v></c>")), "u", SourceScanLimits())
        assertEquals(" A B", result.rows.single().cells[0].value)
        assertEquals(" A B", result.rows.single().cells[0].raw)
        assertEquals("2", result.rows.single().cells[1].value)
        assertEquals("1+1", result.rows.single().cells[1].formula)
    }

    @Test fun phoneticOnlyStringRemainsEmptyWithExplicitLoss() {
        val result = SourceWorkbookParser.parse(workbook(sheet("<c r=\"A1\" t=\"inlineStr\"><is><rPh sb=\"0\" eb=\"0\"><t>hint</t></rPh></is></c>")), "u", SourceScanLimits())
        assertEquals("", result.rows.single().cells.single().value)
        assertTrue(result.issues.any { it.code == "xlsx_phonetic_omitted" })
    }

    @Test fun omittedTextStillConsumesCombinedCharacterBudget() {
        val value = "<t>abc</t><rPh sb=\"0\" eb=\"3\"><t>de</t></rPh>"
        val shared = workbook(sheet("<c r=\"A1\" t=\"s\"><v>0</v></c>"), "<sst><si>$value</si></sst>")
        val inline = workbook(sheet("<c r=\"A1\" t=\"inlineStr\"><is>$value</is></c>"))
        for (bytes in listOf(shared, inline)) {
            val exact = SourceWorkbookParser.parse(bytes, "u", SourceScanLimits(maxCellChars = 5))
            assertEquals("abc", exact.rows.single().cells.single().value)
            val exceeded = SourceWorkbookParser.parse(bytes, "u", SourceScanLimits(maxCellChars = 4))
            assertTrue(exceeded.rows.isEmpty())
            assertTrue(exceeded.issues.any { it.code == "cell_chars_limit" })
        }
    }

    @Test fun malformedPhoneticContextsDoNotBecomeBaseOrFormulaText() {
        val contents = listOf("<rPh><t>outside</t></rPh>",
            "<is><r><rPh><t>nested-run</t></rPh></r></is>",
            "<is><rPh><rPh><t>nested-phonetic</t></rPh></rPh></is>",
            "<is><rPh><f>malicious-formula</f></rPh></is>")
        for (content in contents) {
            val result = SourceWorkbookParser.parse(workbook(sheet("<c r=\"A1\" t=\"inlineStr\">$content</c>")), "u", SourceScanLimits())
            assertTrue(result.issues.any { it.code == "invalid_workbook" })
            assertTrue(result.rows.isEmpty())
        }
    }

    @Test fun sharedPhoneticScopeEndsBeforeNextPlainString() {
        val shared = "<sst><si><t>base</t><rPh><t>hint</t></rPh></si><si><t>next</t></si></sst>"
        val result = SourceWorkbookParser.parse(workbook(sheet("<c r=\"A1\" t=\"s\"><v>0</v></c><c r=\"B1\" t=\"s\"><v>1</v></c>"), shared), "u", SourceScanLimits())
        assertEquals(listOf("base", "next"), result.rows.single().cells.map { it.value })
    }

    @Test fun repeatedLossDiagnosticsStayBoundedAndSourceUnchanged() {
        val hints = (1..150).joinToString("") { "<rPh><t>x</t></rPh>" }
        val bytes = workbook(sheet("<c r=\"A1\" t=\"inlineStr\"><is><t>base</t>$hints</is></c>"))
        val before = bytes.copyOf()
        val result = SourceWorkbookParser.parse(bytes, "u", SourceScanLimits())
        assertEquals("base", result.rows.single().cells.single().value)
        assertEquals(101, result.issues.size)
        assertTrue(result.issues.any { it.code == "diagnostic_limit" })
        assertTrue(before.contentEquals(bytes))
    }

}

package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import java.io.ByteArrayOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import org.junit.Assert.*
import org.junit.Test

/** Independent, synthetic OOXML packages; no external workbook or Android runtime. */
class SourceWorkbookPhoneticAdversarialTest {
    private fun zip(cells: String, shared: String? = null): ByteArray {
        val parts = linkedMapOf(
            "[Content_Types].xml" to "<Types/>",
            "xl/workbook.xml" to "<workbook/>",
            "xl/worksheets/sheet1.xml" to "<worksheet><sheetData><row r=\"1\">$cells</row></sheetData></worksheet>"
        )
        if (shared != null) parts["xl/sharedStrings.xml"] = "<sst>$shared</sst>"
        return ByteArrayOutputStream().also { output ->
            ZipOutputStream(output).use { stream ->
                parts.forEach { (name, xml) ->
                    stream.putNextEntry(ZipEntry(name)); stream.write(xml.toByteArray(Charsets.UTF_8)); stream.closeEntry()
                }
            }
        }.toByteArray()
    }
    private fun inline(body: String) = "<c r=\"A1\" t=\"inlineStr\"><is>$body</is></c>"
    private fun hint(text: String) = "<rPh sb=\"0\" eb=\"1\"><t>$text</t></rPh>"
    private fun parse(bytes: ByteArray, limit: Int = 16000) =
        SourceWorkbookParser.parse(bytes, "content://synthetic/tree/book.xlsx", SourceScanLimits(maxCellChars = limit))
    private fun loss(result: dev.klbt.ageds.core.ParsedSourceRows) {
        assertTrue(result.issues.any { it.code == "xlsx_phonetic_omitted" })
        assertTrue(result.issues.any { it.code == "xlsx_projection" })
    }

    @Test fun sharedRichRunsKeepWhitespaceUnicodeAndRawIndexOnReuse() {
        val bytes = zip("<c t=\"s\"><v>0</v></c><c t=\"s\"><v>0</v></c>",
            "<si><r><t xml:space=\"preserve\"> 東京 </t></r><r><t>🐈</t></r>${hint("とう")}${hint("きょう")}</si>")
        val before = bytes.copyOf()
        val result = parse(bytes)
        assertEquals(listOf(" 東京 🐈", " 東京 🐈"), result.rows.single().cells.map { it.value })
        assertEquals(listOf("0", "0"), result.rows.single().cells.map { it.raw })
        assertArrayEquals(before, bytes)
        loss(result)
    }

    @Test fun inlineRichRunsExcludeHintsFromBothRawAndValue() {
        val result = parse(zip(inline("<r><t xml:space=\"preserve\"> A </t></r><r><t>&amp;B</t></r>${hint("reading")}")))
        val cell = result.rows.single().cells.single()
        assertEquals(" A &B", cell.raw); assertEquals(" A &B", cell.value)
        loss(result)
    }

    @Test fun phoneticOnlyInlineIsExplicitEmptyBase() {
        val result = parse(zip(inline(hint("reading"))))
        assertEquals("", result.rows.single().cells.single().value)
        assertEquals("", result.rows.single().cells.single().raw)
        loss(result)
    }

    @Test fun phoneticOnlySharedStringDoesNotBecomePronunciation() {
        val result = parse(zip("<c t=\"s\"><v>0</v></c>", "<si>${hint("reading")}</si>"))
        assertEquals("", result.rows.single().cells.single().value)
        assertEquals("0", result.rows.single().cells.single().raw)
        loss(result)
    }

    @Test fun omissionDoesNotLeakIntoNextStringOrCachedFormula() {
        val result = parse(zip("<c t=\"s\"><v>0</v></c><c t=\"s\"><v>1</v></c><c><f>1+1</f><v>2</v></c>",
            "<si><t>甲</t>${hint("こう")}</si><si><t xml:space=\"preserve\"> +001 </t></si>"))
        val cells = result.rows.single().cells
        assertEquals(listOf("甲", " +001 ", "2"), cells.map { it.value })
        assertEquals("1+1", cells[2].formula); assertEquals("2", cells[2].raw)
        loss(result)
    }

    @Test fun plainAndRichBaseWithoutHintsRemainLossFree() {
        val result = parse(zip(inline("<t>plain</t>") + "<c r=\"B1\" t=\"s\"><v>0</v></c>", "<si><r><t>A</t></r><r><t> B</t></r></si>"))
        assertEquals(listOf("plain", "A B"), result.rows.single().cells.map { it.value })
        assertEquals(listOf("xlsx_projection"), result.issues.map { it.code })
    }

    @Test fun combinedInlineBaseAndMultipleHintsFitExactCharacterLimit() {
        val result = parse(zip(inline("<t>AB</t>${hint("cd")}${hint("ef")}")), 6)
        assertEquals("AB", result.rows.single().cells.single().value)
        loss(result)
    }

    @Test fun multipleIgnoredInlineHintsCannotResetCharacterBudget() {
        val result = parse(zip(inline("<t>AB</t>${hint("cd")}${hint("efg")}")), 6)
        assertTrue(result.issues.any { it.code == "cell_chars_limit" })
        assertTrue(result.rows.isEmpty())
    }

    @Test fun sharedBaseAndOmittedHintsShareCharacterBudget() {
        val result = parse(zip("<c t=\"s\"><v>0</v></c>", "<si><t>AB</t>${hint("cdefg")}</si>"), 6)
        assertTrue(result.issues.any { it.code == "cell_chars_limit" })
        assertTrue(result.rows.isEmpty())
    }

    @Test fun nestedPhoneticRunsRejectInsteadOfFabricatingCellText() {
        val result = parse(zip(inline("<t>A</t><rPh sb=\"0\" eb=\"1\"><t>x</t>${hint("y")}</rPh>")))
        assertTrue(result.issues.any { it.code == "invalid_workbook" })
        assertTrue(result.rows.isEmpty())
    }

    @Test fun phoneticRunInsideRichRunHasInvalidScope() {
        val result = parse(zip("<c t=\"s\"><v>0</v></c>", "<si><r><t>A</t>${hint("x")}</r></si>"))
        assertTrue(result.issues.any { it.code == "invalid_workbook" })
        assertTrue(result.rows.isEmpty())
    }

    @Test fun phoneticRunDirectlyUnderCellHasInvalidScope() {
        val result = parse(zip("<c t=\"inlineStr\"><is><t>A</t></is>${hint("x")}</c>"))
        assertTrue(result.issues.any { it.code == "invalid_workbook" })
        assertTrue(result.rows.isEmpty())
    }
}

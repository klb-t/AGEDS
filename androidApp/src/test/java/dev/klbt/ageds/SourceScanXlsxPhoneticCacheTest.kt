package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanResult
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.security.MessageDigest
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/** Production engine/parser/cache acceptance; synthetic provider, not Android SAF runtime. */
class SourceScanXlsxPhoneticCacheTest {
    @get:Rule val temporary = TemporaryFolder()
    private val json = Json { ignoreUnknownKeys = true }
    private val phonetic = "PHONETIC_ONLY_SENTINEL"

    // Compact ZIP construction follows SourceWorkbookParserTest; no office library or formula execution.
    private fun workbook(cellXml: String, strings: String? = null): ByteArray {
        val out = ByteArrayOutputStream()
        ZipOutputStream(out).use { zip ->
            val parts = linkedMapOf(
                "[Content_Types].xml" to "<Types/>",
                "xl/workbook.xml" to "<workbook/>",
                "xl/worksheets/sheet1.xml" to "<worksheet><sheetData><row r=\"1\">$cellXml</row></sheetData></worksheet>"
            )
            strings?.let { parts["xl/sharedStrings.xml"] = it }
            parts.forEach { (name, text) ->
                zip.putNextEntry(ZipEntry(name)); zip.write(text.toByteArray(Charsets.UTF_8)); zip.closeEntry()
            }
        }
        return out.toByteArray()
    }

    private fun roundTrip(bytes: ByteArray): Pair<SourceScanResult, String> {
        val sources = temporary.newFolder()
        val original = File(sources, "original.xlsx").apply { writeBytes(bytes) }
        val beforeTime = original.lastModified()
        val uri = "content://phonetic-acceptance/document/exact-id"
        val provider = object : SourceScanProvider {
            override val rootId = "root"
            override val rootUri = "content://phonetic-acceptance/tree/root"
            override fun uri(id: String) = uri
            override fun children(id: String): SourceScanCursor = object : SourceScanCursor {
                var returned = false
                override fun next(): SourceScanDocument? = if (returned) null else {
                    returned = true
                    SourceScanDocument("exact-id", original.name,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", bytes.size.toLong())
                }
                override fun close() = Unit
            }
            override fun openRead(id: String): InputStream = original.inputStream()
        }
        val scanned = SourceScanEngine(provider).scan(scannedAt = "2026-10-01T00:00:00Z")
        val cacheFile = File(temporary.newFolder(), "scan.json")
        val cache = BoundedMetadataCache(cacheFile, 1024 * 1024)
        cache.write(json.encodeToString(scanned))
        val wire = requireNotNull(cache.read())
        val restored = json.decodeFromString<SourceScanResult>(wire)
        assertEquals(scanned, restored)
        assertEquals(uri, restored.files.single().uri)
        assertEquals(provider.rootUri, restored.rootUri)
        assertEquals(bytes.size.toLong(), restored.bytesRead)
        assertEquals(MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") {
            "%02x".format(it.toInt() and 255)
        }, restored.files.single().sha256)
        assertArrayEquals(bytes, original.readBytes())
        assertEquals(beforeTime, original.lastModified())
        assertEquals(listOf("original.xlsx"), sources.list()!!.toList())
        assertEquals(listOf("scan.json"), cacheFile.parentFile!!.list()!!.toList())
        assertFalse(cacheFile.canonicalPath.startsWith(sources.canonicalPath + File.separator))
        return restored to wire
    }

    private fun assertOmission(result: SourceScanResult, wire: String) {
        assertEquals("partial", result.coverage)
        assertEquals("partial", result.files.single().coverage)
        assertTrue(result.files.single().issues.any { it.code == "xlsx_phonetic_omitted" })
        assertFalse(wire.contains(phonetic))
    }

    @Test fun inlineBaseAndRichWhitespaceExcludePhoneticsThroughTypedCache() {
        val (result, wire) = roundTrip(workbook(
            "<c r=\"A1\" t=\"inlineStr\"><is><t xml:space=\"preserve\"> Base </t><rPh sb=\"0\" eb=\"4\"><t>$phonetic</t></rPh></is></c>" +
                "<c r=\"B1\" t=\"inlineStr\"><is><r><t xml:space=\"preserve\"> rich </t></r><r><t xml:space=\"preserve\"> text </t></r><rPh sb=\"0\" eb=\"4\"><t>$phonetic</t></rPh></is></c>"
        ))
        val cells = result.files.single().rows.single().cells
        assertEquals(listOf(" Base ", " rich  text "), cells.map { it.value })
        assertEquals(listOf(" Base ", " rich  text "), cells.map { it.raw })
        assertOmission(result, wire)
    }

    @Test fun sharedRawIndexAndUnevaluatedFormulaSurviveWithExplicitOmission() {
        val (result, wire) = roundTrip(workbook(
            "<c r=\"A1\" t=\"s\"><v>0</v></c><c r=\"B1\"><f>1+1</f><v>99</v></c>",
            "<sst><si><r><t xml:space=\"preserve\"> Base </t></r><r><t>rich</t></r><rPh sb=\"0\" eb=\"4\"><t>$phonetic</t></rPh></si></sst>"
        ))
        val cells = result.files.single().rows.single().cells
        assertEquals("0", cells[0].raw)
        assertEquals(" Base rich", cells[0].value)
        assertEquals("1+1", cells[1].formula)
        assertEquals("99", cells[1].raw)
        assertEquals("99", cells[1].value)
        assertOmission(result, wire)
    }

    @Test fun ordinaryRichTextCacheDoesNotInventPhoneticLoss() {
        val (result, _) = roundTrip(workbook(
            "<c r=\"A1\" t=\"inlineStr\"><is><r><t xml:space=\"preserve\"> normal </t></r><r><t>rich</t></r></is></c>"
        ))
        val file = result.files.single()
        assertEquals(" normal rich", file.rows.single().cells.single().value)
        assertEquals(" normal rich", file.rows.single().cells.single().raw)
        assertFalse(file.issues.any { it.code == "xlsx_phonetic_omitted" })
        assertEquals(listOf("xlsx_projection"), file.issues.map { it.code })
        assertEquals("complete_within_scope", file.coverage)
        assertEquals("complete_within_scope", result.coverage)
    }
}

package dev.klbt.ageds

import dev.klbt.ageds.core.*
import java.io.ByteArrayInputStream
import java.io.StringReader
import java.io.ByteArrayOutputStream
import java.util.zip.ZipInputStream
import javax.xml.parsers.SAXParserFactory
import org.xml.sax.Attributes
import org.xml.sax.InputSource
import org.xml.sax.helpers.DefaultHandler

/** Bounded OOXML stored-cell projection. Never evaluates formulas, styles, dates or external links. */
object SourceWorkbookParser {
    private class Bound(val code: String) : RuntimeException(code)

    fun parse(bytes: ByteArray, locator: String, limits: SourceScanLimits,
              checkCancelled: () -> Unit = {}): ParsedSourceRows {
        val rows = mutableListOf<SourceRow>()
        val issues = mutableListOf<ScanIssue>()
        fun issue(value: ScanIssue) {
            if (issues.size < 100) issues += value
            else if (issues.size == 100) issues += ScanIssue("diagnostic_limit", "Further workbook diagnostics omitted", locator)
        }
        try {
            val members = linkedMapOf<String, ByteArray>()
            var expanded = 0L
            var entries = 0
            ZipInputStream(ByteArrayInputStream(bytes)).use { zip ->
                while (true) {
                    checkCancelled()
                    val entry = zip.nextEntry ?: break
                    if (++entries > limits.maxArchiveEntries) throw Bound("archive_entry_limit")
                    val name = entry.name
                    if (name in members) throw IllegalArgumentException("Duplicate ZIP member: $name")
                    val needed = name == "xl/sharedStrings.xml" || Regex("xl/worksheets/[^/]+\\.xml").matches(name)
                    val output = if (needed) ByteArrayOutputStream() else null
                    val buffer = ByteArray(8192)
                    while (true) {
                        checkCancelled()
                        val n = zip.read(buffer)
                        if (n < 0) break
                        expanded += n
                        if (expanded > limits.maxExpandedBytes) throw Bound("expanded_bytes_limit")
                        output?.write(buffer, 0, n)
                    }
                    // Keep all names to reject duplicates, but bytes only for supported members.
                    members[name] = output?.toByteArray() ?: ByteArray(0)
                    zip.closeEntry()
                }
            }
            if ("[Content_Types].xml" !in members || "xl/workbook.xml" !in members)
                throw IllegalArgumentException("Not an OOXML workbook")
            val strings = mutableListOf<String>()
            members["xl/sharedStrings.xml"]?.let { xml ->
                var inside = false
                var inText = false
                val value = StringBuilder()
                parseXml(xml, object : DefaultHandler() {
                    override fun startElement(uri: String?, local: String?, q: String?, a: Attributes?) {
                        checkCancelled()
                        when (tag(local, q)) { "si" -> { inside = true; value.clear() }; "t" -> inText = inside }
                    }
                    override fun characters(ch: CharArray, start: Int, length: Int) {
                        if (inText) {
                            if (value.length + length > limits.maxCellChars) throw Bound("cell_chars_limit")
                            value.append(ch, start, length)
                        }
                    }
                    override fun endElement(uri: String?, local: String?, q: String?) {
                        when (tag(local, q)) {
                            "t" -> inText = false
                            "si" -> {
                                if (strings.size >= limits.maxCellsPerFile) throw Bound("shared_strings_limit")
                                strings += value.toString(); inside = false
                            }
                        }
                    }
                })
            }
            var cellCount = 0
            val sheets = members.keys.filter { Regex("xl/worksheets/[^/]+\\.xml").matches(it) }.sorted()
            if (sheets.isEmpty()) issue(ScanIssue("no_worksheets", "No supported worksheet parts found", locator))
            sheets.forEach { name ->
                var cells = mutableListOf<SourceCell>()
                var rowIndex = 0
                var rowRef = ""
                var cellRef = ""
                var cellType = ""
                var cellTypeRaw: String? = null
                var field = ""
                var inCell = false
                var inRow = false
                var inline = false
                val raw = StringBuilder()
                val formula = StringBuilder()
                val inlineText = StringBuilder()
                var formulaPresent = false
                parseXml(members.getValue(name), object : DefaultHandler() {
                    override fun startElement(uri: String?, local: String?, q: String?, a: Attributes) {
                        checkCancelled()
                        when (val t = tag(local, q)) {
                            "row" -> {
                                require(!inRow) { "Nested worksheet rows are unsupported" }
                                if (rows.size >= limits.maxRowsPerFile) throw Bound("row_limit")
                                inRow = true
                                cells = mutableListOf(); rowIndex++; rowRef = a.getValue("r") ?: rowIndex.toString()
                            }
                            "c" -> {
                                require(inRow && !inCell) { "Cells must occur in a single worksheet row" }
                                if (++cellCount > limits.maxCellsPerFile) throw Bound("cell_limit")
                                inCell = true; cellRef = a.getValue("r") ?: ""; cellTypeRaw = a.getValue("t"); cellType = cellTypeRaw ?: "n"
                                raw.clear(); formula.clear(); inlineText.clear(); formulaPresent = false
                            }
                            "is" -> inline = inCell
                            "v", "f", "t" -> if (inCell && (t != "t" || inline)) {
                                field = t; if (t == "f") formulaPresent = true
                            }
                        }
                    }
                    override fun characters(ch: CharArray, start: Int, length: Int) {
                        val out = when (field) { "v" -> raw; "f" -> formula; "t" -> inlineText; else -> null }
                        if (out != null) {
                            if (out.length + length > limits.maxCellChars) throw Bound("cell_chars_limit")
                            out.append(ch, start, length)
                        }
                    }
                    override fun endElement(uri: String?, local: String?, q: String?) {
                        when (tag(local, q)) {
                            "v", "f", "t" -> field = ""
                            "is" -> inline = false
                            "c" -> {
                                val stored = if (cellType == "inlineStr") inlineText.toString() else raw.toString()
                                val decoded = if (cellType == "s") {
                                    val index = stored.toIntOrNull()
                                    if (index == null || index !in strings.indices) {
                                        issue(ScanIssue("invalid_shared_string", "Unresolved shared string index retained", "$locator!$name#$cellRef"))
                                        stored
                                    } else strings[index]
                                } else stored
                                var column = 0
                                for (c in cellRef.takeWhile { it in 'A'..'Z' }) {
                                    if (column > 16384) { column = 0; break }
                                    column = column * 26 + (c - 'A' + 1)
                                }
                                if (column !in 1..16384 || !Regex("[A-Z]+[1-9][0-9]*").matches(cellRef)) {
                                    issue(ScanIssue("invalid_cell_reference", "Missing/invalid address retained; column is provisional sequence position", "$locator!$name#$cellRef"))
                                    column = (cells.lastOrNull()?.column ?: 0) + 1
                                }
                                cells += SourceCell(column, stored, decoded, if (formulaPresent) formula.toString() else null,
                                    cellRef.takeIf { it.isNotEmpty() }, cellTypeRaw)
                                inCell = false
                            }
                            "row" -> {
                                require(inRow && !inCell) { "Malformed worksheet row" }
                                if (rows.size >= limits.maxRowsPerFile) throw Bound("row_limit")
                                rows += SourceRow("$locator!$name#row=$rowRef;ordinal=$rowIndex", cells.toList())
                                inRow = false
                            }
                        }
                    }
                })
            }
            issue(ScanIssue("xlsx_projection", "Stored cells only: strict UTF-8 or BOM-marked UTF-16 XML; no styles, date conversion, formula evaluation, workbook ordering or external references", locator))
        } catch (e: Bound) {
            issue(ScanIssue(e.code, "Workbook processing budget reached; remaining content omitted", locator))
        } catch (e: java.util.concurrent.CancellationException) { throw e
        } catch (e: Exception) {
            issue(ScanIssue("invalid_workbook", "Workbook parse failed: ${e.message?.take(200)}", locator))
        }
        return ParsedSourceRows(rows, issues)
    }

    private fun tag(local: String?, qualified: String?) = local?.takeIf { it.isNotEmpty() } ?: qualified.orEmpty().substringAfter(':')

    private fun parseXml(bytes: ByteArray, handler: DefaultHandler) {
        // Decode before preflight, then give SAX exactly that character stream. Alternate
        // encodings cannot hide declarations or cause SAX to reinterpret checked bytes.
        val decoded = SourceTextDecoding.decode(bytes)
        val xml = decoded.text
        val declaredEncoding = Regex("""<\?xml[^?]*encoding\s*=\s*["']([^"']+)["']""", RegexOption.IGNORE_CASE)
            .find(xml)?.groupValues?.get(1)?.lowercase()
        val allowed = when (decoded.encoding) {
            "UTF-16LE" -> setOf("utf-16", "utf-16le")
            "UTF-16BE" -> setOf("utf-16", "utf-16be")
            else -> setOf("utf-8", "utf8", "us-ascii")
        }
        require(declaredEncoding == null || declaredEncoding in allowed) {
            "Unsupported or conflicting workbook XML encoding: $declaredEncoding"
        }
        require(declaredEncoding != "us-ascii" || xml.all { it.code < 128 }) { "Non-ASCII XML declares US-ASCII" }
        require(!xml.contains("<!DOCTYPE", ignoreCase = true) && !xml.contains("<!ENTITY", ignoreCase = true)) {
            "DTD/entity declarations forbidden"
        }
        val factory = SAXParserFactory.newInstance().apply { isNamespaceAware = true; isValidating = false }
        val reader = factory.newSAXParser().xmlReader
        // Android parsers differ in optional feature names; strict preflight above is mandatory.
        runCatching { reader.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true) }
        runCatching { reader.setFeature("http://javax.xml.XMLConstants/feature/secure-processing", true) }
        reader.setFeature("http://xml.org/sax/features/external-general-entities", false)
        reader.setFeature("http://xml.org/sax/features/external-parameter-entities", false)
        reader.entityResolver = org.xml.sax.EntityResolver { _, _ -> throw IllegalArgumentException("External entity forbidden") }
        reader.contentHandler = handler
        reader.errorHandler = handler
        reader.parse(InputSource(StringReader(xml)))
    }
}

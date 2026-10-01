package dev.klbt.ageds

import dev.klbt.ageds.core.*
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.charset.CodingErrorAction
import java.util.concurrent.CancellationException

/** Read-only CFB v3 / BIFF8 projection, not a spreadsheet engine. See docs/NATIVE_XLS.md. */
object SourceXlsParser {
    private class Stop(val code: String, message: String) : RuntimeException(message)
    private fun fail(message: String): Nothing = throw Stop("invalid_xls", message)
    private fun unsupported(message: String): Nothing = throw Stop("unsupported_xls", message)
    private fun bound(code: String): Nothing = throw Stop(code, "XLS processing budget reached; remaining content omitted")
    private fun ByteArray.u16(p: Int): Int {
        if (p < 0 || p > size - 2) fail("Truncated 16-bit field")
        return (this[p].toInt() and 255) or ((this[p + 1].toInt() and 255) shl 8)
    }
    private fun ByteArray.i32(p: Int): Int {
        if (p < 0 || p > size - 4) fail("Truncated 32-bit field")
        return u16(p) or (u16(p + 2) shl 16)
    }
    private fun ByteArray.uint(p: Int) = i32(p).toLong() and 0xffffffffL
    private fun ByteArray.hex() = joinToString("") { "%02x".format(it.toInt() and 255) }
    private fun text(bytes: ByteArray, wide: Boolean): String = if (wide) {
        Charsets.UTF_16LE.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
    } else bytes.joinToString("") { (it.toInt() and 255).toChar().toString() }

    fun parse(bytes: ByteArray, locator: String, limits: SourceScanLimits,
              checkCancelled: () -> Unit = {}): ParsedSourceRows {
        val rows = mutableListOf<SourceRow>()
        val issues = mutableListOf<ScanIssue>()
        fun issue(code: String, message: String, ref: String = locator) {
            if (issues.size < 100) issues += ScanIssue(code, message, ref)
            else if (issues.size == 100) issues += ScanIssue("diagnostic_limit", "Further XLS diagnostics omitted", locator)
        }
        try {
            checkCancelled()
            if (bytes.size > limits.maxFileBytes) bound("file_bytes_limit")
            val (streamName, workbook) = Compound(bytes, limits, checkCancelled).workbook()
            val records = mutableListOf<Record>()
            var pos = 0
            while (pos < workbook.size) {
                checkCancelled()
                if (pos > workbook.size - 4) fail("Truncated BIFF record header")
                if (records.size >= 100000) bound("xls_record_limit")
                val id = workbook.u16(pos); val size = workbook.u16(pos + 2)
                if (id == 0 && size == 0) {
                    for (padding in pos until workbook.size) {
                        if (padding % 4096 == 0) checkCancelled()
                        if (workbook[padding] != 0.toByte()) fail("Nonzero data after BIFF zero padding")
                    }
                    break
                }
                if (id == 0x002f) unsupported("Encrypted XLS is not decoded")
                if (size > 8224 || pos + 4 > workbook.size - size) fail("Invalid BIFF record length at $pos")
                if (id == 0x000a && size != 0) fail("Invalid BIFF EOF length")
                records += Record(id, pos, workbook.copyOfRange(pos + 4, pos + 4 + size))
                pos += 4 + size
            }
            if (records.isEmpty()) fail("Empty Workbook stream")
            fun bof(record: Record, type: Int) {
                if (record.id != 0x0809 || record.data.size < 16) fail("Missing BIFF8 BOF at ${record.offset}")
                if (record.data.u16(0) != 0x0600) unsupported("Only BIFF8 is supported")
                if (record.data.u16(2) != type) unsupported("Unsupported BIFF substream type")
            }
            bof(records.first(), 5)
            val globalEnd = records.indexOfFirst { it.id == 0x000a }
            if (globalEnd < 0) fail("Missing workbook-global EOF")
            val globals = records.take(globalEnd + 1)
            if (globals.any { it.id == 0x002f }) unsupported("Encrypted XLS is not decoded")
            val sheets = globals.filter { it.id == 0x0085 }
            if (sheets.size > limits.maxArchiveEntries) bound("archive_entry_limit")
            val strings = mutableListOf<String>()
            val sstIndex = globals.indexOfFirst { it.id == 0x00fc }
            if (globals.count { it.id == 0x00fc } > 1) fail("Duplicate shared string table")
            if (sstIndex >= 0) {
                var end = sstIndex + 1
                while (end < globals.size && globals[end].id == 0x003c) end++
                try {
                    // Publish the table only after all declared strings and boundaries validate.
                    strings += sharedStrings(globals.subList(sstIndex, end), limits, checkCancelled)
                } catch (e: Stop) {
                    if (e.code !in setOf("invalid_xls", "unsupported_xls")) throw e
                    issue(e.code, "Shared string table omitted: ${e.message}")
                }
            }
            val offsets = records.withIndex().associate { it.value.offset to it.index }
            val usedSheets = mutableSetOf<Int>()
            var cellCount = 0
            var projectionChars = 0L
            for ((sheetOrdinal, sheet) in sheets.withIndex()) {
                checkCancelled()
                val d = sheet.data
                if (d.size < 8) fail("Truncated BoundSheet8")
                val sheetOffset = d.uint(0)
                if (sheetOffset > Int.MAX_VALUE) fail("Invalid sheet offset")
                if (!usedSheets.add(sheetOffset.toInt())) fail("Duplicate sheet stream offset")
                val nameLength = d[6].toInt() and 255
                val nameWide = d[7].toInt() and 1 != 0
                if ((d[7].toInt() and 254) != 0 || d.size != 8 + nameLength * (if (nameWide) 2 else 1)) fail("Invalid sheet name")
                val sheetName = text(d.copyOfRange(8, d.size), nameWide)
                if (d[5].toInt() != 0) { issue("xls_sheet_unsupported", "Chart/macro/non-worksheet substream omitted", "$locator!sheet=${sheetOrdinal + 1}"); continue }
                val start = offsets[sheetOffset.toInt()] ?: fail("Sheet offset is not a record boundary")
                if (start <= globalEnd) fail("Sheet overlaps workbook globals")
                bof(records[start], 0x10)
                var ended = false
                var rowNumber = -1
                var cells = mutableListOf<SourceCell>()
                var rowOffset = 0
                fun flush() {
                    if (cells.isNotEmpty()) {
                        rows += SourceRow("$locator!sheet=${sheetOrdinal + 1};name=$sheetName#row=${rowNumber + 1};offset=$rowOffset", cells.toList())
                        cells = mutableListOf()
                    }
                }
                fun cell(record: Record, row: Int, col: Int, value: String, type: String, formula: String? = null) {
                    if (col !in 0..255) fail("BIFF8 column outside bounds")
                    if (row != rowNumber) {
                        flush()
                        if (rows.size >= limits.maxRowsPerFile) bound("row_limit")
                        rowNumber = row; rowOffset = record.offset
                    }
                    if (++cellCount > limits.maxCellsPerFile) bound("cell_limit")
                    if (record.data.size * 2 > limits.maxCellChars || value.length > limits.maxCellChars) bound("cell_chars_limit")
                    val column = if (col < 26) ('A' + col).toString() else ('A' + col / 26 - 1).toString() + ('A' + col % 26)
                    projectionChars += record.data.size * 2L + value.length + (formula?.length ?: 0)
                    if (projectionChars > limits.maxExpandedBytes) bound("projection_chars_limit")
                    cells += SourceCell(col + 1, record.hex, value, formula,
                        "$column${row + 1};stream=$streamName;offset=${record.offset}", "BIFF8:$type")
                }
                try {
                    for (index in start + 1 until records.size) {
                        checkCancelled()
                        val r = records[index]; val p = r.data
                        if (r.id == 0x000a) { ended = true; break }
                        if (r.id == 0x0809) fail("Worksheet missing EOF before next BOF")
                        fun scalar(value: String, type: String, formula: String? = null) = cell(r, p.u16(0), p.u16(2), value, type, formula)
                        when (r.id) {
                            0x0203 -> { if (p.size != 14) fail("Invalid NUMBER"); scalar(number(p, 6), "NUMBER") }
                            0x027e -> { if (p.size != 10) fail("Invalid RK"); scalar(rk(p.i32(6)), "RK") }
                            0x00bd -> {
                                if (p.size < 12 || (p.size - 6) % 6 != 0) fail("Invalid MULRK")
                                val first = p.u16(2); val count = (p.size - 6) / 6
                                if (p.u16(p.size - 2) != first + count - 1) fail("Invalid MULRK columns")
                                repeat(count) { cell(r, p.u16(0), first + it, rk(p.i32(6 + 6 * it)), "MULRK") }
                            }
                            0x00fd -> {
                                if (p.size != 10) fail("Invalid LABELSST")
                                val idx = p.uint(6)
                                val value = strings.getOrNull(if (idx <= Int.MAX_VALUE) idx.toInt() else -1)
                                if (value == null) issue("xls_unresolved_shared_string", "Unresolved SST index $idx retained as value", "$locator!offset=${r.offset}")
                                scalar(value ?: idx.toString(), "LABELSST")
                            }
                            0x0205 -> {
                                if (p.size != 8 || p[7].toInt() !in 0..1) fail("Invalid BOOLERR")
                                if (p[7].toInt() == 1) scalar("error:${p[6].toInt() and 255}", "BOOLERR")
                                else { if (p[6].toInt() !in 0..1) fail("Invalid boolean"); scalar((p[6].toInt() == 1).toString(), "BOOLERR") }
                            }
                            0x0201 -> { if (p.size != 6) fail("Invalid BLANK"); scalar("", "BLANK") }
                            0x0006 -> {
                                if (p.size < 22 || p.u16(20) > p.size - 22) fail("Invalid FORMULA")
                                val value = if (p.u16(12) != 0xffff) number(p, 6) else when (p[6].toInt()) {
                                    0 -> { issue("xls_formula_string_unsupported", "Formula string cache omitted; raw formula retained", "$locator!offset=${r.offset}"); "" }
                                    1 -> { if (p[8].toInt() !in 0..1) fail("Invalid formula boolean cache"); (p[8].toInt() == 1).toString() }
                                    2 -> "error:${p[8].toInt() and 255}"
                                    3 -> ""
                                    else -> fail("Invalid formula cache type")
                                }
                                scalar(value, "FORMULA", p.copyOfRange(22, 22 + p.u16(20)).hex())
                            }
                            0x0204, 0x00be, 0x00d6 -> issue("xls_cell_record_unsupported", "Cell record 0x${r.id.toString(16)} omitted", "$locator!offset=${r.offset}")
                        }
                    }
                    if (!ended) fail("Worksheet missing EOF")
                } finally { flush() }
            }
            if (sheets.isEmpty()) issue("no_worksheets", "No BoundSheet8 entries found")
        } catch (e: CancellationException) { throw e
        } catch (e: Stop) { issue(e.code, e.message.orEmpty())
        } catch (e: Exception) { issue("invalid_xls", "XLS parse failed: ${e.message?.take(200)}") }
        issue("xls_projection", "Partial BIFF8 stored-cell projection: raw is record-payload hex; no formulas executed, styles/date conversion, links, macros, drawings, or full workbook reconstruction")
        return ParsedSourceRows(rows, issues)
    }

    private data class Record(val id: Int, val offset: Int, val data: ByteArray) {
        val hex: String by lazy { data.hex() }
    }
    private fun number(p: ByteArray, at: Int): String = ByteBuffer.wrap(p, at, 8).order(ByteOrder.LITTLE_ENDIAN).double.toString()
    private fun rk(value: Int): String {
        val n = if (value and 2 != 0) (value shr 2).toDouble() else java.lang.Double.longBitsToDouble((value.toLong() and 0xfffffffcL) shl 32)
        return (if (value and 1 != 0) n / 100 else n).toString()
    }
    /** SST headers remain atomic; only character data consumes a continuation width byte. */
    private fun sharedStrings(records: List<Record>, limits: SourceScanLimits, cancelled: () -> Unit): List<String> {
        val first = records.first().data
        if (first.size < 8) fail("Truncated SST header")
        val count = first.uint(4)
        if (count > first.uint(0)) fail("SST unique count exceeds total count")
        if (count > limits.maxCellsPerFile) bound("shared_strings_limit")
        var recordIndex = 0
        var data = first
        var p = 8
        var decodedChars = 0L
        fun nextRecord() {
            cancelled()
            recordIndex++
            data = records.getOrNull(recordIndex)?.data ?: fail("Truncated SST continuation")
            p = 0
            if (data.isEmpty()) fail("Empty SST continuation")
        }
        val result = ArrayList<String>(count.toInt())
        repeat(count.toInt()) {
            cancelled()
            if (p == data.size) nextRecord() // New string: full header, no width prefix.
            if (data.size - p < 3) fail("SST string header split or truncated")
            val length = data.u16(p)
            val flags = data[p + 2].toInt() and 255
            if (flags and 0xf2 != 0) fail("Invalid Unicode flags")
            val headerSize = 3 + (if (flags and 8 != 0) 2 else 0) + (if (flags and 4 != 0) 4 else 0)
            if (data.size - p < headerSize) fail("SST string header split or truncated")
            if (length > limits.maxCellChars) bound("cell_chars_limit")
            decodedChars += length
            if (decodedChars > limits.maxExpandedBytes) bound("shared_string_chars_limit")
            p += 3
            val runs = if (flags and 8 != 0) data.u16(p).also { p += 2 } else 0
            val ext = if (flags and 4 != 0) data.i32(p).also { p += 4 } else 0
            if (ext < 0) fail("Negative phonetic string size")
            var wide = flags and 1 != 0
            val utf16 = ByteArrayOutputStream(length * 2)
            repeat(length) { character ->
                if (character % 1024 == 0) cancelled()
                if (p == data.size) {
                    nextRecord()
                    val continuationFlags = data[p++].toInt() and 255
                    if (continuationFlags !in 0..1) fail("Invalid SST continuation compression flag")
                    wide = continuationFlags == 1
                }
                val width = if (wide) 2 else 1
                if (data.size - p < width) fail("Split or truncated SST character")
                utf16.write(data[p++].toInt() and 255)
                utf16.write(if (wide) data[p++].toInt() and 255 else 0)
            }
            val tail = runs * 4L + ext
            if (tail > data.size - p) {
                if (recordIndex + 1 < records.size) unsupported("Continued SST rich-text/phonetic metadata is not decoded")
                fail("Truncated SST rich-text/phonetic metadata")
            }
            p += tail.toInt()
            // Decode once, so a surrogate pair may straddle records without replacement.
            result += try { text(utf16.toByteArray(), true) }
                catch (_: java.nio.charset.CharacterCodingException) { fail("Invalid SST UTF-16") }
        }
        if (p != data.size || recordIndex != records.lastIndex) fail("Unexpected SST trailing bytes or continuation")
        return result
    }

    private class Compound(val bytes: ByteArray, val limits: SourceScanLimits, val cancelled: () -> Unit) {
        private val sectorCount = (bytes.size / 512) - 1
        private val claimed = mutableSetOf<Int>()
        private lateinit var fat: IntArray
        private var expanded = 0L
        private fun sector(id: Int): ByteArray {
            cancelled()
            if (id !in 0 until sectorCount) fail("Sector outside file: $id")
            return bytes.copyOfRange((id + 1) * 512, (id + 2) * 512)
        }
        private fun chain(start: Int, size: Long? = null): ByteArray {
            if (size != null && (size < 0 || size > limits.maxExpandedBytes)) bound("expanded_bytes_limit")
            val out = ByteArrayOutputStream()
            var id = start
            val expected = size?.let { (it + 511) / 512 }
            var count = 0
            while (id != -2) {
                cancelled()
                if (expected != null && count >= expected) fail("Overlong sector chain")
                if (!claimed.add(id)) fail("Cyclic or overlapping sector chain")
                val data = sector(id)
                expanded += 512
                if (expanded > limits.maxExpandedBytes) bound("expanded_bytes_limit")
                out.write(data); count++
                id = fat.getOrNull(id) ?: fail("Missing FAT entry")
            }
            if (expected != null && count.toLong() != expected) fail("Truncated sector chain")
            return out.toByteArray().let { if (size != null) it.copyOf(size.toInt()) else it }
        }
        private data class Entry(val name: String, val type: Int, val left: Int, val right: Int, val child: Int, val start: Int, val size: Long)
        fun workbook(): Pair<String, ByteArray> {
            if (bytes.size < 1536 || bytes.size % 512 != 0 || !bytes.copyOfRange(0, 8).contentEquals(byteArrayOf(0xd0.toByte(),0xcf.toByte(),0x11,0xe0.toByte(),0xa1.toByte(),0xb1.toByte(),0x1a,0xe1.toByte()))) fail("Not a CFB file")
            if (bytes.u16(26) != 3 || bytes.u16(30) != 9) unsupported("Only CFB version 3 (512-byte sectors) supported")
            if (bytes.u16(28) != 0xfffe || bytes.u16(32) != 6 || bytes.uint(56) != 4096L || bytes.uint(40) != 0L) fail("Invalid CFB header")
            if (bytes.uint(72) != 0L || bytes.i32(68) != -2) unsupported("Extended DIFAT not supported")
            val fatCount = bytes.uint(44)
            if (fatCount !in 1L..109L || fatCount > sectorCount) fail("Invalid FAT sector count")
            val fatIds = (0 until fatCount.toInt()).map { bytes.i32(76 + it * 4) }
            if (fatIds.toSet().size != fatIds.size) fail("Duplicate FAT sector")
            fat = IntArray(fatIds.size * 128)
            fatIds.forEachIndexed { i, id ->
                claimed.add(id)
                val data = sector(id)
                repeat(128) { fat[i * 128 + it] = data.i32(it * 4) }
            }
            fatIds.forEach { if (fat.getOrNull(it) != -3) fail("FAT sector lacks FAT marker") }
            val directory = chain(bytes.i32(48))
            if (directory.size / 128 > limits.maxArchiveEntries) bound("archive_entry_limit")
            val entries = (0 until directory.size / 128).map { n ->
                val p = n * 128; val type = directory[p + 66].toInt() and 255
                if (type == 0) Entry("", 0, -1, -1, -1, -2, 0) else {
                    val nameSize = directory.u16(p + 64)
                    if (nameSize !in 2..64 || nameSize % 2 != 0 || directory.u16(p + nameSize - 2) != 0) fail("Invalid directory name")
                    if (directory.uint(p + 124) != 0L) fail("Oversized v3 stream")
                    Entry(text(directory.copyOfRange(p, p + nameSize - 2), true), type, directory.i32(p + 68), directory.i32(p + 72), directory.i32(p + 76), directory.i32(p + 116), directory.uint(p + 120))
                }
            }
            val root = entries.firstOrNull() ?: fail("Missing root directory")
            if (root.type != 5) fail("Invalid root directory")
            val pending = java.util.ArrayDeque<Int>()
            if (root.child != -1) pending.add(root.child)
            val visited = mutableSetOf<Int>(); val candidates = mutableListOf<Entry>()
            while (pending.isNotEmpty()) {
                cancelled()
                val id = pending.removeFirst()
                if (id == 0 || !visited.add(id)) fail("Cyclic directory tree")
                val entry = entries.getOrNull(id) ?: fail("Invalid directory reference")
                if (entry.type !in 1..2) fail("Invalid directory child")
                if (entry.left != -1) pending.add(entry.left)
                if (entry.right != -1) pending.add(entry.right)
                if (entry.type == 2 && entry.name.lowercase() in setOf("workbook", "book")) candidates += entry
            }
            if (candidates.size != 1) fail("Expected exactly one root Workbook/Book stream")
            val book = candidates.single()
            if (book.size >= 4096) return book.name to chain(book.start, book.size)
            val miniCount = bytes.uint(64)
            if (miniCount == 0L || miniCount > sectorCount) fail("Missing/invalid MiniFAT")
            val miniFat = chain(bytes.i32(60), miniCount * 512)
            val mini = chain(root.start, root.size)
            val out = ByteArrayOutputStream(); val seen = mutableSetOf<Int>()
            var id = book.start; var count = 0
            val expected = (book.size + 63) / 64
            while (id != -2) {
                cancelled()
                if (count >= expected || !seen.add(id)) fail("Cyclic/overlong mini chain")
                if (id < 0 || id.toLong() * 64 + 64 > mini.size || id.toLong() * 4 + 4 > miniFat.size) fail("Mini sector outside stream")
                out.write(mini, id * 64, 64); count++
                id = miniFat.i32(id * 4)
            }
            if (count.toLong() != expected) fail("Truncated mini chain")
            return book.name to out.toByteArray().copyOf(book.size.toInt())
        }
    }
}

package dev.klbt.ageds

import dev.klbt.ageds.core.*
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.InputStream
import java.security.MessageDigest
import java.util.ArrayDeque

const val SOURCE_DIRECTORY_MIME = "vnd.android.document/directory"

/** Literal provider observations; identity is id, never a filename or path guess. */
data class SourceScanDocument(val id: String, val name: String, val mime: String?, val sizeBytes: Long?)

/** Lazy cursor owned by the engine. null means EOF; implementations must not preload the tree. */
interface SourceScanCursor : Closeable {
    fun next(): SourceScanDocument?
}

/** Narrow read-only execution seam for the existing scanner; not a filesystem platform. */
interface SourceScanProvider {
    val rootId: String
    val rootUri: String
    fun uri(id: String): String
    fun children(id: String): SourceScanCursor
    fun openRead(id: String): InputStream
}

/** Production orchestration shared by the SAF adapter and synthetic host-JVM acceptance. */
class SourceScanEngine(private val provider: SourceScanProvider) {
    fun scan(limits: SourceScanLimits = SourceScanLimits(), scannedAt: String,
             checkCancelled: () -> Unit = {}): SourceScanResult {
        val check = checkCancelled
        data class Node(val id: String, val name: String, val path: String, val mime: String?, val size: Long?, val depth: Int)
        val files = mutableListOf<ScannedSourceFile>()
        val issues = mutableListOf<ScanIssue>()
        val pending = ArrayDeque<Node>()
        val visited = hashSetOf<String>()
        var directories = 0
        var bytesRead = 0L
        var discovered = 0
        var retainedChars = 0L
        var retainedRows = 0
        var retainedCells = 0
        var locatorChars = 0L
        val rootId = provider.rootId
        pending.add(Node(rootId, "", "", SOURCE_DIRECTORY_MIME, null, 0))
        while (pending.isNotEmpty()) {
            check()
            val node = pending.removeFirst()
            val uri = provider.uri(node.id)
            if (!visited.add(node.id)) {
                issues += ScanIssue("repeated_document", "Provider repeated a document ID; second path not traversed", uri.toString()); continue
            }
            if (node.mime == SOURCE_DIRECTORY_MIME) {
                if (directories >= limits.maxDirectories || node.depth > limits.maxDepth) {
                    issues += ScanIssue("directory_limit", "Directory/depth budget reached", uri.toString()); continue
                }
                directories++
                try {
                    provider.children(node.id).use { cursor ->
                        while (true) {
                            check()
                            if (discovered >= limits.maxFiles + limits.maxDirectories) {
                                issues += ScanIssue("entry_limit", "Entry budget reached; directory EOF was not probed", uri); break
                            }
                            val document = cursor.next() ?: break
                            discovered++
                            val id = document.id
                            val name = document.name
                            if (name.length > limits.maxCellChars || id.length > limits.maxCellChars) {
                                issues += ScanIssue("locator_limit", "Oversized document ID/name omitted", uri.toString()); continue
                            }
                            val mime = document.mime
                            val size = document.sizeBytes?.takeIf { it >= 0 }
                            val path = if (node.path.isEmpty()) name else "${node.path}/$name"
                            val locatorCost = id.length.toLong() + name.length + path.length
                            if (locatorChars + locatorCost > 1_000_000) {
                                issues += ScanIssue("locator_budget", "Cumulative document locator budget reached; remaining entries omitted", uri.toString()); break
                            }
                            locatorChars += locatorCost
                            pending.add(Node(id, name, path, mime, size, node.depth + 1))
                        }
                    }
                } catch (e: java.util.concurrent.CancellationException) { throw e
                } catch (e: Exception) {
                    issues += ScanIssue("directory_unreadable", "Directory could not be enumerated: ${e.message?.take(200)}", uri.toString())
                }
                continue
            }
            if (files.size >= limits.maxFiles) {
                issues += ScanIssue("file_limit", "Remaining files were not inspected", uri.toString()); break
            }
            val ext = node.name.substringAfterLast('.', "").lowercase()
            val kind = when {
                ext in setOf("wav", "mp3", "m4a", "ogg", "flac", "aac", "amr", "opus") || node.mime?.startsWith("audio/") == true -> "audio"
                ext in setOf("csv", "tsv", "xlsx", "xls") -> "table"
                else -> "inventory"
            }
            val fileIssues = mutableListOf<ScanIssue>()
            var rows: List<SourceRow> = emptyList()
            var hash: String? = null
            var duration: Double? = null
            var textFormat: SourceTextFormat? = null
            var wavHeader: WavHeaderObservation? = null
            var coverage = "inventory_only"
            try {
                val remaining = (limits.maxTotalBytes - bytesRead).coerceAtLeast(0)
                val cap = minOf(limits.maxFileBytes, remaining)
                if (ext == "wav" && cap > 0) {
                    val input = provider.openRead(node.id)
                    val read = SourceWavReader.read(input, node.size, limits.maxFileBytes, remaining, check)
                    bytesRead += read.bytesRead
                    hash = read.sha256
                    wavHeader = read.wavHeader
                    duration = read.wavHeader.declaredDurationSec
                    fileIssues += (read.issues + read.wavHeader.issues).map { it.copy(locator = uri.toString()) }
                    coverage = when {
                        read.issues.any { it.code == "read_failed" } -> "unreadable"
                        !read.complete || read.wavHeader.status != "observed" || fileIssues.isNotEmpty() -> "partial"
                        else -> "inventory_only"
                    }
                } else if (cap == 0L || (node.size != null && node.size > cap)) {
                    coverage = "partial"
                    fileIssues += ScanIssue("bytes_limit", "Content not read because byte budget would be exceeded", uri.toString())
                } else {
                    val digest = MessageDigest.getInstance("SHA-256")
                    val output = if (ext in setOf("csv", "tsv", "xlsx", "xls")) ByteArrayOutputStream() else null
                    var total = 0L
                    var complete = false
                    provider.openRead(node.id).use { input ->
                        val buffer = ByteArray(8192)
                        while (true) {
                            check()
                            // One extra byte distinguishes an exact bound from a truncated stream.
                            // It is accounted in bytesRead but never retained or hashed as a full file.
                            val n = input.read(buffer, 0, minOf(buffer.size.toLong(), cap - total + 1).toInt())
                            if (n < 0) { complete = true; break }
                            if (n == 0) throw java.io.IOException("Provider stream made no progress")
                            total += n; bytesRead += n
                            if (total > cap) break
                            digest.update(buffer, 0, n); output?.write(buffer, 0, n)
                        }
                    }
                    if (!complete) {
                        coverage = "partial"
                        fileIssues += ScanIssue("bytes_limit", "Content exceeds byte budget; no complete-file hash", uri.toString())
                    } else {
                        hash = digest.digest().joinToString("") { "%02x".format(it) }
                        if (node.size != null && node.size != total)
                            fileIssues += ScanIssue("size_changed", "Provider size differs from bytes read; source may have changed", uri.toString())
                        val data = output?.toByteArray()
                        if (ext in setOf("csv", "tsv", "xlsx", "xls")) {
                            // Shared caps prevent thousands of compressed workbooks expanding into unbounded UI state.
                            if (retainedChars >= 4_000_000 || retainedCells >= 50000 || retainedRows >= 10000) {
                                fileIssues += ScanIssue("result_limit", "Global retained-result budget reached", uri.toString())
                            } else {
                                val perFile = limits.copy(maxCellsPerFile = minOf(limits.maxCellsPerFile, 50000 - retainedCells),
                                    maxRowsPerFile = minOf(limits.maxRowsPerFile, 10000 - retainedRows))
                                val parsed = when (ext) {
                                    "xlsx" -> SourceWorkbookParser.parse(data!!, uri.toString(), perFile, check)
                                    "xls" -> SourceXlsParser.parse(data!!, uri.toString(), perFile, check)
                                    else -> SourceDelimitedParser.parse(data!!, uri.toString(), perFile, ext == "tsv", check)
                                }
                                textFormat = parsed.textFormat
                                val kept = mutableListOf<SourceRow>()
                                for (row in parsed.rows) {
                                    check()
                                    val chars = row.locator.length + row.cells.sumOf { it.raw.length.toLong() + it.value.length + (it.formula?.length ?: 0) + (it.sourceReference?.length ?: 0) + (it.sourceType?.length ?: 0) }
                                    if (retainedChars + chars > 4_000_000) {
                                        fileIssues += ScanIssue("result_limit", "Global retained-text budget reached; remaining rows omitted", uri.toString()); break
                                    }
                                    retainedChars += chars; retainedCells += row.cells.size; retainedRows++; kept += row
                                }
                                rows = kept; fileIssues += parsed.issues
                            }
                            coverage = if (fileIssues.any { it.code != "xlsx_projection" }) "partial" else "complete_within_scope"
                        }
                    }
                }
            } catch (e: java.util.concurrent.CancellationException) { throw e
            } catch (e: Exception) {
                coverage = "unreadable"
                fileIssues += ScanIssue("read_failed", "Read/parse failed: ${e.message?.take(200)}", uri.toString())
            }
            files += ScannedSourceFile(uri.toString(), node.name, node.path, node.mime, node.size, kind, hash, coverage, rows, fileIssues, duration, textFormat, wavHeader)
        }
        val collisions = files.groupBy { it.name }.filterValues { it.size > 1 }
        collisions.forEach { (name, members) ->
            issues += ScanIssue("name_collision", "${members.size} distinct document URIs share the name: $name", members.joinToString(" | ") { it.uri })
        }
        check()
        return SourceScanResult(rootUri = provider.rootUri, scannedAt = scannedAt, files = files,
            scannedDirectories = directories,
            coverage = if (issues.none { it.code != "name_collision" } && files.none { it.coverage in setOf("partial", "unsupported", "unreadable") || it.issues.any { issue -> issue.code != "xlsx_projection" } }) "complete_within_scope" else "partial",
            issues = issues, limits = limits, bytesRead = bytesRead)
    }
}

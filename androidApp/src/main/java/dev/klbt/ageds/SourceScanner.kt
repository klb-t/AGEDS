package dev.klbt.ageds

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import dev.klbt.ageds.core.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.time.Instant
import java.util.ArrayDeque

/** Read-only SAF inventory; URI is identity, path/name only provider observations.
 * No stable snapshot is promised for mutable or remote document providers.
 */
class SourceScanner(private val context: Context) {
    suspend fun scan(treeUri: Uri, limits: SourceScanLimits = SourceScanLimits()): SourceScanResult = withContext(Dispatchers.IO) {
        val coroutine = currentCoroutineContext()
        val check = { coroutine.ensureActive() }
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
        val rootId = DocumentsContract.getTreeDocumentId(treeUri)
        pending.add(Node(rootId, "", "", DocumentsContract.Document.MIME_TYPE_DIR, null, 0))
        while (pending.isNotEmpty()) {
            check()
            val node = pending.removeFirst()
            val uri = DocumentsContract.buildDocumentUriUsingTree(treeUri, node.id)
            if (!visited.add(node.id)) {
                issues += ScanIssue("repeated_document", "Provider repeated a document ID; second path not traversed", uri.toString()); continue
            }
            if (node.mime == DocumentsContract.Document.MIME_TYPE_DIR) {
                if (directories >= limits.maxDirectories || node.depth > limits.maxDepth) {
                    issues += ScanIssue("directory_limit", "Directory/depth budget reached", uri.toString()); continue
                }
                directories++
                try {
                    val children = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, node.id)
                    val projection = arrayOf(DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                        DocumentsContract.Document.COLUMN_DISPLAY_NAME, DocumentsContract.Document.COLUMN_MIME_TYPE,
                        DocumentsContract.Document.COLUMN_SIZE)
                    val cursor = context.contentResolver.query(children, projection, null, null, null)
                        ?: throw IllegalStateException("Provider returned no directory cursor")
                    cursor.use {
                        while (it.moveToNext()) {
                            check()
                            if (discovered >= limits.maxFiles + limits.maxDirectories) {
                                issues += ScanIssue("entry_limit", "Remaining directory entries were not enumerated", uri.toString()); break
                            }
                            discovered++
                            val id = it.getString(0) ?: throw IllegalArgumentException("Missing document ID")
                            val name = it.getString(1) ?: id
                            if (name.length > limits.maxCellChars || id.length > limits.maxCellChars) {
                                issues += ScanIssue("locator_limit", "Oversized document ID/name omitted", uri.toString()); continue
                            }
                            val mime = it.getString(2)
                            val size = if (it.isNull(3)) null else it.getLong(3).takeIf { n -> n >= 0 }
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
            var coverage = "inventory_only"
            try {
                val remaining = (limits.maxTotalBytes - bytesRead).coerceAtLeast(0)
                val cap = minOf(limits.maxFileBytes, remaining)
                if (cap == 0L || (node.size != null && node.size > cap)) {
                    coverage = "partial"
                    fileIssues += ScanIssue("bytes_limit", "Content not read because byte budget would be exceeded", uri.toString())
                } else {
                    val digest = MessageDigest.getInstance("SHA-256")
                    val output = if (ext in setOf("csv", "tsv", "xlsx", "xls", "wav")) ByteArrayOutputStream() else null
                    var total = 0L
                    var complete = false
                    context.contentResolver.openInputStream(uri)?.use { input ->
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
                    } ?: throw IllegalStateException("Provider returned no content stream")
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
                        } else if (ext == "wav") {
                            duration = SourceTextParser.wavDuration(data!!)
                            if (duration == null) fileIssues += ScanIssue("wav_unknown", "Unsupported or incomplete WAV layout; duration unknown", uri.toString())
                        }
                    }
                }
            } catch (e: java.util.concurrent.CancellationException) { throw e
            } catch (e: Exception) {
                coverage = "unreadable"
                fileIssues += ScanIssue("read_failed", "Read/parse failed: ${e.message?.take(200)}", uri.toString())
            }
            files += ScannedSourceFile(uri.toString(), node.name, node.path, node.mime, node.size, kind, hash, coverage, rows, fileIssues, duration, textFormat)
        }
        val collisions = files.groupBy { it.name }.filterValues { it.size > 1 }
        collisions.forEach { (name, members) ->
            issues += ScanIssue("name_collision", "${members.size} distinct document URIs share the name: $name", members.joinToString(" | ") { it.uri })
        }
        check()
        SourceScanResult(rootUri = treeUri.toString(), scannedAt = Instant.now().toString(), files = files,
            scannedDirectories = directories,
            coverage = if (issues.none { it.code != "name_collision" } && files.none { it.coverage in setOf("partial", "unsupported", "unreadable") || it.issues.any { issue -> issue.code != "xlsx_projection" } }) "complete_within_scope" else "partial",
            issues = issues, limits = limits, bytesRead = bytesRead)
    }
}

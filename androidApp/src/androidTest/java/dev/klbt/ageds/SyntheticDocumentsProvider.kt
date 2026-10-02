package dev.klbt.ageds

import android.content.Intent
import android.database.Cursor
import android.database.MatrixCursor
import android.os.Bundle
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import android.provider.DocumentsProvider
import java.io.File
import java.io.FileNotFoundException
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicInteger

/** Synthetic, test-APK-only SAF source. Never reads user-selected directories. */
class SyntheticDocumentsProvider : DocumentsProvider() {
    companion object {
        const val AUTHORITY = "dev.klbt.ageds.test.documents"
        val ROOTS = listOf("normal", "denied", "partial", "repeat", "slow", "stats")
        fun tree(root: String) = DocumentsContract.buildTreeDocumentUri(AUTHORITY, root)
        private val COLUMNS = arrayOf(Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME,
            Document.COLUMN_MIME_TYPE, Document.COLUMN_SIZE, Document.COLUMN_FLAGS)
    }
    private lateinit var sources: File
    private val writes = AtomicInteger()
    private val reads = AtomicInteger()
    private val slowQueries = AtomicInteger()

    override fun onCreate(): Boolean {
        sources = File(context!!.filesDir, "synthetic-saf-fixture").apply { mkdirs() }
        File(sources, "a").writeText("phone,date\n0012,not-a-date\n")
        File(sources, "b").writeText("phone,date\n0099,2020-99-99\n")
        ROOTS.forEach { root ->
            context!!.grantUriPermission("dev.klbt.ageds", tree(root),
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PREFIX_URI_PERMISSION)
        }
        return true
    }
    override fun queryRoots(projection: Array<out String>?): Cursor =
        MatrixCursor(projection ?: arrayOf(DocumentsContract.Root.COLUMN_ROOT_ID))

    private fun row(cursor: MatrixCursor, id: String) {
        val dir = id in ROOTS
        val values = mapOf<String, Any?>(
            Document.COLUMN_DOCUMENT_ID to id,
            Document.COLUMN_DISPLAY_NAME to if (dir) id else "same.csv",
            Document.COLUMN_MIME_TYPE to if (dir) Document.MIME_TYPE_DIR else "text/csv",
            Document.COLUMN_SIZE to if (dir) null else File(sources, id).length(),
            Document.COLUMN_FLAGS to 0)
        cursor.addRow(cursor.columnNames.map { values[it] }.toTypedArray())
    }
    override fun queryDocument(documentId: String, projection: Array<out String>?): Cursor =
        MatrixCursor(projection ?: COLUMNS).also { row(it, documentId) }

    override fun isChildDocument(parentDocumentId: String, documentId: String): Boolean =
        parentDocumentId in ROOTS && documentId in setOf("a", "b")

    override fun queryChildDocuments(parentDocumentId: String, projection: Array<out String>?, sortOrder: String?): Cursor {
        if (parentDocumentId == "denied") throw SecurityException("synthetic permission denied")
        if (parentDocumentId == "slow") {
            slowQueries.incrementAndGet()
            Thread.sleep(500) // Finite slow provider; cancellation must be observed after query returns.
        }
        val cursor = MatrixCursor(projection ?: COLUMNS)
        row(cursor, "a")
        if (parentDocumentId == "partial") {
            // A malformed later row preserves the valid earlier discovery, then marks partial.
            cursor.addRow(cursor.columnNames.map { if (it == Document.COLUMN_DISPLAY_NAME) "broken.csv" else null }.toTypedArray())
        } else {
            row(cursor, "b")
            if (parentDocumentId == "repeat") row(cursor, "a")
        }
        return cursor
    }
    override fun openDocument(documentId: String, mode: String, signal: CancellationSignal?): ParcelFileDescriptor {
        if (mode != "r") {
            writes.incrementAndGet()
            throw SecurityException("synthetic source is read-only")
        }
        if (documentId !in setOf("a", "b")) throw FileNotFoundException(documentId)
        reads.incrementAndGet()
        return ParcelFileDescriptor.open(File(sources, documentId), ParcelFileDescriptor.MODE_READ_ONLY)
    }
    override fun deleteDocument(documentId: String) { writes.incrementAndGet(); throw SecurityException("read-only") }
    override fun renameDocument(documentId: String, displayName: String): String {
        writes.incrementAndGet(); throw SecurityException("read-only")
    }
    override fun createDocument(parentDocumentId: String, mimeType: String, displayName: String): String {
        writes.incrementAndGet(); throw SecurityException("read-only")
    }
    override fun call(method: String, arg: String?, extras: Bundle?): Bundle? {
        if (method != "fixtureSnapshot") return super.call(method, arg, extras)
        return Bundle().apply {
            putInt("writeAttempts", writes.get()); putInt("reads", reads.get()); putInt("slowQueries", slowQueries.get())
            for (id in listOf("a", "b")) putString(id, MessageDigest.getInstance("SHA-256")
                .digest(File(sources, id).readBytes()).joinToString("") { "%02x".format(it) })
        }
    }
}

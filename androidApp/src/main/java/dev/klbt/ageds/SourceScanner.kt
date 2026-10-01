package dev.klbt.ageds

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import dev.klbt.ageds.core.SourceScanLimits
import dev.klbt.ageds.core.SourceScanResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.io.InputStream
import java.time.Instant

/** Read-only SAF binding. SourceScanEngine owns traversal, budgets and projections.
 * URI identifies a document; provider names, paths and sizes remain observations.
 * Mutable/remote providers do not promise a stable snapshot.
 */
class SourceScanner(private val context: Context) {
    suspend fun scan(treeUri: Uri, limits: SourceScanLimits = SourceScanLimits()): SourceScanResult = withContext(Dispatchers.IO) {
        val coroutine = currentCoroutineContext()
        val check = { coroutine.ensureActive() }
        val resolver = context.contentResolver
        val provider = object : SourceScanProvider {
            override val rootId: String = DocumentsContract.getTreeDocumentId(treeUri)
            override val rootUri: String = treeUri.toString()

            private fun documentUri(id: String): Uri = DocumentsContract.buildDocumentUriUsingTree(treeUri, id)
            override fun uri(id: String): String = documentUri(id).toString()

            override fun children(id: String): SourceScanCursor {
                val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, id)
                val projection = arrayOf(DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                    DocumentsContract.Document.COLUMN_DISPLAY_NAME, DocumentsContract.Document.COLUMN_MIME_TYPE,
                    DocumentsContract.Document.COLUMN_SIZE)
                val cursor = resolver.query(childrenUri, projection, null, null, null)
                    ?: throw IllegalStateException("Provider returned no directory cursor")
                // The engine closes this cursor even on an early budget stop or row failure.
                // No child list is materialized at the Android boundary.
                return object : SourceScanCursor {
                    override fun next(): SourceScanDocument? {
                        if (!cursor.moveToNext()) return null
                        val documentId = cursor.getString(0) ?: throw IllegalArgumentException("Missing document ID")
                        val name = cursor.getString(1) ?: documentId
                        val mime = cursor.getString(2)
                        val size = if (cursor.isNull(3)) null else cursor.getLong(3).takeIf { it >= 0 }
                        return SourceScanDocument(documentId, name, mime, size)
                    }
                    override fun close() { cursor.close() }
                }
            }

            override fun openRead(id: String): InputStream = resolver.openInputStream(documentUri(id))
                ?: throw IllegalStateException("Provider returned no content stream")
        }
        SourceScanEngine(provider).scan(limits = limits, scannedAt = Instant.now().toString(), checkCancelled = check)
    }
}

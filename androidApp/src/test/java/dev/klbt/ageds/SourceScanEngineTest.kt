package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.InputStream
import java.security.MessageDigest
import java.util.concurrent.CancellationException

class SourceScanEngineTest {
    private class Provider(val tree: Map<String, List<SourceScanDocument>>, val content: Map<String, ByteArray>) : SourceScanProvider {
        override val rootId = "root"
        override val rootUri = "content://synthetic/root"
        val opened = mutableListOf<String>()
        var closedStreams = 0
        var closedCursors = 0
        var nextCalls = 0
        override fun uri(id: String) = "content://synthetic/$id"
        override fun children(id: String): SourceScanCursor {
            val children = tree[id] ?: emptyList()
            var index = 0
            return object : SourceScanCursor {
                override fun next(): SourceScanDocument? { nextCalls++; return children.getOrNull(index++) }
                override fun close() { closedCursors++ }
            }
        }
        override fun openRead(id: String): InputStream {
            opened += id
            return object : ByteArrayInputStream(content.getValue(id)) {
                override fun close() { closedStreams++; super.close() }
            }
        }
    }
    private fun file(id: String, name: String = "$id.csv", size: Long? = null) = SourceScanDocument(id, name, "text/csv", size)
    private fun directory(id: String) = SourceScanDocument(id, id, SOURCE_DIRECTORY_MIME, null)
    private fun hash(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    @Test fun actualCsvParserAndSuppliedMetadataUseProductionOrchestration() {
        val bytes = "phone,raw\n+001,=formula\n".toByteArray()
        val provider = Provider(mapOf("root" to listOf(file("one", size = bytes.size.toLong()))), mapOf("one" to bytes))
        val result = SourceScanEngine(provider).scan(scannedAt = "literal-time")
        assertEquals("literal-time", result.scannedAt)
        assertEquals(provider.rootUri, result.rootUri)
        assertEquals(1, result.scannedDirectories)
        assertEquals(bytes.size.toLong(), result.bytesRead)
        val source = result.files.single()
        assertEquals(hash(bytes), source.sha256)
        assertEquals("+001", source.rows[1].cells[0].value)
        assertEquals("=formula", source.rows[1].cells[1].value)
        assertEquals("content://synthetic/one", source.uri)
        assertEquals(1, provider.closedStreams)
        assertEquals(1, provider.closedCursors)
    }

    @Test fun breadthFirstIdsCyclesAndDuplicateNamesRemainDistinct() {
        val bytes = "a,b".toByteArray()
        val provider = Provider(mapOf("root" to listOf(directory("folder"), file("first", "same.csv")),
            "folder" to listOf(directory("root"), file("second", "same.csv"))), mapOf("first" to bytes, "second" to bytes))
        val result = SourceScanEngine(provider).scan(scannedAt = "now")
        assertEquals(listOf("first", "second"), provider.opened)
        assertEquals(listOf("same.csv", "folder/same.csv"), result.files.map { it.relativePath })
        assertTrue(result.issues.any { it.code == "repeated_document" })
        assertTrue(result.issues.any { it.code == "name_collision" })
        assertEquals(2, result.scannedDirectories)
    }

    @Test fun entryBoundaryDoesNotMaterializeAnExtraProviderDocument() {
        val provider = Provider(mapOf("root" to listOf(file("one"), file("two"), file("never"))),
            mapOf("one" to byteArrayOf(), "two" to byteArrayOf()))
        val result = SourceScanEngine(provider).scan(SourceScanLimits(maxFiles = 1, maxDirectories = 1), "now")
        assertEquals(2, provider.nextCalls)
        assertTrue(result.issues.any { it.code == "entry_limit" })
        assertTrue(result.issues.any { it.code == "file_limit" })
        assertEquals(listOf("one"), provider.opened)
        assertEquals(1, provider.closedCursors)
    }

    @Test fun knownOversizedNonWavIsInventoryWithoutOpening() {
        val provider = Provider(mapOf("root" to listOf(file("one", size = 100))), emptyMap())
        val result = SourceScanEngine(provider).scan(SourceScanLimits(maxFileBytes = 10), "now")
        assertTrue(provider.opened.isEmpty())
        assertNull(result.files.single().sha256)
        assertEquals(0L, result.bytesRead)
        assertTrue(result.files.single().issues.any { it.code == "bytes_limit" })
    }

    @Test fun nonWavLookaheadByteIsCountedWithoutClaimingCompleteHash() {
        val provider = Provider(mapOf("root" to listOf(file("one"))), mapOf("one" to ByteArray(100)))
        val result = SourceScanEngine(provider).scan(SourceScanLimits(maxFileBytes = 10, maxTotalBytes = 100), "now")
        assertEquals(11L, result.bytesRead)
        assertNull(result.files.single().sha256)
        assertEquals("partial", result.files.single().coverage)
        assertEquals(1, provider.closedStreams)
    }

    @Test fun cancellationStopsWithoutPublishingCompletedResultAndClosesCursor() {
        val provider = Provider(mapOf("root" to listOf(file("one"))), mapOf("one" to byteArrayOf()))
        var checks = 0
        try {
            SourceScanEngine(provider).scan(scannedAt = "now", checkCancelled = {
                checks++
                if (checks == 3) throw CancellationException("stop")
            })
            fail("Cancellation must escape")
        } catch (_: CancellationException) { }
        assertEquals(1, provider.closedCursors)
        assertTrue(provider.opened.isEmpty())
    }
}

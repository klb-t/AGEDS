package dev.klbt.ageds

import dev.klbt.ageds.core.*
import java.io.ByteArrayInputStream
import java.io.InputStream
import org.junit.Assert.*
import org.junit.Test

/** N57 counts real production traversal and parses real synthetic CSV inputs. */
class SourceScanEnginePolicyAdversarialTest {
    private class Provider : SourceScanProvider {
        override val rootId = "root"
        override val rootUri = "content://synthetic/tree/root"
        val lists = mutableMapOf<String, List<SourceScanDocument>>()
        val content = mutableMapOf<String, ByteArray>()
        val queried = mutableListOf<String>()
        val opened = mutableListOf<String>()
        val nextCalls = mutableMapOf<String, Int>()
        var generatedCount = 0
        var generator: ((Int) -> SourceScanDocument)? = null
        var returnedBytes = 0L
        override fun uri(id: String) = "content://synthetic/document/$id"
        override fun children(id: String): SourceScanCursor {
            queried += id
            val rows = lists[id].orEmpty()
            var offset = 0
            return object : SourceScanCursor {
                override fun next(): SourceScanDocument? {
                    nextCalls[id] = nextCalls.getOrDefault(id, 0) + 1
                    if (id == rootId && generator != null) {
                        if (offset >= generatedCount) return null
                        return generator!!(offset++)
                    }
                    return rows.getOrNull(offset++)
                }
                override fun close() = Unit
            }
        }
        override fun openRead(id: String): InputStream {
            opened += id
            return object : ByteArrayInputStream(content[id] ?: byteArrayOf()) {
                override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
                    val count = super.read(buffer, offset, length)
                    if (count > 0) returnedBytes += count
                    return count
                }
                override fun read(): Int {
                    val value = super.read()
                    if (value >= 0) returnedBytes++
                    return value
                }
            }
        }
    }
    private fun file(id: String, name: String = "$id.bin", size: Long? = null) =
        SourceScanDocument(id, name, "application/octet-stream", size)
    private fun directory(id: String, name: String = id) =
        SourceScanDocument(id, name, SOURCE_DIRECTORY_MIME, null)
    private fun scan(provider: Provider, limits: SourceScanLimits = SourceScanLimits()) =
        SourceScanEngine(provider).scan(limits, scannedAt = "synthetic-fixed-time")

    @Test fun cyclesAndRepeatedFileIdsDoNotTraverseOrReadTwice() {
        val p = Provider()
        p.lists["root"] = listOf(directory("sub"), file("a"))
        p.lists["sub"] = listOf(directory("root", "cycle"), file("a", "second-name"), file("b"))
        val result = scan(p)
        assertEquals(listOf("root", "sub"), p.queried)
        assertEquals(listOf("a", "b"), p.opened)
        assertEquals(listOf("content://synthetic/document/a", "content://synthetic/document/b"), result.files.map { it.uri })
        assertEquals(2, result.issues.count { it.code == "repeated_document" })
        assertEquals("partial", result.coverage)
    }

    @Test fun collidingNamesRetainBothUriIdentitiesAndRawNames() {
        val p = Provider()
        p.lists["root"] = listOf(file("left", "same +48??.csv"), file("right", "same +48??.csv"))
        p.content["left"] = "raw,value\n001,first".encodeToByteArray()
        p.content["right"] = "raw,value\n002,second".encodeToByteArray()
        val result = scan(p)
        assertEquals(2, result.files.size)
        assertEquals(2, result.files.map { it.uri }.toSet().size)
        assertEquals(listOf("same +48??.csv", "same +48??.csv"), result.files.map { it.name })
        assertEquals(listOf("001", "002"), result.files.map { it.rows[1].cells[0].value })
        val collision = result.issues.single { it.code == "name_collision" }
        assertTrue(collision.locator!!.contains(p.uri("left")))
        assertTrue(collision.locator!!.contains(p.uri("right")))
    }

    @Test fun zeroDepthAndOneDirectoryBudgetDoNotEnumerateNestedProviders() {
        for (limits in listOf(SourceScanLimits(maxDepth = 0), SourceScanLimits(maxDirectories = 1))) {
            val p = Provider()
            p.lists["root"] = listOf(directory("nested"), file("visible"))
            p.lists["nested"] = listOf(file("hidden"))
            val result = scan(p, limits)
            assertEquals(listOf("root"), p.queried)
            assertEquals(listOf("visible"), p.opened)
            assertEquals(1, result.scannedDirectories)
            assertTrue(result.issues.any { it.code == "directory_limit" })
            assertEquals("partial", result.coverage)
        }
    }

    @Test fun hugeLazyDirectoryDoesNotMaterializeAnyRowAfterEntryBound() {
        val p = Provider()
        p.generatedCount = 1_000_000
        p.generator = { file("file-$it") }
        val result = scan(p, SourceScanLimits(maxFiles = 2, maxDirectories = 1))
        assertEquals(3, p.nextCalls["root"])
        assertEquals(2, p.opened.size)
        assertEquals(2, result.files.size)
        assertTrue(result.issues.any { it.code == "entry_limit" })
        assertTrue(result.issues.any { it.code == "file_limit" })
        assertEquals("partial", result.coverage)
    }

    @Test fun oversizedLocatorIsOmittedBeforeContentRead() {
        val p = Provider()
        p.lists["root"] = listOf(file("too-long-id", "ok"), file("b", "123456789"), file("c", "good"))
        val result = scan(p, SourceScanLimits(maxCellChars = 8))
        assertEquals(listOf("c"), p.opened)
        assertEquals(1, result.files.size)
        assertEquals(2, result.issues.count { it.code == "locator_limit" })
        assertEquals("partial", result.coverage)
    }

    @Test fun cumulativeLocatorBudgetStopsLazyEnumerationExplicitly() {
        val p = Provider()
        val largeName = "n".repeat(40_000)
        p.generatedCount = 1000
        p.generator = { file("$it", largeName) }
        val result = scan(p, SourceScanLimits(maxFiles = 40, maxDirectories = 1, maxCellChars = 40000))
        assertEquals(13, p.nextCalls["root"])
        assertEquals(12, result.files.size)
        assertTrue(result.issues.any { it.code == "locator_budget" })
        assertEquals("partial", result.coverage)
    }

    @Test fun knownOversizeSkipsContentButRetainsInventory() {
        val p = Provider()
        p.lists["root"] = listOf(file("big", size = 11))
        p.content["big"] = ByteArray(11)
        val result = scan(p, SourceScanLimits(maxFileBytes = 10))
        assertTrue(p.opened.isEmpty())
        assertEquals(1, result.files.size)
        assertNull(result.files.single().sha256)
        assertEquals("partial", result.files.single().coverage)
        assertTrue(result.files.single().issues.any { it.code == "bytes_limit" })
    }

    @Test fun nonWavUnknownLengthPreservesDocumentedSentinelPlusOnePolicy() {
        val p = Provider()
        p.lists["root"] = listOf(file("first"), file("second"))
        p.content["first"] = ByteArray(11)
        p.content["second"] = ByteArray(20)
        val result = scan(p, SourceScanLimits(maxFileBytes = 10, maxTotalBytes = 10))
        assertEquals(11L, result.bytesRead)
        assertEquals(11L, p.returnedBytes)
        assertEquals(listOf("first"), p.opened)
        assertTrue(result.files.all { it.sha256 == null && it.coverage == "partial" })
        assertEquals("partial", result.coverage)
    }

    @Test fun wavReadUsesStrictCapWithoutNonWavSentinel() {
        val p = Provider()
        p.lists["root"] = listOf(file("wav", "clip.wav"))
        val header = java.nio.ByteBuffer.allocate(44).order(java.nio.ByteOrder.LITTLE_ENDIAN)
        header.put("RIFF".encodeToByteArray()).putInt(36).put("WAVEfmt ".encodeToByteArray())
        header.putInt(16).putShort(1).putShort(1).putInt(8000).putInt(16000)
        header.putShort(2).putShort(16).put("data".encodeToByteArray()).putInt(0)
        p.content["wav"] = header.array()
        val result = scan(p, SourceScanLimits(maxFileBytes = 10, maxTotalBytes = 10))
        assertEquals(10L, result.bytesRead)
        assertEquals(10L, p.returnedBytes)
        assertEquals("partial", result.coverage)
        assertNull(result.files.single().sha256)
        assertNotNull(result.files.single().wavHeader)
    }

    @Test fun exactEntryCapIsConservativelyPartialWithoutEofProbe() {
        val p = Provider()
        p.lists["root"] = listOf(file("a"), file("b"), file("c"))
        val result = scan(p, SourceScanLimits(maxFiles = 2, maxDirectories = 1))
        assertEquals(3, p.nextCalls["root"])
        assertTrue(result.issues.any { it.code == "entry_limit" })
        assertEquals("partial", result.coverage)
    }

    @Test fun realCsvRowsShareTheTenThousandRowBudgetAcrossFiles() {
        val p = Provider()
        p.lists["root"] = (0..5).map { file("$it", "$it.csv") }
        p.content.putAll((0..5).associate { "$it" to "v\n".repeat(2000).encodeToByteArray() })
        val result = scan(p)
        assertEquals(10_000, result.files.sumOf { it.rows.size })
        assertTrue(result.files.last().rows.isEmpty())
        assertTrue(result.files.last().issues.any { it.code == "result_limit" })
        assertEquals("partial", result.coverage)
    }

    @Test fun realCsvCellsShareTheFiftyThousandCellBudgetAcrossFiles() {
        val p = Provider()
        p.lists["root"] = (0..5).map { file("$it", "$it.csv") }
        val csv = ((1..100).joinToString(",") { "x" } + "\n").repeat(100).encodeToByteArray()
        p.content.putAll((0..5).associate { "$it" to csv })
        val result = scan(p)
        assertEquals(50_000, result.files.sumOf { f -> f.rows.sumOf { it.cells.size } })
        assertTrue(result.files.last().rows.isEmpty())
        assertTrue(result.files.last().issues.any { it.code == "result_limit" })
        assertEquals("partial", result.coverage)
    }

    @Test fun realCsvTextIsTruncatedAtGlobalRetainedCharacterBudget() {
        val p = Provider()
        p.lists["root"] = listOf(file("huge", "huge.csv"))
        p.content["huge"] = ("x".repeat(2000) + "\n").repeat(1100).encodeToByteArray()
        val result = scan(p)
        val rows = result.files.single().rows
        val chars = rows.sumOf { row -> row.locator.length.toLong() + row.cells.sumOf { it.raw.length.toLong() + it.value.length + (it.formula?.length ?: 0) + (it.sourceReference?.length ?: 0) + (it.sourceType?.length ?: 0) } }
        assertTrue(chars <= 4_000_000)
        assertTrue(rows.size in 1..1099)
        assertTrue(result.files.single().issues.any { it.code == "result_limit" })
        assertEquals("partial", result.coverage)
    }

    @Test fun entryBoundaryDoesNotCallThrowingNextMetadataRow() {
        val p = Provider()
        p.generatedCount = 4
        p.generator = { index ->
            check(index < 3) { "metadata row beyond budget must not be requested" }
            file("$index")
        }
        val result = scan(p, SourceScanLimits(maxFiles = 2, maxDirectories = 1))
        assertEquals(3, p.nextCalls["root"])
        assertTrue(result.issues.any { it.code == "entry_limit" })
        assertFalse(result.issues.any { it.code == "directory_unreadable" })
        assertEquals("partial", result.coverage)
    }
}

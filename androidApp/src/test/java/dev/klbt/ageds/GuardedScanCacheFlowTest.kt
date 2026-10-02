package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanResult
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.CancellationException

/** Deterministic typed engine→guarded cache flows; concurrent lock races are tested separately. */
class GuardedScanCacheFlowTest {
    @get:Rule val temporary = TemporaryFolder()
    private val json = Json { ignoreUnknownKeys = true }
    private val audioMarker = "GUARDED_RAW_AUDIO_SENTINEL"

    private fun audio(): ByteArray {
        val bytes = ByteArray(44 + 64)
        fun ascii(at: Int, value: String) { value.toByteArray(Charsets.US_ASCII).copyInto(bytes, at) }
        fun little(at: Int, value: Int, count: Int) { repeat(count) { bytes[at + it] = (value ushr (it * 8)).toByte() } }
        ascii(0, "RIFF"); little(4, bytes.size - 8, 4); ascii(8, "WAVE")
        ascii(12, "fmt "); little(16, 16, 4); little(20, 1, 2); little(22, 1, 2)
        little(24, 8000, 4); little(28, 16000, 4); little(32, 2, 2); little(34, 16, 2)
        ascii(36, "data"); little(40, 64, 4)
        val body = audioMarker.toByteArray(Charsets.US_ASCII)
        for (at in 44 until bytes.size) bytes[at] = body[(at - 44) % body.size]
        return bytes
    }

    private inner class SourceFixture(csvValue: String = " raw  Żółć ") : SourceScanProvider {
        val directory = temporary.newFolder()
        private val original = linkedMapOf("csv" to "key;value\n1;\"$csvValue\"\n".toByteArray(Charsets.UTF_8), "wav" to audio())
        private val files = original.mapValues { (id, bytes) -> File(directory, if (id == "csv") "source.csv" else "source.wav").also { it.writeBytes(bytes) } }
        var opens = 0
        var closes = 0
        override val rootId = "root"
        override val rootUri = "content://guarded-scan/${directory.name}"
        override fun uri(id: String) = "$rootUri/$id"
        override fun children(id: String): SourceScanCursor {
            require(id == rootId)
            val entries = files.entries.iterator()
            return object : SourceScanCursor {
                override fun next(): SourceScanDocument? {
                    if (!entries.hasNext()) return null
                    val (key, file) = entries.next()
                    return SourceScanDocument(key, file.name, if (key == "csv") "text/csv" else "audio/wav", file.length())
                }
                override fun close() = Unit
            }
        }
        override fun openRead(id: String): InputStream {
            opens++
            return object : FilterInputStream(files.getValue(id).inputStream()) {
                override fun close() { super.close(); closes++ }
            }
        }
        fun scan(at: String) = SourceScanEngine(this).scan(scannedAt = at)
        fun assertUnchanged() {
            for ((id, bytes) in original) assertArrayEquals(bytes, files.getValue(id).readBytes())
            assertEquals(setOf("source.csv", "source.wav"), directory.list()!!.toSet())
            assertEquals(opens, closes)
        }
    }

    private fun path(): File = File(temporary.newFolder("metadata"), "scan.json")
    private fun raw(result: SourceScanResult) = json.encodeToString(result)
    private fun assertSnapshot(cache: BoundedMetadataCache, path: File, expected: SourceScanResult) {
        val encoded = requireNotNull(cache.read())
        assertEquals(expected, json.decodeFromString<SourceScanResult>(encoded))
        assertEquals(1, expected.schemaVersion)
        assertFalse(encoded.contains(audioMarker))
        assertEquals(listOf("scan.json"), path.parentFile!!.list()!!.toList())
    }

    @Test fun tokenSupersededAfterStagingCannotReplaceSnapshotButFreshGenerationCanPublish() {
        val source = SourceFixture()
        val file = path()
        val cache = BoundedMetadataCache(file, 4 * 1024 * 1024)
        val gate = SourceScanPublicationGate()
        val previous = source.scan("previous-generation")
        assertTrue(cache.writeGuarded(raw(previous), gate, gate.begin()))
        val before = file.readBytes()
        val staleResult = source.scan("stale-generation")
        val stale = gate.begin()
        var calls = 0
        var next: SourceScanPublicationGate.Token? = null
        val published = cache.writeGuarded(raw(staleResult), gate, stale) {
            if (++calls == 2) {
                val staged = file.parentFile!!.listFiles()!!.filter { it.name != file.name }
                assertEquals(1, staged.size)
                assertTrue(staged.single().length() > 0)
                assertArrayEquals(before, file.readBytes())
                next = gate.begin()
            }
        }
        assertEquals(2, calls)
        assertFalse(published)
        assertFalse(gate.isCurrent(stale))
        assertArrayEquals(before, file.readBytes())
        assertSnapshot(cache, file, previous)
        val fresh = source.scan("fresh-generation")
        assertTrue(cache.writeGuarded(raw(fresh), gate, requireNotNull(next)))
        assertSnapshot(cache, file, fresh)
        assertFalse(file.canonicalPath.startsWith(source.directory.canonicalPath + File.separator))
        source.assertUnchanged()
    }

    @Test fun oversizedActualScanPreservesPriorSnapshotAndFreshSmallResultRemainsUsable() {
        val small = SourceFixture()
        val large = SourceFixture("ą".repeat(3000))
        val previous = small.scan("old")
        val oversized = large.scan("oversized")
        val cap = raw(previous).toByteArray(Charsets.UTF_8).size
        assertTrue(raw(oversized).toByteArray(Charsets.UTF_8).size > cap)
        val file = path()
        val cache = BoundedMetadataCache(file, cap)
        val gate = SourceScanPublicationGate()
        assertTrue(cache.writeGuarded(raw(previous), gate, gate.begin()))
        val before = file.readBytes()
        val rejected = gate.begin()
        var published = false
        val failure = runCatching { published = cache.writeGuarded(raw(oversized), gate, rejected) }.exceptionOrNull()
        assertTrue(failure is IllegalArgumentException)
        assertFalse(published)
        assertTrue(gate.isCurrent(rejected))
        assertArrayEquals(before, file.readBytes())
        assertSnapshot(cache, file, previous)
        val fresh = small.scan("new")
        assertTrue(cache.writeGuarded(raw(fresh), gate, gate.begin()))
        assertSnapshot(cache, file, fresh)
        small.assertUnchanged(); large.assertUnchanged()
    }

    @Test fun canceledStagedTypedWriteKeepsPriorAndDoesNotPoisonNextGeneration() {
        val source = SourceFixture()
        val file = path()
        val cache = BoundedMetadataCache(file, 4 * 1024 * 1024)
        val gate = SourceScanPublicationGate()
        val previous = source.scan("old")
        assertTrue(cache.writeGuarded(raw(previous), gate, gate.begin()))
        val before = file.readBytes()
        val candidate = source.scan("canceled")
        val token = gate.begin()
        var checks = 0
        var published = false
        val failure = runCatching {
            published = cache.writeGuarded(raw(candidate), gate, token) {
                if (++checks == 2) {
                    assertTrue(file.parentFile!!.list()!!.size > 1)
                    throw CancellationException("superseded work")
                }
            }
        }.exceptionOrNull()
        assertTrue(failure is CancellationException)
        assertFalse(published)
        assertArrayEquals(before, file.readBytes())
        assertSnapshot(cache, file, previous)
        val fresh = source.scan("after-cancellation")
        assertTrue(cache.writeGuarded(raw(fresh), gate, gate.begin()))
        assertSnapshot(cache, file, fresh)
        source.assertUnchanged()
    }

    @Test fun atomicReplacementFailurePreservesTypedSnapshotAndFreshGenerationRecovers() {
        val source = SourceFixture()
        val file = path()
        val cache = BoundedMetadataCache(file, 4 * 1024 * 1024)
        val gate = SourceScanPublicationGate()
        val previous = source.scan("old")
        assertTrue(cache.writeGuarded(raw(previous), gate, gate.begin()))
        val before = file.readBytes()
        var attempted = false
        val failing = BoundedMetadataCache(file, 4 * 1024 * 1024, atomicReplace = { staged, destination ->
            attempted = true
            assertEquals(file.toPath(), destination)
            assertTrue(staged.toFile().length() > 0)
            assertArrayEquals(before, file.readBytes())
            throw IOException("synthetic atomic replacement failure")
        })
        val candidate = source.scan("failed-publication")
        var published = false
        val failure = runCatching { published = failing.writeGuarded(raw(candidate), gate, gate.begin()) }.exceptionOrNull()
        assertTrue(attempted)
        assertTrue(failure is IOException)
        assertFalse(published)
        assertArrayEquals(before, file.readBytes())
        assertSnapshot(cache, file, previous)
        val fresh = source.scan("after-io-failure")
        assertTrue(cache.writeGuarded(raw(fresh), gate, gate.begin()))
        assertSnapshot(cache, file, fresh)
        source.assertUnchanged()
    }
}

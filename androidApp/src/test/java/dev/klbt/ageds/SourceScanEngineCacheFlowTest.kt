package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import dev.klbt.ageds.core.SourceScanResult
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.FilterInputStream
import java.io.InputStream
import java.security.MessageDigest
import java.util.Base64
import java.util.concurrent.CancellationException

/** Real engine and cache, deterministic read-only provider; no Android Context or SAF mock. */
class SourceScanEngineCacheFlowTest {
    @get:Rule val temporary = TemporaryFolder()
    private val json = Json { ignoreUnknownKeys = true }
    private val audioMarker = "RAW_AUDIO_BODY_NOT_CACHE_METADATA"

    private fun wav(): ByteArray {
        val bytes = ByteArray(44 + 2560)
        fun ascii(at: Int, value: String) { value.toByteArray(Charsets.US_ASCII).copyInto(bytes, at) }
        fun little(at: Int, value: Int, size: Int) { repeat(size) { bytes[at + it] = (value ushr (8 * it)).toByte() } }
        ascii(0, "RIFF"); little(4, bytes.size - 8, 4); ascii(8, "WAVE")
        ascii(12, "fmt "); little(16, 16, 4); little(20, 1, 2); little(22, 1, 2)
        little(24, 8000, 4); little(28, 16000, 4); little(32, 2, 2); little(34, 16, 2)
        ascii(36, "data"); little(40, 2560, 4)
        val marker = audioMarker.toByteArray(Charsets.UTF_8)
        for (index in 44 until bytes.size) bytes[index] = marker[(index - 44) % marker.size]
        return bytes
    }

    private fun digest(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }

    private inner class Fixture(onlyWav: Boolean = false, invalidCsv: Boolean = false) : SourceScanProvider {
        val sourceDirectory = temporary.newFolder("sources")
        val originals = linkedMapOf<String, File>()
        val originalBytes = linkedMapOf<String, ByteArray>()
        val entries = linkedMapOf<String, SourceScanDocument>()
        val listing = linkedMapOf<String, List<String>>()
        val opened = mutableListOf<String>()
        val closed = mutableListOf<String>()
        var cursorCloses = 0
        override val rootId = "root"
        override val rootUri = "content://cache-flow/tree/synthetic"
        override fun uri(id: String) = "content://cache-flow/document/$id"
        init {
            fun source(id: String, relative: String, name: String, mime: String, bytes: ByteArray) {
                val file = File(sourceDirectory, relative)
                file.parentFile!!.mkdirs(); file.writeBytes(bytes)
                originals[id] = file; originalBytes[id] = bytes.copyOf()
                entries[id] = SourceScanDocument(id, name, mime, bytes.size.toLong())
            }
            source("audio", "recording.wav", "recording.wav", "audio/wav", wav())
            if (onlyWav) {
                listing[rootId] = listOf("audio")
            } else {
                entries["left"] = SourceScanDocument("left", "left", SOURCE_DIRECTORY_MIME, null)
                entries["right"] = SourceScanDocument("right", "right", SOURCE_DIRECTORY_MIME, null)
                source("left-csv", "left/same.csv", "same.csv", "text/csv", "date;value\n\"2026-10-01\";\" raw  Żółć \"\n".toByteArray(Charsets.UTF_8))
                source("right-csv", "right/same.csv", "same.csv", "text/csv", "date;value\n\"2026-10-02\";\"other source\"\n".toByteArray(Charsets.UTF_8))
                source("opaque", "opaque.bin", "opaque.bin", "application/octet-stream", "RAW_INVENTORY_BYTES_NOT_CACHE".repeat(20).toByteArray(Charsets.UTF_8))
                listing[rootId] = listOf("left", "right", "audio", "opaque")
                listing["left"] = listOf("left-csv")
                listing["right"] = listOf("right-csv")
                if (invalidCsv) {
                    source("broken", "broken.csv", "broken.csv", "text/csv", byteArrayOf(0xc3.toByte(), 0x28))
                    listing[rootId] = listing.getValue(rootId) + "broken"
                }
            }
        }
        override fun children(id: String): SourceScanCursor {
            val children = listing.getValue(id).iterator()
            return object : SourceScanCursor {
                override fun next(): SourceScanDocument? = if (children.hasNext()) entries.getValue(children.next()) else null
                override fun close() { cursorCloses++ }
            }
        }
        override fun openRead(id: String): InputStream {
            opened += id
            return object : FilterInputStream(originals.getValue(id).inputStream()) {
                override fun close() { super.close(); closed += id }
            }
        }
        fun assertUnchanged() {
            for ((id, bytes) in originalBytes) assertArrayEquals("Source changed: $id", bytes, originals.getValue(id).readBytes())
            assertEquals(originals.values.map { it.relativeTo(sourceDirectory).path }.sorted(),
                sourceDirectory.walkTopDown().filter { it.isFile }.map { it.relativeTo(sourceDirectory).path }.toList().sorted())
            assertEquals(opened.sorted(), closed.sorted())
        }
        fun scan(at: String, limits: SourceScanLimits = SourceScanLimits()): SourceScanResult =
            SourceScanEngine(this).scan(limits = limits, scannedAt = at)
    }

    private fun cache(cap: Int = 4 * 1024 * 1024): Pair<BoundedMetadataCache, File> {
        val directory = temporary.newFolder("metadata")
        val path = File(directory, "source-scan.json")
        return BoundedMetadataCache(path, cap) to path
    }
    private fun decoded(cache: BoundedMetadataCache) = json.decodeFromString<SourceScanResult>(requireNotNull(cache.read()))

    @Test fun mixedRealScanPublishesAndRestoresRawCellsDistinctUrisAndHeaderMetadata() {
        val fixture = Fixture()
        val scanned = fixture.scan("2026-10-01T03:00:00Z")
        assertEquals(4, scanned.files.size)
        val (cache, path) = cache()
        cache.write(json.encodeToString(scanned))
        val restored = decoded(cache)
        assertEquals(scanned, restored)
        assertEquals(1, restored.schemaVersion)
        assertEquals(fixture.rootUri, restored.rootUri)
        assertEquals("2026-10-01T03:00:00Z", restored.scannedAt)
        assertEquals(SourceScanLimits(), restored.limits)
        assertEquals(fixture.originalBytes.values.sumOf { it.size.toLong() }, restored.bytesRead)
        val collisions = restored.files.filter { it.name == "same.csv" }
        assertEquals(setOf(fixture.uri("left-csv"), fixture.uri("right-csv")), collisions.map { it.uri }.toSet())
        assertEquals(setOf("left/same.csv", "right/same.csv"), collisions.map { it.relativePath }.toSet())
        val raw = collisions.single { it.uri == fixture.uri("left-csv") }.rows.flatMap { it.cells }.single { it.value == " raw  Żółć " }
        assertEquals("\" raw  Żółć \"", raw.raw)
        val conflict = restored.issues.single { it.code == "name_collision" }
        assertTrue(conflict.locator!!.contains(fixture.uri("left-csv")))
        assertTrue(conflict.locator!!.contains(fixture.uri("right-csv")))
        val audio = restored.files.single { it.uri == fixture.uri("audio") }
        assertEquals(0.16, audio.audioDurationSec!!, 0.0)
        assertEquals("observed", audio.wavHeader!!.status)
        assertFalse(audio.wavHeader!!.bodyValidated)
        for (file in restored.files) {
            val id = fixture.originals.keys.single { fixture.uri(it) == file.uri }
            assertEquals(digest(fixture.originalBytes.getValue(id)), file.sha256)
        }
        val stored = requireNotNull(cache.read())
        assertFalse(stored.contains(audioMarker))
        assertFalse(stored.contains(Base64.getEncoder().encodeToString(fixture.originalBytes.getValue("audio"))))
        assertFalse(stored.contains("RAW_INVENTORY_BYTES_NOT_CACHE"))
        assertFalse(path.canonicalPath.startsWith(fixture.sourceDirectory.canonicalPath + File.separator))
        assertEquals(listOf("source-scan.json"), path.parentFile!!.list()!!.toList())
        fixture.assertUnchanged()
        assertEquals(3, fixture.cursorCloses)
    }

    @Test fun actualLargerScanCannotReplacePreviousSnapshotWhenConfiguredCacheCapIsExceeded() {
        val fixture = Fixture()
        val previous = fixture.scan("previous-real-scan", SourceScanLimits(maxFiles = 1))
        val replacement = fixture.scan("replacement-real-scan")
        assertEquals(1, previous.files.size)
        assertEquals(4, replacement.files.size)
        val priorWire = json.encodeToString(previous)
        val nextWire = json.encodeToString(replacement)
        val cap = priorWire.toByteArray(Charsets.UTF_8).size
        assertTrue(nextWire.toByteArray(Charsets.UTF_8).size > cap)
        val (cache, path) = cache(cap)
        cache.write(priorWire)
        val before = path.readBytes()
        var replacementPublished = false
        val failure = runCatching { cache.write(nextWire); replacementPublished = true }.exceptionOrNull()
        assertTrue(failure is IllegalArgumentException)
        assertFalse(replacementPublished)
        assertArrayEquals(before, path.readBytes())
        assertEquals(previous, decoded(cache))
        assertEquals("previous-real-scan", decoded(cache).scannedAt)
        assertEquals(listOf("source-scan.json"), path.parentFile!!.list()!!.toList())
        fixture.assertUnchanged()
    }

    @Test fun canceledPublicationOfCompletedSecondScanKeepsFirstActualSnapshot() {
        val fixture = Fixture()
        val previous = fixture.scan("first-completed-scan")
        val next = fixture.scan("second-completed-scan")
        val (cache, path) = cache()
        cache.write(json.encodeToString(previous))
        var checkpoints = 0
        var published = false
        val failure = runCatching {
            cache.write(json.encodeToString(next)) { if (++checkpoints == 2) throw CancellationException("scan superseded") }
            published = true
        }.exceptionOrNull()
        assertTrue(failure is CancellationException)
        assertFalse(published)
        assertEquals(previous, decoded(cache))
        assertEquals(listOf("source-scan.json"), path.parentFile!!.list()!!.toList())
        fixture.assertUnchanged()
    }

    @Test fun actualMalformedSourceFailureAndOriginalDigestRemainExplicitAfterCacheReadback() {
        val fixture = Fixture(invalidCsv = true)
        val scanned = fixture.scan("scan-with-malformed-source")
        val (cache, _) = cache()
        cache.write(json.encodeToString(scanned))
        val restored = decoded(cache)
        assertEquals(scanned, restored)
        val broken = restored.files.single { it.uri == fixture.uri("broken") }
        assertEquals("partial", broken.coverage)
        assertTrue(broken.issues.any { it.code == "unsupported_text_encoding" })
        assertTrue(broken.rows.isEmpty())
        assertEquals(digest(fixture.originalBytes.getValue("broken")), broken.sha256)
        assertEquals("partial", restored.coverage)
        fixture.assertUnchanged()
    }

    @Test fun headerOnlyActualScanDoesNotGainCompleteHashOrBodyValidationThroughCache() {
        val fixture = Fixture(onlyWav = true)
        val limits = SourceScanLimits(maxFileBytes = 64, maxTotalBytes = 64)
        val scanned = fixture.scan("bounded-header-only-scan", limits)
        val (cache, _) = cache()
        cache.write(json.encodeToString(scanned))
        val restored = decoded(cache)
        assertEquals(scanned, restored)
        assertEquals(limits, restored.limits)
        assertEquals(64L, restored.bytesRead)
        val audio = restored.files.single()
        assertNull(audio.sha256)
        assertEquals("partial", audio.coverage)
        assertEquals(0.16, audio.audioDurationSec!!, 0.0)
        assertFalse(audio.wavHeader!!.bodyValidated)
        assertFalse(audio.wavHeader!!.endOfInput)
        assertEquals(64, audio.wavHeader!!.bytesInspected)
        assertTrue(audio.issues.any { it.code == "bytes_limit" })
        assertFalse(requireNotNull(cache.read()).contains(audioMarker))
        fixture.assertUnchanged()
    }
}

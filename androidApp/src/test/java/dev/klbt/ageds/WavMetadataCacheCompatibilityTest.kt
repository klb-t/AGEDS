package dev.klbt.ageds

import dev.klbt.ageds.core.ScanIssue
import dev.klbt.ageds.core.ScannedSourceFile
import dev.klbt.ageds.core.SourceScanResult
import dev.klbt.ageds.core.WavHeaderObservation
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.nio.charset.CharacterCodingException
import java.util.concurrent.CancellationException

/** Actual cache storage plus SourceScanCache's documented codec, no Android Context mock. */
class WavMetadataCacheCompatibilityTest {
    @get:Rule val temporary = TemporaryFolder()
    private val json = Json { ignoreUnknownKeys = true }
    private fun result(uri: String, message: String = "Header only: źródło 😀") = SourceScanResult(
        rootUri = "content://grant/tree/source", scannedAt = "2026-10-01T01:00:00Z",
        files = listOf(ScannedSourceFile(uri, "literal.wav", "literal.wav", kind = "wav", sizeBytes = 960044,
            coverage = "header_prefix_only", audioDurationSec = 30.0,
            wavHeader = WavHeaderObservation(status = "observed", bytesInspected = 44,
                providerSizeBytes = 960044, riffDeclaredBytes = 960044, formatCode = 1,
                channels = 1, sampleRateHz = 16000, byteRate = 32000, blockAlignBytes = 2,
                bitsPerSample = 16, dataDeclaredBytes = 960000, declaredDurationSec = 30.0,
                durationBasis = "declared_data_bytes_divided_by_header_byte_rate",
                issues = listOf(ScanIssue("body_not_read", message))))),
        scannedDirectories = 1, coverage = "partial", bytesRead = 44)

    @Test fun newMetadataPublishesOutsideOriginalSourceAndRetainsNoAudioCopy() {
        val sources = temporary.newFolder("sources")
        val original = File(sources, "literal.wav")
        val marker = "RAW_AUDIO_SENTINEL_" + "not metadata".repeat(100)
        val bytes = marker.toByteArray(Charsets.UTF_8)
        original.writeBytes(bytes)
        val privateDir = temporary.newFolder("app-private")
        val path = File(privateDir, "ageds-source-scan.json")
        val expected = result(original.toURI().toString())
        val cache = BoundedMetadataCache(path, 4 * 1024 * 1024)
        cache.write(json.encodeToString(expected))
        val stored = requireNotNull(cache.read())
        assertEquals(expected, json.decodeFromString<SourceScanResult>(stored))
        assertArrayEquals(bytes, original.readBytes())
        assertEquals(listOf("literal.wav"), sources.list()!!.toList())
        assertEquals(listOf("ageds-source-scan.json"), privateDir.list()!!.toList())
        assertFalse(stored.contains("RAW_AUDIO_SENTINEL"))
        assertFalse(stored.contains(java.util.Base64.getEncoder().encodeToString(bytes)))
        assertFalse(expected.files.single().wavHeader!!.bodyValidated)
    }

    @Test fun historicalCacheMissingFieldReadsAndRewritesWithoutHeaderInference() {
        val raw = """{"rootUri":"content://old/tree","scannedAt":"old-time","files":[{"uri":"content://old/recording.wav","name":"recording.wav","relativePath":"recording.wav","kind":"wav","audioDurationSec":7.5}],"scannedDirectories":1,"coverage":"partial"}"""
        val path = temporary.newFile("old-cache.json")
        path.writeText(raw, Charsets.UTF_8)
        val cache = BoundedMetadataCache(path, 4 * 1024 * 1024)
        val old = json.decodeFromString<SourceScanResult>(requireNotNull(cache.read()))
        assertEquals(1, old.schemaVersion)
        assertNull(old.files.single().wavHeader)
        assertEquals(7.5, old.files.single().audioDurationSec!!, 0.0)
        cache.write(json.encodeToString(old))
        assertEquals(old, json.decodeFromString<SourceScanResult>(requireNotNull(cache.read())))
        assertFalse(requireNotNull(cache.read()).contains("wavHeader"))
    }

    @Test fun exactUtf8BoundaryFitsButOneByteSmallerKeepsPreviousCache() {
        val raw = json.encodeToString(result("content://źródło/😀.wav"))
        val exact = raw.toByteArray(Charsets.UTF_8).size
        assertTrue(exact > raw.length)
        val path = temporary.newFile("bounded.json")
        val cache = BoundedMetadataCache(path, exact)
        cache.write(raw)
        val before = path.readBytes()
        val failure = runCatching { BoundedMetadataCache(path, exact - 1).write(raw) }.exceptionOrNull()
        assertTrue(failure is IllegalArgumentException)
        assertArrayEquals(before, path.readBytes())
        assertEquals(raw, cache.read())
        assertEquals(listOf("bounded.json"), temporary.root.list()!!.toList())
    }

    @Test fun actualFourMiBUtf8OverflowFailsBeforeReplacingSmallPublishedMetadata() {
        val path = temporary.newFile("bounded.json")
        val cache = BoundedMetadataCache(path, 4 * 1024 * 1024)
        val old = json.encodeToString(result("content://source/old.wav"))
        cache.write(old)
        val oversized = json.encodeToString(result("content://source/new.wav", "ą".repeat(2 * 1024 * 1024)))
        assertTrue(oversized.length < 4 * 1024 * 1024)
        assertTrue(oversized.toByteArray(Charsets.UTF_8).size > 4 * 1024 * 1024)
        assertTrue(runCatching { cache.write(oversized) }.exceptionOrNull() is IllegalArgumentException)
        assertEquals(old, cache.read())
        assertEquals(listOf("bounded.json"), temporary.root.list()!!.toList())
    }

    @Test fun canceledPublicationKeepsOldTypedSnapshotAndDeletesTemporaryMetadata() {
        val path = temporary.newFile("snapshot.json")
        val cache = BoundedMetadataCache(path, 4 * 1024 * 1024)
        val old = result("content://old/a.wav")
        cache.write(json.encodeToString(old))
        var checks = 0
        val failure = runCatching {
            cache.write(json.encodeToString(result("content://new/b.wav"))) {
                if (++checks == 2) throw CancellationException("superseded scan")
            }
        }.exceptionOrNull()
        assertTrue(failure is CancellationException)
        assertEquals(old, json.decodeFromString<SourceScanResult>(requireNotNull(cache.read())))
        assertEquals(listOf("snapshot.json"), temporary.root.list()!!.toList())
    }

    @Test fun oversizedOrInvalidUtf8ExistingCacheFailsClosedWithoutRepair() {
        val path = temporary.newFile("invalid.json")
        val raw = json.encodeToString(result("content://source/a.wav")).toByteArray(Charsets.UTF_8)
        path.writeBytes(raw)
        assertTrue(runCatching { BoundedMetadataCache(path, raw.size - 1).read() }.exceptionOrNull() is IllegalArgumentException)
        assertArrayEquals(raw, path.readBytes())
        val malformed = byteArrayOf(0xc3.toByte(), 0x28)
        path.writeBytes(malformed)
        assertTrue(runCatching { BoundedMetadataCache(path, 1024).read() }.exceptionOrNull() is CharacterCodingException)
        assertArrayEquals(malformed, path.readBytes())
    }
}

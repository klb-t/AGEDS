package dev.klbt.ageds.core

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Frozen wire fixtures, independent of the new producer's JSON construction. */
class WavHeaderCacheSerializationTest {
    private val cacheJson = Json { ignoreUnknownKeys = true }
    private val legacy = """{
        "schemaVersion":1,"rootUri":"content://historical/tree/źródło",
        "scannedAt":"2026-09-30T23:00:00Z","scannedDirectories":1,"coverage":"partial",
        "files":[{"uri":"content://historical/document/recording.wav","name":"recording.wav",
        "relativePath":"recording.wav","sizeBytes":123456,"kind":"wav",
        "coverage":"parsed","audioDurationSec":17.25}],"bytesRead":123456
    }"""

    private fun observed() = WavHeaderObservation(
        status = "partial", coverage = "header_prefix_only", bytesInspected = 44,
        prefixLimitBytes = 65536, endOfInput = false, providerSizeBytes = 210044,
        riffDeclaredBytes = 210044, formatCode = 1, channels = 1, sampleRateHz = 8000,
        byteRate = 16000, blockAlignBytes = 2, bitsPerSample = 16, dataDeclaredBytes = 210000,
        declaredDurationSec = 13.125, durationBasis = "declared_data_bytes_divided_by_header_byte_rate",
        bodyValidated = false, issues = listOf(ScanIssue("historical", "Żółć 😀 <raw> \"quote\"\n", "chunk:44")))

    @Test fun frozenOldSchemaMissingWavMetadataKeepsLegacyDurationWithoutInventedHeader() {
        val restored = cacheJson.decodeFromString<SourceScanResult>(legacy)
        assertEquals(1, restored.schemaVersion)
        assertEquals(17.25, restored.files.single().audioDurationSec)
        assertNull(restored.files.single().wavHeader)
        assertEquals("parsed", restored.files.single().coverage)
        assertEquals("content://historical/tree/źródło", restored.rootUri)
        val encoded = cacheJson.encodeToString(restored)
        assertFalse(encoded.contains("wavHeader"))
        assertEquals(restored, cacheJson.decodeFromString<SourceScanResult>(encoded))
    }

    @Test fun explicitNullAndFutureUnknownFieldDoNotInventHeaderObservation() {
        val raw = legacy.replace("\"audioDurationSec\":17.25", "\"audioDurationSec\":17.25,\"wavHeader\":null,\"futureParser\":{\"opaque\":true}")
        val restored = cacheJson.decodeFromString<SourceScanResult>(raw)
        assertNull(restored.files.single().wavHeader)
        assertEquals(17.25, restored.files.single().audioDurationSec)
    }

    @Test fun everyNewHeaderFieldAndExistingSiblingMetadataRoundtripExactly() {
        val old = cacheJson.decodeFromString<SourceScanResult>(legacy)
        val wav = old.files.single().copy(wavHeader = observed(), audioDurationSec = 13.125,
            coverage = "header_prefix_only", sizeBytes = 210044)
        val csv = ScannedSourceFile("content://tree/data.csv", "data.csv", "data.csv", kind = "csv",
            rows = listOf(SourceRow("row:1", listOf(SourceCell(0, "\" raw \"", " raw ")))),
            textFormat = SourceTextFormat("UTF-8", "strict", 0, ";", "inferred", listOf(";", ","), true, 1))
        val expected = old.copy(files = listOf(wav, csv), bytesRead = 44)
        val encoded = cacheJson.encodeToString(expected)
        val restored = cacheJson.decodeFromString<SourceScanResult>(encoded)
        assertEquals(expected, restored)
        assertEquals(observed(), restored.files.first().wavHeader)
        assertFalse(restored.files.first().wavHeader!!.bodyValidated)
        assertNull(restored.files.last().wavHeader)
        assertEquals(csv.textFormat, restored.files.last().textFormat)
    }

    @Test fun missingDurationAndFailedHeaderStayUnknownAfterRoundtrip() {
        for (status in listOf("partial", "malformed", "unsupported", "size_mismatch")) {
            val file = ScannedSourceFile("content://old/unread.wav", "unread.wav", "unread.wav",
                kind = "wav", coverage = "header_prefix_only",
                wavHeader = WavHeaderObservation(status = status, bytesInspected = 12,
                    issues = listOf(ScanIssue("incomplete", "duration not derived"))))
            val restored = cacheJson.decodeFromString<ScannedSourceFile>(cacheJson.encodeToString(file))
            assertEquals(status, restored.wavHeader!!.status)
            assertNull(restored.wavHeader!!.declaredDurationSec)
            assertNull(restored.audioDurationSec)
            assertFalse(restored.wavHeader!!.bodyValidated)
        }
    }

    @Test fun malformedNewMetadataCannotMasqueradeAsSuccessfullyRestoredObservation() {
        for (value in listOf("[]", "\"audio\"", "{\"bytesInspected\":\"not-a-number\"}",
            "{\"declaredDurationSec\":NaN}", "{\"providerSizeBytes\":9223372036854775808}")) {
            val raw = legacy.replace("\"audioDurationSec\":17.25", "\"audioDurationSec\":17.25,\"wavHeader\":$value")
            assertFailsWith<IllegalArgumentException> { cacheJson.decodeFromString<SourceScanResult>(raw) }
        }
    }

    @Test fun realProbeObservationRoundtripNeverRetainsSuppliedAudioPayload() {
        val sentinel = "RAW_AUDIO_SENTINEL!!".encodeToByteArray()
        val bytes = ByteArray(44 + 2560)
        fun ascii(offset: Int, value: String) { value.encodeToByteArray().copyInto(bytes, offset) }
        fun little(offset: Int, value: Int, size: Int) { repeat(size) { bytes[offset + it] = (value ushr (8 * it)).toByte() } }
        ascii(0, "RIFF"); little(4, bytes.size - 8, 4); ascii(8, "WAVE")
        ascii(12, "fmt "); little(16, 16, 4); little(20, 1, 2); little(22, 1, 2)
        little(24, 8000, 4); little(28, 16000, 4); little(32, 2, 2); little(34, 16, 2)
        ascii(36, "data"); little(40, 2560, 4)
        for (i in 44 until bytes.size) bytes[i] = sentinel[(i - 44) % sentinel.size]
        val before = bytes.copyOf()
        val observation = WavHeaderProbe.probe(bytes, providerSizeBytes = bytes.size.toLong(), endOfInput = true)
        val encoded = cacheJson.encodeToString(observation)
        assertEquals(observation, cacheJson.decodeFromString<WavHeaderObservation>(encoded))
        assertEquals(0.16, observation.declaredDurationSec)
        assertFalse(observation.bodyValidated)
        assertFalse(encoded.contains("RAW_AUDIO_SENTINEL"))
        assertTrue(encoded.encodeToByteArray().size < 4096)
        assertTrue(before.contentEquals(bytes))
    }

    @Test fun observationWireContainsOnlyDeclaredMetadataAndNoRawAudioField() {
        val objectValue: JsonObject = cacheJson.parseToJsonElement(cacheJson.encodeToString(observed())).jsonObject
        val permitted = setOf("status", "coverage", "bytesInspected", "prefixLimitBytes", "endOfInput",
            "providerSizeBytes", "riffDeclaredBytes", "formatCode", "channels", "sampleRateHz", "byteRate",
            "blockAlignBytes", "bitsPerSample", "dataDeclaredBytes", "declaredDurationSec", "durationBasis",
            "bodyValidated", "issues")
        assertTrue(objectValue.keys.all { it in permitted })
        assertTrue(cacheJson.encodeToString(observed()).encodeToByteArray().size < 4096)
    }
}

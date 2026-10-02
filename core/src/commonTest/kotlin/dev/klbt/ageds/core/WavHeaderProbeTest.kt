package dev.klbt.ageds.core

import kotlin.test.*

class WavHeaderProbeTest {
    private fun le(value: Long, count: Int) = ByteArray(count) { (value ushr (it * 8)).toByte() }
    private fun chunk(id: String, payload: ByteArray, declared: Long = payload.size.toLong()): ByteArray =
        id.encodeToByteArray() + le(declared, 4) + payload + if (payload.size % 2 == 1) byteArrayOf(0) else byteArrayOf()
    private fun fmt(format: Int = 1, channels: Int = 1, bits: Int = 16, sampleRate: Long = 16000,
                    byteRate: Long = sampleRate * channels * (bits / 8), extra: ByteArray = byteArrayOf()) =
        chunk("fmt ", le(format.toLong(), 2) + le(channels.toLong(), 2) + le(sampleRate, 4) + le(byteRate, 4) +
            le((channels * (bits / 8)).toLong(), 2) + le(bits.toLong(), 2) + extra)
    private fun riff(vararg chunks: ByteArray): ByteArray {
        val body = "WAVE".encodeToByteArray() + chunks.fold(byteArrayOf()) { acc, bytes -> acc + bytes }
        return "RIFF".encodeToByteArray() + le(body.size.toLong(), 4) + body
    }
    private fun extendedDataPrefix(dataBytes: Long): ByteArray {
        val prefix = riff(fmt(), chunk("data", byteArrayOf(), dataBytes))
        le(prefix.size.toLong() - 8 + dataBytes, 4).copyInto(prefix, 4)
        return prefix
    }

    @Test fun smallCompletePcmHasOnlyDeclaredDuration() {
        val bytes = riff(fmt(), chunk("data", ByteArray(32000)))
        val result = WavHeaderProbe.probe(bytes, bytes.size.toLong(), true)
        assertEquals("observed", result.status)
        assertEquals(1.0, result.declaredDurationSec)
        assertEquals(WavHeaderProbe.DURATION_BASIS, result.durationBasis)
        assertEquals(bytes.size.toLong(), result.riffDeclaredBytes)
        assertEquals(32000L, result.dataDeclaredBytes)
        assertEquals(32000L, result.byteRate)
        assertFalse(result.bodyValidated)
        assertEquals("header_prefix_only", result.coverage)
        assertTrue(result.issues.any { it.code == "audio_body_not_validated" })
    }

    @Test fun largeDataDeclarationNeedsNoBodyOrFullHash() {
        val bytes = extendedDataPrefix(3_200_000L)
        val result = WavHeaderProbe.probe(bytes, 3_200_044L)
        assertEquals("observed", result.status)
        assertEquals(100.0, result.declaredDurationSec)
        assertFalse(result.endOfInput)
        assertTrue(result.issues.any { it.code == "data_body_uninspected" })
    }

    @Test fun unknownProviderAndEmptyDeclaredDataRemainExplicit() {
        val bytes = riff(fmt(), chunk("data", byteArrayOf()))
        val result = WavHeaderProbe.probe(bytes, endOfInput = true)
        assertEquals("observed", result.status)
        assertNull(result.providerSizeBytes)
        assertEquals(0.0, result.declaredDurationSec)
        assertFalse(result.bodyValidated)
    }

    @Test fun narrowFloatAndZeroLengthFmtExtensionAreSupported() {
        val bytes = riff(fmt(format = 3, bits = 32, extra = byteArrayOf(0, 0)), chunk("data", ByteArray(64000)))
        val result = WavHeaderProbe.probe(bytes, bytes.size.toLong(), true)
        assertEquals("observed", result.status)
        assertEquals(1.0, result.declaredDurationSec)
        assertEquals(3, result.formatCode)
    }

    @Test fun compressedFormatPreservesRawFieldsWithoutGuessingDuration() {
        val bytes = riff(fmt(format = 6), chunk("data", ByteArray(100)))
        val result = WavHeaderProbe.probe(bytes, bytes.size.toLong(), true)
        assertEquals("unsupported", result.status)
        assertEquals(6, result.formatCode)
        assertEquals(32000L, result.byteRate)
        assertNull(result.declaredDurationSec)
        assertNull(result.durationBasis)
    }

    @Test fun providerMismatchPreservesDeclarationsWithoutDuration() {
        val bytes = extendedDataPrefix(32000)
        val result = WavHeaderProbe.probe(bytes, 999999)
        assertEquals("size_mismatch", result.status)
        assertEquals(32044L, result.riffDeclaredBytes)
        assertEquals(999999L, result.providerSizeBytes)
        assertEquals(32000L, result.dataDeclaredBytes)
        assertNull(result.declaredDurationSec)
        assertTrue(result.issues.any { it.code == "provider_size_mismatch" })
    }

    @Test fun sameShortPrefixDistinguishesUnknownTailFromObservedEof() {
        val bytes = extendedDataPrefix(32000)
        assertEquals("observed", WavHeaderProbe.probe(bytes).status)
        val ended = WavHeaderProbe.probe(bytes, endOfInput = true)
        assertEquals("malformed", ended.status)
        assertNull(ended.declaredDurationSec)
        assertTrue(ended.issues.any { it.code == "truncated_chunk_payload" })
        assertEquals("partial", WavHeaderProbe.probe(bytes.copyOf(20)).status)
    }

    @Test fun visibleDuplicateAfterDataIsRejected() {
        for (extra in listOf(fmt(), chunk("data", ByteArray(2)))) {
            val bytes = riff(fmt(), chunk("data", ByteArray(2)), extra)
            val result = WavHeaderProbe.probe(bytes, endOfInput = true)
            assertEquals("malformed", result.status)
            assertNull(result.declaredDurationSec)
            assertTrue(result.issues.any { it.code.startsWith("duplicate_") })
        }
    }

    @Test fun rateGeometryAndPartialSampleFramesAreNotRepaired() {
        val rate = WavHeaderProbe.probe(riff(fmt(byteRate = 7), chunk("data", ByteArray(2))), endOfInput = true)
        assertEquals("malformed", rate.status)
        assertEquals(7L, rate.byteRate)
        val frame = WavHeaderProbe.probe(riff(fmt(), chunk("data", ByteArray(3))), endOfInput = true)
        assertEquals("malformed", frame.status)
        assertTrue(frame.issues.any { it.code == "unaligned_data_size" })
    }

    @Test fun oddUnknownChunkPaddingIsIncludedAndBadExtentRejected() {
        val valid = riff(chunk("JUNK", byteArrayOf(42)), fmt(), chunk("data", ByteArray(2)))
        assertEquals("observed", WavHeaderProbe.probe(valid, endOfInput = true).status)
        val malformed = riff(fmt(), chunk("data", byteArrayOf(), 0xffffffffL))
        val result = WavHeaderProbe.probe(malformed)
        assertEquals("malformed", result.status)
        assertTrue(result.issues.any { it.code == "chunk_extent_outside_riff" })
    }

    @Test fun exact64KiBHeaderBoundaryAndOversizedInputStayBounded() {
        val prefix = riff(fmt(), chunk("JUNK", ByteArray(65484)), chunk("data", byteArrayOf(), 32000))
        assertEquals(65536, prefix.size)
        le(97536L - 8, 4).copyInto(prefix, 4)
        val result = WavHeaderProbe.probe(prefix, 97536)
        assertEquals("observed", result.status)
        assertEquals(1.0, result.declaredDurationSec)
        val oversized = WavHeaderProbe.probe(prefix + ByteArray(100), 97536, true)
        assertEquals(65536, oversized.bytesInspected)
        assertFalse(oversized.endOfInput)
        assertTrue(oversized.issues.any { it.code == "prefix_limit" })
    }

    @Test fun arbitraryAndUnsupportedPrefixesReturnObservations() {
        for (bytes in listOf(byteArrayOf(), ByteArray(11), ByteArray(65536) { 0xff.toByte() }, "RIFXabcdefgh".encodeToByteArray())) {
            val result = WavHeaderProbe.probe(bytes)
            assertTrue(result.status in setOf("partial", "unsupported", "malformed"))
            assertNull(result.declaredDurationSec)
            assertFalse(result.bodyValidated)
            assertTrue(result.bytesInspected <= 65536)
        }
    }
}

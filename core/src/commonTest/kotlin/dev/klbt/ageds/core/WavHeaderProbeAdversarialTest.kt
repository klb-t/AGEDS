package dev.klbt.ageds.core

import kotlin.test.*

/** N45 independent synthetic RIFF bytes; no decoder, provider, or private media. */
class WavHeaderProbeAdversarialTest {
    private fun text(s: String) = s.map { it.code.toByte() }.toByteArray()
    private fun u16(value: Int) = ByteArray(2) { (value ushr (8 * it)).toByte() }
    private fun u32(value: Long) = ByteArray(4) { (value ushr (8 * it)).toByte() }
    private fun chunk(name: String, payload: ByteArray, size: Long = payload.size.toLong(), pad: Boolean = true): ByteArray =
        text(name) + u32(size) + payload + if (pad && payload.size % 2 != 0) byteArrayOf(0) else byteArrayOf()
    private fun fmt(format: Int = 1, channels: Int = 1, rate: Long = 8000,
                    byteRate: Long = 16000, alignment: Int = 2, bits: Int = 16): ByteArray =
        chunk("fmt ", u16(format) + u16(channels) + u32(rate) + u32(byteRate) + u16(alignment) + u16(bits))
    private fun riff(vararg chunks: ByteArray, declaredSize: Long? = null): ByteArray {
        val payload = chunks.fold(text("WAVE")) { out, value -> out + value }
        return text("RIFF") + u32(declaredSize ?: payload.size.toLong()) + payload
    }
    private fun data(size: Int = 160) = chunk("data", ByteArray(size))

    private fun probe(bytes: ByteArray, size: Long? = null, eof: Boolean = false) =
        WavHeaderProbe.probe(bytes, providerSizeBytes = size, endOfInput = eof)

    @Test fun validHeaderDurationDoesNotClaimDecodedOrFullBodyValidation() {
        val bytes = riff(fmt(), data())
        val before = bytes.copyOf()
        for (provider in listOf(null, bytes.size.toLong())) {
            val result = probe(bytes, provider, true)
            assertEquals(0.01, result.declaredDurationSec)
            assertEquals("header_prefix_only", result.coverage)
            assertFalse(result.bodyValidated)
            assertEquals("declared_data_bytes_divided_by_header_byte_rate", result.durationBasis)
        }
        assertContentEquals(before, bytes)
    }

    @Test fun declaredDataWithoutBodyIsUsableOnlyAsHeaderObservation() {
        val header = riff(fmt(), chunk("data", byteArrayOf(), size = 16000), declaredSize = 16036)
        val result = probe(header, 16044, false)
        assertEquals(1.0, result.declaredDurationSec)
        assertEquals(16000L, result.dataDeclaredBytes)
        assertFalse(result.bodyValidated)
        assertEquals("header_prefix_only", result.coverage)
        assertNull(probe(header, 16044, true).declaredDurationSec)
    }

    @Test fun providerExtentContradictionNeverProducesDuration() {
        val bytes = riff(fmt(), data())
        for (provider in listOf(bytes.size.toLong() - 1, bytes.size.toLong() + 1, 0L, Long.MAX_VALUE)) {
            val result = probe(bytes, provider, false)
            assertNull(result.declaredDurationSec, "provider=$provider")
            assertEquals("size_mismatch", result.status)
            assertEquals(provider, result.providerSizeBytes)
        }
        assertEquals(0.01, probe(bytes, null, true).declaredDurationSec)
    }

    @Test fun knownEofAtEveryHeaderTruncationCannotInventDuration() {
        val bytes = riff(fmt(), data())
        for (length in 0 until 44) {
            val prefix = bytes.copyOf(length)
            val result = probe(prefix, null, true)
            assertNull(result.declaredDurationSec, "prefix=$length")
            assertFalse(result.bodyValidated)
        }
        assertNull(probe(bytes.copyOf(bytes.size - 1), null, true).declaredDurationSec)
    }

    @Test fun riffExtentMustContainChunkHeaderPayloadAndOddPadding() {
        val bytes = riff(fmt(), data())
        for (declared in listOf(0L, 3L, 4L, 27L, 35L, 36L, 195L)) {
            val changed = bytes.copyOf()
            u32(declared).copyInto(changed, 4)
            assertNull(probe(changed, null, true).declaredDurationSec, "riff=$declared")
        }
        val missingPad = riff(fmt(bits = 8, byteRate = 8000, alignment = 1), chunk("data", byteArrayOf(0), pad = false))
        assertNull(probe(missingPad, missingPad.size.toLong(), true).declaredDurationSec)
    }

    @Test fun oddUnknownChunkPaddingDoesNotShiftTheNextHeader() {
        val padded = riff(chunk("JUNK", byteArrayOf(42)), fmt(), data())
        assertEquals(0.01, probe(padded, padded.size.toLong(), true).declaredDurationSec)
        val unpadded = riff(chunk("JUNK", byteArrayOf(42), pad = false), fmt(), data())
        assertNull(probe(unpadded, unpadded.size.toLong(), true).declaredDurationSec)
    }

    @Test fun visibleDuplicateFmtOrDataInvalidatesEarlierObservation() {
        for (bytes in listOf(riff(fmt(), fmt(), data()), riff(fmt(), data(), data()), riff(fmt(), data(), fmt()))) {
            val result = probe(bytes, bytes.size.toLong(), true)
            assertEquals("malformed", result.status)
            assertNull(result.declaredDurationSec)
        }
    }

    @Test fun unsupportedContainersAndFormatsDoNotGuessPcmDuration() {
        val valid = riff(fmt(), data())
        for (signature in listOf("RIFX", "RF64", "FORM")) {
            val bytes = valid.copyOf()
            text(signature).copyInto(bytes)
            assertNull(probe(bytes, bytes.size.toLong(), true).declaredDurationSec)
        }
        for (format in listOf(0, 2, 6, 7, 0xFFFE, 0xFFFF)) {
            val bytes = riff(fmt(format = format), data())
            assertNull(probe(bytes, bytes.size.toLong(), true).declaredDurationSec, "format=$format")
        }
    }

    @Test fun inconsistentRateAlignmentBitsOrDataFrameCountAreNotDurations() {
        val formats = listOf(fmt(channels = 0), fmt(rate = 0), fmt(byteRate = 0),
            fmt(byteRate = 15999), fmt(alignment = 1), fmt(bits = 0), fmt(bits = 12),
            fmt(format = 3, bits = 16), fmt(channels = 65535, rate = 0xFFFFFFFFL,
                byteRate = 0xFFFFFFFFL, alignment = 65535, bits = 32))
        for ((index, format) in formats.withIndex()) {
            val bytes = riff(format, data())
            assertNull(probe(bytes, bytes.size.toLong(), true).declaredDurationSec, "format-index=$index")
        }
        val partialFrame = riff(fmt(), data(3))
        assertNull(probe(partialFrame, partialFrame.size.toLong(), true).declaredDurationSec)
    }

    @Test fun uint32ChunkLengthDoesNotWrapIntoVisibleHeaders() {
        val bytes = riff(chunk("JUNK", byteArrayOf(), size = 0xFFFFFFFFL), fmt(), data(), declaredSize = 0xFFFFFFFFL)
        val result = probe(bytes, null, false)
        assertNull(result.declaredDurationSec)
        assertFalse(result.bodyValidated)
        assertTrue(result.bytesInspected <= 65536)
    }

    @Test fun exactly64KiBDataHeaderIsSeenButHeaderBeyondCapIsNot() {
        val atBoundary = riff(fmt(), chunk("JUNK", ByteArray(65484)),
            chunk("data", byteArrayOf(), size = 160), declaredSize = 65688)
        assertEquals(65536, atBoundary.size)
        val observed = probe(atBoundary, 65696, false)
        assertEquals(0.01, observed.declaredDurationSec)
        assertTrue(observed.bytesInspected <= 65536)
        val beyondBoundary = riff(fmt(), chunk("JUNK", ByteArray(65486)),
            chunk("data", ByteArray(160)), declaredSize = 65690)
        val beyond = probe(beyondBoundary, beyondBoundary.size.toLong(), false)
        assertNull(beyond.declaredDurationSec)
        assertEquals("partial", beyond.status)
        assertTrue(beyond.bytesInspected <= 65536)
    }

    @Test fun floatAndPcmSupportedWidthsUseDeclaredByteRateOnly() {
        for ((format, bits) in listOf(1 to 8, 1 to 16, 1 to 24, 1 to 32, 3 to 32, 3 to 64)) {
            val align = 2 * bits / 8
            val bytes = riff(fmt(format = format, channels = 2, rate = 1000,
                byteRate = (1000 * align).toLong(), alignment = align, bits = bits), data(100 * align))
            assertEquals(0.1, probe(bytes, bytes.size.toLong(), true).declaredDurationSec)
            assertFalse(probe(bytes, bytes.size.toLong(), true).bodyValidated)
        }
    }

    @Test fun fmtExtensionLengthIsObservedRatherThanGuessed() {
        val base = u16(1) + u16(1) + u32(8000) + u32(16000) + u16(2) + u16(16)
        val zeroExtension = riff(chunk("fmt ", base + u16(0)), data())
        assertEquals(0.01, probe(zeroExtension, zeroExtension.size.toLong(), true).declaredDurationSec)
        for (payload in listOf(base + u16(1), base + byteArrayOf(0), base + u16(0) + byteArrayOf(0, 0))) {
            val bytes = riff(chunk("fmt ", payload), data())
            assertNull(probe(bytes, bytes.size.toLong(), true).declaredDurationSec)
        }
    }

    @Test fun dataBeforeFmtIsNotReinterpretedAndRawDeclarationsRemainVisible() {
        val bytes = riff(data(), fmt())
        val result = probe(bytes, bytes.size.toLong(), true)
        assertEquals("malformed", result.status)
        assertNull(result.declaredDurationSec)
        assertEquals(160L, result.dataDeclaredBytes)
        assertTrue(result.issues.any { it.code == "data_before_fmt" })
        val inconsistent = riff(fmt(byteRate = 15999), data())
        val raw = probe(inconsistent, inconsistent.size.toLong(), true)
        assertEquals(15999L, raw.byteRate)
        assertNull(raw.declaredDurationSec)
    }

    @Test fun unseenTrailingHeadersAreExplicitlyUnknownNotDeclaredUnique() {
        val prefix = riff(fmt(), chunk("data", byteArrayOf(), size = 16000), declaredSize = 17036)
        val result = probe(prefix, 17044, false)
        assertEquals(1.0, result.declaredDurationSec)
        assertFalse(result.bodyValidated)
        assertTrue(result.issues.any { it.code == "trailing_chunks_uninspected" })
        assertTrue(result.issues.any { it.code == "data_body_uninspected" })
    }

    @Test fun negativeProviderAndMaximumUnsignedRiffDoNotOverflowOrInventDuration() {
        val bytes = riff(fmt(), data())
        val negative = probe(bytes, -1, false)
        assertEquals(-1L, negative.providerSizeBytes)
        assertNull(negative.declaredDurationSec)
        assertTrue(negative.issues.any { it.code == "invalid_provider_size" })
        val maximum = text("RIFF") + u32(0xFFFFFFFFL) + text("WAVE")
        val result = probe(maximum, 4294967303L, false)
        assertEquals(4294967303L, result.riffDeclaredBytes)
        assertNull(result.declaredDurationSec)
        assertEquals("partial", result.status)
    }

    @Test fun callerEofBeyondBudgetCannotTurnBoundedPrefixIntoFullInspection() {
        val bytes = riff(fmt(), chunk("data", ByteArray(100000)))
        val result = probe(bytes, bytes.size.toLong(), true)
        assertEquals(65536, result.bytesInspected)
        assertFalse(result.endOfInput)
        assertFalse(result.bodyValidated)
        assertTrue(result.issues.any { it.code == "prefix_limit" })
        assertEquals(6.25, result.declaredDurationSec)
    }
}

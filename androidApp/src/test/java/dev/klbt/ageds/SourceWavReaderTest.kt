package dev.klbt.ageds

import dev.klbt.ageds.core.WavHeaderProbe
import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.CancellationException

/** Generated RIFF bytes only; exercises production stream policy without SAF. */
class SourceWavReaderTest {
    private class Tracked(bytes: ByteArray) : ByteArrayInputStream(bytes) {
        var closedByOwner = false
        var delivered = 0
        override fun read(buffer: ByteArray, offset: Int, length: Int): Int = super.read(buffer, offset, length).also {
            if (it > 0) delivered += it
        }
        override fun close() { closedByOwner = true; super.close() }
    }
    private fun le16(value: Int) = byteArrayOf(value.toByte(), (value ushr 8).toByte())
    private fun le32(value: Int) = ByteArray(4) { (value ushr (it * 8)).toByte() }
    private fun wav(dataSize: Int): ByteArray = "RIFF".toByteArray() + le32(36 + dataSize) + "WAVEfmt ".toByteArray() +
        le32(16) + le16(1) + le16(1) + le32(8000) + le32(16000) + le16(2) + le16(16) +
        "data".toByteArray() + le32(dataSize) + ByteArray(dataSize)
    private fun hash(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    @Test fun completeSmallWavGetsWholeFileHashAndSeparateHeaderEstimate() {
        val bytes = wav(16000)
        val input = Tracked(bytes)
        val result = SourceWavReader.read(input, bytes.size.toLong(), 20000, 20000)
        assertTrue(result.complete)
        assertEquals(bytes.size.toLong(), result.bytesRead)
        assertEquals(hash(bytes), result.sha256)
        assertEquals(1.0, result.wavHeader.declaredDurationSec!!, 0.0)
        assertTrue(result.wavHeader.endOfInput)
        assertFalse(result.wavHeader.bodyValidated)
        assertTrue(input.closedByOwner)
    }

    @Test fun knownOversizeGetsOnlyHeaderPrefixAndNoWholeHash() {
        val bytes = wav(120000)
        val input = Tracked(bytes)
        val result = SourceWavReader.read(input, bytes.size.toLong(), 100000, 200000)
        assertEquals(WavHeaderProbe.MAX_PREFIX_BYTES.toLong(), result.bytesRead)
        assertEquals(WavHeaderProbe.MAX_PREFIX_BYTES, input.delivered)
        assertNull(result.sha256)
        assertFalse(result.complete)
        assertFalse(result.wavHeader.endOfInput)
        assertTrue(result.issues.any { it.code == "bytes_limit" })
        assertTrue(input.closedByOwner)
    }

    @Test fun unknownSizeFullHashDoesNotRetainWholeAudioOrPretendPrefixIsEof() {
        val bytes = wav(120000)
        val input = Tracked(bytes)
        val result = SourceWavReader.read(input, null, 200000, 200000)
        assertTrue(result.complete)
        assertEquals(hash(bytes), result.sha256)
        assertEquals(bytes.size.toLong(), result.bytesRead)
        assertTrue(result.wavHeader.bytesInspected <= WavHeaderProbe.MAX_PREFIX_BYTES)
        assertFalse(result.wavHeader.endOfInput)
        assertFalse(result.wavHeader.bodyValidated)
    }

    @Test fun completeUnknownLargeStreamChecksDeclaredSizeAgainstActualEof() {
        val bytes = wav(120000)
        le32(200036).copyInto(bytes, 4)
        le32(200000).copyInto(bytes, 40)
        val result = SourceWavReader.read(Tracked(bytes), null, 300000, 300000)
        assertTrue(result.complete)
        assertEquals(hash(bytes), result.sha256)
        assertEquals("size_mismatch", result.wavHeader.status)
        assertNull(result.wavHeader.declaredDurationSec)
        assertTrue(result.wavHeader.issues.any { it.code == "wav_stream_size_mismatch" })
    }

    @Test fun unknownSizeStopsAtHardBudgetWithoutOneByteSentinel() {
        val input = Tracked(wav(120000))
        val result = SourceWavReader.read(input, null, 100000, 12345)
        assertEquals(12345L, result.bytesRead)
        assertEquals(12345, input.delivered)
        assertNull(result.sha256)
        assertFalse(result.complete)
    }

    @Test fun exactBudgetDoesNotInventEofOrCompleteHash() {
        val bytes = wav(10)
        val result = SourceWavReader.read(Tracked(bytes), bytes.size.toLong(), bytes.size.toLong(), 100000)
        assertEquals(bytes.size.toLong(), result.bytesRead)
        assertFalse(result.complete)
        assertNull(result.sha256)
    }

    @Test fun lyingLargeProviderSizeCannotPreventObservedSmallEofHash() {
        val bytes = wav(10)
        val result = SourceWavReader.read(Tracked(bytes), 1000000, 100000, 100000)
        assertTrue(result.complete)
        assertEquals(hash(bytes), result.sha256)
        assertTrue(result.issues.any { it.code == "size_changed" })
    }

    @Test fun partialReadFailureRetainsBudgetChargeAndClosesStream() {
        var closed = false
        var calls = 0
        val input = object : ByteArrayInputStream(wav(16000)) {
            override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
                if (++calls > 1) throw IOException("synthetic provider failure")
                return super.read(buffer, offset, length)
            }
            override fun close() { closed = true }
        }
        val result = SourceWavReader.read(input, null, 20000, 20000)
        assertEquals(8192L, result.bytesRead)
        assertTrue(result.issues.any { it.code == "read_failed" })
        assertNull(result.sha256)
        assertFalse(result.complete)
        assertTrue(closed)
    }

    @Test fun cancellationPropagatesAfterClosingInput() {
        val input = Tracked(wav(16000))
        try {
            SourceWavReader.read(input, null, 20000, 20000) { throw CancellationException("test") }
            fail("Cancellation must propagate")
        } catch (_: CancellationException) { }
        assertTrue(input.closedByOwner)
        assertEquals(0, input.delivered)
    }
}

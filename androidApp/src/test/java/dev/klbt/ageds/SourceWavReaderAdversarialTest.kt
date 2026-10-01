package dev.klbt.ageds

import org.junit.Assert.*
import org.junit.Test
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import java.util.concurrent.CancellationException

/** Count real returned bytes and requested lengths, independent of provider size claims. */
class SourceWavReaderAdversarialTest {
    private class CountedStream(private val data:ByteArray,private val chunk:Int=Int.MAX_VALUE) : InputStream() {
        var consumed=0
        var calls=0
        var closed=0
        var zeroReads=false
        var zeroAfter:Int?=null
        var throwClose=false
        var throwAfter:Int?=null
        val requests=mutableListOf<Int>()
        override fun read():Int {
            calls++
            if(throwAfter?.let { consumed>=it }==true) throw IOException("synthetic failure")
            return if(consumed==data.size) -1 else data[consumed++].toInt() and 255
        }
        override fun read(target:ByteArray,offset:Int,length:Int):Int {
            calls++;requests.add(length)
            require(length>0) { "zero-length read cannot establish EOF" }
            if(throwAfter?.let { consumed>=it }==true) throw IOException("synthetic failure")
            if(zeroReads || zeroAfter?.let { consumed>=it }==true) return 0
            if(consumed==data.size) return -1
            val count=minOf(length,chunk,data.size-consumed)
            data.copyInto(target,offset,consumed,consumed+count);consumed+=count;return count
        }
        override fun close() { closed++;if(throwClose) throw IOException("synthetic close failure") }
    }
    private fun wav(dataLength:Int):ByteArray {
        val out=ByteArray(44+dataLength)
        fun ascii(at:Int,text:String)=text.toByteArray().copyInto(out,at)
        fun u16(at:Int,n:Int) { out[at]=n.toByte();out[at+1]=(n ushr 8).toByte() }
        fun u32(at:Int,n:Int) { repeat(4) { out[at+it]=(n ushr (8*it)).toByte() } }
        ascii(0,"RIFF");u32(4,out.size-8);ascii(8,"WAVE");ascii(12,"fmt ");u32(16,16)
        u16(20,1);u16(22,1);u32(24,8000);u32(28,16000);u16(32,2);u16(34,16)
        ascii(36,"data");u32(40,dataLength)
        for(i in 44 until out.size) out[i]=(i%251).toByte()
        return out
    }
    private fun hash(bytes:ByteArray)=MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }
    private fun read(stream:InputStream,size:Long?,fileCap:Long,totalCap:Long)=SourceWavReader.read(stream,size,fileCap,totalCap)

    @Test fun smallCompleteStreamHashesExactlyReturnedBytesAndCloses() {
        val bytes=wav(100);val stream=CountedStream(bytes,7);val result=read(stream,bytes.size.toLong(),1000,1000)
        assertEquals(bytes.size.toLong(),result.bytesRead);assertEquals(hash(bytes),result.sha256)
        assertTrue(result.complete);assertEquals(1,stream.closed);assertEquals(bytes.size,stream.consumed)
    }
    @Test fun unknownExactFileBudgetDoesNotReadSentinelOrClaimCompleteHash() {
        val bytes=wav(100);val stream=CountedStream(bytes);val result=read(stream,null,bytes.size.toLong(),1000)
        assertEquals(bytes.size,stream.consumed);assertEquals(bytes.size.toLong(),result.bytesRead)
        assertFalse(result.complete);assertNull(result.sha256);assertEquals(1,stream.closed)
    }
    @Test fun knownExactBudgetStillCannotProveProviderEofFromSizeClaim() {
        val bytes=wav(100);val stream=CountedStream(bytes);val result=read(stream,bytes.size.toLong(),bytes.size.toLong(),1000)
        assertEquals(bytes.size,stream.consumed);assertFalse(result.complete);assertNull(result.sha256)
    }
    @Test fun remainingTotalBudgetDominatesLargerPerFileAllowance() {
        val stream=CountedStream(wav(1000),11);val result=read(stream,null,1000,63)
        assertEquals(63,stream.consumed);assertEquals(63L,result.bytesRead);assertNull(result.sha256)
        assertTrue(stream.requests.all { it<=63 });assertEquals(1,stream.closed)
    }
    @Test fun perFileBudgetDominatesLargerRemainingTotalAllowance() {
        val stream=CountedStream(wav(1000));val result=read(stream,null,31,1000)
        assertEquals(31,stream.consumed);assertEquals(31L,result.bytesRead);assertFalse(result.complete)
    }
    @Test fun knownOversizedProviderUsesAtMost64KiBProbe() {
        val stream=CountedStream(wav(200000));val result=read(stream,200044,150000,150000)
        assertTrue(stream.consumed<=65536);assertTrue(stream.consumed>0)
        assertEquals(stream.consumed.toLong(),result.bytesRead);assertFalse(result.complete);assertNull(result.sha256)
    }
    @Test fun zeroRemainingBudgetPerformsNoReadAndClosesInput() {
        val stream=CountedStream(wav(100));val result=read(stream,null,1000,0)
        assertEquals(0,stream.calls);assertEquals(0L,result.bytesRead);assertFalse(result.complete);assertNull(result.sha256);assertEquals(1,stream.closed)
    }
    @Test fun providerUnderstatesLengthButFullActualStreamStillHasExactHash() {
        val bytes=wav(100);val stream=CountedStream(bytes);val result=read(stream,1,1000,1000)
        assertEquals(bytes.size.toLong(),result.bytesRead);assertEquals(hash(bytes),result.sha256);assertTrue(result.complete)
        assertTrue(result.issues.any { it.code=="size_changed" })
    }
    @Test fun providerUnderstatesLengthCannotCauseReadBeyondBudget() {
        val stream=CountedStream(wav(1000));val result=read(stream,1,80,90)
        assertEquals(80,stream.consumed);assertEquals(80L,result.bytesRead);assertNull(result.sha256);assertFalse(result.complete)
    }
    @Test fun unknownLengthReachingEofBeforeBudgetCanHashFullFile() {
        val bytes=wav(100);val stream=CountedStream(bytes,1);val result=read(stream,null,1000,1000)
        assertTrue(result.complete);assertEquals(hash(bytes),result.sha256);assertEquals(bytes.size.toLong(),result.bytesRead)
    }
    @Test fun emptyStreamIsClosedAndCannotInventAudioDuration() {
        val stream=CountedStream(byteArrayOf());val result=read(stream,null,1000,1000)
        assertTrue(result.complete);assertEquals(hash(byteArrayOf()),result.sha256);assertEquals(0L,result.bytesRead);assertEquals(1,stream.closed)
        assertNull(result.wavHeader.declaredDurationSec)
    }
    @Test fun cancellationClosesInputWithoutContinuingOrReturningPartialSuccess() {
        val stream=CountedStream(wav(1000),7);var checks=0
        try {
            SourceWavReader.read(stream,null,1000,1000) { if(++checks==3) throw CancellationException("synthetic cancellation") }
            fail("Cancellation must propagate")
        } catch(_:CancellationException) { assertEquals(1,stream.closed);assertTrue(stream.consumed<1000) }
    }
    @Test fun zeroProgressFailsPromptlyWithoutInventingEofAndCloses() {
        val stream=CountedStream(wav(100)).apply { zeroReads=true };val result=read(stream,null,1000,1000)
        assertEquals(1,stream.calls);assertEquals(0L,result.bytesRead);assertFalse(result.complete);assertNull(result.sha256)
        assertTrue(result.issues.any { it.code=="read_failed" });assertEquals(1,stream.closed)
    }
    @Test fun partialReadThenIoFailureRetainsConsumedBudgetWithoutFullHash() {
        val stream=CountedStream(wav(100),7).apply { throwAfter=14 };val result=read(stream,null,1000,1000)
        assertEquals(14,stream.consumed);assertEquals(14L,result.bytesRead);assertFalse(result.complete);assertNull(result.sha256)
        assertTrue(result.issues.any { it.code=="read_failed" });assertEquals(1,stream.closed)
    }
    @Test fun partialReadThenZeroProgressRetainsConsumedBudget() {
        val stream=CountedStream(wav(100),7).apply { zeroAfter=14 };val result=read(stream,null,1000,1000)
        assertEquals(14L,result.bytesRead);assertFalse(result.complete);assertNull(result.sha256)
        assertTrue(result.issues.any { it.code=="read_failed" });assertEquals(1,stream.closed)
    }
    @Test fun closeFailureDoesNotConvertReadIntoCompleteSuccess() {
        val stream=CountedStream(wav(100)).apply { throwClose=true };val result=read(stream,null,1000,1000)
        assertEquals(144L,result.bytesRead);assertFalse(result.complete);assertNull(result.sha256)
        assertTrue(result.issues.any { it.code=="read_failed" });assertEquals(1,stream.closed)
    }
    @Test fun fullStreamBeyondPrefixStillHashesEntireFileAndRetainsAtMost64KiBHeader() {
        val bytes=wav(200000);val stream=CountedStream(bytes,701);val result=read(stream,null,300000,300000)
        assertEquals(bytes.size.toLong(),result.bytesRead);assertEquals(hash(bytes),result.sha256);assertTrue(result.complete)
        assertTrue(result.wavHeader.bytesInspected<=65536)
        assertFalse(result.wavHeader.bodyValidated)
    }
    @Test fun failureThenNextFileUsesOnlyRemainingActualBudget() {
        val firstStream=CountedStream(wav(100),7).apply { throwAfter=14 }
        val first=read(firstStream,null,1000,50)
        val secondStream=CountedStream(wav(100));val second=read(secondStream,null,1000,50-first.bytesRead)
        assertEquals(50L,first.bytesRead+second.bytesRead);assertEquals(50,firstStream.consumed+secondStream.consumed)
    }

    @Test fun overstatedProviderSizeCanStillReachActualEofAndHashSmallStream() {
        val bytes=wav(100);val stream=CountedStream(bytes);val result=read(stream,1000000,1000,1000)
        assertTrue(result.complete);assertEquals(hash(bytes),result.sha256);assertEquals(bytes.size.toLong(),result.bytesRead)
        assertTrue(result.issues.any { it.code=="size_changed" })
    }
    @Test fun cancellationBeforeFirstReadStillClosesAcquiredInput() {
        val stream=CountedStream(wav(100))
        try {
            SourceWavReader.read(stream,null,1000,1000) { throw CancellationException("cancel immediately") }
            fail("Cancellation must propagate")
        } catch(_:CancellationException) { assertEquals(0,stream.consumed);assertEquals(1,stream.closed) }
    }

    @Test fun actualEofOverridesRiffLengthClaimsBeyondRetainedPrefixWithoutLosingHash() {
        for(delta in listOf(-2,2)) {
            val bytes=wav(200000)
            val declaredRiff=bytes.size-8+delta
            val declaredData=bytes.size-44+delta
            repeat(4) { bytes[4+it]=(declaredRiff ushr (it*8)).toByte();bytes[40+it]=(declaredData ushr (it*8)).toByte() }
            val stream=CountedStream(bytes);val result=read(stream,null,300000,300000)
            assertTrue(result.complete);assertEquals(hash(bytes),result.sha256)
            assertEquals("size_mismatch",result.wavHeader.status);assertNull(result.wavHeader.declaredDurationSec)
            assertNull(result.wavHeader.durationBasis)
            assertTrue(result.wavHeader.issues.any { it.code=="wav_stream_size_mismatch" })
        }
    }

}

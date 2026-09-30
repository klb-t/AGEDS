package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import org.junit.Assert.*
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.concurrent.CancellationException

/** Independent minimal CFB/BIFF generator, no third-party spreadsheet writer. */
class SourceAdversarialXlsTest {
    private fun put16(b: ByteArray, p: Int, value: Int) { b[p] = value.toByte(); b[p+1] = (value shr 8).toByte() }
    private fun put32(b: ByteArray, p: Int, value: Int) { repeat(4) { b[p+it] = (value shr (it*8)).toByte() } }
    private fun record(id: Int, bytes: ByteArray) = ByteArray(4).also { put16(it,0,id); put16(it,2,bytes.size) } + bytes
    private fun bof(type: Int) = record(0x0809, ByteArray(16).also { put16(it,0,0x0600); put16(it,2,type) })
    private fun number(row: Int, col: Int) = record(0x0203, ByteArray(14).also {
        put16(it,0,row); put16(it,2,col); ByteBuffer.wrap(it,6,8).order(ByteOrder.LITTLE_ENDIAN).putDouble(12.5)
    })
    private fun compound(cells: ByteArray = number(0,0), tailPoison: Boolean = false): ByteArray {
        val sheet = bof(0x10) + cells + record(0x000a, byteArrayOf())
        val bound = ByteArray(9).also { put32(it,0,37); it[6]=1; it[8]='S'.code.toByte() }
        val stream = (bof(5) + record(0x0085,bound) + record(0x000a,byteArrayOf()) + sheet).copyOf(4096)
        if (tailPoison) stream[4095] = 1
        val out = ByteArray(5632)
        byteArrayOf(0xd0.toByte(),0xcf.toByte(),0x11,0xe0.toByte(),0xa1.toByte(),0xb1.toByte(),0x1a,0xe1.toByte()).copyInto(out)
        put16(out,24,0x003e); put16(out,26,3); put16(out,28,0xfffe); put16(out,30,9); put16(out,32,6)
        put32(out,44,1); put32(out,48,1); put32(out,56,4096); put32(out,60,-2); put32(out,68,-2)
        repeat(109) { put32(out,76+it*4,-1) }; put32(out,76,0)
        repeat(128) { put32(out,512+it*4,-1) }; put32(out,512,-3); put32(out,516,-2)
        for (sector in 2..9) put32(out,512+sector*4, if (sector == 9) -2 else sector+1)
        fun entry(offset: Int, name: String, type: Int, child: Int, start: Int, size: Int) {
            val encoded = (name + "\u0000").toByteArray(Charsets.UTF_16LE)
            encoded.copyInto(out,offset); put16(out,offset+64,encoded.size); out[offset+66]=type.toByte(); out[offset+67]=1
            put32(out,offset+68,-1); put32(out,offset+72,-1); put32(out,offset+76,child)
            put32(out,offset+116,start); put32(out,offset+120,size)
        }
        entry(1024,"Root Entry",5,1,-2,0); entry(1152,"Workbook",2,-1,2,4096)
        stream.copyInto(out,1536)
        return out
    }
    private fun parse(name: String, bytes: ByteArray, limits: SourceScanLimits = SourceScanLimits()): dev.klbt.ageds.core.ParsedSourceRows {
        val before = bytes.copyOf()
        println("N15_INPUT $name ${MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }} ${bytes.size}")
        val parsed = SourceXlsParser.parse(bytes,"synthetic:$name",limits)
        assertArrayEquals("Parser mutated source input",before,bytes)
        return parsed
    }
    @Test fun controlFixtureProjectsOneStoredNumber() {
        val parsed = parse("control",compound())
        assertEquals("12.5", parsed.rows.single().cells.single().value)
        assertFalse(parsed.issues.any { it.code == "invalid_xls" })
    }
    @Test(timeout=2000) fun workbookFatCycleIsRejected() {
        val bytes = compound().also { put32(it,512+2*4,2) }
        val parsed = parse("fat-cycle",bytes)
        assertTrue(parsed.rows.isEmpty()); assertTrue(parsed.issues.any { it.code == "invalid_xls" })
    }
    @Test(timeout=2000) fun directorySiblingCycleIsRejected() {
        val bytes = compound().also { put32(it,1152+68,1) }
        val parsed = parse("directory-cycle",bytes)
        assertTrue(parsed.rows.isEmpty()); assertTrue(parsed.issues.any { it.code == "invalid_xls" })
    }
    @Test fun workbookCannotOverlapDirectorySector() {
        val bytes = compound().also { put32(it,1152+116,1) }
        val parsed = parse("overlap",bytes)
        assertTrue(parsed.rows.isEmpty()); assertTrue(parsed.issues.any { it.code == "invalid_xls" })
    }
    @Test fun hugeUnsignedWorkbookLengthIsBoundedBeforeAllocation() {
        val bytes = compound().also { put32(it,1152+120,-1) }
        val parsed = parse("unsigned-length",bytes)
        assertTrue(parsed.rows.isEmpty()); assertTrue(parsed.issues.any { it.code == "expanded_bytes_limit" })
    }
    @Test fun biffRecordAboveMaximumIsRejected() {
        val bytes = compound().also { put16(it,1536+37+20+2,8225) }
        val parsed = parse("oversized-record",bytes)
        assertTrue(parsed.rows.isEmpty()); assertTrue(parsed.issues.any { it.code == "invalid_xls" })
    }
    @Test(timeout=2000) fun zeroRecordWithNonzeroTailIsRejected() {
        val parsed = parse("zero-tail-poison",compound(tailPoison=true))
        assertTrue(parsed.issues.any { it.code == "invalid_xls" })
    }
    @Test fun cellBudgetPreservesCompletedCellsAndReportsOmission() {
        val parsed = parse("cell-budget",compound(number(0,0)+number(0,1)),SourceScanLimits(maxCellsPerFile=1))
        assertEquals(1,parsed.rows.sumOf { it.cells.size }); assertTrue(parsed.issues.any { it.code == "cell_limit" })
    }
    @Test fun rowBudgetCannotLeakLastPendingRow() {
        val parsed = parse("row-budget",compound(number(0,0)+number(1,0)),SourceScanLimits(maxRowsPerFile=1))
        assertEquals(1,parsed.rows.size); assertTrue(parsed.issues.any { it.code == "row_limit" })
    }
    @Test fun repeatedMulrkPayloadCannotAmplifyRetainedTextBeyondBudget() {
        val payload = ByteArray(6 + 6*256).also { b ->
            put16(b,0,0); put16(b,2,0); put16(b,b.size-2,255)
            repeat(256) { put32(b,6 + it*6,42*4+2) }
        }
        val parsed = parse("mulrk-amplification",compound(record(0x00bd,payload)),SourceScanLimits(maxExpandedBytes=6000))
        val retained = parsed.rows.flatMap { it.cells }.sumOf { it.raw.length + it.value.length + (it.formula?.length ?: 0) }
        assertTrue(retained <= 6000)
        assertTrue(parsed.rows.sumOf { it.cells.size } in 1..255)
        assertTrue(parsed.issues.any { it.code == "projection_chars_limit" })
    }
    @Test fun invalidFormulaBooleanIsNotInventedAsTrue() {
        val payload = ByteArray(22).also { it[6]=1; it[8]=2; put16(it,12,0xffff) }
        val parsed = parse("invalid-formula-bool",compound(record(0x0006,payload)))
        assertTrue(parsed.rows.isEmpty())
        assertTrue(parsed.issues.any { it.code == "invalid_xls" })
    }
    @Test fun cancellationPropagatesWithoutPartialSuccess() {
        var calls = 0
        try {
            SourceXlsParser.parse(compound(),"synthetic",SourceScanLimits()) { if (++calls > 4) throw CancellationException("test") }
            fail("Cancellation should propagate")
        } catch (_: CancellationException) { assertTrue(calls > 4) }
    }
}

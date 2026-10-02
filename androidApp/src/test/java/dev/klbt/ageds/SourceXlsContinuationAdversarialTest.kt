package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import org.junit.Assert.*
import org.junit.Test

/** Independent SST payloads; only the pre-existing generic CFB envelope is shared. */
class SourceXlsContinuationAdversarialTest {
    private fun u16(n:Int)=byteArrayOf(n.toByte(),(n ushr 8).toByte())
    private fun u32(n:Int)=ByteArray(4) { (n ushr (8*it)).toByte() }
    private fun stringHeader(length:Int,wide:Boolean=false)=u16(length)+byteArrayOf(if(wide) 1 else 0)
    private fun compressed(s:String)=s.map { it.code.toByte() }.toByteArray()
    private fun wide(s:String)=s.toByteArray(Charsets.UTF_16LE)
    private fun parsed(parts:List<ByteArray>,count:Int=1,limits:SourceScanLimits=SourceScanLimits()):dev.klbt.ageds.core.ParsedSourceRows {
        val table=XlsFixture.record(0xfc,u32(count)+u32(count)+parts.first())+
            parts.drop(1).fold(byteArrayOf()) { all,p -> all+XlsFixture.record(0x3c,p) }
        val cells=(0 until count).fold(byteArrayOf()) { all,i -> all+XlsFixture.record(0xfd,u16(0)+u16(i)+u16(0)+u32(i)) }
        val bytes=XlsFixture.cfb(XlsFixture.workbook(cells,table));val before=bytes.copyOf()
        val result=SourceXlsParser.parse(bytes,"synthetic:independent-continuation",limits)
        assertArrayEquals("source bytes mutated",before,bytes)
        assertTrue("projection must remain explicitly partial",result.issues.any { it.code=="xls_projection" })
        return result
    }
    private fun values(parts:List<ByteArray>,count:Int=1):List<String> {
        val result=parsed(parts,count)
        assertFalse(result.issues.toString(),result.issues.any { it.code=="invalid_xls" || it.code=="xls_unresolved_shared_string" })
        return result.rows.flatMap { it.cells }.map { it.value }
    }
    private fun omitted(parts:List<ByteArray>,count:Int=1) {
        val result=parsed(parts,count)
        assertTrue("requires explicit omission diagnostic",result.issues.any { it.code!="xls_projection" && it.code!="xls_unresolved_shared_string" })
        val cells=result.rows.flatMap { it.cells }
        assertEquals((0 until count).map { it.toString() },cells.map { it.value })
        assertTrue(result.issues.any { it.code=="xls_unresolved_shared_string" })
        assertEquals("00000000000000000000",cells.first().raw)
    }

    @Test fun compressedWideCompressedTransitionsPreserveExactWhitespaceAndUnicode() {
        assertEquals(listOf(" Aąć \t"),values(listOf(stringHeader(6)+compressed(" A"),byteArrayOf(1)+wide("ąć"),byteArrayOf(0)+compressed(" \t"))))
    }
    @Test fun wideToCompressedTransitionDoesNotInterpretLowBytesAsUtf8() {
        assertEquals(listOf("ąéÿ"),values(listOf(stringHeader(3,true)+wide("ą"),byteArrayOf(0)+byteArrayOf(0xe9.toByte(),0xff.toByte()))))
    }
    @Test fun continuationAtCharacterStartConsumesFlagEvenWithNoPriorCharacters() {
        assertEquals(listOf("ą"),values(listOf(stringHeader(1),byteArrayOf(1)+wide("ą"))))
    }
    @Test fun completeStringBoundaryStartsNewHeaderWithoutCompressionPrefix() {
        assertEquals(listOf("A","ąć"),values(listOf(stringHeader(1)+compressed("A"),stringHeader(2,true)+wide("ąć")),2))
    }
    @Test fun emptyStringBoundaryCannotStealNextHeaderByteAsFlag() {
        assertEquals(listOf("","B"),values(listOf(stringHeader(0),stringHeader(1)+compressed("B")),2))
    }
    @Test fun richMetadataWithinFinalRecordIsSkippedWithoutChangingFollowingText() {
        val header=u16(2)+byteArrayOf(8)+u16(1)
        val tail=u16(0)+u16(3)
        assertEquals(listOf("AB","C"),values(listOf(header+compressed("A"),byteArrayOf(0)+compressed("B")+tail+stringHeader(1)+compressed("C")),2))
    }
    @Test fun splitUtf16CodeUnitIsOmittedRatherThanRepaired() {
        omitted(listOf(stringHeader(1,true)+byteArrayOf(0x05),byteArrayOf(1,0x01)))
    }
    @Test fun truncatedFinalCharacterDoesNotPublishDecodedPrefix() {
        omitted(listOf(stringHeader(4)+compressed("AB"),byteArrayOf(0)+compressed("C")))
    }
    @Test fun missingOrInvalidContinuationCompressionFlagIsNotText() {
        for(flag in listOf(2,8,255)) omitted(listOf(stringHeader(2)+compressed("A"),byteArrayOf(flag.toByte())+compressed("B")))
        omitted(listOf(stringHeader(2)+compressed("A"),byteArrayOf()))
    }
    @Test fun fixedHeaderCannotCrossRecordBoundary() {
        val full=u16(2)+byteArrayOf(12)+u16(1)+u32(0)
        for(split in 1 until full.size) omitted(listOf(full.copyOfRange(0,split),full.copyOfRange(split,full.size)+compressed("AB")+ByteArray(4)))
    }
    @Test fun richTailAcrossRecordsHasExplicitOmissionInsteadOfGuessedBytes() {
        omitted(listOf(u16(1)+byteArrayOf(8)+u16(1)+compressed("A")+byteArrayOf(0,0),byteArrayOf(0,0)))
    }
    @Test fun malformedLaterStringCannotLeaveEarlierSstEntryTrusted() {
        omitted(listOf(stringHeader(1)+compressed("A")+stringHeader(3)+compressed("B"),byteArrayOf(0)+compressed("C")),2)
    }
    @Test fun declaredStringLengthBudgetIsCheckedBeforeMaterialization() {
        val result=parsed(listOf(stringHeader(100)+compressed("A"),byteArrayOf(0)+compressed("B")),limits=SourceScanLimits(maxCellChars=32))
        assertTrue(result.issues.any { it.code=="cell_chars_limit" })
        assertFalse(result.rows.flatMap { it.cells }.any { it.value=="AB" })
    }
    @Test fun continuedTextKeepsRawLabelIndexAndRecordLocator() {
        val result=parsed(listOf(stringHeader(2)+compressed("A"),byteArrayOf(0)+compressed("B")))
        val cell=result.rows.single().cells.single()
        assertEquals("AB",cell.value);assertEquals("00000000000000000000",cell.raw)
        assertTrue(cell.sourceReference!!.startsWith("A1;stream=Workbook;offset="))
    }
    @Test fun surrogatePairSplitBetweenCompleteCodeUnitsIsPreserved() {
        assertEquals(listOf("🐈"),values(listOf(stringHeader(2,true)+byteArrayOf(0x3d,0xd8.toByte()),byteArrayOf(1,0x08,0xdc.toByte()))))
    }

    @Test fun declaredSharedStringCountIsBoundedBeforeDecoding() {
        val result=parsed(listOf(stringHeader(1)+compressed("A"),stringHeader(1)+compressed("B")),count=2,limits=SourceScanLimits(maxCellsPerFile=1))
        assertTrue(result.issues.any { it.code=="shared_strings_limit" });assertTrue(result.rows.isEmpty())
    }
    @Test fun cumulativeDecodedCharacterBudgetPrecedesIncompleteNextString() {
        val result=parsed(listOf(stringHeader(3000)+ByteArray(3000) { 65 },stringHeader(3000)+compressed("B")),count=2,limits=SourceScanLimits(maxExpandedBytes=4608,maxCellChars=6000))
        assertTrue(result.issues.toString(),result.issues.any { it.code=="shared_string_chars_limit" })
        assertTrue(result.rows.isEmpty())
    }

}

package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.CancellationException

class SourceDelimitedFormatsTest {
    private val limits = SourceScanLimits()
    private fun parse(text: String) = SourceDelimitedParser.parse(text.toByteArray(), "u", limits)

    @Test fun bomUtf16BothOrdersPreserveLexicalFields() {
        for ((charset, bom) in listOf(Charsets.UTF_16LE to byteArrayOf(-1, -2), Charsets.UTF_16BE to byteArrayOf(-2, -1))) {
            val result = SourceDelimitedParser.parse(bom + "name;value\r\n\"Zażółć\";007".toByteArray(charset), "u", limits)
            assertEquals("\"Zażółć\"", result.rows[1].cells[0].raw)
            assertEquals("007", result.rows[1].cells[1].value)
            assertEquals(charset.name(), result.textFormat!!.encoding)
            assertEquals(2, result.textFormat!!.bomBytes)
            assertEquals(";", result.textFormat!!.delimiter)
            assertTrue(result.issues.isEmpty())
        }
    }
    @Test fun utf8BomIsTransportOnlyAndUtf8DefaultIsExplicit() {
        val bom = byteArrayOf(-17, -69, -65)
        val result = SourceDelimitedParser.parse(bom + "a,b\nx,y".toByteArray(), "u", limits)
        assertEquals("a", result.rows[0].cells[0].raw)
        assertEquals("bom", result.textFormat!!.encodingBasis)
        assertEquals("strict_utf8_default", parse("a,b").textFormat!!.encodingBasis)
    }
    @Test fun invalidBytesAreNotReplacedOrGuessed() {
        for (bytes in listOf(byteArrayOf(-61, 40), byteArrayOf(-1, -2, 65), byteArrayOf(-2, -1, -40, 0),
                byteArrayOf(-1, -2, 0, 0, 65, 0, 0, 0), "a,b".toByteArray(Charsets.UTF_16LE))) {
            val result = SourceDelimitedParser.parse(bytes, "u", limits)
            assertTrue(result.rows.isEmpty())
            assertEquals("unsupported_text_encoding", result.issues.single().code)
        }
    }
    @Test fun separatorsInsideQuotesDoNotVoteForWrongDialect() {
        val result = parse("name;value\n\"x,y|z\";\"a\nb\"")
        assertEquals(";", result.textFormat!!.delimiter)
        assertEquals("\"x,y|z\"", result.rows[1].cells[0].raw)
        assertEquals("a\nb", result.rows[1].cells[1].value)
        assertFalse(result.textFormat!!.delimiterAmbiguous)
    }
    @Test fun pipeAndTabAreSupportedWithoutNumericNormalization() {
        for (separator in listOf('|', '\t')) {
            val result = parse("number${separator}date\n+001${separator}not-a-date")
            assertEquals(separator.toString(), result.textFormat!!.delimiter)
            assertEquals("+001", result.rows[1].cells[0].value)
        }
    }
    @Test fun ambiguousDialectsKeepCandidatesAndProvisionalProjection() {
        val result = parse("a,b;c\nd,e;f")
        assertEquals(listOf(",", ";"), result.textFormat!!.delimiterCandidates)
        assertTrue(result.textFormat!!.delimiterAmbiguous)
        assertEquals("provisional_comma_default", result.textFormat!!.delimiterBasis)
        assertEquals("b;c", result.rows[0].cells[1].raw)
        assertEquals("delimiter_ambiguous", result.issues.single().code)
    }
    @Test fun noEvidenceAndMalformedQuotesRemainExplicit() {
        assertEquals("delimiter_undetermined", parse("one\ntwo").issues.single().code)
        val malformed = parse("a,b\n\"unclosed,c")
        assertTrue(malformed.issues.any { it.code == "malformed_csv" })
        assertEquals("\"unclosed,c", malformed.rows[1].cells.single().raw)
    }
    @Test fun tsvIsExplicitEvenWhenCommaLooksPlausible() {
        val result = SourceDelimitedParser.parse("a,b\tc,d".toByteArray(), "u", limits, tsv = true)
        assertEquals("tsv_extension", result.textFormat!!.delimiterBasis)
        assertFalse(result.textFormat!!.delimiterAmbiguous)
        assertEquals(listOf("a,b", "c,d"), result.rows.single().cells.map { it.raw })
    }
    @Test fun parsingLimitsAndInferenceSampleAreDeclared() {
        val result = SourceDelimitedParser.parse("a;b\n".repeat(40).toByteArray(), "u", limits.copy(maxRowsPerFile = 2))
        assertEquals(2, result.rows.size)
        assertTrue(result.issues.any { it.code == "row_limit" })
        assertTrue(result.textFormat!!.sampleTruncated)
    }
    @Test fun cancellationPropagatesFromInferenceAndParsing() {
        try {
            SourceDelimitedParser.parse("a;b\nc;d".toByteArray(), "u", limits) { throw CancellationException("cancel") }
            fail("Cancellation must propagate")
        } catch (_: CancellationException) { }
    }
}

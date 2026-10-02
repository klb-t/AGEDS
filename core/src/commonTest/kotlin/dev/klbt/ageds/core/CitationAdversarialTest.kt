package dev.klbt.ageds.core

import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

/** Independent fixtures: neither whitespace nor ASR errors are repaired. */
class CitationAdversarialTest {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private fun transcript(segments: String, report: String = "" ): Transcript = json.decodeFromString(
        """{"id":41,"artifactId":7,"text":"unrelated full text","segments":[$segments]$report}"""
    )
    private val first = """{"start":1.0,"end":3.0,"text":" A\tB","words":[{"start":1.0,"end":1.2,"word":" A"},{"start":2.0,"end":3.0,"word":"\tB"}]}"""
    private val second = """{"start":4.0,"end":5.0,"text":" é\n","words":[{"start":4.0,"end":5.0,"word":" é\n"}]}"""
    private val report = """, "wordTiming":{"status":"available","segments":[{"segment_index":0,"word_selection_available":true,"word_count":2},{"segment_index":1,"word_selection_available":true,"word_count":1}]}"""

    @Test fun rawSegmentQuoteAndVersionAreExact() {
        val selected = CitationSelection.segments(transcript("$first,$second"), listOf(0, 1))
        assertEquals(" A\tB é\n", selected.quoteText)
        assertEquals(41L, selected.derivedTextId)
        assertEquals(7L, selected.artifactId)
        assertEquals(1000L, selected.startMs)
        assertEquals(5000L, selected.endMs)
        assertEquals(listOf(0, 1), selected.request.segmentIndices)
        assertEquals(selected.quoteText, selected.request.quoteText)
    }

    @Test fun segmentOrderDuplicatesAndHolesAreRejected() {
        val t = transcript("$first,$second,$second")
        for (indices in listOf(emptyList(), listOf(-1), listOf(3), listOf(0,0), listOf(1,0), listOf(0,2))) {
            assertFailsWith<IllegalArgumentException>(indices.toString()) { CitationSelection.segments(t, indices) }
        }
    }

    @Test fun segmentOverlapRejectedButGapPreserved() {
        val overlapping = second.replace("4.0", "2.9")
        assertFailsWith<IllegalArgumentException> { CitationSelection.segments(transcript("$first,$overlapping"), listOf(0,1)) }
        assertEquals(5000L, CitationSelection.segments(transcript("$first,$second"), listOf(0,1)).endMs)
    }

    @Test fun wordSelectionRetainsGapsWhitespaceAndExactVersion() {
        val selected = CitationSelection.words(transcript("$first,$second", report), listOf(WordRef(0,1), WordRef(1,0)))
        assertEquals("\tB é\n", selected.quoteText)
        assertEquals(2000L, selected.startMs)
        assertEquals(5000L, selected.endMs)
        assertEquals(41L, selected.request.derivedTextId)
        assertEquals(listOf(WordRef(0,1), WordRef(1,0)), selected.request.wordRefs)
    }

    @Test fun wordSelectionRejectsMissingAvailabilityButSegmentsRemainAvailable() {
        val t = transcript(first)
        assertFailsWith<IllegalArgumentException> { CitationSelection.words(t, listOf(WordRef(0,0))) }
        assertEquals(" A\tB", CitationSelection.segments(t, listOf(0)).quoteText)
    }

    @Test fun reportedAvailabilityCannotOverrideCorruptUnselectedWord() {
        for (corrupt in listOf(first.replace("\"end\":3.0,\"word\"", "\"end\":4.0,\"word\""), first.replace("\\tB\"}", " B\"}"))) {
            val t = transcript(corrupt, report)
            assertFailsWith<IllegalArgumentException> { CitationSelection.words(t, listOf(WordRef(0,0))) }
            assertEquals(" A\tB", CitationSelection.segments(t, listOf(0)).quoteText)
        }
    }

    @Test fun wordReferencesMustBeContiguousUniqueAndOrdered() {
        val t = transcript("$first,$second", report)
        for (refs in listOf(emptyList(), listOf(WordRef(-1,0)), listOf(WordRef(0,-1)), listOf(WordRef(0,2)), listOf(WordRef(0,0),WordRef(1,0)), listOf(WordRef(0,0),WordRef(0,0)), listOf(WordRef(0,1),WordRef(0,0)))) {
            assertFailsWith<IllegalArgumentException>(refs.toString()) { CitationSelection.words(t, refs) }
        }
    }

    @Test fun wordOverlapAndOutOfBoundsAreRejected() {
        for (corrupt in listOf(first.replace("2.0", "1.1"), first.replace("\"start\":1.0,\"end\":1.2", "\"start\":0.9,\"end\":1.2"))) {
            assertFailsWith<IllegalArgumentException> { CitationSelection.words(transcript(corrupt, report), listOf(WordRef(0,0))) }
        }
    }

    @Test fun negativeNonFiniteAndOverflowTimesNeverBecomeValidSelections() {
        for (time in listOf(-1.0, Double.NaN, Double.POSITIVE_INFINITY, 9223372036854776.0)) {
            val t = Transcript(41, 7, text="x", segments=listOf(TranscriptSegment(time,time,"x")))
            assertFailsWith<IllegalArgumentException>(time.toString()) { CitationSelection.segments(t,listOf(0)) }
        }
    }

    @Test fun nearestMillisecondUsesTiesToEven() {
        val t = Transcript(41,7,text="x",segments=listOf(TranscriptSegment(0.0005,0.0015,"x")))
        val selected = CitationSelection.segments(t,listOf(0))
        assertEquals(0L,selected.startMs)
        assertEquals(2L,selected.endMs)
    }

    @Test fun switchingTranscriptCreatesNewPinnedSelectionWithoutChangingOldOne() {
        val old = transcript(first)
        val previous = CitationSelection.segments(old,listOf(0))
        val next = CitationSelection.segments(old.copy(id=42, segments=listOf(TranscriptSegment(1.0,3.0,"corrected"))),listOf(0))
        assertEquals(41L,previous.request.derivedTextId)
        assertEquals(" A\tB",previous.request.quoteText)
        assertEquals(42L,next.request.derivedTextId)
        assertEquals("corrected",next.request.quoteText)
    }
    @Test fun wireBooleanTimesAreNeverCoercedToNumbers() {
        for (raw in listOf("true", "false", "null", "NaN", "Infinity", "1e999")) {
            kotlin.test.assertFails(raw) {
                val t = transcript("""{"start":$raw,"end":3,"text":"x"}""")
                CitationSelection.segments(t,listOf(0))
            }
        }
    }

    @Test fun selectionSnapshotsDoNotAliasCallerLists() {
        val indices = mutableListOf(0,1)
        val selected = CitationSelection.segments(transcript("$first,$second"),indices)
        indices.clear()
        assertEquals(listOf(0,1),selected.request.segmentIndices)
    }

}

package dev.klbt.ageds.core

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import kotlin.test.assertTrue

class CitationSelectionTest {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private fun transcript() = Transcript(7, 3, text = "Zażółć  gęślą!", segments = listOf(
        TranscriptSegment(0.0, 1.0, "Zażółć", listOf(TranscriptWord(0.0, 1.0, "Zażółć"))),
        TranscriptSegment(1.0, 2.5, "  gęślą!", listOf(TranscriptWord(1.0, 2.0, "  gęślą"), TranscriptWord(2.0, 2.5, "!")))
    ), wordTiming = WordTiming(status = "available", segments = listOf(WordTimingSegment(0, true), WordTimingSegment(1, true))))

    @Test fun segmentPreviewPinsIdentityAndExactQuote() {
        val result = CitationSelection.segments(transcript(), listOf(0, 1))
        assertEquals(7L, result.derivedTextId)
        assertEquals(3L, result.artifactId)
        assertEquals("Zażółć  gęślą!", result.quoteText)
        assertEquals(2500L, result.endMs)
        assertEquals("segment", result.precision)
        assertEquals(result.quoteText, result.request.quoteText)
    }

    @Test fun wordPreviewCanCrossAdjacentSegments() {
        val result = CitationSelection.words(transcript(), listOf(WordRef(0, 0), WordRef(1, 0)))
        assertEquals("Zażółć  gęślą", result.quoteText)
        assertEquals(2000L, result.endMs)
        assertEquals("word_asr", result.precision)
        assertNull(result.request.segmentIndices)
    }

    @Test fun callerCannotMutatePinnedRequest() {
        val indices = mutableListOf(0, 1)
        val result = CitationSelection.segments(transcript(), indices)
        indices.clear()
        val outgoing = result.request.segmentIndices
        (outgoing as? MutableList<Int>)?.clear()
        assertEquals(listOf(0, 1), result.request.segmentIndices)
        assertEquals(7L, result.request.derivedTextId)
    }

    @Test fun oldTranscriptStillAllowsSegmentsButNeverInventsWordAvailability() {
        val old = json.decodeFromString<Transcript>("""{"id":7,"artifactId":3,"text":"x","segments":[{"start":0,"end":1,"text":"x"}]}""")
        assertNull(old.wordTiming)
        assertEquals("x", CitationSelection.segments(old, listOf(0)).quoteText)
        assertFailsWith<IllegalArgumentException> { CitationSelection.words(old, listOf(WordRef(0, 0))) }
    }

    @Test fun httpCasingMatchesVersionResponseAndCitationRequest() {
        val version = json.decodeFromString<TranscriptVersion>("""{"id":7,"artifact_id":3,"run_id":2,"model":"tiny","language":"pl","created_at":"now"}""")
        assertEquals(3L, version.artifactId)
        assertEquals(2L, version.runId)
        val request = json.encodeToString(CitationSelection.words(transcript(), listOf(WordRef(1, 0))).request)
        assertTrue(request.contains("\"derivedTextId\":7"))
        assertTrue(request.contains("\"segment_index\":1"))
        assertTrue(request.contains("\"word_index\":0"))
        assertTrue(request.contains("\"quoteText\":\"  gęślą\""))
    }

    @Test fun segmentSelectionMatchesHttpCountBoundary() {
        val segments = List(10_001) { i -> TranscriptSegment(i.toDouble(), i + 1.0, "x") }
        val transcript = Transcript(7, 3, text = "x".repeat(10_001), segments = segments)
        val permitted = CitationSelection.segments(transcript, (0 until 10_000).toList())
        assertEquals(10_000, permitted.request.segmentIndices?.size)
        assertEquals(10_000, permitted.quoteText.length)
        assertEquals(10_000_000L, permitted.endMs)
        assertFailsWith<IllegalArgumentException> {
            CitationSelection.segments(transcript, (0 until 10_001).toList())
        }
    }

    @Test fun millisecondsUseFiniteRangeAndNearestEvenRounding() {
        assertEquals(0L, CitationSelection.milliseconds(0.0005))
        assertEquals(2L, CitationSelection.milliseconds(0.0015))
        for (time in listOf(Double.NaN, Double.POSITIVE_INFINITY, -1.0, 9223372036854776.0)) {
            assertFailsWith<IllegalArgumentException> { CitationSelection.milliseconds(time) }
        }
    }
}

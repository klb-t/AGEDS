package dev.klbt.ageds

import dev.klbt.ageds.core.*
import org.junit.Assert.*
import org.junit.Test

class CitationDisplayProjectionTest {
    @Test fun truncatedDisplayPreservesRawTextAndOriginalCrossSegmentIndices() {
        val first = List(5_000) { TranscriptWord(0.0, 1.0, " A ") }
        val second = List(5_002) { TranscriptWord(1.0, 2.0, "  Żółć\n") }
        val result = CitationDisplayProjection.words(Transcript(1, 2, text = "raw",
            segments = listOf(TranscriptSegment(0.0, 1.0, "raw", first), TranscriptSegment(1.0, 2.0, "raw", second))))
        assertEquals(10_000, result.words.size)
        assertTrue(result.truncated)
        assertEquals(WordRef(0, 4_999), result.words[4_999].first)
        assertEquals(WordRef(1, 4_999), result.words.last().first)
        assertEquals("  Żółć\n", result.words.last().second)
    }

    @Test fun hugeLazyWordSourceIsNotTraversedBeyondPrefixAndSentinel() {
        var observed = 0
        val words = object : AbstractList<TranscriptWord>() {
            override val size = 1_000_000
            override fun get(index: Int): TranscriptWord {
                check(index <= 10_000) { "Display traversed outside bounded prefix" }
                observed++
                return TranscriptWord(0.0, 1.0, " exact ")
            }
        }
        val result = CitationDisplayProjection.words(Transcript(1, 2, text = "raw",
            segments = listOf(TranscriptSegment(0.0, 1.0, "raw", words))))
        assertTrue(result.truncated)
        assertEquals(10_001, observed)
        assertEquals(10_000, result.words.size)
        assertFalse(CitationDisplayProjection.words(null).truncated)
    }
}

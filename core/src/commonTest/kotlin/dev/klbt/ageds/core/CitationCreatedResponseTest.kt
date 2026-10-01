package dev.klbt.ageds.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class CitationCreatedResponseTest {
    private val json = Json

    private fun segmentTranscript() = Transcript(
        id = 17,
        artifactId = 12,
        text = "yesyes",
        segments = listOf(
            TranscriptSegment(0.0, 0.5, "yes"),
            TranscriptSegment(10.0, 10.5, "yes"),
        ),
    )

    private fun wordTranscript() = Transcript(
        id = 17,
        artifactId = 12,
        text = " A B",
        segments = listOf(TranscriptSegment(
            start = 0.0,
            end = 1.0015,
            text = " A B",
            words = listOf(
                TranscriptWord(0.0, 0.0005, " A"),
                TranscriptWord(1.0005, 1.0015, " B"),
            ),
        )),
        wordTiming = WordTiming(
            status = "available",
            segments = listOf(WordTimingSegment(0, true)),
        ),
    )

    private fun selector(raw: String): JsonObject = json.parseToJsonElement(raw).jsonObject

    private fun saved(selected: SelectedCitation, selector: JsonObject, id: Long = 91) = Citation(
        id = id,
        artifactId = selected.artifactId,
        derivedTextId = selected.derivedTextId,
        startMs = selected.startMs,
        endMs = selected.endMs,
        quoteText = selected.quoteText,
        quoteSha256 = "server-owned-fixture",
        selector = selector,
    )

    @Test fun exactSegmentProjectionAcceptsNumericSpellingAndObjectKeyOrder() {
        val selected = CitationSelection.segments(segmentTranscript(), listOf(0))
        val response = saved(selected, selector("""{
            "precision":"segment","rounding":"nearest_ms","stored_time_unit":"milliseconds",
            "time_unit":"seconds","text_join":"concatenate_exact","indices":[0.0],"kind":"segments"
        }"""))

        assertEquals(response, selected.requireMatchingCreated(response))
    }

    @Test fun repeatedTextAndRangeCannotHideAnotherSegmentOccurrence() {
        val selected = CitationSelection.segments(segmentTranscript(), listOf(0))
        val otherOccurrence = selector("""{
            "kind":"segments","indices":[1],"text_join":"concatenate_exact","time_unit":"seconds",
            "stored_time_unit":"milliseconds","rounding":"nearest_ms","precision":"segment"
        }""")

        assertFailsWith<IllegalArgumentException> {
            selected.requireMatchingCreated(saved(selected, otherOccurrence))
        }
    }

    @Test fun selectorIsExactAndNeverCoercesStringsOrBooleansToNumbers() {
        val selected = CitationSelection.segments(segmentTranscript(), listOf(0))
        val variants = listOf(
            """{"kind":"segments","indices":["0"],"text_join":"concatenate_exact","time_unit":"seconds","stored_time_unit":"milliseconds","rounding":"nearest_ms","precision":"segment"}""",
            """{"kind":"segments","indices":[false],"text_join":"concatenate_exact","time_unit":"seconds","stored_time_unit":"milliseconds","rounding":"nearest_ms","precision":"segment"}""",
            """{"kind":"segments","indices":[9007199254740993],"text_join":"concatenate_exact","time_unit":"seconds","stored_time_unit":"milliseconds","rounding":"nearest_ms","precision":"segment"}""",
            """{"kind":"segments","indices":[0],"text_join":"concatenate_exact","time_unit":"seconds","stored_time_unit":"milliseconds","rounding":"nearest_ms"}""",
            """{"kind":"segments","indices":[0],"text_join":"concatenate_exact","time_unit":"seconds","stored_time_unit":"milliseconds","rounding":"nearest_ms","precision":"segment","extra":0}""",
        )

        variants.forEach { raw ->
            assertFailsWith<IllegalArgumentException>(raw) {
                selected.requireMatchingCreated(saved(selected, selector(raw)))
            }
        }
    }

    @Test fun exactWordProjectionIncludesOrderedRefsRawBoundsAndCanonicalMetadata() {
        val selected = CitationSelection.words(wordTranscript(), listOf(WordRef(0, 0), WordRef(0, 1)))
        val exact = selector("""{
            "kind":"words","word_refs":[{"word_index":0.0,"segment_index":0},{"segment_index":0.0,"word_index":1}],
            "text_join":"concatenate_exact","time_unit":"seconds","stored_time_unit":"milliseconds",
            "rounding":"nearest_ms","precision":"word_asr","source_start":0.0,"source_end":1.0015,
            "alignment_verification":"not_performed"
        }""")
        assertEquals(0L, selected.startMs)
        assertEquals(1002L, selected.endMs)
        assertEquals(91L, selected.requireMatchingCreated(saved(selected, exact)).id)

        val reversed = selector("""{
            "kind":"words","word_refs":[{"segment_index":0,"word_index":1},{"segment_index":0,"word_index":0}],
            "text_join":"concatenate_exact","time_unit":"seconds","stored_time_unit":"milliseconds",
            "rounding":"nearest_ms","precision":"word_asr","source_start":0,"source_end":1.0015,
            "alignment_verification":"not_performed"
        }""")
        assertFailsWith<IllegalArgumentException> {
            selected.requireMatchingCreated(saved(selected, reversed))
        }
        for (changed in listOf(
            JsonObject(exact + ("source_start" to JsonPrimitive(0.0005))),
            JsonObject(exact + ("source_end" to JsonPrimitive(1.0015001))),
            JsonObject(exact + ("alignment_verification" to JsonPrimitive("performed"))),
            JsonObject(exact - "source_end"),
        )) {
            assertFailsWith<IllegalArgumentException> {
                selected.requireMatchingCreated(saved(selected, changed))
            }
        }
    }

    @Test fun responseIdentityRangeQuoteAndPositiveIdAreAllRequired() {
        val selected = CitationSelection.segments(segmentTranscript(), listOf(0))
        val exact = selector("""{
            "kind":"segments","indices":[0],"text_join":"concatenate_exact","time_unit":"seconds",
            "stored_time_unit":"milliseconds","rounding":"nearest_ms","precision":"segment"
        }""")
        val valid = saved(selected, exact)
        val variants = listOf(
            valid.copy(id = 0),
            valid.copy(artifactId = 13),
            valid.copy(derivedTextId = 18),
            valid.copy(quoteText = "yes "),
            valid.copy(startMs = 1),
            valid.copy(endMs = 501),
        )

        variants.forEach { response ->
            assertFailsWith<IllegalArgumentException> { selected.requireMatchingCreated(response) }
        }
    }

    @Test fun expectedProjectionDoesNotAliasCallerLists() {
        val indices = mutableListOf(0)
        val selected = CitationSelection.segments(segmentTranscript(), indices)
        indices[0] = 1
        val firstOccurrence = selector("""{
            "kind":"segments","indices":[0],"text_join":"concatenate_exact","time_unit":"seconds",
            "stored_time_unit":"milliseconds","rounding":"nearest_ms","precision":"segment"
        }""")

        assertEquals(listOf(0), selected.request.segmentIndices)
        selected.requireMatchingCreated(saved(selected, firstOccurrence))
    }

    @Test fun rawWordBoundsAcceptEquivalentPythonAndJvmShortestDoubleSpellings() {
        val tiny = Double.MIN_VALUE
        val transcript = Transcript(
            id = 17,
            artifactId = 12,
            text = "x",
            segments = listOf(TranscriptSegment(
                start = tiny,
                end = tiny * 2,
                text = "x",
                words = listOf(TranscriptWord(tiny, tiny * 2, "x")),
            )),
            wordTiming = WordTiming(
                status = "available",
                segments = listOf(WordTimingSegment(0, true)),
            ),
        )
        val selected = CitationSelection.words(transcript, listOf(WordRef(0, 0)))
        val pythonSpelling = selector("""{
            "kind":"words","word_refs":[{"segment_index":0,"word_index":0}],
            "text_join":"concatenate_exact","time_unit":"seconds","stored_time_unit":"milliseconds",
            "rounding":"nearest_ms","precision":"word_asr","source_start":5e-324,"source_end":1e-323,
            "alignment_verification":"not_performed"
        }""")

        assertEquals(0L, selected.startMs)
        assertEquals(0L, selected.endMs)
        selected.requireMatchingCreated(saved(selected, pythonSpelling))
    }
}

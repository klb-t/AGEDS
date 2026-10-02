package dev.klbt.ageds.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

/** Independent response fixtures: equal text and rounded ranges do not identify an occurrence. */
class CitationResponseAdversarialTest {
    private val json = Json

    private fun selector(raw: String): JsonObject = json.parseToJsonElement(raw).jsonObject

    private fun segmentTranscript() = Transcript(
        id = 61,
        artifactId = 23,
        text = "xx",
        segments = listOf(
            TranscriptSegment(0.0001, 0.0002, "x"),
            TranscriptSegment(0.0003, 0.0004, "x"),
        ),
    )

    private fun wordTranscript() = Transcript(
        id = 61,
        artifactId = 23,
        text = "xx",
        segments = listOf(TranscriptSegment(
            start = 0.0001,
            end = 0.0004,
            text = "xx",
            words = listOf(
                TranscriptWord(0.0001, 0.0002, "x"),
                TranscriptWord(0.0003, 0.0004, "x"),
            ),
        )),
        wordTiming = WordTiming(
            status = "available",
            segments = listOf(WordTimingSegment(0, true)),
        ),
    )

    private fun segmentSelector(indices: String = "[0]"): JsonObject = selector("""{
        "kind":"segments","indices":$indices,"text_join":"concatenate_exact",
        "time_unit":"seconds","stored_time_unit":"milliseconds",
        "rounding":"nearest_ms","precision":"segment"
    }""")

    private fun wordSelector(
        refs: String = """[{"segment_index":0,"word_index":0}]""",
        sourceStart: String = "0.0001",
        sourceEnd: String = "0.0002",
    ): JsonObject = selector("""{
        "kind":"words","word_refs":$refs,"text_join":"concatenate_exact",
        "time_unit":"seconds","stored_time_unit":"milliseconds",
        "rounding":"nearest_ms","precision":"word_asr",
        "source_start":$sourceStart,"source_end":$sourceEnd,
        "alignment_verification":"not_performed"
    }""")

    private fun saved(selected: SelectedCitation, responseSelector: JsonObject, id: Long = 101) = Citation(
        id = id,
        artifactId = selected.artifactId,
        derivedTextId = selected.derivedTextId,
        startMs = selected.startMs,
        endMs = selected.endMs,
        quoteText = selected.quoteText,
        quoteSha256 = "server-owned-not-an-occurrence-identifier",
        selector = responseSelector,
    )

    @Test fun sameSegmentTextAndSameRoundedRangeCannotSubstituteAnotherOccurrence() {
        val selected = CitationSelection.segments(segmentTranscript(), listOf(0))
        assertEquals(0L, selected.startMs)
        assertEquals(0L, selected.endMs)

        assertFailsWith<IllegalArgumentException> {
            selected.requireMatchingCreated(saved(selected, segmentSelector("[1]")))
        }
    }

    @Test fun sameWordTextAndSameRoundedRangeCannotSubstituteAnotherReference() {
        val selected = CitationSelection.words(wordTranscript(), listOf(WordRef(0, 0)))
        assertEquals("x", selected.quoteText)
        assertEquals(0L, selected.startMs)
        assertEquals(0L, selected.endMs)

        val otherOccurrence = wordSelector(
            refs = """[{"segment_index":0,"word_index":1}]""",
            sourceStart = "0.0003",
            sourceEnd = "0.0004",
        )
        assertFailsWith<IllegalArgumentException> {
            selected.requireMatchingCreated(saved(selected, otherOccurrence))
        }
    }

    @Test fun exactSegmentAndWordResponsesAreAccepted() {
        val segment = CitationSelection.segments(segmentTranscript(), listOf(0))
        val segmentResponse = saved(segment, selector("""{
            "precision":"segment","rounding":"nearest_ms","stored_time_unit":"milliseconds",
            "time_unit":"seconds","text_join":"concatenate_exact","indices":[0.0],"kind":"segments"
        }"""))
        assertEquals(segmentResponse, segment.requireMatchingCreated(segmentResponse))

        val word = CitationSelection.words(wordTranscript(), listOf(WordRef(0, 0)))
        val wordResponse = saved(word, selector("""{
            "alignment_verification":"not_performed","source_end":0.0002000,"source_start":1e-4,
            "precision":"word_asr","rounding":"nearest_ms","stored_time_unit":"milliseconds",
            "time_unit":"seconds","text_join":"concatenate_exact",
            "word_refs":[{"word_index":0.0,"segment_index":0e0}],"kind":"words"
        }"""))
        assertEquals(wordResponse, word.requireMatchingCreated(wordResponse))
    }

    @Test fun selectorObjectsAreUnorderedButArraysRemainOrdered() {
        val selected = CitationSelection.segments(segmentTranscript(), listOf(0, 1))
        val reorderedKeys = selector("""{
            "precision":"segment","indices":[0.0,1e0],"kind":"segments",
            "rounding":"nearest_ms","stored_time_unit":"milliseconds",
            "text_join":"concatenate_exact","time_unit":"seconds"
        }""")
        selected.requireMatchingCreated(saved(selected, reorderedKeys))

        assertFailsWith<IllegalArgumentException> {
            selected.requireMatchingCreated(saved(selected, segmentSelector("[1,0]")))
        }
    }

    @Test fun missingExtraAndChangedCanonicalMetadataAreRejected() {
        val selected = CitationSelection.segments(segmentTranscript(), listOf(0))
        val exact = segmentSelector()
        val variants = listOf(
            JsonObject(exact - "precision"),
            JsonObject(exact + ("unexpected" to JsonPrimitive(0))),
            JsonObject(exact + ("text_join" to JsonPrimitive("join_with_space"))),
            JsonObject(exact + ("rounding" to JsonPrimitive("floor"))),
            JsonObject(exact + ("stored_time_unit" to JsonPrimitive("seconds"))),
        )

        variants.forEach { changed ->
            assertFailsWith<IllegalArgumentException>(changed.toString()) {
                selected.requireMatchingCreated(saved(selected, changed))
            }
        }
    }

    @Test fun numericSpellingsKeepExactIndicesAndBinaryEquivalentRawTimes() {
        val selected = CitationSelection.segments(segmentTranscript(), listOf(0))
        for (indices in listOf("[\"0\"]", "[false]", "[null]", "[9007199254740992]", "[1e-999999999999999999999]")) {
            assertFailsWith<IllegalArgumentException>(indices) {
                selected.requireMatchingCreated(saved(selected, segmentSelector(indices)))
            }
        }

        val word = CitationSelection.words(wordTranscript(), listOf(WordRef(0, 0)))
        // Both spellings decode to the same IEEE-754 value; Python and the JVM
        // are allowed to choose different shortest decimal renderings for it.
        word.requireMatchingCreated(saved(word, wordSelector(sourceStart = "0.00010000000000000001")))
        assertFailsWith<IllegalArgumentException> {
            word.requireMatchingCreated(saved(word, wordSelector(sourceStart = "0.00010000000000002")))
        }
    }

    @Test fun selectionAndRequestsKeepDefensiveCopiesOfCallerLists() {
        // A two-element toList() copy is mutable on JVM. A singleton copy may
        // implement java.util.List while refusing clear(), so that fixture
        // would fail before it could test independence of the returned copy.
        val indices = mutableListOf(0, 1)
        val segment = CitationSelection.segments(segmentTranscript(), indices)
        indices[0] = 1
        (segment.request.segmentIndices as? MutableList<Int>)?.clear()
        assertEquals(listOf(0, 1), segment.request.segmentIndices)
        segment.requireMatchingCreated(saved(segment, segmentSelector("[0,1]")))

        val refs = mutableListOf(WordRef(0, 0), WordRef(0, 1))
        val word = CitationSelection.words(wordTranscript(), refs)
        refs[0] = WordRef(0, 1)
        (word.request.wordRefs as? MutableList<WordRef>)?.clear()
        assertEquals(listOf(WordRef(0, 0), WordRef(0, 1)), word.request.wordRefs)
        word.requireMatchingCreated(saved(word, wordSelector(
            refs = """[{"segment_index":0,"word_index":0},{"segment_index":0,"word_index":1}]""",
            sourceEnd = "0.0004",
        )))
    }

    @Test fun responseIdentityAndPositiveIdArePartOfTheFrozenContract() {
        val selected = CitationSelection.segments(segmentTranscript(), listOf(0))
        val valid = saved(selected, segmentSelector())
        for (changed in listOf(
            valid.copy(id = 0),
            valid.copy(id = -1),
            valid.copy(artifactId = 24),
            valid.copy(derivedTextId = 62),
            valid.copy(quoteText = "x "),
            valid.copy(startMs = 1),
            valid.copy(endMs = 1),
        )) {
            assertFailsWith<IllegalArgumentException> {
                selected.requireMatchingCreated(changed)
            }
        }
    }

    @Test fun millisecondProjectionUsesNearestTiesToEvenAtHalfBoundaries() {
        val cases = listOf(
            0.0005 to 0L,
            0.0015 to 2L,
            0.0025 to 2L,
            1.0005 to 1000L,
            1.0015 to 1002L,
        )
        cases.forEach { (seconds, expected) ->
            assertEquals(expected, CitationSelection.milliseconds(seconds), seconds.toString())
        }
    }
}

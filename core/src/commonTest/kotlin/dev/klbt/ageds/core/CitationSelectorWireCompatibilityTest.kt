package dev.klbt.ageds.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

/** Independent wire-level checks for Python JSON numbers decoded by kotlinx.serialization. */
class CitationSelectorWireCompatibilityTest {
    private val json = Json

    private fun transcript(sourceStart: Double = 0.0, sourceEnd: Double = 1.25) = Transcript(
        id = 7,
        artifactId = 3,
        text = " word",
        segments = listOf(TranscriptSegment(
            start = 0.0,
            end = 1.25,
            text = " word",
            words = listOf(TranscriptWord(sourceStart, sourceEnd, " word")),
        )),
        wordTiming = WordTiming(
            status = "available",
            segments = listOf(WordTimingSegment(0, true)),
        ),
    )

    private fun selector(raw: String): JsonObject = json.parseToJsonElement(raw).jsonObject

    private fun saved(selected: SelectedCitation, selector: JsonObject) = Citation(
        id = 19,
        artifactId = selected.artifactId,
        derivedTextId = selected.derivedTextId,
        startMs = selected.startMs,
        endMs = selected.endMs,
        quoteText = selected.quoteText,
        quoteSha256 = "server-owned-fixture",
        selector = selector,
    )

    private fun wordSelector(sourceStart: String = "0", sourceEnd: String = "1.25") = selector("""{
        "kind":"words","word_refs":[{"segment_index":0,"word_index":0}],
        "text_join":"concatenate_exact","time_unit":"seconds","stored_time_unit":"milliseconds",
        "rounding":"nearest_ms","precision":"word_asr","source_start":$sourceStart,"source_end":$sourceEnd,
        "alignment_verification":"not_performed"
    }""")

    @Test fun decimalExponentTrailingZerosAndNegativeZeroAreTheSameJsonNumbers() {
        val selected = CitationSelection.words(transcript(), listOf(WordRef(0, 0)))
        val equivalentSpellings = listOf(
            wordSelector(sourceStart = "-0", sourceEnd = "1.2500"),
            wordSelector(sourceStart = "0e+9", sourceEnd = "125e-2"),
            wordSelector(sourceStart = "0.000", sourceEnd = "0.125E1"),
        )

        equivalentSpellings.forEach { responseSelector ->
            val response = saved(selected, responseSelector)
            assertEquals(response, selected.requireMatchingCreated(response))
        }
    }

    @Test fun adjacentLargeIntegerIndicesAreNeverCollapsedThroughDouble() {
        val selected = CitationSelection.segments(
            Transcript(
                id = 7,
                artifactId = 3,
                text = "x",
                segments = listOf(TranscriptSegment(0.0, 0.0, "x")),
            ),
            listOf(0),
        )
        val wrong = selector("""{
            "kind":"segments","indices":[9007199254740993],
            "text_join":"concatenate_exact","time_unit":"seconds","stored_time_unit":"milliseconds",
            "rounding":"nearest_ms","precision":"segment"
        }""")

        assertFailsWith<IllegalArgumentException> {
            selected.requireMatchingCreated(saved(selected, wrong))
        }
    }

    @Test fun pythonAndJvmShortestSubnormalSpellingsDescribeTheSameRawTime() {
        val selected = CitationSelection.words(
            transcript(Double.MIN_VALUE, Double.MIN_VALUE * 2),
            listOf(WordRef(0, 0)),
        )

        val pythonSpelling = wordSelector(sourceStart = "5e-324", sourceEnd = "1e-323")
        assertEquals(
            saved(selected, pythonSpelling),
            selected.requireMatchingCreated(saved(selected, pythonSpelling)),
        )
    }

    @Test fun nonFiniteProgrammaticJsonPrimitivesAreRejected() {
        val selected = CitationSelection.words(transcript(), listOf(WordRef(0, 0)))
        listOf(Double.NaN, Double.POSITIVE_INFINITY, Double.NEGATIVE_INFINITY).forEach { value ->
            val changed = wordSelector().toMutableMap().apply {
                this["source_start"] = JsonPrimitive(value)
            }
            assertFailsWith<IllegalArgumentException> {
                selected.requireMatchingCreated(saved(selected, JsonObject(changed)))
            }
        }
    }

    @Test fun exactObjectShapeAndPrimitiveKindsRemainPartOfTheContract() {
        val selected = CitationSelection.words(transcript(), listOf(WordRef(0, 0)))
        val exact = wordSelector()
        val variants = listOf(
            JsonObject(exact.toMutableMap().apply { this["source_start"] = JsonPrimitive("0") }),
            JsonObject(exact.toMutableMap().apply { this["source_start"] = JsonPrimitive(false) }),
            JsonObject(exact.toMutableMap().apply { this["unrequested"] = JsonPrimitive(0) }),
            JsonObject(exact.toMutableMap().apply { remove("alignment_verification") }),
        )

        variants.forEach { responseSelector ->
            assertFailsWith<IllegalArgumentException> {
                selected.requireMatchingCreated(saved(selected, responseSelector))
            }
        }
    }
}

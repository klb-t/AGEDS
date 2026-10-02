package dev.klbt.ageds.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlin.math.round

/** A validated preview pinned to exactly one transcript; request lists are defensive copies. */
class SelectedCitation internal constructor(
    val artifactId: Long,
    val derivedTextId: Long,
    val quoteText: String,
    val startMs: Long,
    val endMs: Long,
    val precision: String,
    segmentIndices: List<Int>? = null,
    wordRefs: List<WordRef>? = null,
    expectedSelector: JsonObject,
) {
    private val indices = segmentIndices?.toList()
    private val refs = wordRefs?.toList()
    private val selector = expectedSelector
    val request: CitationCreate
        get() = CitationCreate(derivedTextId, indices?.toList(), refs?.toList(), quoteText)

    /** Refuse a successful-looking response unless it describes this exact frozen projection. */
    fun requireMatchingCreated(saved: Citation): Citation {
        require(saved.id > 0 && saved.artifactId == artifactId && saved.derivedTextId == derivedTextId &&
            saved.quoteText == quoteText && saved.startMs == startMs && saved.endMs == endMs &&
            jsonSemanticallyEqual(saved.selector, selector)) {
            "Created citation does not match the selected projection"
        }
        return saved
    }
}

/** Local validation mirrors the server's raw projection rules; saving remains server-validated. */
object CitationSelection {
    fun milliseconds(seconds: Double): Long {
        require(seconds.isFinite() && seconds >= 0) { "Time must be finite, nonnegative seconds" }
        val rounded = round(seconds * 1000.0) // Python round: nearest, ties to even.
        require(rounded.isFinite() && rounded < 9223372036854775808.0) { "Time exceeds millisecond range" }
        return rounded.toLong()
    }

    fun segments(transcript: Transcript, indices: List<Int>): SelectedCitation {
        checkIdentity(transcript)
        require(indices.isNotEmpty() && indices.size <= 10_000) { "Select between 1 and 10000 segments" }
        require(indices.all { it in transcript.segments.indices }) { "Segment index outside transcript" }
        require(indices.zipWithNext().all { (a, b) -> b.toLong() == a.toLong() + 1 }) {
            "Segments must be contiguous, unique and ascending"
        }
        val selected = indices.map { transcript.segments[it] }
        selected.forEach(::validateSegment)
        require(selected.zipWithNext().all { (a, b) -> b.start >= a.end }) { "Segment times overlap or are out of order" }
        return SelectedCitation(transcript.artifactId, transcript.id, selected.joinToString("") { it.text },
            milliseconds(selected.first().start), milliseconds(selected.last().end), "segment", segmentIndices = indices,
            expectedSelector = JsonObject(mapOf(
                "kind" to JsonPrimitive("segments"),
                "indices" to JsonArray(indices.map { JsonPrimitive(it) }),
                "text_join" to JsonPrimitive("concatenate_exact"),
                "time_unit" to JsonPrimitive("seconds"),
                "stored_time_unit" to JsonPrimitive("milliseconds"),
                "rounding" to JsonPrimitive("nearest_ms"),
                "precision" to JsonPrimitive("segment"),
            )))
    }

    fun words(transcript: Transcript, refs: List<WordRef>): SelectedCitation {
        checkIdentity(transcript)
        require(refs.isNotEmpty() && refs.size <= 10_000) { "Select between 1 and 10000 stored words" }
        val report = transcript.wordTiming
        require(report != null && report.schemaVersion == 1 && report.kind == "asr_word_timing" &&
            report.status in listOf("available", "partial")) { "Stored word timing availability is unknown or unavailable" }
        val validated = mutableMapOf<Int, List<TranscriptWord>>()
        val selected = refs.map { ref ->
            require(ref.segmentIndex in transcript.segments.indices) { "Segment index outside transcript" }
            val words = validated.getOrPut(ref.segmentIndex) {
                val availability = report.segments.filter { it.segmentIndex == ref.segmentIndex }
                require(availability.size == 1 && availability.single().wordSelectionAvailable) { "Word timing unavailable for segment" }
                validateWords(transcript.segments[ref.segmentIndex])
            }
            require(ref.wordIndex in words.indices) { "Word index outside segment" }
            words[ref.wordIndex]
        }
        refs.zipWithNext().forEach { (a, b) ->
            require((a.segmentIndex == b.segmentIndex && b.wordIndex.toLong() == a.wordIndex.toLong() + 1) ||
                (b.segmentIndex.toLong() == a.segmentIndex.toLong() + 1 &&
                    a.wordIndex == validated.getValue(a.segmentIndex).lastIndex && b.wordIndex == 0)) {
                "Words must be contiguous, unique and ascending"
            }
            require(a.segmentIndex == b.segmentIndex || transcript.segments[b.segmentIndex].start >= transcript.segments[a.segmentIndex].end) {
                "Segment times overlap or are out of order"
            }
        }
        val sourceStart = requireNotNull(selected.first().start)
        val sourceEnd = requireNotNull(selected.last().end)
        return SelectedCitation(transcript.artifactId, transcript.id, selected.joinToString("") { it.word },
            milliseconds(sourceStart), milliseconds(sourceEnd), "word_asr", wordRefs = refs,
            expectedSelector = JsonObject(mapOf(
                "kind" to JsonPrimitive("words"),
                "word_refs" to JsonArray(refs.map { ref -> JsonObject(mapOf(
                    "segment_index" to JsonPrimitive(ref.segmentIndex),
                    "word_index" to JsonPrimitive(ref.wordIndex),
                )) }),
                "text_join" to JsonPrimitive("concatenate_exact"),
                "time_unit" to JsonPrimitive("seconds"),
                "stored_time_unit" to JsonPrimitive("milliseconds"),
                "rounding" to JsonPrimitive("nearest_ms"),
                "precision" to JsonPrimitive("word_asr"),
                "source_start" to JsonPrimitive(sourceStart),
                "source_end" to JsonPrimitive(sourceEnd),
                "alignment_verification" to JsonPrimitive("not_performed"),
            )))
    }

    private fun checkIdentity(transcript: Transcript) {
        require(transcript.id > 0 && transcript.artifactId > 0) { "A concrete transcript and artifact are required" }
    }

    private fun validateSegment(segment: TranscriptSegment) {
        require(segment.text.isNotBlank()) { "Segment has no nonblank raw text" }
        milliseconds(segment.start)
        milliseconds(segment.end)
        require(segment.end >= segment.start) { "Segment end precedes start" }
    }

    private fun validateWords(segment: TranscriptSegment): List<TranscriptWord> {
        validateSegment(segment)
        require(segment.words.isNotEmpty() && segment.words.size <= 100_000) { "Word timing unavailable or exceeds validation limit" }
        var previousEnd = segment.start
        segment.words.forEach { word ->
            require(word.word.isNotBlank()) { "Word has no nonblank raw text" }
            val start = requireNotNull(word.start) { "Word start unknown" }
            val end = requireNotNull(word.end) { "Word end unknown" }
            milliseconds(start)
            milliseconds(end)
            require(end >= start && start >= previousEnd && end <= segment.end) { "Word times overlap, are out of order or outside segment" }
            previousEnd = end
        }
        require(segment.words.joinToString("") { it.word } == segment.text) { "Raw words differ from stored segment text" }
        return segment.words
    }
}

private fun jsonSemanticallyEqual(
    actual: JsonElement,
    expected: JsonElement,
    binaryFloat: Boolean = false,
): Boolean = when {
    actual is JsonNull && expected is JsonNull -> true
    actual is JsonObject && expected is JsonObject ->
        actual.keys == expected.keys && actual.all { (key, value) ->
            jsonSemanticallyEqual(
                value,
                expected.getValue(key),
                binaryFloat = key == "source_start" || key == "source_end",
            )
        }
    actual is JsonArray && expected is JsonArray ->
        actual.size == expected.size && actual.indices.all { index ->
            jsonSemanticallyEqual(actual[index], expected[index])
        }
    actual is JsonPrimitive && expected is JsonPrimitive ->
        primitiveSemanticallyEqual(actual, expected, binaryFloat)
    else -> false
}

private fun primitiveSemanticallyEqual(
    actual: JsonPrimitive,
    expected: JsonPrimitive,
    binaryFloat: Boolean,
): Boolean {
    if (actual.isString || expected.isString) return actual.isString && expected.isString && actual.content == expected.content
    val actualBoolean = actual.booleanOrNull
    val expectedBoolean = expected.booleanOrNull
    if (actualBoolean != null || expectedBoolean != null) return actualBoolean != null && actualBoolean == expectedBoolean
    if (binaryFloat) {
        val actualNumber = actual.doubleOrNull
        val expectedNumber = expected.doubleOrNull
        return actualNumber != null && expectedNumber != null && actualNumber.isFinite() &&
            expectedNumber.isFinite() && actualNumber == expectedNumber
    }
    val actualNumber = canonicalJsonNumber(actual.content) ?: return false
    val expectedNumber = canonicalJsonNumber(expected.content) ?: return false
    return actualNumber == expectedNumber
}

/** Exact decimal comparison avoids Double collapsing adjacent large JSON integers. */
private fun canonicalJsonNumber(raw: String): String? {
    var cursor = 0
    val negative = raw.startsWith('-').also { if (it) cursor++ }
    val exponentAt = raw.indexOfAny(charArrayOf('e', 'E'), cursor).let { if (it < 0) raw.length else it }
    val mantissa = raw.substring(cursor, exponentAt)
    val dot = mantissa.indexOf('.')
    val integer = if (dot < 0) mantissa else mantissa.substring(0, dot)
    val fraction = if (dot < 0) "" else mantissa.substring(dot + 1)
    if (integer.isEmpty() || integer.any { !it.isDigit() } || fraction.any { !it.isDigit() }) return null
    val exponent = if (exponentAt == raw.length) 0L else raw.substring(exponentAt + 1).toLongOrNull() ?: return null
    var digits = (integer + fraction).trimStart('0')
    if (digits.isEmpty()) return "0"
    val trailing = digits.length - digits.trimEnd('0').length
    digits = digits.dropLast(trailing)
    val fractionLength = fraction.length.toLong()
    if (exponent < Long.MIN_VALUE + fractionLength) return null
    val untrimmedScale = exponent - fractionLength
    if (untrimmedScale > Long.MAX_VALUE - trailing.toLong()) return null
    val scale = untrimmedScale + trailing.toLong()
    return (if (negative) "-" else "") + digits + "e" + scale
}

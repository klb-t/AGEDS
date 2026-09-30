package dev.klbt.ageds.core

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
) {
    private val indices = segmentIndices?.toList()
    private val refs = wordRefs?.toList()
    val request: CitationCreate
        get() = CitationCreate(derivedTextId, indices?.toList(), refs?.toList(), quoteText)
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
            milliseconds(selected.first().start), milliseconds(selected.last().end), "segment", segmentIndices = indices)
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
        return SelectedCitation(transcript.artifactId, transcript.id, selected.joinToString("") { it.word },
            milliseconds(requireNotNull(selected.first().start)), milliseconds(requireNotNull(selected.last().end)),
            "word_asr", wordRefs = refs)
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
